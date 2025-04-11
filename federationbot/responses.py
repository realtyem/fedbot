"""
Response types for Matrix federation API requests.

Provides strongly-typed response classes for handling Matrix federation API responses,
including error handling and diagnostic information. Response objects include caching
hints based on success/failure to help reduce federation traffic while maintaining
reasonable retry periods.

The response classes are used by FederationApi to provide type-safe access to HTTP
responses, JSON data, error codes and diagnostic information from Matrix homeservers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from dataclasses import dataclass, field

from multidict import CIMultiDictProxy

from federationbot.resolver import Diagnostics, ServerDiscoveryBaseResult

if TYPE_CHECKING:
    from types import SimpleNamespace

    from federationbot.server_result import DiagnosticInfo

# Matrix spec recommends caching to avoid excess federation traffic
GOOD_RESULT_TIMEOUT_MS = 24 * 60 * 60 * 1000  # 24 hours for successful responses
BAD_RESULT_TIMEOUT_MS = 5 * 60 * 1000  # 5 minutes for failed responses


@dataclass(kw_only=True)
class MatrixResponse:
    """
    Base class for all Matrix federation API responses.

    Provides common fields for both successful responses and errors. All response types
    inherit from this class to ensure consistent error handling and diagnostic data.

    Attributes:
        http_code: Status code (0 for system errors, otherwise HTTP status)
        reason: Status message ("OK" or error description)
        json_response: Parsed JSON response as dict
        diag_info: Optional diagnostic info from delegation
        errcode: Matrix protocol error code if error occurred
        error: Detailed error message if error occurred
        tracing_context: Request timing data for debugging
    """

    http_code: int = field(default=0)
    reason: str = field(default="")
    json_response: dict[str, Any] = field(default_factory=dict)
    diag_info: DiagnosticInfo | None = field(default=None)
    server_result: ServerDiscoveryBaseResult | None = field(default=None)
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    time_taken: float = field(default=0.0)
    errcode: str | None = field(default=None)
    error: str | None = field(default=None)
    tracing_context: SimpleNamespace | None = field(default=None)


@dataclass
class MatrixError(MatrixResponse, Exception):
    """
    Exception class for Matrix federation errors.

    Combines MatrixResponse fields with Python's Exception class to allow
    raising federation errors while preserving response data.
    """


@dataclass(kw_only=True)
class MatrixFederationResponse(MatrixResponse):
    """
    Standard response type for Matrix federation API requests.

    Used for federation endpoints that don't require specialized response parsing.
    Inherits all fields from MatrixResponse without adding additional attributes.

    Attributes:
        http_code: HTTP status code from federation response
        reason: Status message from federation response
    """

    headers: CIMultiDictProxy[str]


@dataclass(kw_only=True)
class MakeJoinResponse(MatrixFederationResponse):
    """
    Specialized response for the make_join federation endpoint.

    Parses and validates the response from a make_join request, which provides
    the data needed to join a room through federation.

    Note: Room version is received as a string but stored as an integer.

    Attributes:
        room_version: Version number of the room being joined
        prev_events: List of parent event IDs in the room's DAG
        auth_events: List of event IDs needed to authenticate this join
    """

    room_version: int = field(default=0, init=False)
    prev_events: list[str] = field(default_factory=list, init=False)
    auth_events: list[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        """
        Initialize derived fields from JSON response data.

        Extracts and converts room version, previous events, and auth events
        from the make_join response JSON. Sets defaults if fields are missing.
        """
        self.room_version = int(self.json_response.get("room_version", 1))
        self.prev_events = self.json_response.get("event", {}).get("prev_events", [])
        self.auth_events = self.json_response.get("event", {}).get("auth_events", [])
