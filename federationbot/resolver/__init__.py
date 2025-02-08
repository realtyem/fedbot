from __future__ import annotations

from typing import Any
from dataclasses import dataclass

from aiohttp.client_reqrep import CIMultiDictProxy
from aiohttp.tracing import Trace

from federationbot.errors import WellKnownSchemeError


@dataclass(slots=True)
class WellKnownLookupResult:
    """
    When the result is from a well known request, it goes in here. Base class
    for all well known request responses(including errors)
    """


@dataclass(slots=True)
class WellKnownDiagnosticResult(WellKnownLookupResult):
    """
    Adds to WellKnownLookupResult with additional data from the result

        host: The hostname or IP
        port: The port, if one was found
        status_code: The HTTP status code from the lookup
        content_type: If it was 'application/json' or something else
        context_trace: For the response times
    """

    host: str
    port: str | None
    status_code: int
    content_type: str
    context_trace: Trace
    headers: CIMultiDictProxy


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


def check_and_maybe_split_server_name(server_name: str) -> tuple[str, str | None]:
    """
    Checks that a server name does not have a scheme prepended to it(something seen
    in the wild), then splits the server_name from any potential port number that is
    appended. Can be used for well known or when we think there may be a port

    Args:
        server_name: a server domain as expected by the matrix spec, with or without
            port number

    Returns: Tuple of the domain and(if it exists) the port as strings(or None)

    Raises: WellKnownSchemeError if a 'http' related scheme was found
    """
    server_host: str = server_name
    server_port: str | None = None

    if server_name.startswith(("http:", "https")) or "://" in server_name:
        raise WellKnownSchemeError(reason=server_name)

    # str.split() will raise a ValueError if the value to split by isn't there
    try:
        server_host, server_port = server_name.split(":", maxsplit=1)
    except ValueError:
        # Accept this gracefully, as it is probably a common path
        pass

    return server_host, server_port


def parse_and_check_well_known_response(response: dict[str, Any]) -> tuple[str | None, str | None]:
    """
    Parse the dictionary returned by the well-known request. Collect DiagnosticInfo
        throughout the process. Follow the spec from Step 3

    Should get at least a 'host' and hopefully a 'port'

    Args:
        response: The Dict with the response from well-known

    Returns:
         Tuple of host(or None), port(or None)
    """
    host = None
    port = None

    # In theory, got a good response. Should be JSON of
    # {"m.server": "example.com:433"} if there was a port
    well_known_result: str | None = response.get("m.server", None)
    if well_known_result:
        # I tried to find a library or module that would comprehensively handle
        # parsing a URL without a scheme, yarl came close. I guess we'll just
        # have to cover the basics by hand.
        host, port = check_and_maybe_split_server_name(well_known_result)

    return host, port
