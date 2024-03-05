from typing import Any, Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime
import time

from aiohttp import RequestInfo
from multidict import CIMultiDictProxy

from federationbot.server_result import ServerResult

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
        now = int(time.time_ns() / 1000)
        self.server_result.last_contact = now
        self.server_result.drop_after = now + GOOD_RESULT_TIMEOUT_MS
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
        self.server_result.drop_after = int(
            (time.time_ns() / 1000) + BAD_RESULT_TIMEOUT_MS
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
    old_verify_keys: Dict[str, Any]
    valid_until_ts: Optional[int]
    verify_keys: Dict[str, Any]
    signatures: Dict[str, Any]

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
        self.old_verify_keys = {}
        self.verify_keys = {}
        self.signatures = {}
        self.server_name = self.response_dict.get("server_name", "")
        self.valid_until_ts = self.response_dict.get("valid_until_ts", None)
        if self.valid_until_ts and self.valid_until_ts > 0:
            self.valid_until_pretty = str(
                datetime.fromtimestamp(float(self.valid_until_ts / 1000))
            )
        else:
            self.valid_until_pretty = "Unknown"

        old_verify_keys = self.response_dict.get("old_verify_keys", {})
        for key_id, key_data in old_verify_keys.items():
            this_key = self.old_verify_keys.setdefault(key_id, {})
            # key_data should have two dict keys inside for each old key:
            # an 'expired_ts' for when it was last used, and
            # a 'key' that holds the actual unpadded base64 key
            if key_data:
                expired_ts = key_data.get("expired_ts", 0)
                if expired_ts > 0:
                    expired_pretty = (
                        "EXPIRED: "
                        f"{str(datetime.fromtimestamp(float(expired_ts / 1000)))}"
                    )
                else:
                    expired_pretty = "EXPIRED: Unknown"
                this_key.setdefault("expired_ts", expired_ts)
                this_key.setdefault("expired_pretty", expired_pretty)
                this_key.setdefault("key", key_data.get("key", ""))

        verify_keys = self.response_dict.get("verify_keys", {})
        for key_id, key in verify_keys.items():
            # verify keys should have a key of the key_id, then another dict inside
            # containing 'key' and the actual key itself. If it's missing, the server
            # didn't have it(which shouldn't happen).
            self.verify_keys.setdefault(key_id, key)

        signatures = self.response_dict.get("signatures", {})
        for server_name, key_data in signatures.items():
            # signatures should be separated by server_name, inside of which has the
            # key_id: key as strings
            self.signatures.setdefault(server_name, key_data)

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
