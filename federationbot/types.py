from typing import Any, Dict, NewType
from enum import Enum, auto

from unpaddedbase64 import decode_base64

ServerName = NewType("ServerName", str)
KeyID = NewType("KeyID", str)


class Signature:
    signature: str
    decoded_signature: bytes

    def __init__(self, signature: str) -> None:
        self.signature = signature
        self.decoded_signature = decode_base64(self.signature)


class SignatureContainer:
    keyid: Dict[KeyID, Signature]

    def __init__(self, container_data: Dict[str, str]) -> None:
        self.keyid = dict()
        for key_id, signature in container_data.items():
            self.keyid[KeyID(key_id)] = Signature(signature)


class Signatures:
    servers: Dict[ServerName, SignatureContainer]

    def __init__(self, data: Dict[str, Any]) -> None:
        self.servers = dict()
        for server, container in data.items():
            self.servers[ServerName(server)] = SignatureContainer(container)


class SignatureVerifyResult(Enum):
    SUCCESS = auto()
    FAIL = auto()
    UNKNOWN = auto()
