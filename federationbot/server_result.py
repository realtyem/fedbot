"""
Matrix server discovery and connection state tracking.

Provides classes for tracking server discovery results and maintaining connection
state during Matrix federation attempts. Used by FederationApi to handle server
delegation, connection preferences, and diagnostic information.

Key classes:
- ServerResult: Connection details and state for discovered servers
- DiagnosticInfo: Discovery process diagnostics and status tracking
- ResponseStatusType: Status enums for discovery steps
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from dataclasses import dataclass, field
from enum import Enum

from federationbot.errors import ServerUnreachable

if TYPE_CHECKING:
    from types import SimpleNamespace


class ResponseStatusType(Enum):
    """Status types for server discovery and connection attempts."""

    OK = "OK"
    ERROR = "Error"
    NONE = "None"


@dataclass(slots=True)
class DiagnosticInfo:
    """
    Aggregates diagnostic information during server discovery process.

    Tracks status of well-known, SRV, DNS and connection tests while collecting
    diagnostic messages for debugging federation issues.
    """

    diagnostics_enabled: bool
    list_of_results: list[str] = field(default_factory=list, init=True)
    well_known_test_status: ResponseStatusType = field(default=ResponseStatusType.NONE, init=False)
    srv_test_status: ResponseStatusType = field(default=ResponseStatusType.NONE, init=False)
    srv_test_result: bool = field(default=False, init=False)
    dns_test_status: ResponseStatusType = field(default=ResponseStatusType.NONE, init=False)
    dns_test_result: bool = field(default=False, init=False)
    connection_test_status: ResponseStatusType = field(default=ResponseStatusType.NONE, init=False)
    tls_handled_by: str | None = field(default=None, init=False)
    retries: int = field(default=0, init=False)
    trace_ctx: SimpleNamespace | None = field(default=None, init=False)

    def error(self, comment: str, front_pad: str = "   ") -> None:
        """Add an error message to diagnostic results."""
        self.list_of_results.extend([f"{front_pad}{comment}"])

    def add(self, comment: str, front_pad: str = "   ") -> None:
        """Add a diagnostic message if diagnostics are enabled."""
        if self.diagnostics_enabled:
            self.list_of_results.extend([f"{front_pad}{comment}"])

    def mark_step_num(self, step_num: str, comment: str = "", front_pad: str = "") -> None:
        """Record the start of a new discovery step."""
        self.add(f"Checking {step_num}: {comment}", front_pad=front_pad)

    def mark_well_known_maybe_found(self) -> None:
        """Record successful well-known lookup."""
        self.add("Well-Known appears valid")
        self.well_known_test_status = ResponseStatusType.OK

    def mark_no_well_known(self) -> None:
        """Record absence of well-known record."""
        self.add("No Well-Known found")
        self.well_known_test_status = ResponseStatusType.NONE

    def mark_error_on_well_known(self) -> None:
        """Record error during well-known lookup."""
        self.add("Well-Known error")
        self.well_known_test_status = ResponseStatusType.ERROR

    def mark_dns_record_found(self) -> None:
        """Record successful DNS record lookup."""
        self.dns_test_result = True
        self.dns_test_status = ResponseStatusType.OK

    def mark_srv_record_found(self) -> None:
        """Record successful SRV record lookup."""
        self.srv_test_status = ResponseStatusType.OK
        self.srv_test_result = True

    def get_well_known_status(self) -> str:
        """
        Get string representation of well-known lookup status.

        Returns:
            Value of the well_known_test_status enum.
        """
        return self.well_known_test_status.value

    def get_srv_record_status(self) -> str:
        """
        Get string representation of SRV record lookup status.

        Returns:
            Value of the srv_test_status enum.
        """
        return self.srv_test_status.value

    def get_dns_record_status(self) -> str:
        """
        Get string representation of DNS record lookup status.

        Returns:
            Value of the dns_test_status enum.
        """
        return self.dns_test_status.value

    def get_connectivity_test_status(self) -> str:
        """
        Get string representation of connection test status.

        Returns:
            Value of the connection_test_status enum.
        """
        return self.connection_test_status.value


@dataclass(slots=True)
class ServerResult:
    """
    Holds connection details and state for a discovered Matrix server.

    Maintains list of available IP addresses, ports, and connection preferences,
    along with diagnostic information from the discovery process.
    """

    host: str = field(default_factory=str)
    well_known_host: str | None = field(default=None)
    port: str = field(default_factory=str)
    host_header: str = field(default_factory=str)
    sni_server_name: str = field(default_factory=str)
    errors: list[str] = field(default_factory=list)
    diag_info: DiagnosticInfo = field(
        default_factory=lambda: DiagnosticInfo(diagnostics_enabled=False),
    )
    list_of_ip4_port_tuples: list[tuple[str, str]] = field(default_factory=list)
    list_of_ip6_port_tuples: list[tuple[str, str]] = field(default_factory=list)
    unhealthy: str | None = None
    retry_time_s: float = 0
    use_sni: bool = True
    chosen_ip_port_tuple: tuple[str, str] | None = None

    def get_ip_port_or_hostname(self) -> tuple[str, str]:
        """
        Get preferred IP/port tuple for connecting to server.

        Tries hostname first (for DNS caching), then falls back to IPv4/IPv6 addresses.

        Returns:
            Tuple of (host:str, port:str) for connecting to server.

        Raises:
            ServerUnreachable: If no valid connection details are available.
        """
        if self.port and (self.well_known_host or self.host):
            return (self.well_known_host or self.host, self.port)

        if self.list_of_ip4_port_tuples:
            return self.list_of_ip4_port_tuples[0]

        if self.list_of_ip6_port_tuples:
            return self.list_of_ip6_port_tuples[0]

        msg = f"{self.host} had no ip_port tuples at critical stage"
        raise ServerUnreachable(msg)
