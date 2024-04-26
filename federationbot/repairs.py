from typing import Any, Dict, List, Optional

from federationbot import get_domain_from_id
from federationbot.errors import EventKeyMissing
from federationbot.utils import full_dict_copy


def repair_missing_origin_from_redacted_event(jsondict_to_check: Dict[str, Any]) -> Dict[str, Any]:
    """
    Attempt a repair on an Event that has a missing 'origin' key. Just attempt the repair, validation is done elsewhere.

    Historically, this seemed to happen on Conduit version from 0.7.0-alpha and Conduwuit prior to 0.1.15.

    The problem stemmed from the reference/content hashes and signatures being calculated *with* the key, then it
    being removed after redaction which caused failures when authenticating the Event.

    Args:
        jsondict_to_check: The dictionary form of an Event

    Returns: Tuple of (optional string error message, optional dictionary form of an Event including the repair if it
        was done)

    Raises: EventKeyMissing if the 'sender' key used to restore 'origin' is missing
    """
    sender_key: Optional[str] = jsondict_to_check.get("sender", None)
    if sender_key is None:
        raise EventKeyMissing("'sender' key was missing from Event")

    host = get_domain_from_id(sender_key)
    full_copy = full_dict_copy(jsondict_to_check)
    full_copy.update({"origin": host})
    return full_copy


def remove_incorrectly_included_key_by_list(
    jsondict_to_check: Dict[str, Any], keys_to_remove: List[str]
) -> Dict[str, Any]:
    full_copy = full_dict_copy(jsondict_to_check)
    for key_to_remove in keys_to_remove:
        full_copy.pop(key_to_remove)
    return full_copy
