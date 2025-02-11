import logging

from backoff._typing import Details
from dns.asyncresolver import Resolver
from dns.message import Message
import backoff
import dns.resolver


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


class CachingDNSResolver:
    def __init__(self):
        self.dns_resolver = Resolver()
        # DNS timeout for a request default is 2 seconds
        # DNS lifetime of requests default is 5 seconds
        # The lifetime we can touch, make it longer to give some more time for slow DNS servers
        self.dns_resolver.lifetime = 10.0

    def dns_query(
        self,
        server_name: str,
        query_type: str,
    ):
        query = dns.message.make_query(server_name, query_type)
        nameserver = self.dns_resolver.nameservers[0]

        # This returns a tuple(Message, used_tcp_bool), just get the first part
        response = dns.query.udp_with_fallback(query, str(nameserver))[0]

        if response.rcode() == dns.rcode.SERVFAIL:
            # diag_info.error(f"No '{query_type}' record for '{server_name}'(SERVFAIL) potential DNSSEC validation fail")
            pass
        elif response.rcode() == dns.rcode.NXDOMAIN:
            # diag_info.error(f"No '{query_type}' record for '{server_name}'(NXDOMAIN)")
            pass

        if (
            response.rcode() != dns.rcode.NOERROR
            and response.rcode() != dns.rcode.NXDOMAIN
            and response.rcode() != dns.rcode.SERVFAIL
        ):
            # server_discovery_logger.warning(
            #     "DNS query %s for %s got %r, %r", query_type, server_name, response.rcode(), response.answer
            # )
            pass

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
        list_of_host_port_tuples: list[tuple[str, str]],
        check_cname: bool = True,
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """
        Check DNS records for A, then AAAA, and (optionally) CNAME which will replace the A or AAAA records returned.

        Args:
            list_of_host_port_tuples: A list of tuples, matching [host:string, port:string]. Port isn't used, but is
                passed through to the returned result.
            check_cname: if checking for CNAME as well

        Returns: a 2-Tuple of Lists, one for IP4(A records) and one for IP6(AAAA records). Each list contains another
            2-Tuple of [ip_address:str, port:str]
        """
        list_of_ip4_port_tuples: list[tuple[str, str]] = []
        list_of_ip6_port_tuples: list[tuple[str, str]] = []
        for host_port_entry in list_of_host_port_tuples:
            host, port = host_port_entry

            try:
                ip4_list, ip6_list = self._check_dns_for_reg_records(
                    server_name=host,
                    check_cname=check_cname,
                )

            # TODO: Not sure we hit this any more, needs stress testing
            except dns.resolver.NoNameservers:
                # diag_info.add("Hit a possible SERVFAIL condition, working around")
                # server_discovery_logger.warning("Do we still hit this? %s", host)
                pass

            # This one is still used, I think. It hits the backoff system, so I think yes
            except dns.resolver.LifetimeTimeout:
                # Wait for a moment before retrying, as this is a DNS server failure,
                # the request timed out several times probably because the DNS server
                # was busy. The backoff handler should handle this for us, but log it anyway
                # diag_info.error("Not able to contact any Nameservers, retried 3 times(Timeout)")
                pass

            except dns.resolver.NXDOMAIN:
                # diag_info.error(f"No DNS records found for '{host}'(NXDOMAIN)")
                pass

            except Exception as e:
                # diag_info.error(f"Hit a DNS exception: {e}")
                # server_discovery_logger.error("Hit a DNS exception: %r", e)
                pass

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
    ) -> tuple[list[str], list[str]]:
        a_ip_addresses: list[str] = []
        a4_ip_addresses: list[str] = []

        # Apparently CNAME records piggyback on the A request somehow, use it if needed
        a_cname_responses = self.dns_query(server_name, "A")
        a4_responses = self.dns_query(server_name, "AAAA")

        if check_cname:
            server_name = self.recursively_resolve_cname_dns_record(server_name, a_cname_responses)

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
            # diag_info.mark_dns_record_found()
            # diag_info.add(f"DNS 'A' record found: {server_name} -> {rdata.address}")

        if not a_ip_addresses:
            # diag_info.add(f"No 'A' DNS record found for '{server_name}'")
            pass

        for rdata in a4_responses:
            a4_ip_addresses.extend((str(rdata.address),))
            # diag_info.mark_dns_record_found()
            # diag_info.add(f"DNS 'AAAA' record found: {server_name} -> {rdata.address}")

        if not a4_ip_addresses:
            # diag_info.add(f"No 'AAAA' DNS record found for '{server_name}'")
            pass

        return a_ip_addresses, a4_ip_addresses

    def recursively_resolve_cname_dns_record(
        self,
        server_name: str,
        initial_dns_message: Message,
    ) -> str:
        """
        CNAME records are allowed to be recursive. One can point at another. Resolve them until we don't get any more
            and return the hostname.
            Note: does not check for a circular reference pattern yet, that comes later.

        Args:
            server_name: The host to check
            initial_dns_message: The already queried for result, everything needed should be in here

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
            # diag_info.add(f"DNS 'CNAME' record found: {server_name} -> {found_cname_target}")

            # Setting the result to last_cname_found means it was the last one found, duh
            last_cname_found = self.recursively_resolve_cname_dns_record(found_cname_target, initial_dns_message)

        return last_cname_found

    async def check_dns_for_srv_records(self, server_name: str) -> list[tuple[str, str]]:
        """
        Check for SRV records. Wrap the inner version of the function to allow for backoff control and error handling.

        Args:
            server_name: the hostname to look up

        Returns: List containing Tuples of hostnames that need to be resolved and the port that was found
        """
        host_port_tuples: list[tuple[str, str]] = []

        try:
            maybe_returned_tuples = await self._check_dns_for_srv_records(
                server_name=server_name,
            )

        # Still not convinced this is necessary anymore, watch for logging to say we are
        except dns.resolver.NoNameservers:
            # diag_info.add("Hit a possible SERVFAIL condition, working around")
            # server_discovery_logger.warning("Are we still hitting this? SRV lookup %s", server_name)
            try:
                return await self._check_dns_for_srv_records(server_name)
            except Exception as e:
                # Wait for a moment before retrying, as this is a DNS server failure,
                # aka SERVFAIL was returned
                # diag_info.error(f"Not able to contact any Nameservers, retried 3 times, {e}")
                pass

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
    async def _check_dns_for_srv_records(self, server_name: str) -> list[tuple[str, str]]:
        """
        Check DNS records for the SRV records. First for a '_matrix-fed._tcp." then for the
        deprecated '_matrix._tcp.'

        Args:
            server_name: The hostname to look up
        Returns:
            List of optional tuples of string of the hostname(or None), string of the port number(or
            None). The non-deprecated record for returned values will be first, if both exist.
        """
        host_port_tuples: list[tuple[str, str]] = []

        try:
            srv_responses = self.dns_query(f"_matrix-fed._tcp.{server_name}", "SRV")

        except dns.resolver.NoAnswer:
            # diag_info.add(f"No 'SRV' record for '_matrix-fed._tcp.{server_name}'")
            pass
        except dns.resolver.NXDOMAIN:
            # diag_info.error(f"No 'SRV' record for '_matrix-fed._tcp.{server_name}'(NXDOMAIN)")
            pass

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
                # diag_info.mark_srv_record_found()
                # diag_info.add(f"SRV record found: '_matrix-fed._tcp.{server_name}' -> " f"{host}:{port}")
                host_port_tuples.extend(((host, port),))

        try:
            dep_responses = self.dns_query(f"_matrix._tcp.{server_name}", "SRV")

        except dns.resolver.NoAnswer:
            # diag_info.add(f"No 'SRV' record for '_matrix._tcp.{server_name}'")
            pass
        except dns.resolver.NXDOMAIN:
            # diag_info.error(f"No 'SRV' record for '_matrix._tcp.{server_name}'(NXDOMAIN)")
            pass

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
                # diag_info.mark_srv_record_found()
                # diag_info.add(f"SRV record found: '_matrix._tcp.{server_name}' -> " f"{host}:{port}")
                host_port_tuples.extend(((host, port),))

        if not host_port_tuples:
            # diag_info.add("No 'SRV' records found")
            pass

        return host_port_tuples
