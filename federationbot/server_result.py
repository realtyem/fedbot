from typing import List, Optional
from dataclasses import dataclass
from enum import Enum


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

    def append_from(self, other_info: List[str]):
        self.list_of_results.extend(other_info)

    def mark_step_num(
        self, step_num: str, comment: str = "", front_pad: str = ""
    ) -> None:
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
    srv_host: Optional[str]
    port: str
    host_header: str
    sni_server_name: str
    errors: List[str]
    error_reason: Optional[str]
    diag_info: DiagnosticInfo
    unhealthy: Optional[str] = None
    retry_time_s: float = 0
    use_sni: bool = True

    def __init__(
        self,
        port: str,
        host_header: Optional[str] = None,
        sni_server_name: Optional[str] = None,
        host: Optional[str] = None,
        well_known_host: Optional[str] = None,
        srv_host: Optional[str] = None,
        diag_info: DiagnosticInfo = DiagnosticInfo(False),
    ) -> None:
        self.host = host if host else ""
        self.port = port
        self.host_header = host_header if host_header else ""
        self.sni_server_name = sni_server_name if sni_server_name else ""
        self.well_known_host = well_known_host
        self.srv_host = srv_host
        self.errors = []
        self.error_reason = None
        self.diag_info = diag_info

    def is_well_known(self) -> bool:
        return self.well_known_host is not None

    def is_srv(self) -> bool:
        return self.srv_host is not None

    def to_line(self) -> str:
        if self.srv_host:
            host = self.srv_host

        elif self.well_known_host:
            host = self.well_known_host

        else:
            host = self.host

        return f"{host}:{self.port}"

    def to_summary_line(self, include_original_host: bool = True) -> str:
        buffer = ""
        if self.is_srv():
            buffer = f"{self.srv_host}"

        if self.is_well_known():
            buffer = f"{self.well_known_host}" f"{' -> '+buffer if buffer else ''}"

        if self.host and include_original_host:
            buffer = f"{self.host}{'('+buffer+')' if buffer else ''}"

        return f"{buffer}{':'+self.port}"

    def get_host(self) -> str:
        if self.srv_host:
            return self.srv_host
        if self.well_known_host:
            return self.well_known_host
        return self.host


@dataclass
class ServerResultError(ServerResult):
    """
    The error form of a ServerResult. The important detail is the error_reason
    """

    def __init__(
        self,
        port: Optional[str] = None,
        host_header: Optional[str] = None,
        host: Optional[str] = None,
        well_known_host: Optional[str] = None,
        srv_host: Optional[str] = None,
        error_reason: Optional[str] = None,
        diag_info: DiagnosticInfo = DiagnosticInfo(False),
    ) -> None:
        super().__init__(
            host_header=host_header,
            host=host,
            port=port if port else "",
            well_known_host=well_known_host,
            srv_host=srv_host,
            diag_info=diag_info,
        )
        self.error_reason = error_reason
