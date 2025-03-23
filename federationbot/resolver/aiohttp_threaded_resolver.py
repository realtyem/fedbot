from abc import ABC, abstractmethod
import asyncio
import logging
import socket

from aiohttp.abc import ResolveResult

try:
    import aiodns

    aiodns_default = hasattr(aiodns.DNSResolver, "getaddrinfo")
except ImportError:
    aiodns = None  # type: ignore[assignment]
    aiodns_default = False


logger = logging.getLogger(__name__)


class AbstractResolver(ABC):
    """Abstract DNS resolver."""

    @abstractmethod
    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        """Return IP address for given hostname"""

    @abstractmethod
    async def close(self) -> None:
        """Release resolver"""


_NUMERIC_SOCKET_FLAGS = socket.AI_NUMERICHOST | socket.AI_NUMERICSERV
_NAME_SOCKET_FLAGS = socket.NI_NUMERICHOST | socket.NI_NUMERICSERV


# class AsyncResolver(AbstractResolver):
#     """Use the `aiodns` package to make asynchronous DNS lookups"""
#
#     def __init__(
#         self,
#         *args: Any,
#         **kwargs: Any,
#     ) -> None:
#         """
#         is a kwarg that can be passed in
#         loop: Optional[asyncio.AbstractEventLoop] = None,
#
#         """
#         if aiodns is None:
#             raise RuntimeError("Resolver requires aiodns library")
#
#         self._resolver = aiodns.DNSResolver(*args, **kwargs)
#
#         if not hasattr(self._resolver, "gethostbyname"):
#             # aiodns 1.1 is not available, fallback to DNSResolver.query
#             self.resolve = self._resolve_with_query  # type: ignore
#
#     async def resolve(
#         self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
#     ) -> list[ResolveResult]:
#         try:
#             resp = await self._resolver.getaddrinfo(
#                 host,
#                 port=port,
#                 type=socket.SOCK_STREAM,
#                 family=family,
#                 flags=socket.AI_ADDRCONFIG,
#             )
#         except aiodns.error.DNSError as exc:
#             msg = exc.args[1] if len(exc.args) >= 1 else "DNS lookup failed"
#             raise OSError(None, msg) from exc
#         hosts: list[ResolveResult] = []
#         for node in resp.nodes:
#             address: Union[tuple[bytes, int], tuple[bytes, int, int, int]] = node.addr
#             family = node.family
#             if family == socket.AF_INET6:
#                 if len(address) > 3 and address[3]:
#                     # This is essential for link-local IPv6 addresses.
#                     # LL IPv6 is a VERY rare case. Strictly speaking, we should use
#                     # getnameinfo() unconditionally, but performance makes sense.
#                     result = await self._resolver.getnameinfo(
#                         (address[0].decode("ascii"), *address[1:]),
#                         _NAME_SOCKET_FLAGS,
#                     )
#                     resolved_host = result.node
#                 else:
#                     resolved_host = address[0].decode("ascii")
#                     port = address[1]
#             else:  # IPv4
#                 assert family == socket.AF_INET
#                 resolved_host = address[0].decode("ascii")
#                 port = address[1]
#             hosts.append(
#                 ResolveResult(
#                     hostname=host,
#                     host=resolved_host,
#                     port=port,
#                     family=family,
#                     proto=0,
#                     flags=_NUMERIC_SOCKET_FLAGS,
#                 )
#             )
#
#         if not hosts:
#             raise OSError(None, "DNS lookup failed")
#
#         return hosts
#
#     async def _resolve_with_query(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[dict[str, Any]]:
#         if family == socket.AF_INET6:
#             qtype = "AAAA"
#         else:
#             qtype = "A"
#
#         try:
#             resp = await self._resolver.query(host, qtype)
#         except aiodns.error.DNSError as exc:
#             msg = exc.args[1] if len(exc.args) >= 1 else "DNS lookup failed"
#             raise OSError(None, msg) from exc
#
#         hosts = []
#         for rr in resp:
#             hosts.append(
#                 {
#                     "hostname": host,
#                     "host": rr.host,
#                     "port": port,
#                     "family": family,
#                     "proto": 0,
#                     "flags": socket.AI_NUMERICHOST,
#                 }
#             )
#
#         if not hosts:
#             raise OSError(None, "DNS lookup failed")
#
#         return hosts
#
#     async def close(self) -> None:
#         self._resolver.cancel()


class ThreadedResolver(AbstractResolver):
    """Threaded resolver.

    Uses an Executor for synchronous getaddrinfo() calls.
    concurrent.futures.ThreadPoolExecutor is used by default.
    """

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        logger.info("Resolving: host: %s, port: %d, family: %r", host, port, family)
        infos = await self._loop.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
            family=family,
            flags=socket.AI_ADDRCONFIG,
        )

        hosts: list[ResolveResult] = []
        for family, socket_kind, proto, cname, address in infos:
            if family == socket.AF_INET6:
                if len(address) < 3:
                    # IPv6 is not supported by Python build,
                    # or IPv6 is not enabled in the host
                    continue
                if address[3]:
                    # This is essential for link-local IPv6 addresses.
                    # LL IPv6 is a VERY rare case. Strictly speaking, we should use
                    # getnameinfo() unconditionally, but performance makes sense.
                    resolved_host, _port = await self._loop.getnameinfo(address, _NAME_SOCKET_FLAGS)
                    port = int(_port)
                else:
                    resolved_host, port = address[:2]
            else:  # IPv4
                assert family == socket.AF_INET
                resolved_host, port = address  # type: ignore[misc]
            hosts.append(
                ResolveResult(
                    hostname=host,
                    host=resolved_host,
                    port=port,
                    family=family,
                    proto=proto,
                    flags=_NUMERIC_SOCKET_FLAGS,
                )
            )
            logger.info(
                "Result: hostname: %s, resolved_host: %s, port: %d, family: %r, proto: %d, flags: %r, socket_kind: %r, cname: %s",
                host,
                resolved_host,
                port,
                family,
                proto,
                _NUMERIC_SOCKET_FLAGS,
                socket_kind,
                cname,
            )

        return hosts

    async def close(self) -> None:
        pass
