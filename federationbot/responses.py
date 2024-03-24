from typing import Any, Dict, List, Optional
from dataclasses import dataclass
import time

from aiohttp import RequestInfo
from multidict import CIMultiDictProxy
from unpaddedbase64 import decode_base64

from federationbot.server_result import ServerResult
from federationbot.types import KeyID, Signatures
from federationbot.utils import full_dict_copy

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


class ServerKey:
    """
    Object to store the Server Signing Keys in both the unpadded base64 form as they are
    sent as well as the already decoded into bytes form.
    """

    encoded_key: str
    decoded_key: bytes

    def __init__(self, key: str) -> None:
        self.encoded_key = key
        self.decoded_key = decode_base64(self.encoded_key)


class KeyContainer:
    """
    A unifying class to hold the expiration time and the server signing key. This is
    used for both 'verify_keys' and 'old_verify_keys'
    """

    key: ServerKey
    valid_until_ts: int

    def __init__(self, key_data: Dict[str, str], valid_until_ts: Optional[int]) -> None:
        # There may be times when we need an empty container(most likely from a
        # subclass. Allow this to be empty, but assume it will be filled in manually
        # later
        key = key_data.get("key", "")
        self.key = ServerKey(key)
        if valid_until_ts is not None:
            self.valid_until_ts = valid_until_ts
        else:
            # old_verify_keys handling below

            # This should never be None, but *someone* has there's set to 0, which is
            # obviously wrong. Since this is an edge case, it will be ignored if
            # there are no other options, but may be overridden when merging notary
            # server data. The spec also says to ignore values like this when verifying
            # signatures.
            expired_ts = key_data.get("expired_ts", 0)
            self.valid_until_ts = int(expired_ts)


class ServerVerifyKeys:
    """
    Encapsulates server keys for verifying Events and other responses. All attributes
    mirror the server response for server keys

    Attributes:
        verify_keys:
        valid_until_ts:
    """

    verify_keys: Dict[KeyID, KeyContainer]
    _raw_data: Dict[str, Any]

    def __init__(self, data: Dict[str, Any]) -> None:
        self.verify_keys = {}
        valid_until_ts = data.get("valid_until_ts", None)
        # verify_keys will be the Dict of:
        # "key_id":
        #       {
        #           "key": <base64 encoded key hash string>
        #       }
        verify_keys = data.get("verify_keys", {})
        for key_id, key_data in verify_keys.items():
            self.verify_keys[KeyID(key_id)] = KeyContainer(key_data, valid_until_ts)

        # old_verify_keys will be the Dict of:
        # "key_id":
        #       {
        #           "key": <base64 encoded key hash string>,
        #           "expired_ts": <int of timestamp in ms UTC>
        #       }
        old_verify_keys = data.get("old_verify_keys", {})
        for key_id, key_data in old_verify_keys.items():
            # Pass a None for the valid_until_ts as KeyContainer is set up to handle the
            # old_verify_keys data directly
            self.verify_keys[KeyID(key_id)] = KeyContainer(key_data, None)

        self._raw_data = full_dict_copy(data)

    def update_key_data_from_dict(self, data: Dict[str, Any]) -> None:
        """
        Update an existing entry with new(ish) data.
        Args:
            data: The response from the server keys request

        Returns: None

        """
        verify_keys = data.get("verify_keys", {})
        old_verify_keys = data.get("old_verify_keys", {})
        valid_until_ts = data.get("valid_until_ts", 0)

        for key_id, key_data in verify_keys.items():
            if key_id in self.verify_keys:
                # It is extremely unlikely that a notary response will have a
                # primary 'key set' with an older timestamp than we already have.
                # But, if it should be newer update what's here.
                if valid_until_ts > self.verify_keys[key_id].valid_until_ts:
                    self.verify_keys[key_id].valid_until_ts = valid_until_ts
            else:
                # First instance seen of this key at this point, add it
                self.verify_keys[key_id] = KeyContainer(key_data, valid_until_ts)

        for o_key_id, o_key_data in old_verify_keys.items():
            if o_key_id in self.verify_keys:
                expired_ts = o_key_data.get("expired_ts", 0)
                if self.verify_keys[o_key_id].valid_until_ts <= expired_ts:
                    # Update the data if the timestamp is newer
                    self.verify_keys[o_key_id].valid_until_ts = expired_ts
            else:
                # The KeyContainer class can deal with the attached 'expired_ts'
                # directly
                self.verify_keys[o_key_id] = KeyContainer(o_key_data, None)

    def update_key_data_from_list(
        self, data_from_notary_response: Dict[str, List[Dict[str, Any]]]
    ) -> None:
        """
        Suitable for passing a notary response into directly. Make sure the
        "server_keys" part of the dict/json is passed in or this will do nothing.

        Args:
            data_from_notary_response: what it says

        Returns: None

        """
        server_keys = data_from_notary_response.get("server_keys", [])
        for entry in server_keys:
            self.update_key_data_from_dict(entry)


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
