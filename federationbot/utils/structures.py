"""
Data structure manipulation utilities.

Helper functions for working with Python data structures:
- Dictionary operations (key/value length calculations)
- Deep copying with JSON serialization
"""

from __future__ import annotations

import json
from typing import Any

json_decoder = json.JSONDecoder()


def extract_max_key_len_from_dict(data: dict[str, Any]) -> int:
    """
    Find the maximum length of any key in a dictionary.

    Returns:
        Length of the longest key string
    """
    return max(len(key) for key in data) if data else 0


def extract_max_value_len_from_dict(data: dict[str, Any]) -> int:
    """
    Find the maximum length of any value when converted to string in a dictionary.

    Returns:
        Length of the longest value when converted to string
    """
    return max(len(str(value)) for value in data.values()) if data else 0


def full_dict_copy(data_to_copy: dict[str, Any]) -> dict[str, Any]:
    """
    Make a deep copy of a dictionary to avoid mutating any sub-keys.

    Returns:
        New dictionary with all nested structures copied
    """
    return json_decoder.decode(json.dumps(data_to_copy))
