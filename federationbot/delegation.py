from typing import Callable, Dict, Optional, Tuple
from asyncio import sleep
import ipaddress
import time

from dns.asyncresolver import Resolver
from mautrix.util.logging import TraceLogger
import dns.resolver

from federationbot.errors import MalformedServerNameError
from federationbot.responses import FederationBaseResponse
from federationbot.server_result import (
    DiagnosticInfo,
    ResponseStatusType,
    ServerResult,
    ServerResultError,
)


def check_and_maybe_split_server_name(server_name: str) -> Tuple[str, Optional[str]]:
    """
    Checks that a server name does not have a scheme prepended to it(something seen
    in the wild), then splits the server_name from any potential port number that is
    appended.

    Args:
        server_name: a server domain as expected by the matrix spec, with or without
            port number

    Returns: Tuple of the domain and(if it exists) the port as strings(or None)
    """
    server_host: str = server_name
    server_port: Optional[str] = None

    if server_name.startswith(("http:", "https")) or "://" in server_name:
        raise MalformedServerNameError

    # str.split() will raise a ValueError if the value to split by isn't there
    try:
        server_host, server_port = server_name.split(":", maxsplit=1)
    except ValueError:
        # Accept this gracefully, as it is probably the common path
        pass
    finally:
        return server_host, server_port


