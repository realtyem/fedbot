from typing import Any, Dict, List, Optional, Tuple, Union
from abc import ABC, abstractmethod
from json import JSONDecoder
import asyncio
import json
import logging
import socket

from aiohttp import ClientResponse, ClientSession, ClientTimeout, ContentTypeError, TCPConnector
from aiohttp.abc import ResolveResult
import aiodns

from federationbot.cache import TTLCache
from federationbot.errors import WellKnownSchemeError
from federationbot.resolver import (
    DelegatedServer,
    WellKnownDiagnosticResult,
    WellKnownLookupFailure,
    WellKnownLookupResult,
    WellKnownSchemeFailure,
    parse_and_check_well_known_response,
)
from federationbot.tracing import make_fresh_trace_config

logger = logging.getLogger(__name__)


class AbstractResolver(ABC):
    """Abstract DNS resolver."""

    @abstractmethod
    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> List[ResolveResult]:
        """Return IP address for given hostname"""

    @abstractmethod
    async def close(self) -> None:
        """Release resolver"""


_NUMERIC_SOCKET_FLAGS = socket.AI_NUMERICHOST | socket.AI_NUMERICSERV
_NAME_SOCKET_FLAGS = socket.NI_NUMERICHOST | socket.NI_NUMERICSERV


class AsyncResolver(AbstractResolver):
    """Use the `aiodns` package to make asynchronous DNS lookups"""

    def __init__(
        self,
        *args: Any,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        **kwargs: Any,
    ) -> None:
        if aiodns is None:
            raise RuntimeError("Resolver requires aiodns library")

        self._resolver = aiodns.DNSResolver(*args, loop, **kwargs)

        if not hasattr(self._resolver, "gethostbyname"):
            # aiodns 1.1 is not available, fallback to DNSResolver.query
            self.resolve = self._resolve_with_query  # type: ignore

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> List[ResolveResult]:
        try:
            resp = await self._resolver.getaddrinfo(
                host,
                port=port,
                type=socket.SOCK_STREAM,
                family=family,
                flags=socket.AI_ADDRCONFIG,
            )
        except aiodns.error.DNSError as exc:
            msg = exc.args[1] if len(exc.args) >= 1 else "DNS lookup failed"
            raise OSError(None, msg) from exc
        hosts: List[ResolveResult] = []
        for node in resp.nodes:
            address: Union[Tuple[bytes, int], Tuple[bytes, int, int, int]] = node.addr
            family = node.family
            if family == socket.AF_INET6:
                if len(address) > 3 and address[3]:
                    # This is essential for link-local IPv6 addresses.
                    # LL IPv6 is a VERY rare case. Strictly speaking, we should use
                    # getnameinfo() unconditionally, but performance makes sense.
                    result = await self._resolver.getnameinfo(
                        (address[0].decode("ascii"), *address[1:]),
                        _NAME_SOCKET_FLAGS,
                    )
                    resolved_host = result.node
                else:
                    resolved_host = address[0].decode("ascii")
                    port = address[1]
            else:  # IPv4
                assert family == socket.AF_INET
                resolved_host = address[0].decode("ascii")
                port = address[1]
            hosts.append(
                ResolveResult(
                    hostname=host,
                    host=resolved_host,
                    port=port,
                    family=family,
                    proto=0,
                    flags=_NUMERIC_SOCKET_FLAGS,
                )
            )

        if not hosts:
            raise OSError(None, "DNS lookup failed")

        return hosts

    async def _resolve_with_query(self, host: str, port: int = 0, family: int = socket.AF_INET) -> List[Dict[str, Any]]:
        if family == socket.AF_INET6:
            qtype = "AAAA"
        else:
            qtype = "A"

        try:
            resp = await self._resolver.query(host, qtype)
        except aiodns.error.DNSError as exc:
            msg = exc.args[1] if len(exc.args) >= 1 else "DNS lookup failed"
            raise OSError(None, msg) from exc

        hosts = []
        for rr in resp:
            hosts.append(
                {
                    "hostname": host,
                    "host": rr.host,
                    "port": port,
                    "family": family,
                    "proto": 0,
                    "flags": socket.AI_NUMERICHOST,
                }
            )

        if not hosts:
            raise OSError(None, "DNS lookup failed")

        return hosts

    async def close(self) -> None:
        self._resolver.cancel()


# Both are in seconds(float)
WELL_KNOWN_SOCKET_CONNECT_TIMEOUT = 10
WELL_KNOWN_SOCKET_READ_TIMEOUT = 10
# 1 hour
WELL_KNOWN_LOOKUP_GOOD_TTL_MS = 1000 * 60 * 60 * 1
# 5 minutes
WELL_KNOWN_LOOKUP_BAD_TTL_MS = 1000 * 60 * 5


