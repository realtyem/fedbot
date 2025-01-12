from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from types import SimpleNamespace

from federationbot.server_result import DiagnosticInfo

# The spec recommends caching responses for a while, to avoid excess traffic
# For good results, keep for 24 hours
GOOD_RESULT_TIMEOUT_MS = 24 * 60 * 60 * 1000
# For bad results, only keep for 5 minutes
BAD_RESULT_TIMEOUT_MS = 5 * 60 * 1000


@dataclass(kw_only=True)
class MatrixResponse:
    """
    The absolute base class for everything returned by requests, including exception information

    Attributes:
        http_code: Integer based status coding, 0 for system/connection level error, http values otherwise
        reason: String message, either OK or a more verbose error
        json_response: Any JSON received converted into Dict[str, Any]. Will be {} if nothing was received
        diag_info: This is usually only included when using the delegation command
        errcode: If there was an error, the 'errcode' from json
        error: If there was an error, the 'error' from json
    """

    http_code: int = 0
    reason: str = ""
    json_response: Dict[str, Any] = field(default_factory=dict)
    diag_info: Optional[DiagnosticInfo] = None
    errcode: Optional[str] = None
    error: Optional[str] = None
    tracing_context: Optional[SimpleNamespace] = None


@dataclass
class MatrixError(MatrixResponse, Exception):
    """
    Generic Matrix-related error
    """


@dataclass
class MatrixFederationResponse(MatrixResponse):
    """
    Attributes:
        http_code:
        reason:
    """


@dataclass
class MakeJoinResponse(MatrixResponse):
    """
    Parsed data from a make_join request
    """

    room_version: int = field(default=0, init=False)
    prev_events: List[str] = field(default_factory=list, init=False)
    auth_events: List[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        # room version actually comes in as a string
        # TODO: find out why it's not an integer
        self.room_version = int(self.json_response.get("room_version", 1))
        self.prev_events = self.json_response.get("event", {}).get("prev_events", [])
        self.auth_events = self.json_response.get("event", {}).get("auth_events", [])
