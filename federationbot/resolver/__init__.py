from __future__ import annotations

from typing import Any
from dataclasses import dataclass, field
from enum import StrEnum
import ipaddress
import itertools
import socket

from aiohttp.client_reqrep import CIMultiDictProxy
from aiohttp.tracing import Trace

from federationbot.errors import WellKnownParsingError, WellKnownSchemeError


@dataclass(slots=True)
class WellKnownLookupResult:
    """
    When the result is from a well known request, it goes in here. Base class
    for all well known request responses(including errors)
    """


@dataclass(slots=True)
class WellKnownDiagnosticResult(WellKnownLookupResult):
    """
    Supplement WellKnownLookupResult with additional data from the result

        host: The hostname or IP
        port: The port, if one was found
        status_code: The HTTP status code from the lookup
        content_type: If it was 'application/json' or something else
        context_trace: For the response times
    """

    host: str
    port: int
    status_code: int
    content_type: str
    context_trace: Trace
    headers: CIMultiDictProxy


@dataclass(slots=True)
class NoWellKnown(WellKnownLookupResult):
    """When there was no result from well known request, but it was not a failure"""

    status_code: int


@dataclass(slots=True)
class WellKnownLookupFailure(WellKnownLookupResult):
    """
    There was a well known request failure. Base class
    """

    status_code: int | None
    reason: str


@dataclass(slots=True)
class WellKnownSchemeFailure(WellKnownLookupFailure):
    """
    The well known request had a scheme attached, which is against spec
    """


@dataclass(slots=True)
class WellKnownParseFailure(WellKnownLookupFailure):
    """
    The well known request had a parsing error, which is against spec
    """


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


def check_and_maybe_split_server_name(server_name: str) -> tuple[str, int]:
    """
    Checks that a server name does not have a scheme prepended to it(something seen
    in the wild), then splits the server_name from any potential port number that is
    appended. Can be used for well known or when we think there may be a port

    Args:
        server_name: a server domain as expected by the matrix spec, with or without
            port number

    Returns: Tuple of the domain as a string andthe port as integer(0 if not found)

    Raises: WellKnownSchemeError if a 'http' related scheme was found
    """
    server_host: str = server_name
    server_port: int = 0

    if server_name.startswith(("http:", "https")) or "://" in server_name:
        raise WellKnownSchemeError(reason=server_name)

    # str.split() will raise a ValueError if the value to split by isn't there
    try:
        server_host, _port = server_name.split(":", maxsplit=1)
        server_port = int(_port)
    except ValueError:
        # Accept this gracefully, as it is probably a common path
        pass

    return server_host, server_port


def parse_and_check_well_known_response(response: dict[str, Any]) -> tuple[str | None, int]:
    """
    Parse the dictionary returned by the well-known request. Collect DiagnosticInfo
        throughout the process. Follow the spec from Step 3

    Should get at least a 'host' and hopefully a 'port'

    Args:
        response: The Dict with the response from well-known

    Returns:
         Tuple of host(or None), port(or 0 if not found)
    """
    host = None
    port = 0

    # In theory, got a good response. Should be JSON of
    # {"m.server": "example.com:433"} if there was a port
    well_known_result = response.get("m.server", None)
    if isinstance(well_known_result, str):
        # I tried to find a library or module that would comprehensively handle
        # parsing a URL without a scheme, yarl came close. I guess we'll just
        # have to cover the basics by hand.
        host, port = check_and_maybe_split_server_name(well_known_result)
    else:
        raise WellKnownParsingError(reason=f"{response}")

    return host, port


_NUMERIC_SOCKET_FLAGS = socket.AI_NUMERICHOST | socket.AI_NUMERICSERV


class StatusEnum(StrEnum):
    OK = "OK"
    ERROR = "ERROR"
    NONE = "NONE"


@dataclass(slots=True)
class ServerDiscoveryStatus:
    well_known: StatusEnum = StatusEnum.NONE
    dns: StatusEnum = StatusEnum.NONE
    srv: StatusEnum = StatusEnum.NONE
    connection: StatusEnum = StatusEnum.NONE


@dataclass(slots=True)
class IpAddressAndPort:
    ip_address: str
    port: int


@dataclass(slots=True)
class Diagnostics:
    """
    Aggregates diagnostic information during server discovery process.

    Tracks status of well-known, SRV, DNS and connection tests while collecting
    diagnostic messages for debugging federation issues.
    """

    output_list: list[str] = field(default_factory=list)
    status: ServerDiscoveryStatus = field(default_factory=ServerDiscoveryStatus)


@dataclass(slots=True)
class ServerDiscoveryBaseResult:
    """
    The base class for what happened during the discovery process
    """

    diagnostics: Diagnostics | None


@dataclass(slots=True)
class ServerDiscoveryResult(ServerDiscoveryBaseResult):
    """
    The fully resolved data for connecting to a Matrix federation server endpoint

    Attributes:
        hostname:
        list_of_resolved_addresses:
        host_header:
        sni:
    """

    hostname: str
    list_of_resolved_addresses: list[IpAddressAndPort]
    host_header: str
    sni: str
    time_for_complete_delegation: float
    # diagnostics: Diagnostics | None = None


@dataclass(slots=True)
class ServerDiscoveryErrorResult(ServerDiscoveryBaseResult):
    """
    An error occurred during the discovery process
    """

    error: str
    # diagnostics: Diagnostics | None = None


@dataclass(slots=True)
class DnsResult:
    """
    The DNS results. Empty lists are not always an error on their own, watch for text in 'error'

    Attributes:
        hosts: list of strings designating the IP addresses the hostname resolved to.
            May be empty to reflect no answers
        targets: The results from a SRV query as a list of tuples[ip_address, port]
        error: The string with an error message or None
    """

    hosts: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(slots=True)
class ServerDiscoveryDnsBaseResult:
    """
    What gets added to the ServerDiscoveryResult
    """


@dataclass(slots=True)
class ServerDiscoveryDnsResult(ServerDiscoveryDnsBaseResult):
    """
    Successful
    """

    a_result: DnsResult
    a4_result: DnsResult

    def __bool__(self) -> bool:
        if self.a_result.error or self.a4_result.error:
            return False
        return True

    def get_errors(self) -> list[str]:
        errors = []
        if self.a_result.error:
            errors.append(self.a_result.error)
        if self.a4_result.error:
            errors.append(self.a4_result.error)
        return errors

    def get_hosts(self) -> list[str]:
        return list(itertools.chain(self.a_result.hosts, self.a4_result.hosts))


@dataclass(slots=True)
class ServerDiscoveryDnsErrorResult(ServerDiscoveryDnsBaseResult):
    """
    Was an error
    """
