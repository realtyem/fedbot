"""
Time and date utilities.

Functions for handling time-related operations:
- Converting timestamps to human-readable formats
- Matrix-specific time formatting
"""

from __future__ import annotations

from datetime import datetime


def pretty_print_timestamp(timestamp: int) -> str:
    """
    Convert millisecond timestamp to human readable datetime string.

    Args:
        timestamp: Unix timestamp in milliseconds

    Returns:
        Human readable datetime string
    """
    return str(datetime.fromtimestamp(float(timestamp / 1000)))  # noqa: DTZ006
