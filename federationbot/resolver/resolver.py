from typing import Any
from collections.abc import Callable, Coroutine
from ipaddress import IPv4Address
from json import JSONDecoder
import asyncio
import ipaddress
import json
import logging
import time

from aiohttp import ClientResponse, client_exceptions
from yarl import URL

from federationbot.cache import TTLCache
from federationbot.errors import RedirectRetry, RequestError, WellKnownParsingError, WellKnownSchemeError
from federationbot.resolver import (
    Diagnostics,
    IpAddressAndPort,
    NoWellKnown,
    ServerDiscoveryBaseResult,
    ServerDiscoveryErrorResult,
    ServerDiscoveryResult,
    StatusEnum,
    WellKnownDiagnosticResult,
    WellKnownLookupFailure,
    WellKnownLookupResult,
    WellKnownParseFailure,
    WellKnownSchemeFailure,
    check_and_maybe_split_server_name,
    is_this_an_ip_address,
    parse_and_check_well_known_response,
)
from federationbot.resolver.dns import CachingDNSResolver

logger = logging.getLogger(__name__)
logger.setLevel("INFO")


USER_AGENT_STRING = "AllYourServerBelongsToUs 0.1.1"

# Both are in seconds(float)
WELL_KNOWN_SOCKET_CONNECT_TIMEOUT = 3
WELL_KNOWN_SOCKET_READ_TIMEOUT = 10
# 1 hour
WELL_KNOWN_LOOKUP_GOOD_TTL_MS = 1000 * 60 * 60 * 1
# 5 minutes
WELL_KNOWN_LOOKUP_BAD_TTL_MS = 1000 * 60 * 5


def filter_to_only_ipv4_addresses(list_of_ip_addresses: list[str]) -> list[IpAddressAndPort]:
    list_of_only_ipv4_addresses = []
    for ip_address in list_of_ip_addresses:
        try:
            _ip_address = ipaddress.ip_address(ip_address)
            if isinstance(_ip_address, IPv4Address):
                list_of_only_ipv4_addresses.append(IpAddressAndPort(ip_address, 443))
        except ValueError:
            pass
    return list_of_only_ipv4_addresses


