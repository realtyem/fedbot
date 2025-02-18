from abc import ABC, abstractmethod
from json import JSONDecoder
import json
import logging
import socket

from aiohttp import ClientResponse, ClientSession, ClientTimeout, SocketTimeoutError, TCPConnector, client_exceptions
from aiohttp.abc import ResolveResult

from federationbot.api import USER_AGENT_STRING
from federationbot.cache import TTLCache
from federationbot.errors import (
    WellKnownClientError,
    WellKnownError,
    WellKnownParsingError,
    WellKnownSchemeError,
    WellKnownServerError,
    WellKnownServerTimeout,
)
from federationbot.resolver import (
    WellKnownDiagnosticResult,
    WellKnownLookupFailure,
    WellKnownLookupResult,
    WellKnownParseFailure,
    WellKnownSchemeFailure,
    parse_and_check_well_known_response,
)
from federationbot.tracing import make_fresh_trace_config

logger = logging.getLogger(__name__)


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
        connector = TCPConnector(
            ttl_dns_cache=10,
            family=socket.AddressFamily.AF_INET,
            limit=10000,
            limit_per_host=3,
            # happy_eyeballs_delay=None,
            # interleave=3,
            force_close=True,
        )
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
            return cached_result

        result = await self.make_well_known_request(server_name)
        if isinstance(result, WellKnownDiagnosticResult):
            self._well_known_cache.set(server_name, result)
        elif isinstance(result, WellKnownLookupFailure):
            self._well_known_cache.set(server_name, result, ttl_displacer_ms=30 * 1000)

        return result

    async def make_well_known_request(self, server_name: str) -> WellKnownLookupResult:
        try:
            response = await self._fetch_well_known(server_name)
        except WellKnownError as e:
            error_return = WellKnownLookupFailure(status_code=None, reason=e.reason)
            return error_return

        async with response:
            status_code = response.status
            headers = response.headers
            context_tracing = response._traces[0]  # noqa: W0212  # pylint:disable=protected-access

            # There can be a range of status codes, but only 404 specifically is called out
            if 200 <= status_code < 600 and not status_code == 404:
                try:
                    text_content = await response.text()
                    content = self.json_decoder.decode(text_content)
                except json.decoder.JSONDecodeError:
                    return WellKnownLookupFailure(
                        status_code=status_code, reason="JSONDecodeError: No usable data in response"
                    )
                except client_exceptions.ServerTimeoutError as e:
                    return WellKnownLookupFailure(
                        status_code=status_code, reason=f"{e.__class__.__name__}: Timed out while reading response"
                    )
            else:
                return WellKnownLookupFailure(status_code=status_code, reason="Not found")

        try:
            host, port = parse_and_check_well_known_response(content)
        except WellKnownSchemeError as e:
            return WellKnownSchemeFailure(status_code=status_code, reason=e.reason)
        except WellKnownParsingError as e:
            return WellKnownParseFailure(status_code=status_code, reason=e.reason)

        if not host:
            return WellKnownLookupFailure(status_code=status_code, reason="No host found")
        # TODO: Remember to set the SNI header
        # TODO: parse the headers for the cache control stuff, sort out ttl options

        return WellKnownDiagnosticResult(
            host=host,
            port=port,
            status_code=status_code,
            content_type=headers.get("content-type", "No value found"),
            context_trace=context_tracing,
            headers=headers,
        )

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

        request_headers = {"User-Agent": USER_AGENT_STRING}
        try:
            response = await self.http_client.request("GET", url, headers=request_headers, timeout=client_timeouts)

        # Split the different exceptions up based on where the information is extracted from
        except client_exceptions.ClientConnectorCertificateError as e:
            # This is one of the errors I found while probing for SNI TLS
            raise WellKnownServerError(  # pylint: disable=bad-exception-cause
                reason=f"{e.__class__.__name__}, {str(e.certificate_error)}",
            ) from e

        except (
            client_exceptions.ClientConnectorSSLError,
            client_exceptions.ClientSSLError,
        ) as e:
            # This is one of the errors I found while probing for SNI TLS
            raise WellKnownServerError(reason=f"{e.__class__.__name__}, {e.strerror}") from e

        except (
            # e is an OSError, may have e.strerror, possibly e.os_error.strerror
            client_exceptions.ClientProxyConnectionError,
            # e is an OSError, may have e.strerror, possibly e.os_error.strerror
            client_exceptions.ClientConnectorError,
            # e is an OSError, may have e.strerror
            client_exceptions.ClientOSError,
        ) as e:
            if hasattr(e, "os_error"):
                # This gets type ignored, as it is defined but for some reason mypy can't figure that out
                raise WellKnownClientError(reason=f"{e.__class__.__name__}, {e.os_error.strerror}") from e  # type: ignore[attr-defined]

            raise WellKnownClientError(reason=f"{e.__class__.__name__}, {e.strerror}") from e

        except (
            # For exceptions during http proxy handling(non-200 responses)
            client_exceptions.ClientHttpProxyError,  # e.message
            # For exceptions after getting a response, maybe unexpected termination?
            client_exceptions.ClientResponseError,  # e.message, base class, possibly e.status too
            # For exceptions where the server disconnected early
            client_exceptions.ServerDisconnectedError,  # e.message
        ) as e:
            raise WellKnownError(reason=f"{e.__class__.__name__}, {str(e.message)}") from e

        except (
            client_exceptions.ConnectionTimeoutError,
            SocketTimeoutError,
            client_exceptions.ServerTimeoutError,
        ) as e:
            # SocketTimeoutError is hit when aiohttp cancels a request that takes to long
            # ServerTimeoutError is asyncio.TimeoutError under it's hood
            raise WellKnownServerTimeout(
                reason=f"{e.__class__.__name__} after {WELL_KNOWN_SOCKET_CONNECT_TIMEOUT} seconds"
            ) from e

        except (
            #     socket.gaierror,
            #     ConnectionRefusedError,
            #     # Broader OS error
            #     OSError,
            #     client_exceptions.ServerFingerprintMismatch,  # e.expected, e.got
            #     client_exceptions.InvalidURL,  # e.url
            client_exceptions.ClientPayloadError,  # e
            #     client_exceptions.ServerConnectionError,  # e
            #     client_exceptions.ClientConnectionError,  # e
            #     client_exceptions.ClientError,  # e
            Exception,  # e
        ) as e:
            logger.error("Had a problem while placing well known REQUEST to %s", server_name, exc_info=True)
            raise WellKnownError(reason=f"{e.__class__.__name__}, {str(e)}") from e

        return response
