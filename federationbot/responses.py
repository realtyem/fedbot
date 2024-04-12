from typing import Any, Dict, List, Optional
from dataclasses import dataclass

from aiohttp import RequestInfo
from multidict import CIMultiDictProxy

from federationbot.server_result import ServerResult
from federationbot.types import ServerVerifyKeys, Signatures

# The spec recommends caching responses for a while, to avoid excess traffic
# For good results, keep for 24 hours
GOOD_RESULT_TIMEOUT_MS = 24 * 60 * 60 * 1000
# For bad results, only keep for 5 minutes
BAD_RESULT_TIMEOUT_MS = 5 * 60 * 1000


@dataclass
class FederationBaseResponse:
    server_result: ServerResult
    status_code: int
    reason: Optional[str]
    errors: List[str]
    headers: Optional[CIMultiDictProxy]
    request_info: Optional[RequestInfo]
    response_dict: Dict[str, Any]

    def __init__(
        self,
        status_code: int,
        status_reason: Optional[str],
        response_dict: Dict[str, Any],
        server_result: ServerResult,
        list_of_errors: Optional[List[str]] = None,
        headers: Optional[CIMultiDictProxy] = None,
        request_info: Optional[RequestInfo] = None,
    ) -> None:
        if list_of_errors is None:
            list_of_errors = []
        self.server_result = server_result
        self.status_code = status_code
        self.reason = status_reason
        self.response_dict = response_dict
        self.errors = list_of_errors
        self.headers = headers
        self.request_info = request_info


@dataclass
class FederationErrorResponse(FederationBaseResponse):
    def __init__(
        self,
        status_code: int,
        status_reason: Optional[str],
        response_dict: Dict[str, Any],
        server_result: ServerResult,
        list_of_errors: Optional[List[str]] = None,
        headers: Optional[CIMultiDictProxy] = None,
        request_info: Optional[RequestInfo] = None,
    ) -> None:
        super().__init__(
            status_code,
            status_reason,
            response_dict=response_dict,
            server_result=server_result,
            list_of_errors=list_of_errors,
            headers=headers,
            request_info=request_info,
        )
        if self.response_dict:
            self.reason = self.response_dict.get("error", self.reason)


class FederationVersionResponse(FederationBaseResponse):
    server_software: str
    server_version: str

    def __init__(
        self,
        status_code: int,
        status_reason: Optional[str],
        response_dict: Dict[str, Any],
        server_result: ServerResult,
        list_of_errors: Optional[List[str]] = None,
        headers: Optional[CIMultiDictProxy] = None,
    ) -> None:
        super().__init__(
            status_code,
            status_reason,
            response_dict=response_dict,
            server_result=server_result,
            list_of_errors=list_of_errors,
            headers=headers,
        )
        server_block = self.response_dict.get("server", {})
        self.server_software = server_block.get("name", "")
        self.server_version = server_block.get("version", "")

    @classmethod
    def from_response(
        cls, base_response: FederationBaseResponse
    ) -> "FederationVersionResponse":
        return cls(
            base_response.status_code,
            base_response.reason,
            response_dict=base_response.response_dict,
            server_result=base_response.server_result,
            list_of_errors=base_response.errors,
            headers=base_response.headers,
        )


class FederationServerKeyResponse(FederationBaseResponse):
    server_name: str
    signatures: Signatures
    server_verify_keys: ServerVerifyKeys

    def __init__(
        self,
        status_code: int,
        status_reason: Optional[str],
        response_dict: Dict[str, Any],
        server_result: ServerResult,
        list_of_errors: Optional[List[str]] = None,
        headers: Optional[CIMultiDictProxy] = None,
    ) -> None:
        super().__init__(
            status_code,
            status_reason,
            response_dict=response_dict,
            server_result=server_result,
            list_of_errors=list_of_errors,
            headers=headers,
        )
        self.server_name = self.response_dict.get("server_name", "")

        self.server_verify_keys = ServerVerifyKeys(self.response_dict)

        signatures = self.response_dict.get("signatures", {})
        self.signatures = Signatures(signatures)

    @classmethod
    def from_response(
        cls, base_response: FederationBaseResponse
    ) -> "FederationServerKeyResponse":
        return cls(
            base_response.status_code,
            base_response.reason,
            response_dict=base_response.response_dict,
            server_result=base_response.server_result,
            list_of_errors=base_response.errors,
            headers=base_response.headers,
        )


@dataclass
class MakeJoinResponse:
    """
    Parsed data from a make_join request
    """

    room_version: int
    prev_events: List[str]
    auth_events: List[str]