class ServerDiscoveryResolver:
    """
    Combine server discovery techniques of both Well Known and SRV resolution
    """

    _had_well_known_cache: TTLCache[str, str]
    _well_known_cache: TTLCache[str, WellKnownLookupResult]
    server_discovery_cache: TTLCache[str, ServerDiscoveryResult]
    json_decoder: JSONDecoder
    exp_dns_resolver: CachingDNSResolver

    def __init__(self, request_cb: Callable[..., Coroutine[Any, Any, ClientResponse]]) -> None:
        # resolver = ThreadedResolver()
        # resolver = CachingDNSResolver()
        # nameserver = Do53Nameserver("192.168.2.1")
        # self.dns_resolver = dns.asyncresolver.Resolver()
        # self.dns_resolver.nameservers = [nameserver]

        # connector = TCPConnector(
        #     use_dns_cache=True,
        #     ttl_dns_cache=300,
        #     family=socket.AddressFamily.AF_INET,
        #     limit=10000,
        #     limit_per_host=3,
        #     # resolver=resolver,  # type: ignore[arg-type]
        #     # happy_eyeballs_delay=None,
        #     # interleave=3,
        #     force_close=True,
        # )
        self._request = request_cb
        # self.http_client = ClientSession(
        #     connector=connector,
        #     trace_configs=[make_fresh_trace_config()],
        # )
        self.json_decoder = json.JSONDecoder()
        self._had_well_known_cache = TTLCache()
        # well known should have a rather long time on it by default, failures will have
        # a shorter time to prevent consistent "re-lookups"
        self._well_known_cache = TTLCache(ttl_default_ms=WELL_KNOWN_LOOKUP_GOOD_TTL_MS)
        self.server_discovery_cache: TTLCache[str, ServerDiscoveryBaseResult] = TTLCache()
        self.exp_dns_resolver = CachingDNSResolver()

    async def discover_server(self, server_name: str, run_diagnostics: bool = False) -> ServerDiscoveryBaseResult:
        logger.debug("Discovering server: %s", server_name)
        cached_server_result = self.server_discovery_cache.get(server_name)
        if cached_server_result and not run_diagnostics:
            logger.debug("Using cached response for: %s\n%r", server_name, cached_server_result)
            return cached_server_result

        server_result = await self._discover_server(server_name, run_diagnostics=run_diagnostics)
        logger.debug("Server discovery complete for: %s\n%r", server_name, server_result)
        self.server_discovery_cache.set(
            server_name,
            server_result,
        )
        return server_result

    async def _discover_server(self, server_name: str, run_diagnostics: bool = False) -> ServerDiscoveryBaseResult:
        # The Diagnostics object will not actually log anything if run_diagnostics is False
        diag = Diagnostics(enable_diagnostics=run_diagnostics)

        time_start = time.time()
        host, port = check_and_maybe_split_server_name(server_name)

        # Step One
        diag.log("Step 1: Checking if server name is a literal IP address")
        if is_this_an_ip_address(host):
            diag.log(f"  {host} is a literal IP address")
            if port == 0:
                diag.log("No port provided, using 8448")
                port = 8448
            else:
                diag.log(f"Port provided: {port}")

            ip_port_object = IpAddressAndPort(ip_address=host, port=port)
            return ServerDiscoveryResult(
                hostname=host,
                list_of_resolved_addresses=[ip_port_object],
                host_header=f"{host}{':' + str(port) if port else ''}",
                sni=host,
                time_for_complete_delegation=time.time() - time_start,
                diagnostics=diag,
            )

        # Step Two
        diag.log("Step 2: Checking if hostname is resolvable and has a port")
        # If this makes it to step 6, reuse these
        initial_dns_responses = await self.exp_dns_resolver.resolve_reg_records(host, diagnostics=diag)
        logger.debug("dns results from step 2: %s:\n%r", server_name, initial_dns_responses.get_hosts())
        if not initial_dns_responses.get_hosts():
            return ServerDiscoveryErrorResult(error=initial_dns_responses.get_errors()[0], diagnostics=diag)

        if port != 0:
            list_of_ip_objects = []
            diag.log(f"  {port} was included")
            for dns_resolved_ip in initial_dns_responses.get_hosts():
                list_of_ip_objects.append(IpAddressAndPort(ip_address=dns_resolved_ip, port=port))

            return ServerDiscoveryResult(
                hostname=host,
                list_of_resolved_addresses=list_of_ip_objects,
                host_header=f"{host}:{port}",
                sni=host,
                time_for_complete_delegation=time.time() - time_start,
                diagnostics=diag,
            )

        # Step Three - Well known
        # well_known_resolved_results: list[ResolveResult] = []
        diag.log("Step 3: Checking for well known delegation")

        # Should be able to use the initial dns response to look this up. However, clever people sometimes do silly
        # things. As an example: beeper.com resolves to two different IPv4 addresses for it's well known endpoint.
        # So, if there was only a single initial dns response, use it directly. Otherwise, let aiohttp's happy eyeballs
        # algorithm deal with it, and whoever answers first, wins.
        well_known_result = await self.get_well_known(server_name, initial_dns_responses.get_hosts(), diagnostics=diag)

        if isinstance(well_known_result, WellKnownDiagnosticResult):
            # Step 3.1, does the well known have a literal IP?
            if is_this_an_ip_address(well_known_result.host):
                diag.log(f"Step 3.1: Well known points to literal IP address: {well_known_result.host}")

                # Literal IP's can not have SRV records
                if well_known_result.port in [0, None]:
                    diag.log("  No port provided, using 8448")
                    # TODO: fix whatever is happening with port here, it's not used
                    ip_port_object = IpAddressAndPort(ip_address=well_known_result.host, port=8448)
                else:
                    diag.log(f"  Port provided: {port}")
                    ip_port_object = IpAddressAndPort(ip_address=well_known_result.host, port=well_known_result.port)

                return ServerDiscoveryResult(
                    hostname=host,
                    list_of_resolved_addresses=[ip_port_object],
                    host_header=f"{well_known_result.host}{':' + str(well_known_result.port) if well_known_result.port else ''}",
                    sni=well_known_result.host,
                    time_for_complete_delegation=time.time() - time_start,
                    diagnostics=diag,
                )

            # Step 3.2, resolve the hostname IF there was a port
            if well_known_result.port:
                diag.log(f"Step 3.2: Well known had attached port: {well_known_result.port}")

                well_known_dns_query_results = await self.exp_dns_resolver.resolve_reg_records(
                    well_known_result.host, diagnostics=diag
                )
                if not well_known_dns_query_results.get_hosts():
                    return ServerDiscoveryErrorResult(
                        error=well_known_dns_query_results.get_errors()[0], diagnostics=diag
                    )

                list_of_ip_objects = []

                for dns_resolved_ip in well_known_dns_query_results.get_hosts():
                    list_of_ip_objects.append(IpAddressAndPort(ip_address=dns_resolved_ip, port=well_known_result.port))

                return ServerDiscoveryResult(
                    hostname=host,
                    list_of_resolved_addresses=list_of_ip_objects,
                    host_header=f"{well_known_result.host}:{well_known_result.port}",
                    sni=well_known_result.host,
                    time_for_complete_delegation=time.time() - time_start,
                    diagnostics=diag,
                )

            # Step 3.3 and 3.4, there was no port, resolve SRV records
            diag.log("Step 3.3(and 3.4) SRV query based on well known response")
            try:
                well_known_srv_results = await self.exp_dns_resolver.resolve_srv_records(
                    well_known_result.host, diagnostics=diag
                )
            except Exception:
                well_known_srv_results = []

            if well_known_srv_results:
                list_of_ip_objects = []
                for srv_result in well_known_srv_results:
                    resolved_ip = srv_result[0]
                    resolved_port = srv_result[1]
                    list_of_ip_objects.append(IpAddressAndPort(ip_address=resolved_ip, port=resolved_port))

                return ServerDiscoveryResult(
                    hostname=host,
                    list_of_resolved_addresses=list_of_ip_objects,
                    host_header=f"{well_known_result.host}",
                    sni=well_known_result.host,
                    time_for_complete_delegation=time.time() - time_start,
                    diagnostics=diag,
                )

            # There was a host, but not a port in the well known, default to 8448
            diag.log("Step 3.5 Resolve DNS for host from well known and use port 8448")
            well_known_dns_query_results = await self.exp_dns_resolver.resolve_reg_records(
                well_known_result.host, diagnostics=diag
            )
            if not well_known_dns_query_results.get_hosts():
                return ServerDiscoveryErrorResult(error=well_known_dns_query_results.get_errors()[0], diagnostics=diag)

            list_of_ip_objects = []
            for dns_resolved_ip in well_known_dns_query_results.get_hosts():
                list_of_ip_objects.append(IpAddressAndPort(ip_address=dns_resolved_ip, port=8448))

            return ServerDiscoveryResult(
                hostname=host,
                list_of_resolved_addresses=list_of_ip_objects,
                host_header=well_known_result.host,
                sni=well_known_result.host,
                time_for_complete_delegation=time.time() - time_start,
                diagnostics=diag,
            )

        # Step 4 and 5, we have a hostname but no port, resolve SRV records
        diag.log("Step 4(and 5) Check for SRV records")
        try:
            srv_results = await self.exp_dns_resolver.resolve_srv_records(host, diagnostics=diag)
        except Exception:
            srv_results = []

        if srv_results:
            list_of_ip_objects = []
            for srv_result in srv_results:
                srv_ip = srv_result[0]
                srv_port = srv_result[1]
                list_of_ip_objects.append(IpAddressAndPort(ip_address=srv_ip, port=srv_port))

            return ServerDiscoveryResult(
                hostname=host,
                list_of_resolved_addresses=list_of_ip_objects,
                host_header=f"{host}",
                sni=host,
                time_for_complete_delegation=time.time() - time_start,
                diagnostics=diag,
            )

        # Step 6, default port to 8448
        diag.log("Step 6 No other port found, default to 8448")

        list_of_ip_objects = []
        for dns_result in initial_dns_responses.get_hosts():
            list_of_ip_objects.append(IpAddressAndPort(ip_address=dns_result, port=8448))

        return ServerDiscoveryResult(
            hostname=host,
            list_of_resolved_addresses=list_of_ip_objects,
            host_header=host,
            sni=host,
            time_for_complete_delegation=time.time() - time_start,
            diagnostics=diag,
        )

    async def get_well_known(
        self, server_name: str, list_of_ip_addresses: list[str], diagnostics: Diagnostics
    ) -> WellKnownLookupResult:
        """
        Retrieve a cached entry if it was found, or begin the actual lookup
        """
        # had_valid_well_known = self._had_well_known_cache.get(server_name)
        logger.debug("get_well_known: %s placing request", server_name)
        if cached_result := self._well_known_cache.get(server_name):
            logger.debug("get_well_known: %s found cached request", server_name)
            if diagnostics:
                diagnostics.log(f"  Found cached well known result for: {server_name}")
                if isinstance(cached_result, WellKnownDiagnosticResult):
                    diagnostics.log(f"    host and port: {cached_result.host}:{cached_result.port}")
                elif isinstance(cached_result, WellKnownLookupFailure):
                    logger.warning("get_well_known: %s found cached error:\n%r", server_name, cached_result)
                    diagnostics.log(f"    code: {cached_result.status_code}, reason: {cached_result.reason}")
                else:
                    assert isinstance(cached_result, NoWellKnown)
                    diagnostics.log(f"    code: {cached_result.status_code}")
            return cached_result

        if diagnostics:
            diagnostics.log("  Making request to well-known")
        result = await self.make_well_known_request(server_name, list_of_ip_addresses, diagnostics)
        logger.debug("get_well_known: %s finished request\n%r", server_name, result)

        if isinstance(result, WellKnownDiagnosticResult):
            self._well_known_cache.set(server_name, result)
        elif isinstance(result, WellKnownLookupFailure):
            logger.warning("get_well_known: %s received failure:\n%r", server_name, result)
            self._well_known_cache.set(server_name, result, 30 * 1000)

        return result

    async def make_well_known_request(
        self, server_name: str, list_of_ip_addresses: list[str], diagnostics: Diagnostics
    ) -> WellKnownLookupResult:
        # Until IPv6 works on my server, curate the list of ip addresses to only have IPv4
        list_of_only_ipv4_addresses = filter_to_only_ipv4_addresses(list_of_ip_addresses)

        try:
            last_server_name_tried = server_name
            last_host_header = server_name
            last_sni_host_name = server_name
            while True:
                try:
                    list_of_coros: list[asyncio.Task] = []
                    for ip_address in list_of_only_ipv4_addresses:

                        coro = self._request(
                            ip_address,
                            last_server_name_tried,
                            "/.well-known/matrix/server",
                            host_header=last_host_header,
                            sni_host_name=last_sni_host_name,
                        )
                        list_of_coros.append(asyncio.create_task(coro))

                    try:
                        done, pending = await asyncio.wait(list_of_coros, return_when=asyncio.FIRST_COMPLETED)
                    except ValueError as e:
                        logger.exception("%s, %s: %r", server_name, last_server_name_tried, e)
                        raise RequestError("No addresses found?") from e

                    for task in pending:
                        task.cancel()
                    response: ClientResponse = done.pop().result()

                except RedirectRetry as e:
                    # The request returned a 301 status code, we are allowed to follow those. Parse out the new server
                    # name and request it's DNS then request again
                    logger.info("Redirect detected for %s: pointing to %s", server_name, e.location)
                    new_location_url = URL(e.location)
                    new_host = new_location_url.host
                    # The possibility exists that the host could be None here, but it's highly unlikely(unless the other
                    # side messed up their reverse proxy that gave the 301). Mypy doesn't know it's unlikely, so explain
                    assert new_host is not None
                    diagnostics.log(f"  {last_server_name_tried} wants to redirect to {new_host}")
                    last_sni_host_name = new_host
                    last_host_header = new_host
                    last_server_name_tried = new_host

                    new_dns_responses = await self.exp_dns_resolver.resolve_reg_records(
                        new_host, diagnostics=diagnostics
                    )

                    if not new_dns_responses.get_hosts():
                        return WellKnownLookupFailure(status_code=None, reason=new_dns_responses.get_errors()[0])

                    list_of_only_ipv4_addresses = filter_to_only_ipv4_addresses(new_dns_responses.get_hosts())

                else:
                    logger.debug("request to %s%s completed", server_name, "/.well-known/matrix/server")
                    break

        except RequestError as e:
            if diagnostics:
                diagnostics.status.well_known = StatusEnum.ERROR
                diagnostics.log(f"    Error: {e.reason}")
            error_return = WellKnownLookupFailure(status_code=None, reason=e.reason)
            return error_return

        async with response:
            status_code = response.status
            headers = response.headers
            context_tracing = response._traces[0]._trace_config_ctx  # noqa: W0212  # pylint:disable=protected-access

            # There can be a range of status codes, but only 404 specifically is called out
            if 200 <= status_code < 600 and not status_code == 404:
                try:
                    text_content = await response.text()
                    content = self.json_decoder.decode(text_content)
                except json.decoder.JSONDecodeError as e:
                    if diagnostics:
                        diagnostics.status.well_known = StatusEnum.ERROR
                        diagnostics.log(f"    Code: {status_code}, Error: {e.msg}")
                    return WellKnownLookupFailure(
                        status_code=status_code, reason="JSONDecodeError: No usable data in response"
                    )
                except client_exceptions.ServerTimeoutError as e:
                    if diagnostics:
                        diagnostics.status.well_known = StatusEnum.ERROR
                        diagnostics.log(f"    Code: {status_code}, Error: {e.strerror}")
                    return WellKnownLookupFailure(
                        status_code=status_code, reason=f"{e.__class__.__name__}: Timed out while reading response"
                    )
            else:
                if diagnostics:
                    diagnostics.log(f"    Code: {status_code}")

                return NoWellKnown(status_code=status_code)

        try:
            host, port = parse_and_check_well_known_response(content)
        except WellKnownSchemeError as e:
            if diagnostics:
                diagnostics.status.well_known = StatusEnum.ERROR
                diagnostics.log(f"    Code: {status_code}, Error: {e.reason}")

            return WellKnownSchemeFailure(status_code=status_code, reason=e.reason)
        except WellKnownParsingError as e:
            if diagnostics:
                diagnostics.status.well_known = StatusEnum.ERROR
                diagnostics.log(f"    Code: {status_code}, Error: {e.reason}")

            return WellKnownParseFailure(status_code=status_code, reason=e.reason)

        if not host:
            if diagnostics:
                diagnostics.status.well_known = StatusEnum.ERROR
                diagnostics.log(f"    Code: {status_code}, Error: No host found")

            return WellKnownLookupFailure(status_code=status_code, reason="No host found")
        # TODO: Remember to set the SNI header
        # TODO: parse the headers for the cache control stuff, sort out ttl options
        if diagnostics:
            diagnostics.status.well_known = StatusEnum.OK
            diagnostics.log(f"    host and port: {host}:{port}")
            end = context_tracing.request_end
            start = context_tracing.request_start
            diagnostics.log(f"    request response time: {1000 * (end - start):.3f} milliseconds")

        return WellKnownDiagnosticResult(
            host=host,
            port=port,
            status_code=status_code,
            content_type=headers.get("content-type", "No value found"),
            context_trace=context_tracing,
            headers=headers,
        )
