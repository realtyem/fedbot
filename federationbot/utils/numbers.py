"""
Number manipulation utilities.

Provides consistent rounding and truncation functions with decimal place support:
- Round up/down with configurable precision
- Half-up rounding (standard rounding)
- Decimal truncation
"""

from __future__ import annotations

import math


def is_int(maybe_int: str) -> int | None:
    """
    Check if a string is a valid integer.

    Returns:
        Integer if valid, None otherwise
    """
    try:
        result = int(maybe_int)
    except ValueError:
        return None

    return result


def round_up(n: float, decimals: int = 0) -> float:
    """
    Round a number up to specified decimal places.

    Returns:
        Number rounded up to specified decimal places
    """
    multiplier = 10**decimals
    return math.ceil(n * multiplier) / multiplier


def round_down(n: float, decimals: int = 0) -> float:
    """
    Round a number down to specified decimal places.

    Returns:
        Number rounded down to specified decimal places
    """
    multiplier = 10**decimals
    return math.floor(n * multiplier) / multiplier


def round_half_up(n: float, decimals: int = 0) -> float:
    """
    Round a number to nearest value, with halves rounded up.

    Returns:
        Number rounded to nearest value (halves rounded up)
    """
    multiplier = 10**decimals
    return math.floor(n * multiplier + 0.5) / multiplier


def truncate(n: float, decimals: int = 0) -> float:
    """
    Truncate a float to specified decimal places.

    Returns:
        Number truncated to specified decimal places
    """
    multiplier = 10**decimals
    return int(n * multiplier) / multiplier
