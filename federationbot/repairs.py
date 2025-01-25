"""
Repair functions for handling known Matrix federation event issues.

Provides functions to fix common issues with Matrix events, particularly focusing on
historical bugs in various homeserver implementations. These repairs are applied
before event validation to ensure compatibility while maintaining security.

The main issues handled are missing fields in redacted events and incorrectly
included fields that should have been redacted.
"""

from __future__ import annotations

from typing import Any

from federationbot import get_domain_from_id
from federationbot.errors import EventKeyMissing
from federationbot.utils import full_dict_copy


def repair_missing_origin_from_redacted_event(jsondict_to_check: dict[str, Any]) -> dict[str, Any]:
    """
    Restore missing 'origin' key in redacted events.

    Fixes a historical bug in Conduit (0.7.0-alpha) and Conduwuit (pre-0.1.15) where
    the 'origin' field was incorrectly removed during redaction, breaking event
    authentication since hashes were calculated with the field present.

    Args:
        jsondict_to_check: Event dictionary to repair

    Returns:
        Copy of input dict with origin field restored

    Raises:
        EventKeyMissing: If sender field is missing
    """
    if not (sender_key := jsondict_to_check.get("sender")):
        msg = "'sender' key was missing from Event"
        raise EventKeyMissing(msg)

    host = get_domain_from_id(sender_key)
    full_copy = full_dict_copy(jsondict_to_check)
    full_copy["origin"] = host
    return full_copy


def remove_incorrectly_included_key_by_list(
    jsondict_to_check: dict[str, Any],
    keys_to_remove: list[str],
) -> dict[str, Any]:
    """
    Remove keys that should not be present in an event.

    Args:
        jsondict_to_check: Event dictionary to clean
        keys_to_remove: Keys to remove from the dictionary

    Returns:
        Copy of input dict with specified keys removed
    """
    full_copy = full_dict_copy(jsondict_to_check)
    for key in keys_to_remove:
        full_copy.pop(key, None)
    return full_copy
