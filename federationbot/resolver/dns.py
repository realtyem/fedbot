from __future__ import annotations

import logging

from dns.asyncresolver import Resolver
from dns.message import ANSWER, Message
from dns.name import from_text
from dns.nameserver import Do53Nameserver, Nameserver
from dns.rdataclass import IN
from dns.rdatatype import AAAA, CNAME, SRV, A, RdataType
from dns.resolver import NXDOMAIN, LifetimeTimeout, LRUCache, NoAnswer, NoNameservers
import backoff
import dns

from federationbot.cache import TTLCache
from federationbot.requests.backoff import backoff_dns_backoff_logging_handler, backoff_dns_giveup_logging_handler
from federationbot.resolver import Diagnostics, DnsResult, ServerDiscoveryDnsResult, StatusEnum

logger = logging.getLogger("dns")
logger.setLevel("INFO")
# backoff_logger = logging.getLogger("dns_backoff")


# def backoff_srv_backoff_logging_handler(details: Details) -> None:
#     wait = details.get("wait", 0.0)
#     tries = details.get("tries", 0)
#     # args is a tuple(self, server_name, diag_info), we want the second slot
#     host = details.get("args", (None, "arg not found"))[1]
#     backoff_logger.debug(
#         "DNS SRV query backing off %.2f seconds after %d tries on host %s",
#         wait,
#         tries,
#         host,
#     )


# def backoff_srv_giveup_logging_handler(details: Details) -> None:
#     elapsed = details.get("elapsed", 0.0)
#     tries = details.get("tries", 0)
#     # args is a tuple(self, server_name, diag_info), we want the second slot
#     host = details.get("args", (None, "arg not found"))[1]
#     backoff_logger.info(
#         "DNS SRV query giving up after %d tries and %.2f seconds on host %s",
#         tries,
#         elapsed,
#         host,
#     )


# def backoff_dns_backoff_logging_handler(details: Details) -> None:
#     wait = details.get("wait", 0.0)
#     tries = details.get("tries", 0)
#     # args is a tuple(self, server_name, diag_info), we want the second slot
#     host = details.get("args", (None, "arg not found"))[1]
#     backoff_logger.debug(
#         "DNS query backing off %.2f seconds after %d tries on host %s",
#         wait,
#         tries,
#         host,
#     )


