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

from federationbot.events import EventBase
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
    headers: CIMultiDictProxy[str] = field(default=None)
    reason: str = field(default="")
    json_response: dict[str, Any] = field(default_factory=dict)
    diag_info: DiagnosticInfo | None = field(default=None)
    server_result: ServerDiscoveryBaseResult | None = field(default=None)
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    time_taken: float = field(default=0.0)
    errcode: str | None = field(default=None)
    error: str | None = field(default=None)
    tracing_context: SimpleNamespace | None = field(default=None)


@dataclass(kw_only=True)
class MatrixError(MatrixResponse, Exception):
    """
    Exception class for Matrix federation errors.

    Combines MatrixResponse fields with Python's Exception class to allow
    raising federation errors while preserving response data.
    """

    def __post_init__(self) -> None:
        self.errcode = self.json_response.get("errcode")
        self.error = self.json_response.get("error")


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


@dataclass(slots=True, kw_only=True)
class MakeJoinResponse(MatrixFederationResponse):
    """
    Specialized response for the make_join federation endpoint.

    Parses and validates the response from a make_join request, which provides
    the data needed to join a room through federation.

    Note: Room version is received as a string but stored as an integer.

    Attributes:
        room_version: Version of the room being joined
        prev_events: List of parent event IDs in the room's DAG
        auth_events: List of event IDs needed to authenticate this join
    """

    room_version: str = field(init=False)
    prev_events: list[str] = field(init=False)
    auth_events: list[str] = field(init=False)

    def __post_init__(self) -> None:
        """
        Initialize derived fields from JSON response data.

        Extracts and converts room version, previous events, and auth events
        from the make_join response JSON. Sets defaults if fields are missing.
        """
        self.room_version = self.json_response.get("room_version", "1")
        self.prev_events = self.json_response.get("event", {}).get("prev_events", [])
        self.auth_events = self.json_response.get("event", {}).get("auth_events", [])


@dataclass(slots=True, init=False)
class TimestampToEventResponse:
    """
    Specialized response for the `/timestamp_to_event` endpoint

    Attributes:
        event_id: The event ID closest to the time requested
        origin_server_ts: The timestamp on that event
    """

    event_id: str
    origin_server_ts: int

    def __init__(self, matrix_response: MatrixResponse) -> None:
        event_id = matrix_response.json_response.get("event_id")
        assert isinstance(event_id, str)
        self.event_id = event_id
        origin_server_ts = matrix_response.json_response.get("origin_server_ts")
        assert isinstance(origin_server_ts, int)
        self.origin_server_ts = origin_server_ts


@dataclass(slots=True, init=False)
class RoomHeadData:
    """
    Object to contain the data at the very newest end of the room
    """

    make_join_response: MakeJoinResponse
    newest_event: EventBase
    auth_event_count: int
    prev_event_count: int

    def __init__(self, make_join_response_obj: MakeJoinResponse, events_list: list[EventBase]) -> None:
        self.make_join_response = make_join_response_obj
        self.auth_event_count = len(make_join_response_obj.auth_events)
        self.prev_event_count = len(make_join_response_obj.prev_events)
        newest_timestamp = 0
        for event in events_list:
            if event.origin_server_ts > newest_timestamp:
                newest_timestamp = event.origin_server_ts
                self.newest_event = event

    def print_summary_line(self) -> str:
        # Don't include glyphs from the event being summarized, we want the glyphs for the join response
        return (
            self.newest_event.to_summary(include_glyphs=False)
            + f" | {self.auth_event_count * 'A'}:{self.prev_event_count * 'P'}"
        )
