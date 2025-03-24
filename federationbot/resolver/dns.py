import logging

from dns.asyncresolver import Resolver
from dns.message import ANSWER, Message
from dns.name import from_text
from dns.nameserver import Do53Nameserver
from dns.rdataclass import IN
from dns.rdatatype import AAAA, CNAME, SRV, A, RdataType
from dns.resolver import NXDOMAIN, LRUCache, NoAnswer
import dns

from federationbot.cache import TTLCache
from federationbot.resolver import Diagnostics, DnsResult, ServerDiscoveryDnsResult, StatusEnum

logger = logging.getLogger("dns")
logger.setLevel("INFO")


DNS_SRV_GOOD_RESULT_CACHE_TTL_MS = 1000 * 60 * 60
DNS_SRV_BAD_RESULT_CACHE_TTL_MS = 1000 * 60


class CachingDNSResolver:
    def __init__(self):
        nameserver = Do53Nameserver("192.168.2.1")
        self.dns_resolver = Resolver()
        self.dns_resolver.nameservers = [nameserver]
        self.dns_resolver.cache = LRUCache()
        self.dns_srv_query_cache: TTLCache[str, Message] = TTLCache()

        # DNS timeout for a request default is 2 seconds
        # DNS lifetime of requests default is 5 seconds
        # The lifetime we can touch, make it longer to give some more time for slow DNS servers
        self.dns_resolver.lifetime = 10.0

    async def query(
        self, server_name: str, rdtype: RdataType = A, check_cname: bool = True, diagnostics: Diagnostics | None = None
    ) -> DnsResult:
        """
        Place the dns resolution request. Catch the errors, so we control how it is returned

        Args:
            server_name: A list of tuples, matching [host:string, port:string]. Port isn't used, but is
                passed through to the returned result.
            rdtype: The specific type of record to resolve
            check_cname:
            diagnostics: verbose output storage

        Returns: a tuple containing two lists of strings. The first is the IP addresses found, and the second is
            pretty formatted texted displaying the CNAME server targets
        """
        results: list[str] = []
        error_message = None
        try:
            # We don't raise on NoAnswer, this creates an empty Answer that won't break iteration
            answer = await self.dns_resolver.resolve(server_name, rdtype, raise_on_no_answer=True)

            last_host_found = str(server_name)
            if check_cname:
                while True:
                    name = from_text(last_host_found)
                    last_name_searched = server_name
                    try:
                        cname_rrset = answer.response.find_rrset(ANSWER, name, IN, CNAME, create=False)
                    except KeyError:
                        break

                    for rdata in cname_rrset:
                        found_cname_target = str(rdata.target)

                        if diagnostics:
                            diagnostics.log(f"    Found CNAME record: {last_name_searched} -> {found_cname_target}")

                        last_host_found = found_cname_target
            # Use create=True here to simulate an empty list, so iteration doesn't break
            responses = answer.response.find_rrset(ANSWER, from_text(last_host_found), IN, rdtype, create=True)

            for rdata in responses:
                results.append(str(rdata.address))
                if diagnostics:
                    # TODO: this is stupid and we need a better way
                    diagnostics.status.dns = StatusEnum.OK
                    diagnostics.log(f"    Found Resolved IP address: {str(rdata.address)}")

        except NoAnswer as e:
            error_message = "NoAnswer"
            if diagnostics:
                diagnostics.status.dns = StatusEnum.ERROR
                diagnostics.log(f"  {error_message}: {e}")

        except NXDOMAIN as e:
            error_message = "NXDOMAIN"
            if diagnostics:
                diagnostics.status.dns = StatusEnum.ERROR
                diagnostics.log(f"  {error_message}: {e}")

        except Exception as e:
            logger.error("%s: %r", server_name, e, exc_info=True)
            error_message = str(e)
            if diagnostics:
                diagnostics.status.dns = StatusEnum.ERROR
                diagnostics.log(f"  {error_message}")

        return DnsResult(hosts=results, error=error_message)

    async def resolve_reg_records(
        self,
        server_name: str,
        check_cname: bool = True,
        diagnostics: Diagnostics | None = None,
    ) -> ServerDiscoveryDnsResult:
        logger.debug("resolve_reg_records: %s", server_name)
        if diagnostics:
            diagnostics.log(f"  Starting DNS query for: {server_name}")

        a_results = await self.query(server_name, A, check_cname=check_cname, diagnostics=diagnostics)
        logger.debug("resolve_reg_records: %s, a_results:\n%r", server_name, a_results)
        a4_results = await self.query(server_name, AAAA, check_cname=check_cname, diagnostics=diagnostics)
        logger.debug("resolve_reg_records: %s, a4_results:\n%r", server_name, a4_results)

        server_discovery_dns_result = ServerDiscoveryDnsResult(a_result=a_results, a4_result=a4_results)
        logger.debug(
            "resolve_reg_records: %s, server_discovery_dns_result:\n%r", server_name, server_discovery_dns_result
        )

        return server_discovery_dns_result

    async def _resolve_srv_records(
        self, server_name: str, diagnostics: Diagnostics | None = None
    ) -> list[tuple[str, int]]:
        host_port_tuples: list[tuple[str, int]] = []
        srv_name = from_text(server_name)

        srv_answers = self.dns_srv_query_cache.get(server_name)
        if diagnostics and srv_answers is not None:
            diagnostics.log("    Found cached SRV result")

        if srv_answers is None:
            query = dns.message.make_query(srv_name, SRV)
            nameserver = self.dns_resolver.nameservers[0]

            # This returns a tuple(Message, used_tcp_bool), just get the first part
            srv_answers = dns.query.udp_with_fallback(query, nameserver.answer_nameserver())[0]

        if (
            srv_answers.rcode() != dns.rcode.NOERROR
            and srv_answers.rcode() != dns.rcode.NXDOMAIN
            and srv_answers.rcode() != dns.rcode.SERVFAIL
        ):
            if diagnostics:
                diagnostics.status.srv = StatusEnum.ERROR
                diagnostics.log(f"    Received {srv_answers.rcode()} response")
            logger.warning(
                "DNS query %s for %s got %r, %r", "SRV", server_name, srv_answers.rcode(), srv_answers.answer
            )

        # logger.debug("SRV: answer: %r", srv_answers.to_text())
        # Use the 'create' kwarg so that an exception isn't raised when it is not found
        srv_responses = srv_answers.find_rrset(ANSWER, srv_name, IN, SRV, create=True)
        # logger.debug("SRV: filtered responses: %r", srv_responses)
        for rdata in srv_responses:
            # Sometimes hosts returned from DNS queries contain appended periods
            # logger.info("SRV: RDATA %r", rdata)
            host = str(rdata.target).rstrip(".")
            port = int(rdata.port)
            if diagnostics:
                diagnostics.status.srv = StatusEnum.OK
                diagnostics.log(f"    Received SRV target and port: {host}:{port}")
            host_port_tuples.append((host, port))

        ip_port_tuples = []
        if host_port_tuples:
            # Need to resolve each one, and keep the port with it
            for _host, _port in host_port_tuples:
                # The spec says not to resolve any CNAME records found from this host
                dns_records = await self.resolve_reg_records(_host, check_cname=False, diagnostics=diagnostics)
                for _found_ip in dns_records.get_hosts():
                    ip_port_tuples.append((_found_ip, _port))

        if ip_port_tuples:
            self.dns_srv_query_cache.set(server_name, srv_answers, DNS_SRV_GOOD_RESULT_CACHE_TTL_MS)
        else:
            self.dns_srv_query_cache.set(server_name, srv_answers, DNS_SRV_BAD_RESULT_CACHE_TTL_MS)

        return ip_port_tuples

    async def resolve_srv_records(
        self, server_name: str, diagnostics: Diagnostics | None = None
    ) -> list[tuple[str, int]]:
        """
        Check for SRV records. Wrap the inner version of the function to allow for clean cache wrapping.
        First check for the newer '_matrix-fed._tcp.' SRV record, and if that is not found, use the
        deprecated '_matrix._tcp.' SRV record

        Args:
            server_name: the hostname to look up
            diagnostics:

        Returns: List containing Tuples of hostnames that need to be resolved and the port that was found
        """
        # logger.debug("Preparing to request SRV records for %s", server_name)
        if diagnostics:
            diagnostics.log(f"  Starting SRV query for: _matrix-fed._tcp.{server_name}")

        matrix_fed_answers = await self._resolve_srv_records(f"_matrix-fed._tcp.{server_name}", diagnostics=diagnostics)
        if matrix_fed_answers:
            return matrix_fed_answers

        if diagnostics:
            diagnostics.log(f"  Starting SRV query for: _matrix._tcp.{server_name}")
        deprecated_matrix_answers = await self._resolve_srv_records(
            f"_matrix._tcp.{server_name}", diagnostics=diagnostics
        )

        return deprecated_matrix_answers