# def backoff_dns_giveup_logging_handler(details: Details) -> None:
#     elapsed = details.get("elapsed", 0.0)
#     tries = details.get("tries", 0)
#     # args is a tuple(self, server_name, diag_info), we want the second slot
#     host = details.get("args", (None, "arg not found"))[1]
#     backoff_logger.info(
#         "DNS query giving up after %d tries and %.2f seconds on host %s",
#         tries,
#         elapsed,
#         host,
#     )


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

    @backoff.on_exception(
        backoff.expo,
        (LifetimeTimeout, dns.resolver.NoNameservers),
        max_tries=1,
        logger=None,
        on_backoff=[backoff_dns_backoff_logging_handler],
        on_giveup=[backoff_dns_giveup_logging_handler],
        max_value=2.0,
        base=1.0,
    )
    async def query(
        self,
        server_name: str,
        diagnostics: Diagnostics,
        rdtype: RdataType = A,
        check_cname: bool = True,
    ) -> DnsResult:
        """
        Place the dns resolution request. Catch the errors, so we control how it is returned

        Args:
            server_name: A list of tuples, matching [host:string, port:string]. Port isn't used, but is
                passed through to the returned result.
            diagnostics: verbose output storage
            rdtype: The specific type of record to resolve
            check_cname:

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

                    try:
                        cname_rrset = answer.response.find_rrset(ANSWER, name, IN, CNAME, create=False)
                    except KeyError:
                        break

                    for rdata in cname_rrset:
                        found_cname_target = str(rdata.target)

                        diagnostics.log(f"    Found CNAME record: {last_host_found} -> {found_cname_target}")

                        last_host_found = found_cname_target
            # Use create=True here to simulate an empty list, so iteration doesn't break
            responses = answer.response.find_rrset(ANSWER, from_text(last_host_found), IN, rdtype, create=True)

            for rdata in responses:
                results.append(str(rdata.address))
                # TODO: this is stupid and we need a better way
                diagnostics.status.dns = StatusEnum.OK
                diagnostics.log(f"    Found Resolved IP address: {str(rdata.address)}")

        except (NoAnswer, NXDOMAIN, NoNameservers) as e:
            error_message = str(e)
            # If one of the two queries done was OK, just use that. Stupid ipv6
            if diagnostics.status.dns != StatusEnum.OK:
                diagnostics.status.dns = StatusEnum.ERROR
                diagnostics.log(f"  {e}")

        except LifetimeTimeout:
            raise

        except Exception as e:
            logger.error("%s: %r", server_name, e, exc_info=True)
            error_message = str(e)
            diagnostics.status.dns = StatusEnum.ERROR
            diagnostics.log(f"  {error_message}")

        return DnsResult(hosts=results, error=error_message)

    async def resolve_reg_records(
        self,
        server_name: str,
        diagnostics: Diagnostics,
        check_cname: bool = True,
    ) -> ServerDiscoveryDnsResult:
        logger.debug("resolve_reg_records: %s", server_name)
        diagnostics.log(f"  Starting DNS query for: {server_name}")

        a_results = None
        a4_results = None
        try:
            a_results = await self.query(server_name, diagnostics, A, check_cname=check_cname)
        except Exception as e:
            logger.debug("resolve_reg_records: %s, A FAILED: %r", server_name, e)
        else:
            logger.debug("resolve_reg_records: %s, a_results:\n%r", server_name, a_results)

        try:
            a4_results = await self.query(server_name, diagnostics, AAAA, check_cname=check_cname)
        except Exception as e:
            logger.debug("resolve_reg_records: %s, AAAA FAILED: %r", server_name, e)
        else:
            logger.debug("resolve_reg_records: %s, a4_results:\n%r", server_name, a4_results)

        server_discovery_dns_result = ServerDiscoveryDnsResult(a_result=a_results, a4_result=a4_results)
        logger.debug(
            "resolve_reg_records: %s, server_discovery_dns_result:\n%r", server_name, server_discovery_dns_result
        )

        return server_discovery_dns_result

    # @backoff.on_exception(
    #     backoff.expo,
    #     (dns.exception.Timeout, dns.resolver.NoNameservers),
    #     max_tries=1,
    #     logger=None,
    #     on_backoff=[backoff_srv_backoff_logging_handler],
    #     on_giveup=[backoff_srv_giveup_logging_handler],
    #     max_value=2.0,
    #     base=1.0,
    # )
    async def _resolve_srv_records(self, server_name: str, diagnostics: Diagnostics) -> list[tuple[str, int]]:
        host_port_tuples: list[tuple[str, int]] = []
        srv_name = from_text(server_name)

        srv_answers = self.dns_srv_query_cache.get(server_name)
        if srv_answers is not None:
            diagnostics.log("    Found cached SRV result")

        else:
            query = dns.message.make_query(srv_name, SRV)
            nameserver = self.dns_resolver.nameservers[0]
            # Mypy thinks this could be a string, make sure it know otherwise
            assert isinstance(nameserver, Nameserver)
            # This returns a tuple(Message, used_tcp_bool), just get the first part
            srv_answers = dns.query.udp_with_fallback(query, nameserver.answer_nameserver())[0]

        if (
            srv_answers.rcode() != dns.rcode.NOERROR
            and srv_answers.rcode() != dns.rcode.NXDOMAIN
            and srv_answers.rcode() != dns.rcode.SERVFAIL
        ):
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
            diagnostics.status.srv = StatusEnum.OK
            diagnostics.log(f"    Received SRV target and port: {host}:{port}")
            host_port_tuples.append((host, port))

        ip_port_tuples = []
        if host_port_tuples:
            # Need to resolve each one, and keep the port with it
            for _host, _port in host_port_tuples:
                # The spec says not to resolve any CNAME records found from this host
                dns_records = await self.resolve_reg_records(_host, diagnostics, check_cname=False)
                for _found_ip in dns_records.get_hosts():
                    ip_port_tuples.append((_found_ip, _port))

        if ip_port_tuples:
            self.dns_srv_query_cache.set(server_name, srv_answers, DNS_SRV_GOOD_RESULT_CACHE_TTL_MS)
        else:
            self.dns_srv_query_cache.set(server_name, srv_answers, DNS_SRV_BAD_RESULT_CACHE_TTL_MS)

        return ip_port_tuples

    async def resolve_srv_records(self, server_name: str, diagnostics: Diagnostics) -> list[tuple[str, int]]:
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
        diagnostics.log(f"  Starting SRV query for: _matrix-fed._tcp.{server_name}")

        matrix_fed_answers = await self._resolve_srv_records(f"_matrix-fed._tcp.{server_name}", diagnostics)
        if matrix_fed_answers:
            return matrix_fed_answers

        diagnostics.log(f"  Starting SRV query for: _matrix._tcp.{server_name}")
        deprecated_matrix_answers = await self._resolve_srv_records(f"_matrix._tcp.{server_name}", diagnostics)

        return deprecated_matrix_answers