def is_this_an_ip_address(host: str) -> bool:
    """
    Check with the ipaddress library if this is a Literal IP(works for both ipv4 and
        ipv6)

    Returns: bool
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        # This isn't a real ipv4 or ipv6 address
        # This is probably the common path
        return False
    else:
        return True


def _parse_and_check_well_known_response(
    response: FederationBaseResponse, diag_info: DiagnosticInfo
) -> Tuple[Optional[str], Optional[str], DiagnosticInfo]:
    """
    Parse the response returned by the well-known request. Collect DiagnosticInfo
        throughout the process. Follow the spec from Step 3

    Args:
        response: The FederationBaseResponse with the response from well-known
        diag_info: The DiagnosticInfo to add to

    Returns:
         Tuple of host(or None), port(or None), DiagnosticInfo
    """
    host = None
    port = None
    if response.status_code == 404:
        diag_info.mark_no_well_known()
    elif response.status_code != 200:
        # For whatever reason(which should be in the errors/diag returned),
        # there was no usable well-known
        diag_info.mark_error_on_well_known()

    else:
        # In theory, got a good response. Should be JSON of
        # {"m.server": "example.com:433"} if there was a port
        try:
            well_known_result = response.response_dict["m.server"]
        except KeyError:
            diag_info.error("Well-Known missing 'm.server' JSON key")
            diag_info.mark_error_on_well_known()

        else:
            # I tried to find a library or module that would comprehensively handle
            # parsing a URL without a scheme, yarl came close. I guess we'll just
            # have to cover the basics by hand.
            try:
                host, port = check_and_maybe_split_server_name(well_known_result)
            except MalformedServerNameError:
                diag_info.error(
                    "Well-Known 'm.server' has a scheme when it should not:"
                )
                diag_info.error(f"{well_known_result}", front_pad="      ")
                diag_info.mark_error_on_well_known()
            except AttributeError:
                # Apparently this happens if a server has their well-known set like a
                # client well-known. Don't print custom error message showing the result
                # as it could be spammy(and not fit)
                diag_info.error(
                    "Well-Known 'm.server' has wrong attributes, should be a host/port"
                )
                diag_info.mark_error_on_well_known()

            else:
                diag_info.mark_well_known_maybe_found()

    return host, port, diag_info


class DelegationHandler:
    def __init__(self, logger: TraceLogger) -> None:
        self.dns_resolver = Resolver()
        # DNS lifetime of requests default is 5 seconds
        self.dns_resolver.lifetime = 10.0
        # DNS timeout for a request default is 2 seconds
        self.logger = logger
        self.cached_server_names: Dict[str, ServerResult] = {}

    async def check_dns_for_reg_records(
        self,
        server_name: str,
        check_cname: bool = True,
        diag_info: DiagnosticInfo = DiagnosticInfo(False),
    ) -> Tuple[Optional[str], Optional[str], Optional[str], DiagnosticInfo]:
        """
        Check DNS records for A, then AAAA, and (optionally) CNAME.

        Args:
            server_name: the hostname to look up
            check_cname: if checking for CNAME as well
            diag_info: while we always collect errors, additional data is collected with
                this

        Returns: Tuple of (IP for A record, IP for AAAA record, IP for CNAME,
            DiagnosticInfo). Any of the IP records can be None
        """
        retries = 0
        while retries < 3:
            try:
                return await self._check_dns_for_reg_records(
                    server_name=server_name,
                    check_cname=check_cname,
                    diag_info=diag_info,
                )

            except dns.resolver.NoNameservers:
                # Wait for a moment before retrying, as this is a DNS server failure,
                # aka SERVFAIL was returned
                await sleep(1)
                retries = retries + 1
                diag_info.error(f"DNS SERVFAIL: retry count: {retries}")

            except dns.resolver.NXDOMAIN:
                # No records are being returned, don't bother retrying
                retries = 10
                diag_info.error(
                    f"DNS records for '{server_name}' do not exist(NXDOMAIN)"
                )

            # mypy complains that this isn't in the module...
            except dns.resolver.LifetimeTimeout:
                # Wait for a moment before retrying, as this is a DNS server failure,
                # the request timed out several times probably because the DNS server
                # was busy
                await sleep(1)
                retries = retries + 1
                diag_info.error(f"DNS timed out: retry count: {retries}")

        return None, None, None, diag_info

    async def _check_dns_for_reg_records(
        self,
        server_name: str,
        check_cname: bool,
        diag_info: DiagnosticInfo,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], DiagnosticInfo]:
        a_ip_address = None
        a4_ip_address = None
        cname_ip_address = None
        diag_info.mark_step_num("for DNS records")

        # A records return a dns.rdtypes.IN.A object that has an 'address' property
        try:
            a_records: dns.resolver.Answer = await self.dns_resolver.resolve(
                server_name, "A"
            )

        except dns.resolver.NoAnswer:
            diag_info.add(f"No 'A' DNS record found for '{server_name}'")

        else:
            for rdata in a_records:
                a_ip_address = rdata.address
                diag_info.mark_dns_record_found()
                diag_info.add(f"DNS 'A' record found: {server_name} -> {a_ip_address}")

        # AAAA records return a dns.rdtypes.IN.AAAA object that also uses 'address'
        try:
            a4_records: dns.resolver.Answer = await self.dns_resolver.resolve(
                server_name, "AAAA"
            )

        except dns.resolver.NoAnswer:
            diag_info.add(f"No 'AAAA' DNS record found for '{server_name}'")

        else:
            for rdata in a4_records:
                a4_ip_address = rdata.address
                diag_info.mark_dns_record_found()
                diag_info.add(
                    f"DNS 'AAAA' record found: {server_name} -> {a4_ip_address}"
                )

        if check_cname:
            # CNAME records return a dns.rdtypes.ANY.CNAME object which uses 'target'
            try:
                cname_records: dns.resolver.Answer = await self.dns_resolver.resolve(
                    server_name, "CNAME"
                )

            except dns.resolver.NoAnswer:
                diag_info.add(f"No 'CNAME' DNS record found for '{server_name}'")

            else:
                for rdata in cname_records:
                    cname_ip_address = rdata.target
                    diag_info.add(
                        f"DNS 'CNAME' record found: {server_name} -> {cname_ip_address}"
                    )

        return a_ip_address, a4_ip_address, cname_ip_address, diag_info

    async def check_dns_for_srv_records(
        self, server_name: str, diag_info: DiagnosticInfo = DiagnosticInfo(False)
    ) -> Tuple[Optional[str], Optional[str], DiagnosticInfo]:
        """
        Check for SRV records.

        Args:
            server_name: the hostname to look up
            diag_info: while we always collect errors, additional data is collected with
                this

        Returns: Tuple of(optional string of hostname, optional string of port number,
            DiagnosticInfo)
        """
        retries = 0
        while retries < 3:
            try:
                return await self._check_dns_for_srv_records(
                    server_name=server_name,
                    diag_info=diag_info,
                )

            except dns.resolver.NoNameservers:
                # Wait for a moment before retrying, as this is a DNS server failure,
                # aka SERVFAIL was returned
                await sleep(1)
                retries = retries + 1
                diag_info.error(f"DNS SERVFAIL: retry count: {retries}")

            except dns.resolver.NXDOMAIN:
                # No records are being returned, don't bother retrying
                retries = 10
                diag_info.error(
                    f"SRV DNS records for '{server_name}' do not exist(NXDOMAIN)"
                )

            # except dns.resolver.LifetimeTimeout as e:
            #     self.logger.info(
            #       "check_dns_for_srv_records: LifetimeTimeout Exception: for server "
            #       f"{server_name}:\n {e}"
            #     )

        return None, None, diag_info

    async def _check_dns_for_srv_records(
        self, server_name: str, diag_info: DiagnosticInfo
    ) -> Tuple[Optional[str], Optional[str], DiagnosticInfo]:
        """
        Check DNS records for a SRV record. First for a '_matrix-fed._tcp." then for the
        deprecated '_matrix._tcp.'

        Args:
            server_name: The hostname to look up
            diag_info: Always collect errors, but other diagnostics can be collected too
        Returns:
            tuple of string of the hostname(or None), string of the port number(or
            None), and the DiagnosticInfo object. Prefer the non-deprecated record for
            returned values, if both exist.
        """
        host = None
        port = None
        dep_host = None
        dep_port = None
        # SRV records return dns.rdtypes.IN.SRV
        try:
            srv_records: dns.resolver.Answer = await self.dns_resolver.resolve(
                f"_matrix-fed._tcp.{server_name}", "SRV"
            )

        except dns.resolver.NoAnswer:
            diag_info.add(f"No 'SRV' record for '_matrix-fed._tcp.{server_name}'")

        else:
            for rdata in srv_records:
                host = str(rdata.target).rstrip(".")
                port = rdata.port
                diag_info.mark_srv_record_found()
                diag_info.add(
                    f"SRV record found: '_matrix-fed._tcp.{server_name}' -> "
                    f"{host}:{port}"
                )

        try:
            deprecated_srv_records = await self.dns_resolver.resolve(
                "_matrix._tcp." + server_name, "SRV"
            )
        except dns.resolver.NoAnswer:
            diag_info.add(f"No 'SRV' record for '_matrix._tcp.{server_name}'")

        else:
            for rdata in deprecated_srv_records:
                dep_host = str(rdata.target).rstrip(".")
                dep_port = rdata.port
                diag_info.mark_srv_record_found()
                diag_info.add(
                    f"SRV record found: '_matrix._tcp.{server_name} -> "
                    f"{dep_host}:{dep_port}"
                )

        return host or dep_host, port or dep_port, diag_info

    def _retrieve_or_evict_server_result_from_cache(
        self, server_name: str, force_reload: bool = False
    ) -> Optional[ServerResult]:
        """
        An 'on-demand' way to process if it's time to refresh an existing ServerResult

        Args:
             server_name: The server name to check for existing results
             force_reload: Optionally ignore cached previous results
        Returns: Optional ServerResult or ServerErrorResult
        """
        resolved_server = self.cached_server_names.get(server_name, None)
        server_result = None
        if resolved_server:
            now = int(time.time_ns() / 1000)
            if force_reload or resolved_server.drop_after < now:
                # drop_after may be zero if this server was never actually contacted, so
                # reload the entry in case it came back in the meantime
                self.cached_server_names.pop(server_name)

            else:
                server_result = resolved_server

        return server_result

    async def maybe_handle_delegation(
        self,
        server_name: str,
        get_callback: Callable,
        force_reload: bool = False,
        diag_info: DiagnosticInfo = DiagnosticInfo(False),
    ) -> ServerResult:
        """
        Perform server discovery for a given server name.

        Args:
            server_name: The name as supplied from the back of a mxid
            get_callback: Pass through the callable to retrieve well-known data
            force_reload: Optionally ignore cached previous results
            diag_info: Diagnostic collection object

        Returns: A ServerResult(or ServerResultError) object with all the information
            found during delegation discovery. May include well-known and SRV record
            information.
        """

        # Check for cached copy of ServerResult
        server_result = self._retrieve_or_evict_server_result_from_cache(
            server_name, force_reload
        )
        if not server_result:
            server_result = await self._maybe_handle_delegation(
                server_name, get_callback=get_callback, diag_info=diag_info
            )
            # sni_response: FederationBaseResponse = await get_callback(
            #     server_result=server_result,
            #     destination_server=server_name,
            #     path="/_matrix/federation/v1/version",
            #     method="GET",
            #     delegation_check=False,
            #     diagnostics=True,
            # )
            # diag_info.append_from(sni_response.errors)
            # if sni_response.status_code > -1:
            #     self.cached_server_names[server_name] = sni_response.server_result
            # else:
            #     server_result.use_sni = False
            #     diag_info.add("Trying without SNI")
            #     no_sni_response: FederationBaseResponse = await get_callback(
            #         server_result=server_result,
            #         destination_server=server_name,
            #         path="/_matrix/federation/v1/version",
            #         method="GET",
            #         delegation_check=False,
            #         diagnostics=True,
            #     )
            #     diag_info.append_from(no_sni_response.errors)
            self.cached_server_names[server_name] = server_result

        return server_result

    async def _maybe_handle_delegation(
        self,
        server_name: str,
        get_callback: Callable,
        diag_info: DiagnosticInfo = DiagnosticInfo(False),
    ) -> ServerResult:
        # The process to determine the ultimate final host:port is defined in the
        # spec.
        # https://spec.matrix.org/v1.9/server-server-api/#resolving-server-names
        # Basically:
        # 1. If it's a literal IP, then use that either with the port supplied or
        #    8448
        # 2. If it's a hostname with an explicit port, resolve with DNS
        #    to an A, AAAA or CNAME record
        # 3. If it's a hostname with no explicit port, request from
        #    <server_name>/.well-known/matrix/server and parse the json. Anything
        #    wrong, skip to step 4. Want <delegated_server_name>[:<delegated_port>]
        #    3a. Same as 1 above, except don't just use 8448(step 3e)
        #    3b. Same as 2 above
        #    3c. If no explicit port, check for a SRV record at
        #        _matrix-fed._tcp.<delegated_server_name> to get the port number.
        #        Resolve with A or AAAA but not CNAME record
        #    3d. (deprecated) Check _matrix._tcp.<delegated_server_name> instead
        #    3e. (there was no port, remember), resolve using provided delegated
        #        hostname and use port 8448. Resolve with A or AAAA but not CNAME
        #        record.
        # 4. (no well-known) Check SRV record(same as 3c above)
        # 5. (deprecated) Check other SRV record(same as 3d above)
        # 6. Use the supplied server_name and try port 8448

        # Try and split the server_name from any potential port
        try:
            host, port = check_and_maybe_split_server_name(server_name)
        except MalformedServerNameError:
            diag_info.error(f"Server name was malformed: {server_name}")
            return ServerResultError(
                error_reason=f"Server name was malformed: {server_name}",
                diag_info=diag_info,
            )

        # Spec step 1, check for literal IP
        # HOST header should be the server_name, as it was passed in(including port)
        diag_info.mark_step_num("Step 1", "Checking for Literal IP")
        if is_this_an_ip_address(host):
            diag_info.add(f"Server is literal IP: {host}")
            server_result = ServerResult(
                host=host,
                port=port or "8448",
                host_header=f"{server_name}{':'+port if port else ''}",
                sni_server_name=server_name,
                diag_info=diag_info,
            )
            return server_result

        # Spec step 2: if there is a port, use the given host as the server name
        # HOST header should be the server_name, as it was passed in(including port)
        diag_info.mark_step_num("Step 2", "Checking for explicit Port on server name")
        if port:
            diag_info.add(f"Server name has explicit port: {port}")
            # The DNS test itself adds the diagnostic info, don't need the return ip's
            # (if any are even found)
            (
                _,
                _,
                _,
                diag_info,
            ) = await self.check_dns_for_reg_records(host, diag_info=diag_info)
            server_result = ServerResult(
                host=host,
                port=port,
                host_header=f"{server_name}",
                sni_server_name=host,
                diag_info=diag_info,
            )
            return server_result

        # Spec step 3: Well-Known pre-parsing
        diag_info.mark_step_num("Step 3", "Well-Known")

        response: FederationBaseResponse = await get_callback(
            destination_server=host,
            path="/.well-known/matrix/server",
            delegation_check=False,
            diagnostics=True,
        )

        diag_info.append_from(response.errors)

        # If host is None, this well-known was no good, pull from diag_info to find out
        # if it was not there, or had an error.
        # If port is None, then check SRV record
        # _parse_and_check_well_known_response() will mark the diag_info for us.
        (
            well_known_host,
            well_known_port,
            diag_info,
        ) = _parse_and_check_well_known_response(response=response, diag_info=diag_info)

        if not well_known_host:
            if diag_info.well_known_test_status == ResponseStatusType.NONE:
                # There is actually nothing to do here, so ride the conditional to the
                # next section
                pass
            elif diag_info.well_known_test_status == ResponseStatusType.ERROR:
                # There is nothing to do here either, but leave this condition in place
                # so as to highlight the explicitness(and allow for additional
                # processing later, maybe)
                pass
            # The else that would other wise be here is for ResponseStatusType.OK, but
            # since well_known_host returned None it doesn't exist. Effectively skipping
            # the next section to move down to Step 4 and beyond.
        else:
            # Step 3.1 Literal IP
            # Same as Step 1 except use well-known discovered values
            # (imply port 8448, if port was not included)
            # HOST header should be the result of the well_known
            # request(including port)
            diag_info.mark_step_num("Step 3.1", "Checking Well-Known for Literal IP")
            if is_this_an_ip_address(well_known_host):
                host_header = f"{well_known_host}:{well_known_port if well_known_port else '8448'}"

                diag_info.add(
                    f"Host defined in Well-Known was Literal IP {host_header}"
                )
                diag_info.mark_well_known_maybe_found()
                server_result = ServerResult(
                    host=host,
                    well_known_host=well_known_host,
                    port=well_known_port if well_known_port else "8448",
                    host_header=host_header,
                    sni_server_name=well_known_host,
                    diag_info=diag_info,
                )
                return server_result

            if well_known_port:
                # A port was found, and since we are here that means that host was not
                # None. Go with it

                # Step 3b, same as Step 2 above
                # HOST header should be the result of the well_known
                # request(including port)
                diag_info.mark_step_num(
                    "Step 3.2", "Checking Well-Known Host for explicit Port"
                )

                # Because of our explicit conditional above
                diag_info.add(f"Explicit port found: {well_known_port}")

                (_, _, _, diag_info,) = await self.check_dns_for_reg_records(
                    well_known_host, diag_info=diag_info
                )
                host_header = f"{well_known_host}:{well_known_port}"

                server_result = ServerResult(
                    host=host,
                    well_known_host=well_known_host,
                    port=well_known_port,
                    host_header=host_header,
                    sni_server_name=well_known_host,
                    diag_info=diag_info,
                )
                return server_result

            else:
                # Step 3c(and 3d), check SRV records then resolve regular DNS
                # records, except for CNAME. Still no explicit port.
                # HOST header should be the well_known host only, no port
                diag_info.mark_step_num(
                    "Step 3.3(and 3.4)",
                    "Checking for SRV records of host from Well-Known",
                )
                srv_host, srv_port, diag_info = await self.check_dns_for_srv_records(
                    well_known_host, diag_info=diag_info
                )

                if srv_host and srv_port:
                    # SRV records should always have a port, that is literally
                    # their purpose
                    (_, _, _, diag_info,) = await self.check_dns_for_reg_records(
                        srv_host, check_cname=False, diag_info=diag_info
                    )

                    server_result = ServerResult(
                        host=host,
                        well_known_host=well_known_host,
                        srv_host=srv_host,
                        port=srv_port,
                        host_header=well_known_host,
                        sni_server_name=well_known_host,
                        diag_info=diag_info,
                    )
                    return server_result

                # Step 3e, no SRV records and no explicit port, use well-known with
                # implied port 8448
                # HOST header should be the well_known host, no port
                diag_info.mark_step_num(
                    "Step 3.5",
                    "Checking for implied port 8448 of host from Well-Known",
                )

                (_, _, _, diag_info,) = await self.check_dns_for_reg_records(
                    well_known_host, diag_info=diag_info
                )

                server_result = ServerResult(
                    host=host,
                    well_known_host=well_known_host,
                    port="8448",
                    host_header=well_known_host,
                    sni_server_name=well_known_host,
                    diag_info=diag_info,
                )
                return server_result

        # So well-known was a bust, move on to SRV records
        # Step 4 and 5(the deprecated SRV)
        # HOST header should be the original server name, no port
        diag_info.mark_step_num("Step 4(and 5)", "Checking for SRV records")
        srv_host, srv_port, diag_info = await self.check_dns_for_srv_records(
            host, diag_info=diag_info
        )

        if srv_host and srv_port:
            # SRV records should always have a port, that is literally their
            # purpose
            (
                _,
                _,
                _,
                diag_info,
            ) = await self.check_dns_for_reg_records(srv_host, diag_info=diag_info)

            server_result = ServerResult(
                host=host,
                srv_host=srv_host,
                port=srv_port,
                host_header=host,
                sni_server_name=host,
                diag_info=diag_info,
            )
            return server_result

        # Step 6, no SRV records and no explicit port,
        # use provided hostname with implied port 8448
        diag_info.mark_step_num("Step 6", "Use implied port 8448")
        (
            _,
            _,
            _,
            diag_info,
        ) = await self.check_dns_for_reg_records(host, diag_info=diag_info)

        server_result = ServerResult(
            host=host,
            port="8448",
            host_header=host,
            sni_server_name=host,
            diag_info=diag_info,
        )
        return server_result
