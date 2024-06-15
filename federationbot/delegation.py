from typing import Any, Callable, Dict, List, Optional, Tuple
import ipaddress
import json
import logging

from aiohttp import client_exceptions
from backoff._typing import Details
from dns.asyncresolver import Resolver
from dns.message import Message
import backoff
import dns.resolver

from federationbot.errors import FedBotException, SchemeError
from federationbot.server_result import DiagnosticInfo, ServerResult

server_discovery_logger = logging.getLogger("server_discovery")
backoff_logger = logging.getLogger("dns_backoff")


def backoff_srv_backoff_logging_handler(details: Details) -> None:
    wait = details.get("wait", 0.0)
    tries = details.get("tries", 0)
    # args is a tuple(self, server_name, diag_info), we want the second slot
    host = details.get("args", (None, "arg not found"))[1]
    backoff_logger.debug(
        "DNS SRV query backing off %.2f seconds after %d tries on host %s",
        wait,
        tries,
        host,
    )


def backoff_srv_giveup_logging_handler(details: Details) -> None:
    elapsed = details.get("elapsed", 0.0)
    tries = details.get("tries", 0)
    # args is a tuple(self, server_name, diag_info), we want the second slot
    host = details.get("args", (None, "arg not found"))[1]
    backoff_logger.info(
        "DNS SRV query giving up after %d tries and %.2f seconds on host %s",
        tries,
        elapsed,
        host,
    )


def backoff_dns_backoff_logging_handler(details: Details) -> None:
    wait = details.get("wait", 0.0)
    tries = details.get("tries", 0)
    # args is a tuple(self, server_name, diag_info), we want the second slot
    host = details.get("args", (None, "arg not found"))[1]
    backoff_logger.debug(
        "DNS query backing off %.2f seconds after %d tries on host %s",
        wait,
        tries,
        host,
    )


