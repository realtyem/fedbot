from typing import List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum
from types import SimpleNamespace


class ResponseStatusType(Enum):
    OK = "OK"
    ERROR = "Error"
    NONE = "None"


@dataclass
class DiagnosticInfo:
    """
    A data collection object, used to aggregate the results of delegation and store the
    error/diagnostic messages.
    """

    diagnostics_enabled: bool
    list_of_results: List[str]
    well_known_test_status: ResponseStatusType
    srv_test_status: ResponseStatusType
    srv_test_result: bool
    dns_test_status: ResponseStatusType
    dns_test_result: bool
    connection_test_status: ResponseStatusType
    tls_handled_by: Optional[str]
    retries: int = 0
    trace_ctx: Optional[SimpleNamespace] = None

    def __init__(self, enable_diagnostics: bool) -> None:
        self.diagnostics_enabled = enable_diagnostics
        self.list_of_results = []
        self.well_known_test_status = ResponseStatusType.NONE
        self.srv_test_status = ResponseStatusType.NONE
        self.srv_test_result = False
        self.dns_test_status = ResponseStatusType.NONE
        self.dns_test_result = False
        self.connection_test_status = ResponseStatusType.NONE
        self.tls_handled_by = None

    def error(self, comment: str, front_pad: str = "   ") -> None:
        self.list_of_results.extend([f"{front_pad}{comment}"])

    def add(self, comment: str, front_pad: str = "   ") -> None:
        if self.diagnostics_enabled:
            self.list_of_results.extend([f"{front_pad}{comment}"])

    def mark_step_num(self, step_num: str, comment: str = "", front_pad: str = "") -> None:
        self.add(
            f"Checking {step_num}: {comment}",
            front_pad=front_pad,
        )

    def mark_well_known_maybe_found(self) -> None:
        self.add("Well-Known appears valid")
        self.well_known_test_status = ResponseStatusType.OK

    def mark_no_well_known(self) -> None:
        self.add("No Well-Known found")
        self.well_known_test_status = ResponseStatusType.NONE

    def mark_error_on_well_known(self) -> None:
        self.add("Well-Known error")
        self.well_known_test_status = ResponseStatusType.ERROR

    def mark_dns_record_found(self) -> None:
        self.dns_test_result = True
        self.dns_test_status = ResponseStatusType.OK

    def mark_srv_record_found(self) -> None:
        self.srv_test_status = ResponseStatusType.OK
        self.srv_test_result = True

    def get_well_known_status(self) -> str:
        return self.well_known_test_status.value

    def get_srv_record_status(self) -> str:
        return self.srv_test_status.value

    def get_dns_record_status(self) -> str:
        return self.dns_test_status.value

    def get_connectivity_test_status(self) -> str:
        return self.connection_test_status.value


@dataclass
class ServerResult:
    """
    The information for how to connect to a server, either directly or through discovery

    """

    host: str
    well_known_host: Optional[str]
    host_header: str
    sni_server_name: str
    errors: List[str]
    diag_info: DiagnosticInfo
    list_of_ip4_port_tuples: List[Tuple[str, str]]
    list_of_ip6_port_tuples: List[Tuple[str, str]]
    unhealthy: Optional[str] = None
    retry_time_s: float = 0
    use_sni: bool = True
    chosen_ip_port_tuple: Optional[Tuple[str, str]] = None

    def __init__(
        self,
        list_of_ip4_port_tuples: List[Tuple[str, str]],
        list_of_ip6_port_tuples: List[Tuple[str, str]],
        host_header: Optional[str] = None,
        sni_server_name: Optional[str] = None,
        host: Optional[str] = None,
        well_known_host: Optional[str] = None,
        diag_info: DiagnosticInfo = DiagnosticInfo(False),
    ) -> None:
        self.host = host if host else ""
        self.host_header = host_header if host_header else ""
        self.sni_server_name = sni_server_name if sni_server_name else ""
        self.well_known_host = well_known_host
        self.errors = []
        self.diag_info = diag_info
        self.list_of_ip4_port_tuples = list_of_ip4_port_tuples
        self.list_of_ip6_port_tuples = list_of_ip6_port_tuples

    def get_ip_port_or_hostname(self) -> Tuple[str, str]:
        # For the moment, just choose the first tuple in the list, and remember it's a (host:str, port:str)
        if not self.chosen_ip_port_tuple and self.list_of_ip4_port_tuples:
            self.chosen_ip_port_tuple = self.list_of_ip4_port_tuples[0]
        if not self.chosen_ip_port_tuple and self.list_of_ip6_port_tuples:
            self.chosen_ip_port_tuple = self.list_of_ip6_port_tuples[0]
        # Fallback to the hostname, which forces another DNS lookup in case something went wrong
        try:
            assert self.chosen_ip_port_tuple is not None
        except AssertionError:
            print(f"{self.host} had no ip_port tuples at critical stage")
        return self.chosen_ip_port_tuple  # or self.well_known_host or self.host
