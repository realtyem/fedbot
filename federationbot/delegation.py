from typing import Any, Callable, Dict, Optional, Tuple
from asyncio import sleep
import ipaddress
import json

from aiohttp import client_exceptions
from dns.asyncresolver import Resolver
from mautrix.util.logging import TraceLogger
import dns.resolver

from federationbot.errors import FedBotException, WellKnownHasSchemeError
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
        raise WellKnownHasSchemeError

    # str.split() will raise a ValueError if the value to split by isn't there
    try:
        server_host, server_port = server_name.split(":", maxsplit=1)
    except ValueError:
        # Accept this gracefully, as it is probably the common path
        pass

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

    return True


def _parse_and_check_well_known_response(
    response: Dict[str, Any], diag_info: DiagnosticInfo
) -> Tuple[Optional[str], Optional[str]]:
    """
    Parse the dictionary returned by the well-known request. Collect DiagnosticInfo
        throughout the process. Follow the spec from Step 3

    Should get at least a 'host' and hopefully a 'port'

    Args:
        response: The Dict with the response from well-known
        diag_info: The DiagnosticInfo to add to

    Returns:
         Tuple of host(or None), port(or None)
    """
    host = None
    port = None

    # In theory, got a good response. Should be JSON of
    # {"m.server": "example.com:433"} if there was a port
    well_known_result: Optional[str] = response.get("m.server", None)
    if well_known_result is None:
        diag_info.error("Well-Known missing 'm.server' JSON key")
        diag_info.mark_error_on_well_known()

    else:
        # I tried to find a library or module that would comprehensively handle
        # parsing a URL without a scheme, yarl came close. I guess we'll just
        # have to cover the basics by hand.
        try:
            host, port = check_and_maybe_split_server_name(well_known_result)
        except WellKnownHasSchemeError:
            diag_info.error("Well-Known 'm.server' has a scheme when it should not:")
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

    return host, port


class DelegationHandler:
    def __init__(self, logger: TraceLogger) -> None:
        self.dns_resolver = Resolver()
        # DNS lifetime of requests default is 5 seconds
        self.dns_resolver.lifetime = 10.0
        # DNS timeout for a request default is 2 seconds
        self.logger = logger
        self.json_decoder = json.JSONDecoder()

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

    async def make_well_known_request(
        self, request_cb: Callable, host: str, diag_info: DiagnosticInfo
    ) -> Optional[Dict[str, Any]]:
        """
        Make the GET request to the well-known endpoint. Borrow the error handling code from FederationHandler
        Args:
            request_cb: The Callable on FederationHandler. Use _federation_request()
            host: The basic host to check
            diag_info: DiagnosticInfo object to append info/errors too

        Returns: A Dict[str, Any] of the JSON returned, or None

        """
        content: Optional[Dict[str, Any]] = None

        try:
            # This will return a context manager called ClientResponse that will need to be parsed below
            response = await request_cb(
                destination_server_name=host, path="/.well-known/matrix/server"
            )

        # The callback used above handles a boatload of individual exceptions and consolidates them into one
        # that is easier to extract displayable data from.
        except FedBotException as e:
            diag_info.error(f"{e.summary_exception}")
            if e.__class__.__name__ != "PluginTimeout":
                diag_info.add(f"{e.long_exception}")
            return None

        async with response:
            status = response.status
            reason = response.reason
            headers = response.headers

            if status == 200:
                # Potentially anything from 200 up to 500 can have something to say
                try:
                    content = await response.json()
                except client_exceptions.ContentTypeError:
                    diag_info.error(
                        "Response had Content-Type: "
                        f"{headers.get('Content-Type', 'None Found')}"
                    )
                    diag_info.add(
                        "Expected Content-Type of 'application/json', will try "
                        "work-around"
                    )
                    try:
                        text_result = await response.text()
                        content = self.json_decoder.decode(text_result)
                    except json.decoder.JSONDecodeError:
                        # self.logger.info(f"text_result: {text_result}")
                        diag_info.error("JSONDecodeError, work-around failed")

        diag_info.add(f"Request status: code:{status}, reason: {reason}")

        # Mark the DiagnosticInfo, as that's how any error codes get passed out
        if status == 404:
            diag_info.mark_no_well_known()
        elif status != 200 or (status == 200 and content is None):
            # Don't forget to work around Caddy defaulting to 200 for unknown endpoints. I still believe this is against
            # spec and therefore is an error. This condition only holds water for well-known, as there are instances
            # where a 200 and an empty {} are legitimate responses(like for /send)
            if status == 200:
                self.logger.debug(f"well-known: HIT possible caddy condition: {host}")
            # For whatever reason(which should be in the errors/diag returned),
            # there was no usable well-known
            diag_info.mark_error_on_well_known()
        else:
            diag_info.add(f"{content}")

        return content

    async def handle_well_known_delegation(
        self,
        original_host: str,
        well_known_host: str,
        well_known_port: Optional[str],
        diag_info: DiagnosticInfo,
    ) -> ServerResult:
        # Step 3.1 Literal IP
        # Same as Step 1 except use well-known discovered values
        # (imply port 8448, if port was not included)
        # HOST header should be the result of the well_known
        # request(including port)
        diag_info.mark_step_num("Step 3.1", "Checking Well-Known for Literal IP")
        if is_this_an_ip_address(well_known_host):
            host_header = f"{well_known_host}:{well_known_port or '8448'}"

            diag_info.add(f"Host defined in Well-Known was Literal IP {host_header}")
            diag_info.mark_well_known_maybe_found()
            server_result = ServerResult(
                host=original_host,
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
                host=original_host,
                well_known_host=well_known_host,
                port=well_known_port,
                host_header=host_header,
                sni_server_name=well_known_host,
                diag_info=diag_info,
            )
            return server_result

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
                host=original_host,
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

        (
            _,
            _,
            _,
            diag_info,
        ) = await self.check_dns_for_reg_records(well_known_host, diag_info=diag_info)

        server_result = ServerResult(
            host=original_host,
            well_known_host=well_known_host,
            port="8448",
            host_header=well_known_host,
            sni_server_name=well_known_host,
            diag_info=diag_info,
        )
        return server_result

    async def handle_delegation(
        self,
        server_name: str,
        fed_req_callback: Callable,
        diag_info: DiagnosticInfo = DiagnosticInfo(False),
    ) -> ServerResult:
        """
        Perform server discovery for a given server name.

        Args:
            server_name: The name as supplied from the back of a mxid
            fed_req_callback: Pass through the callable to retrieve well-known data
            diag_info: Diagnostic collection object

        Returns: A ServerResult(or ServerResultError) object with all the information
            found during delegation discovery. May include well-known and SRV record
            information.
        """

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
        except WellKnownHasSchemeError:
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

        # Borrow our FederationHandler error handling to make this request
        content = await self.make_well_known_request(
            request_cb=fed_req_callback,
            host=host,
            diag_info=diag_info,
        )

        if not content:
            diag_info.add("No usable data in response")

        # If there is content, then something came back. Find out if it's useful or not
        else:
            # If host is None, this well-known was no good, pull from diag_info to find out
            # if it was not there, or had an error.
            # If port is None, then check SRV record
            # _parse_and_check_well_known_response() will mark the diag_info for us.
            (well_known_host, well_known_port,) = _parse_and_check_well_known_response(
                response=content, diag_info=diag_info
            )

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
                server_result = await self.handle_well_known_delegation(
                    original_host=host,
                    well_known_host=well_known_host,
                    well_known_port=well_known_port,
                    diag_info=diag_info,
                )
                return server_result
        # diag_info.append_from(response.errors)

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