def backoff_dns_giveup_logging_handler(details: Details) -> None:
    elapsed = details.get("elapsed", 0.0)
    tries = details.get("tries", 0)
    # args is a tuple(self, server_name, diag_info), we want the second slot
    host = details.get("args", (None, "arg not found"))[1]
    backoff_logger.info(
        "DNS query giving up after %d tries and %.2f seconds on host %s",
        tries,
        elapsed,
        host,
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
        raise SchemeError("SchemeError", f"Scheme should not be present for '{server_name}'")

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
        except SchemeError:
            diag_info.error("Well-Known 'm.server' has a scheme when it should not:")
            diag_info.error(f"{well_known_result}", front_pad="      ")
            diag_info.mark_error_on_well_known()
        except AttributeError:
            # Apparently this happens if a server has their well-known set like a
            # client well-known. Don't print custom error message showing the result
            # as it could be spammy(and not fit)
            diag_info.error("Well-Known 'm.server' has wrong attributes, should be a host/port")
            diag_info.mark_error_on_well_known()

        else:
            diag_info.mark_well_known_maybe_found()

    return host, port


class DelegationHandler:
    def __init__(self) -> None:
        self.dns_resolver = Resolver()
        # DNS timeout for a request default is 2 seconds
        # DNS lifetime of requests default is 5 seconds
        # The lifetime we can touch, make it longer to give some more time for slow DNS servers
        self.dns_resolver.lifetime = 10.0
        self.json_decoder = json.JSONDecoder()

    def dns_query(
        self,
        server_name: str,
        query_type: str,
        diag_info: DiagnosticInfo = DiagnosticInfo(False),
    ):
        query = dns.message.make_query(server_name, query_type)
        nameserver = self.dns_resolver.nameservers[0]

        # This returns a tuple(Message, used_tcp_bool), just get the first part
        response = dns.query.udp_with_fallback(query, str(nameserver))[0]

        if response.rcode() == dns.rcode.SERVFAIL:
            diag_info.error(f"No '{query_type}' record for '{server_name}'(SERVFAIL) potential DNSSEC validation fail")
        elif response.rcode() == dns.rcode.NXDOMAIN:
            diag_info.error(f"No '{query_type}' record for '{server_name}'(NXDOMAIN)")

        if (
            response.rcode() != dns.rcode.NOERROR
            and response.rcode() != dns.rcode.NXDOMAIN
            and response.rcode() != dns.rcode.SERVFAIL
        ):
            server_discovery_logger.warning(
                f"DNS query {query_type} for {server_name} got {response.rcode()}, {response.answer}"
            )

        # NOTE: To disable DNSSEC validation and try again to see what it says. For now, don't use since seems to work
        # even without.
        # query.flags = query.flags | dns.flags.CD
        # response = dns.query.udp_with_fallback(query, "1.1.1.1")[0]
        # server_discovery_logger.info(f"DNSSEC fallback response: {response.answer}")
        #
        # a_records: dns.resolver.Answer = await self.dns_resolver.resolve(server_name, "A")
        return response

    def check_dns_from_list_for_reg_records(
        self,
        list_of_host_port_tuples: List[Tuple[str, str]],
        check_cname: bool = True,
        diag_info: DiagnosticInfo = DiagnosticInfo(False),
    ) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
        """
        Check DNS records for A, then AAAA, and (optionally) CNAME which will replace the A or AAAA records returned.

        Args:
            list_of_host_port_tuples: A list of tuples, matching [host:string, port:string]. Port isn't used, but is
                passed through to the returned result.
            check_cname: if checking for CNAME as well
            diag_info: while we always collect errors, additional data is collected with
                this

        Returns: a 2-Tuple of Lists, one for IP4(A records) and one for IP6(AAAA records). Each list contains another
            2-Tuple of [ip_address:str, port:str]
        """
        diag_info.mark_step_num("for DNS records")
        list_of_ip4_port_tuples: List[Tuple[str, str]] = []
        list_of_ip6_port_tuples: List[Tuple[str, str]] = []
        for host_port_entry in list_of_host_port_tuples:
            host, port = host_port_entry

            try:
                ip4_list, ip6_list = self._check_dns_for_reg_records(
                    server_name=host,
                    check_cname=check_cname,
                    diag_info=diag_info,
                )

            # TODO: Not sure we hit this any more, needs stress testing
            except dns.resolver.NoNameservers:
                diag_info.add("Hit a possible SERVFAIL condition, working around")
                server_discovery_logger.warning(f"Do we still hit this? {host}")

            # This one is still used, I think. It hits the backoff system, so I think yes
            except dns.resolver.LifetimeTimeout:
                # Wait for a moment before retrying, as this is a DNS server failure,
                # the request timed out several times probably because the DNS server
                # was busy. The backoff handler should handle this for us, but log it anyway
                diag_info.error("Not able to contact any Nameservers, retried 3 times(Timeout)")

            except dns.resolver.NXDOMAIN:
                diag_info.error(f"No DNS records found for '{host}'(NXDOMAIN)")

            except Exception as e:
                diag_info.error(f"Hit a DNS exception: {e}")
                server_discovery_logger.error(f"Hit a DNS exception: {e}")

            else:
                for ip4 in ip4_list:
                    list_of_ip4_port_tuples.extend(((ip4, port),))
                for ip6 in ip6_list:
                    list_of_ip6_port_tuples.extend(((ip6, port),))

        return list_of_ip4_port_tuples, list_of_ip6_port_tuples

    @backoff.on_exception(
        backoff.expo,
        dns.resolver.LifetimeTimeout,
        max_tries=3,
        logger=None,
        on_backoff=[backoff_dns_backoff_logging_handler],
        on_giveup=[backoff_dns_giveup_logging_handler],
        max_value=2.0,
        base=1.0,
    )
    def _check_dns_for_reg_records(
        self,
        server_name: str,
        check_cname: bool,
        diag_info: DiagnosticInfo,
    ) -> Tuple[List[str], List[str]]:
        a_ip_addresses: List[str] = []
        a4_ip_addresses: List[str] = []

        # Apparently CNAME records piggyback on the A request somehow, use it if needed
        a_cname_responses = self.dns_query(server_name, "A")
        a4_responses = self.dns_query(server_name, "AAAA")

        if check_cname:
            server_name = self.recursively_resolve_cname_dns_record(server_name, a_cname_responses, diag_info)

            # Re-fetch these if a new hostname was found through the CNAME resolution
            a_cname_responses = self.dns_query(server_name, "A")
            a4_responses = self.dns_query(server_name, "AAAA")

        if a_cname_responses.rcode() == dns.rcode.NXDOMAIN and a4_responses.rcode() == dns.rcode.NXDOMAIN:
            raise dns.resolver.NXDOMAIN

        name = dns.name.from_text(server_name)
        a_responses = a_cname_responses.find_rrset(
            dns.message.ANSWER, name, dns.rdataclass.IN, dns.rdatatype.A, create=True
        )
        a4_responses = a4_responses.find_rrset(
            dns.message.ANSWER, name, dns.rdataclass.IN, dns.rdatatype.AAAA, create=True
        )

        for rdata in a_responses:
            a_ip_addresses.extend((str(rdata.address),))
            diag_info.mark_dns_record_found()
            diag_info.add(f"DNS 'A' record found: {server_name} -> {rdata.address}")

        if not a_ip_addresses:
            diag_info.add(f"No 'A' DNS record found for '{server_name}'")

        for rdata in a4_responses:
            a4_ip_addresses.extend((str(rdata.address),))
            diag_info.mark_dns_record_found()
            diag_info.add(f"DNS 'AAAA' record found: {server_name} -> {rdata.address}")

        if not a4_ip_addresses:
            diag_info.add(f"No 'AAAA' DNS record found for '{server_name}'")

        return a_ip_addresses, a4_ip_addresses

    def recursively_resolve_cname_dns_record(
        self,
        server_name: str,
        initial_dns_message: Message,
        diag_info: DiagnosticInfo,
    ) -> str:
        """
        CNAME records are allowed to be recursive. One can point at another. Resolve them until we don't get any more
            and return the hostname.
            Note: does not check for a circular reference pattern yet, that comes later.

        Args:
            server_name: The host to check
            initial_dns_message: The already queried for result, everything needed should be in here
            diag_info: DiagnosticInfo for adding messages to display

        Returns: Final CNAME resolved hostname to look up, or original server hostname

        """
        # 1. pass in the Message from the original query
        # 2. if there is a CNAME from that server_name
        #    a. recursively call same function with new server_name as target from the CNAME
        #    b. return last found if nothing else found
        name = dns.name.from_text(server_name)
        cname_rrset = initial_dns_message.find_rrset(
            dns.message.ANSWER, name, dns.rdataclass.IN, dns.rdatatype.CNAME, create=True
        )

        # Start with the original server name, then it will be returned if nothing else is found
        last_cname_found = server_name

        # There should ever only be one single *final* cname to use
        for rdata in cname_rrset:
            found_cname_target = str(rdata.target)
            diag_info.add(f"DNS 'CNAME' record found: {server_name} -> {found_cname_target}")

            # Setting the result to last_cname_found means it was the last one found, duh
            last_cname_found = self.recursively_resolve_cname_dns_record(
                found_cname_target, initial_dns_message, diag_info
            )

        return last_cname_found

    async def check_dns_for_srv_records(
        self, server_name: str, diag_info: DiagnosticInfo = DiagnosticInfo(False)
    ) -> List[Tuple[str, str]]:
        """
        Check for SRV records. Wrap the inner version of the function to allow for backoff control and error handling.

        Args:
            server_name: the hostname to look up
            diag_info: while we always collect errors, additional data is collected with
                this

        Returns: List containing Tuples of hostnames that need to be resolved and the port that was found
        """
        host_port_tuples: List[Tuple[str, str]] = []

        try:
            maybe_returned_tuples = await self._check_dns_for_srv_records(
                server_name=server_name,
                diag_info=diag_info,
            )

        # Still not convinced this is necessary anymore, watch for logging to say we are
        except dns.resolver.NoNameservers:
            diag_info.add("Hit a possible SERVFAIL condition, working around")
            server_discovery_logger.warning(f"Are we still hitting this? SRV lookup {server_name}")
            try:
                return await self._check_dns_for_srv_records(server_name, diag_info)
            except Exception as e:
                # Wait for a moment before retrying, as this is a DNS server failure,
                # aka SERVFAIL was returned
                diag_info.error(f"Not able to contact any Nameservers, retried 3 times, {e}")

        else:
            host_port_tuples.extend(maybe_returned_tuples)

        return host_port_tuples

    @backoff.on_exception(
        backoff.expo,
        dns.resolver.LifetimeTimeout,
        max_tries=3,
        logger=None,
        on_backoff=[backoff_srv_backoff_logging_handler],
        on_giveup=[backoff_srv_giveup_logging_handler],
        max_value=2.0,
        base=1.0,
    )
    async def _check_dns_for_srv_records(self, server_name: str, diag_info: DiagnosticInfo) -> List[Tuple[str, str]]:
        """
        Check DNS records for the SRV records. First for a '_matrix-fed._tcp." then for the
        deprecated '_matrix._tcp.'

        Args:
            server_name: The hostname to look up
            diag_info: Always collect errors, but other diagnostics can be collected too
        Returns:
            List of optional tuples of string of the hostname(or None), string of the port number(or
            None). The non-deprecated record for returned values will be first, if both exist.
        """
        host_port_tuples: List[Tuple[str, str]] = []

        try:
            srv_responses = self.dns_query(f"_matrix-fed._tcp.{server_name}", "SRV")

        except dns.resolver.NoAnswer:
            diag_info.add(f"No 'SRV' record for '_matrix-fed._tcp.{server_name}'")
        except dns.resolver.NXDOMAIN:
            diag_info.error(f"No 'SRV' record for '_matrix-fed._tcp.{server_name}'(NXDOMAIN)")

        else:
            name = dns.name.from_text(f"_matrix-fed._tcp.{server_name}")
            # Use the 'create' kwarg so that an exception isn't raised when it is not found
            srv_response = srv_responses.find_rrset(
                dns.message.ANSWER, name, dns.rdataclass.IN, dns.rdatatype.SRV, create=True
            )

            for rdata in srv_response:
                # Sometimes hosts returned from DNS queries contain appended periods
                host = str(rdata.target).rstrip(".")
                port = rdata.port
                diag_info.mark_srv_record_found()
                diag_info.add(f"SRV record found: '_matrix-fed._tcp.{server_name}' -> " f"{host}:{port}")
                host_port_tuples.extend(((host, port),))

        try:
            dep_responses = self.dns_query(f"_matrix._tcp.{server_name}", "SRV")

        except dns.resolver.NoAnswer:
            diag_info.add(f"No 'SRV' record for '_matrix._tcp.{server_name}'")
        except dns.resolver.NXDOMAIN:
            diag_info.error(f"No 'SRV' record for '_matrix._tcp.{server_name}'(NXDOMAIN)")

        else:
            dep_name = dns.name.from_text(f"_matrix._tcp.{server_name}")
            # Use the 'create' kwarg so that an exception isn't raised
            dep_response = dep_responses.find_rrset(
                dns.message.ANSWER, dep_name, dns.rdataclass.IN, dns.rdatatype.SRV, create=True
            )

            for rdata in dep_response:
                # Sometimes hosts returned from DNS queries contain appended periods
                host = str(rdata.target).rstrip(".")
                port = rdata.port
                diag_info.mark_srv_record_found()
                diag_info.add(f"SRV record found: '_matrix._tcp.{server_name}' -> " f"{host}:{port}")
                host_port_tuples.extend(((host, port),))

        if not host_port_tuples:
            diag_info.add("No 'SRV' records found")

        return host_port_tuples

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
        status, content = await self.make_simple_request(request_cb, host, "/.well-known/matrix/server", diag_info)
        # Mark the DiagnosticInfo, as that's how any error codes get passed out
        if status == 404:
            diag_info.mark_no_well_known()
        elif status != 200 or (status == 200 and content is None):
            # Don't forget to work around Caddy defaulting to 200 for unknown endpoints. I still believe this is against
            # spec and therefore is an error. This condition only holds water for well-known, as there are instances
            # where a 200 and an empty {} are legitimate responses(like for /send)
            if status == 200:
                server_discovery_logger.debug("well-known: HIT possible caddy condition: %s", host)
            # For whatever reason(which should be in the errors/diag returned),
            # there was no usable well-known
            diag_info.mark_error_on_well_known()
        else:
            diag_info.add(f"{content}")

        return content

    async def make_simple_request(
        self, request_cb: Callable, host: str, path: str, diag_info: DiagnosticInfo
    ) -> Tuple[int, Optional[Dict[str, Any]]]:
        content: Optional[Dict[str, Any]] = None

        try:
            # This will return a context manager called ClientResponse that will need to be parsed below
            response = await request_cb(host, path)

        # The callback used above handles a boatload of individual exceptions and consolidates them into one
        # that is easier to extract displayable data from.
        except FedBotException as e:
            diag_info.error(f"{e.summary_exception}")
            if e.__class__.__name__ != "PluginTimeout":
                diag_info.add(f"{e.long_exception}")
            return 0, None

        async with response:
            status = response.status
            reason = response.reason
            headers = response.headers

            if status == 200:
                # Potentially anything from 200 up to 500 can have something to say
                try:
                    content = await response.json()
                except client_exceptions.ContentTypeError:
                    diag_info.error("Response had Content-Type: " f"{headers.get('Content-Type', 'None Found')}")
                    diag_info.add("Expected Content-Type of 'application/json', will try work-around")
                except json.decoder.JSONDecodeError:
                    server_discovery_logger.warning("JSONDecodeError from request on %s to %s", host, path)
                    diag_info.error("JSONDecodeError")
                    diag_info.add("Content-Type was correct, but contained unusable data")
                if not content:
                    try:
                        text_result = await response.text()
                        content = self.json_decoder.decode(text_result)
                    except json.decoder.JSONDecodeError:
                        # self.logger.info(f"text_result: {text_result}")
                        diag_info.error("JSONDecodeError, work-around failed")

        diag_info.add(f"Request status: code:{status}, reason: {reason}")

        return status, content

    async def handle_well_known_delegation(
        self,
        original_host: str,
        well_known_host: str,
        well_known_port: Optional[str],
        diag_info: DiagnosticInfo,
    ) -> ServerResult:
        # These will be added to the returned ServerResult
        ip4_address_port_tuples: List[Tuple[str, str]] = []
        ip6_address_port_tuples: List[Tuple[str, str]] = []

        # Step 3.1 Literal IP
        # Same as Step 1 except use well-known discovered values (imply port 8448, if port was not included)
        # HOST header should be the result of the well_known request(including port)
        diag_info.mark_step_num("Step 3.1", "Checking Well-Known for Literal IP")
        if is_this_an_ip_address(well_known_host):
            if "." in well_known_host:
                # It's a loose check, as it's not really important right now which one this goes in
                ip4_address_port_tuples = [(well_known_host, well_known_port or "8448")]

            else:
                ip6_address_port_tuples = [(well_known_host, well_known_port or "8448")]

            diag_info.add(f"Host defined in Well-Known was Literal IP {well_known_host}:{well_known_port or '8448'}")
            diag_info.mark_well_known_maybe_found()

            # Literal IP address names will not have any DNS resolution done
            return ServerResult(
                ip4_address_port_tuples,
                ip6_address_port_tuples,
                host=original_host,
                well_known_host=well_known_host,
                host_header=f"{well_known_host}:{well_known_port or '8448'}",
                sni_server_name=well_known_host,
                diag_info=diag_info,
            )

        if well_known_port:
            # A port was found, and since we are here that means that host was not None. Go with it

            # Step 3b, same as Step 2
            # HOST header should be the result of the well_known request(including port)
            diag_info.mark_step_num("Step 3.2", "Checking Well-Known Host for explicit Port")
            diag_info.add(f"Explicit port found: {well_known_port}")

            (
                _ip4_address_port_tuples,
                _ip6_address_port_tuples,
            ) = self.check_dns_from_list_for_reg_records([(well_known_host, well_known_port)], diag_info=diag_info)

            return ServerResult(
                _ip4_address_port_tuples,
                _ip6_address_port_tuples,
                host=original_host,
                well_known_host=well_known_host,
                host_header=f"{well_known_host}:{well_known_port}",
                sni_server_name=well_known_host,
                diag_info=diag_info,
            )

        # Step 3c(and 3d), check SRV records then resolve regular DNS records, except for CNAME. Still no explicit port.
        # HOST header should be the well_known host only, no port
        diag_info.mark_step_num(
            "Step 3.3(and 3.4)",
            "Checking for SRV records of host from Well-Known",
        )
        # Grab these but make them into a Set to deduplicate. If both SRV record types are used, they may just match.
        # It's important to remember that this returns 'host', 'port' tuples, not ip addresses
        set_of_srv_result_tuples = set(await self.check_dns_for_srv_records(well_known_host, diag_info=diag_info))

        for srv_result in set_of_srv_result_tuples:
            srv_host, srv_port = srv_result

            # There may be multiple of these, if SRV is defined more than once. Extend the list to get them all.
            # Some ding-a-ling may have even gotten the ports mixed up, so keep them together
            (
                _ip4_address_port_tuples,
                _ip6_address_port_tuples,
            ) = self.check_dns_from_list_for_reg_records([(srv_host, srv_port)], check_cname=False, diag_info=diag_info)

            ip4_address_port_tuples.extend(_ip4_address_port_tuples)
            ip6_address_port_tuples.extend(_ip6_address_port_tuples)

        if ip4_address_port_tuples or ip6_address_port_tuples:
            # If there are no SRV records, both of these will be empty Lists
            return ServerResult(
                ip4_address_port_tuples,
                ip6_address_port_tuples,
                host=original_host,
                well_known_host=well_known_host,
                host_header=well_known_host,
                sni_server_name=well_known_host,
                diag_info=diag_info,
            )

        # Step 3e, no SRV records and no explicit port, use well-known with implied port 8448
        # HOST header should be the well_known host, no port
        diag_info.mark_step_num(
            "Step 3.5",
            "Checking for implied port 8448 of host from Well-Known",
        )

        (
            ip4_addresses,
            ip6_addresses,
        ) = self.check_dns_from_list_for_reg_records([(well_known_host, "8448")], diag_info=diag_info)

        return ServerResult(
            ip4_addresses,
            ip6_addresses,
            host=original_host,
            well_known_host=well_known_host,
            host_header=well_known_host,
            sni_server_name=well_known_host,
            diag_info=diag_info,
        )

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

        except SchemeError as e:
            diag_info.error(e.long_exception)
            # TODO: is there something smarter to do here? Pretty sure this can't happen
            raise e

        # Spec step 1, check for literal IP
        # HOST header should be the server_name, as it was passed in(including port but only if supplied).
        diag_info.mark_step_num("Step 1", "Checking for Literal IP")
        ip4_address_port_tuples: List[Tuple[str, str]] = []
        ip6_address_port_tuples: List[Tuple[str, str]] = []

        if is_this_an_ip_address(host):
            diag_info.add(f"Server is literal IP: {host}")
            if "." in host:
                ip4_address_port_tuples = [(host, port or "8448")]

            else:
                ip6_address_port_tuples = [(host, port or "8448")]

            return ServerResult(
                ip4_address_port_tuples,
                ip6_address_port_tuples,
                host=host,
                # Remember that the HOST header only gets a port if one was included in the server name
                host_header=f"{host}{':'+port if port else ''}",
                sni_server_name=server_name,
                diag_info=diag_info,
            )

        # Spec step 2: if there is a port, use the given host as the server name
        # HOST header should be the server_name, as it was passed in(including port)
        diag_info.mark_step_num("Step 2", "Checking for explicit Port on server name")
        if port:
            diag_info.add(f"Server name has explicit port: {port}")

            (
                ip4_address_port_tuples,
                ip6_address_port_tuples,
            ) = self.check_dns_from_list_for_reg_records([(host, port)], diag_info=diag_info)

            return ServerResult(
                ip4_address_port_tuples,
                ip6_address_port_tuples,
                host=host,
                host_header=f"{host}:{port}",
                sni_server_name=host,
                diag_info=diag_info,
            )

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

        else:
            # If port is None, then check SRV record
            # _parse_and_check_well_known_response() will mark the diag_info for us.
            (
                well_known_host,
                well_known_port,
            ) = _parse_and_check_well_known_response(response=content, diag_info=diag_info)

            if well_known_host:
                return await self.handle_well_known_delegation(
                    original_host=host,
                    well_known_host=well_known_host,
                    well_known_port=well_known_port,
                    diag_info=diag_info,
                )

        # Step 4 and 5(the deprecated SRV)
        # HOST header should be the original server name, no port
        diag_info.mark_step_num("Step 4(and 5)", "Checking for SRV records")

        # Grab these but them into a Set to deduplicate. If both SRV record types are used, they may just match
        set_of_srv_result_tuples = set(await self.check_dns_for_srv_records(host, diag_info=diag_info))

        for srv_result in set_of_srv_result_tuples:
            srv_host, srv_port = srv_result

            (
                _ip4_address_port_tuples,
                _ip6_address_port_tuples,
            ) = self.check_dns_from_list_for_reg_records([(srv_host, srv_port)], check_cname=False, diag_info=diag_info)

            ip4_address_port_tuples.extend(_ip4_address_port_tuples)
            ip6_address_port_tuples.extend(_ip6_address_port_tuples)

        if ip4_address_port_tuples or ip6_address_port_tuples:
            if len(ip4_address_port_tuples) > 1 or len(ip6_address_port_tuples) > 1:
                server_discovery_logger.warning(
                    f"STEP 5 ISSUE FOUND: {server_name} ip-port tuples potential issue: {ip4_address_port_tuples} {ip6_address_port_tuples}"
                )

            return ServerResult(
                ip4_address_port_tuples,
                ip6_address_port_tuples,
                host=host,
                well_known_host=host,
                host_header=host,
                sni_server_name=host,
                diag_info=diag_info,
            )

        # Step 6, no SRV records and no explicit port,
        # use provided hostname with implied port 8448
        diag_info.mark_step_num("Step 6", "Use implied port 8448")

        (
            ip4_address_port_tuples,
            ip6_address_port_tuples,
        ) = self.check_dns_from_list_for_reg_records([(host, "8448")], diag_info=diag_info)

        # if ip4_address_port_tuples or ip6_address_port_tuples:
        return ServerResult(
            ip4_address_port_tuples,
            ip6_address_port_tuples,
            host=host,
            host_header=host,
            sni_server_name=host,
            diag_info=diag_info,
        )