class ServerDiscoveryResolver:
    """
    Combine server discovery techniques of both Well Known and SRV resolution
    """

    _had_well_known_cache: TTLCache[str, str]
    _well_known_cache: TTLCache[str, WellKnownLookupResult]
    http_client: ClientSession
    json_decoder: JSONDecoder

    def __init__(self) -> None:
        connector = TCPConnector(ttl_dns_cache=60 * 60 * 5, limit=10000, limit_per_host=1, force_close=True)
        self.http_client = ClientSession(
            connector=connector,
            trace_configs=[make_fresh_trace_config()],
        )
        self.json_decoder = json.JSONDecoder()
        self._had_well_known_cache = TTLCache()
        # well known should have a rather long time on it by default, failures will have
        # a shorter time to prevent consistent "re-lookups"
        self._well_known_cache = TTLCache(ttl_default_ms=WELL_KNOWN_LOOKUP_GOOD_TTL_MS)

    async def get_well_known(self, server_name: str) -> WellKnownLookupResult:
        """
        Retrieve a cached entry if it was found, or begin the actual lookup
        """
        # had_valid_well_known = self._had_well_known_cache.get(server_name)
        if cached_result := self._well_known_cache.get(server_name):
            logger.debug("Found cached result for '%s': %r", (server_name, cached_result))
            return cached_result
        result = await self.make_well_known_request(server_name)
        if isinstance(result, WellKnownDiagnosticResult):
            self._well_known_cache.set(server_name, result)
        elif isinstance(result, WellKnownLookupFailure):
            self._well_known_cache.set(server_name, result, ttl_displacer_ms=30 * 1000)
        logger.debug("Got result from '%s': %r", (server_name, result))
        return result

    async def make_well_known_request(self, server_name: str) -> WellKnownLookupResult:
        response = await self._fetch_well_known(server_name)
        try:
            async with response:
                status_code = response.status
                headers = response.headers
                content_type = headers.get("content_type", None)
                context_tracing = response._traces[0]  # noqa: W0212  # pylint:disable=protected-access

                try:
                    content = await response.json(
                        encoding="utf-8", loads=self.json_decoder.decode, content_type=content_type
                    )
                except ContentTypeError:
                    for other_content_type in ["application/octet-stream", "text/html"]:
                        content = await response.json(
                            encoding="utf-8", loads=self.json_decoder.decode, content_type=other_content_type
                        )
                        if content:
                            content_type = other_content_type
                            break

                logger.debug("content was %r", (content,))
        except Exception:
            logger.warning("Had a problem with well known RESPONSE from '%s'", (server_name,))
            # Maybe raise here, not sure yet
            raise

        try:
            host, port = parse_and_check_well_known_response(content)
        except WellKnownSchemeError as e:
            logger.error("Well known result had a scheme error: '%s'", (e.server_name,), exc_info=True)
            return WellKnownSchemeFailure(delegated_server=None, result=server_name)

        if not host:
            return WellKnownLookupFailure(delegated_server=None)
        # TODO: Remember to set the SNI header
        # TODO: parse the headers for the cache control stuff, sort out ttl options
        delegated_server = DelegatedServer(host=host, port=port, sni_header_string="")

        return WellKnownDiagnosticResult(
            delegated_server=delegated_server,
            status_code=status_code,
            content_type=content_type,
            context_trace=context_tracing,
            headers=headers,
        )

    # TODO: maybe apply backoff here for retries
    async def _fetch_well_known(self, server_name: str) -> ClientResponse:
        url = f"https://{server_name}/.well-known/matrix/server"
        client_timeouts = ClientTimeout(
            # Don't limit the total connection time, as incremental reads are handled distinctly by sock_read
            total=None,
            # connect should be None, as sock_connect is behavior intended. This is for waiting for a connection from
            # the pool as well as establishing the connection itself.
            connect=None,
            # This is the most useful for detecting bad servers
            sock_connect=WELL_KNOWN_SOCKET_CONNECT_TIMEOUT,
            # This is the one that may have the longest time, as we wait for a server to send a response
            sock_read=WELL_KNOWN_SOCKET_READ_TIMEOUT,
            # defaults to 5, for roundups on timeouts
            # ceil_threshold=5.0,
        )

        try:
            return await self.http_client.get(url, timeout=client_timeouts)
        # TODO: check the api.py for the exceptions we watch for here
        except Exception:
            logger.warning("Had a problem while placing well known REQUEST to %s", (server_name,), exc_info=True)
            raise
