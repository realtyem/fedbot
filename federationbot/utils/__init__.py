"""
Utility functions and classes for the Federation Bot.

This package provides a collection of utilities for formatting, display, and data manipulation:
- Text and HTML formatting for Matrix messages
- Progress bar implementations (text and bitmap-based)
- Display utilities for formatting tabular data
- Matrix-specific helper functions
- Number manipulation and rounding utilities
- Data structure helpers
- Time formatting utilities

All commonly used functions and classes are imported and exposed at the package level
to maintain backward compatibility with the original utils.py module.
"""

from .bitmap_progress import BitmapProgressBar, BitmapProgressBarStyle, ProgressBar
from .colors import Colors
from .display import (
    DataSet,
    DisplayLineColumnConfig,
    Justify,
    pad,
)
from .formatting import (
    add_color,
    bold,
    br,
    combine_lines_to_fit_event,
    combine_lines_to_fit_event_html,
    wrap_in_code_tags,
    wrap_in_details,
)
from .matrix import get_domain_from_id
from .numbers import round_down, round_half_up, round_up, truncate
from .structures import (
    extract_max_key_len_from_dict,
    extract_max_value_len_from_dict,
    full_dict_copy,
)
from .time import pretty_print_timestamp

__all__ = [
    "BitmapProgressBar",
    "BitmapProgressBarStyle",
    "Colors",
    "DataSet",
    "DisplayLineColumnConfig",
    "Justify",
    "ProgressBar",
    "add_color",
    "bold",
    "br",
    "combine_lines_to_fit_event",
    "combine_lines_to_fit_event_html",
    "extract_max_key_len_from_dict",
    "extract_max_value_len_from_dict",
    "full_dict_copy",
    "get_domain_from_id",
    "pad",
    "pretty_print_timestamp",
    "round_down",
    "round_half_up",
    "round_up",
    "truncate",
    "wrap_in_code_tags",
    "wrap_in_details",
]
