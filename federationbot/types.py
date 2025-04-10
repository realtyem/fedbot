"""
Type definitions and containers for Matrix federation data.

Provides strongly-typed classes for handling Matrix federation data structures:
- Server names and key IDs
- Signatures and signature containers
- Server keys and key containers
"""

from __future__ import annotations

from typing import Any
from dataclasses import dataclass, field
from enum import Enum, auto

from unpaddedbase64 import decode_base64

from federationbot.primitives import KeyID, ServerName
from federationbot.utils import full_dict_copy


@dataclass(slots=True, init=False, repr=False)
class RoomAlias:
    """House and split the RoomAlias into it's components"""

    alias: str
    origin_server: str
    list_of_servers_can_join_via: list[str]

    def __init__(self, room_alias: str) -> None:
        _split_alias = room_alias.split(":", maxsplit=1)
        self.alias = _split_alias[0]
        self.origin_server = _split_alias[1]
        self.list_of_servers_can_join_via = []

    # def __str__(self) -> str:
    #     return f"{self.alias}:{self.origin_server}"

    def __repr__(self) -> str:
        return f"{self.alias}:{self.origin_server}"


@dataclass(frozen=True, slots=True)
class Signature:
    """Container for a Matrix digital signature."""

    signature: str
    decoded_signature: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Decode the signature after initialization."""
        object.__setattr__(self, "decoded_signature", decode_base64(self.signature))


@dataclass(slots=True)
class SignatureContainer:
    """Container mapping key IDs to their signatures."""

    keyid: dict[KeyID, Signature] = field(default_factory=dict, init=False)
    _raw_dict: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Create signature container from raw signature data."""
        for key_id, signature in self._raw_dict.items():
            self.keyid[KeyID(key_id)] = Signature(signature)


@dataclass(slots=True)
class Signatures:
    """Top-level container for all signatures from all servers."""

    servers: dict[ServerName, SignatureContainer] = field(default_factory=dict, init=False)
    _raw_data: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Create signatures container from raw signature data."""
        for server, container in self._raw_data.items():
            self.servers[ServerName(server)] = SignatureContainer(container)


class SignatureVerifyResult(Enum):
    """
    Result of a signature verification operation.

    Used to indicate the outcome of verifying a Matrix digital signature.

    Attributes:
        SUCCESS: Signature verified successfully
        FAIL: Signature verification failed
        UNKNOWN: Verification status could not be determined
    """

    SUCCESS = auto()
    FAIL = auto()
    UNKNOWN = auto()


@dataclass(frozen=True)
class ServerKey:
    """
    Server signing key in both encoded and decoded forms.

    Attributes:
        encoded_key: Base64 encoded key string
        decoded_key: Decoded key bytes
    """

    encoded_key: str
    decoded_key: bytes = field(init=False)

    def __post_init__(self) -> None:
        """Decode the key after initialization."""
        object.__setattr__(self, "decoded_key", decode_base64(self.encoded_key))


@dataclass(init=False)
class KeyContainer:
    """Container for a server key and its validity period."""

    key: ServerKey
    valid_until_ts: int

    def __init__(self, key_data: dict[str, str], valid_until_ts: int | None) -> None:
        """
        Create key container (with validity period) from raw key data.

        Handles both current and old verify keys. For old keys (valid_until_ts=None),
        uses expired_ts from key_data.

        Note: The expired_ts field should never be None, but some servers incorrectly set
        it to 0. This edge case is handled by accepting the 0 value, which may be
        overridden when merging notary server data. The Matrix spec indicates these
        values should be ignored during signature verification.

        Args:
            key_data: Dictionary containing key and optional expired_ts
            valid_until_ts: Timestamp when key expires, or None for old keys
        """
        self.key = ServerKey(key_data.get("key", ""))
        self.valid_until_ts = valid_until_ts if valid_until_ts is not None else int(key_data.get("expired_ts", 0))


@dataclass
class ServerVerifyKeys:
    """
    Complete set of verification keys for a server.

    The verify_keys dict in data should have format:
        "key_id": {
            "key": "<base64 encoded key hash string>"
        }

    The old_verify_keys dict should have format:
        "key_id": {
            "key": "<base64 encoded key hash string>",
            "expired_ts": <int of timestamp in ms UTC>
        }
    """

    verify_keys: dict[KeyID, KeyContainer] = field(default_factory=dict, init=False)
    _raw_data: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        valid_until_ts = self._raw_data.get("valid_until_ts")

        # Process current keys
        for key_id, key_data in self._raw_data.get("verify_keys", {}).items():
            self.verify_keys[KeyID(key_id)] = KeyContainer(key_data, valid_until_ts)

        # Process old keys
        for key_id, key_data in self._raw_data.get("old_verify_keys", {}).items():
            self.verify_keys[KeyID(key_id)] = KeyContainer(key_data, None)

    def update_key_data_from_dict(self, data: dict[str, Any]) -> None:
        """
        Update existing keys with new data from a single key set.

        Updates validity periods for existing keys and adds any new keys.
        For existing keys, takes the maximum validity timestamp between current and new.
        This handles the edge case where a notary response might have an older timestamp
        than we already have, though this is extremely unlikely.

        Args:
            data: Dictionary containing verify_keys and old_verify_keys mappings
        """
        verify_keys = data.get("verify_keys", {})
        old_verify_keys = data.get("old_verify_keys", {})
        valid_until_ts = data.get("valid_until_ts", 0)

        for key_id, key_data in verify_keys.items():
            if key_id in self.verify_keys:
                self.verify_keys[key_id].valid_until_ts = max(
                    valid_until_ts,
                    self.verify_keys[key_id].valid_until_ts,
                )
            else:
                self.verify_keys[key_id] = KeyContainer(key_data, valid_until_ts)

        for o_key_id, o_key_data in old_verify_keys.items():
            if o_key_id in self.verify_keys:
                expired_ts = o_key_data.get("expired_ts", 0)
                self.verify_keys[o_key_id].valid_until_ts = max(
                    expired_ts,
                    self.verify_keys[o_key_id].valid_until_ts,
                )
            else:
                self.verify_keys[o_key_id] = KeyContainer(o_key_data, None)

    def update_key_data_from_list(
        self,
        data_from_notary_response: dict[str, list[dict[str, Any]]],
    ) -> None:
        """
        Update keys from a notary server response containing multiple key sets.

        Processes each key set in the response and updates keys accordingly.
        Requires "server_keys" to be present in the response or will do nothing.
        Stores the full notary response in _raw_data.

        Args:
            data_from_notary_response: Dictionary with "server_keys" list of key sets
        """
        server_keys = data_from_notary_response.get("server_keys", [])
        for entry in server_keys:
            self.update_key_data_from_dict(entry)
        self._raw_data = full_dict_copy(data_from_notary_response)


    """
    """

