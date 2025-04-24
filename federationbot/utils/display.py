"""
Display formatting utilities for text-based interfaces.

Provides classes and functions for formatting tabular data with configurable:
- Column justification
- Headers and delimiters
- Horizontal and vertical layouts
- Padding and alignment
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar
from enum import Enum

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mautrix.types import EventID


class Justify(Enum):
    """
    Define text justification options for display formatting.

    Attributes:
        RIGHT: Justify text to the right (pad at front)
        LEFT: Justify text to the left (pad at end)
    """

    RIGHT = "right"  # or front padded
    LEFT = "left"  # or just padded


class DisplayLineColumnConfig:
    """
    Configure and format a column for tabular display.

    Handles column-level formatting including header text, width calculations,
    justification, and separator characters.

    Attributes:
        header_name: Text to display as column header
        line_size: Current width of the column in characters
        justification: Direction to justify content (Justify.LEFT or Justify.RIGHT)
        horizontal_separator: String used between columns
    """

    header_name: str
    line_size: int
    justification: Justify
    horizontal_separator: str

    def __init__(
        self,
        header_name: str,
        initial_size: int | None = None,
        justify: Justify = Justify.LEFT,
        horizontal_separator: str = ": ",
    ) -> None:
        """
        Initialize a new column configuration.

        Args:
            header_name: Text to use as column header
            initial_size: Starting column width (defaults to header length if None)
            justify: Text justification direction
            horizontal_separator: String to use between columns
        """
        self.header_name = header_name
        self.line_size = initial_size or len(header_name)
        self.justification = justify
        self.horizontal_separator = horizontal_separator

    def maybe_update_column_width(self, new_value: int | str | None) -> None:
        """
        Update column width if new value requires more space.

        Takes either a string (uses its length) or an integer (direct width)
        and updates the column width if the new value is larger.

        Args:
            new_value: String content or explicit width to check
        """
        if new_value:
            if isinstance(new_value, str):
                self.line_size = max(self.line_size, len(new_value))
            else:
                self.line_size = max(self.line_size, new_value)

    @property
    def size(self) -> int:
        """
        Get the current column width.

        Returns:
            Current width of the column in characters
        """
        return self.line_size

    def pad(self, data_piece: str | int | None = None, additional_padding: int = 0) -> str:
        """
        Pad content to match column width with optional extra padding.

        Args:
            data_piece: Content to pad (uses header if None)
            additional_padding: Extra padding beyond column width

        Returns:
            Padded string formatted according to justification
        """
        if isinstance(data_piece, int):
            data_piece = str(data_piece)
        what_to_display = data_piece if data_piece is not None else self.header_name
        if self.justification == Justify.RIGHT:
            return f"{pad(what_to_display, self.size + additional_padding, front=True)}"

        return f"{pad(what_to_display, self.size + additional_padding)}"

    def front_pad(self, data_piece: str | None = None) -> str:
        """
        Pad content from the front to match column width.

        Args:
            data_piece: Content to pad (uses header if None)

        Returns:
            String padded from the front to match column width
        """
        content = data_piece if data_piece is not None else self.header_name
        return f"{pad(content, self.size, front=True)}"

    def render_pretty_line(self, header: str, item_to_render: Any, force: bool = False) -> str:
        """
        Render a single line with header and item.

        Args:
            header: Header text for the line
            item_to_render: Content to display after header
            force: If True, render even if item_to_render is None

        Returns:
            Formatted string with header, separator, and content
        """
        if item_to_render is not None or force:
            return f"{self.front_pad(header)}{self.horizontal_separator}{item_to_render or ''}\n"
        return ""

    def render_pretty_list(
        self,
        header: str | None,
        list_to_render: Sequence[str | int | EventID],
    ) -> str:
        """
        Render a list of items with a header.

        Args:
            header: Header text to display (only on first line)
            list_to_render: Sequence of items to display

        Returns:
            Multi-line string with header and items
        """
        if not list_to_render:
            return ""

        lines = []
        padded_header = self.front_pad(header)
        separator = self.horizontal_separator
        separator_spaces = " " * len(separator)

        for i, item in enumerate(list_to_render):
            h = padded_header if i == 0 else self.front_pad("")
            sep = separator if i == 0 else separator_spaces
            lines.append(f"{h}{sep}{item}")

        return "\n".join(lines) + "\n"


def pad(
    orig_string: str,
    pad_to: int,
    pad_with: str = " ",
    front: bool = False,
    trim_backend: bool = False,
) -> str:
    """
    Pad a string to a specified length with a given character.

    Args:
        orig_string: String to pad
        pad_to: Target length
        pad_with: Character to use for padding
        front: If True, add padding at start of string
        trim_backend: If True, trim end of string

    Returns:
        Padded string of specified length
    """
    length_of_orig = len(orig_string)
    if length_of_orig >= pad_to:
        return orig_string if not trim_backend else orig_string[:pad_to]

    padding = pad_with * (pad_to - length_of_orig)
    return padding + orig_string if front else orig_string + padding


class DataSet:
    """
    Hold and format a collection of data with optional headers and delimiters.

    Manages a set of data lines that can be rendered either horizontally or vertically,
    with configurable headers, delimiters, and formatting.

    Attributes:
        dc: Display configuration for the dataset
        vert_delimiter: Character used for vertical delimiter lines
        horz_delimiter: String used for horizontal delimiters
        separator: Character used between items
        data_tuple_collection: List of data pairs with headers
        preliminary_data: Raw data lines before formatting
        final_data: Formatted data lines ready for display
    """

    dc: DisplayLineColumnConfig
    vert_delimiter: str = "-"
    horz_delimiter: str = ": "
    separator: str = " "
    data_tuple_collection: ClassVar[list[tuple[str, list[str]]]] = []
    preliminary_data: list[str]
    final_data: list[str]

    def __init__(
        self,
        header_name: str | None,
        vert_delimiter: str | None,
        horz_delimiter: str | None,
        separator: str | None,
        *,
        justification: Justify | None,
        preliminary_data: list[str] | None,
    ) -> None:
        """
        Initialize a new dataset with formatting options.

        Args:
            header_name: Text to use as header (empty string if None)
            vert_delimiter: Character for vertical lines (default '-')
            horz_delimiter: String for horizontal separators (default ': ')
            separator: Character between items (default ' ')
            justification: Text alignment direction
            preliminary_data: Initial data lines to format
        """
        dc = DisplayLineColumnConfig(header_name or "")
        self.dc = dc
        if vert_delimiter:
            self.vert_delimiter = vert_delimiter
        if horz_delimiter:
            self.horz_delimiter = horz_delimiter
        if separator:
            self.separator = separator
        if justification:
            self.update_justification(justification)
        if preliminary_data:
            self.preliminary_data = preliminary_data
            self._recalculate_column_width()

    def construct_delimiter_line(self) -> str:
        """
        Construct a delimiter line based on column width.

        Returns:
            String of delimiter characters matching column width
        """
        return f"{pad('', pad_to=self.dc.size, pad_with=self.vert_delimiter)}"

    def _recalculate_column_width(self) -> None:
        for line in self.preliminary_data:
            self.dc.maybe_update_column_width(len(line))

    def update_justification(self, justify: Justify) -> None:
        """
        Update justification and rerender content.

        Args:
            justify: New justification direction to apply
        """
        self.dc.justification = justify
        self._prerender()

    def _prerender(self) -> None:
        self._recalculate_column_width()
        self.final_data = []
        for line in self.preliminary_data:
            self.final_data.extend([f"{self.dc.pad(line)}"])

    def load_fresh_data(self, new_data: list[str]) -> None:
        """
        Load new data and recalculate column width.

        Args:
            new_data: New data lines to format
        """
        self.preliminary_data = new_data
        self._recalculate_column_width()

    def get_unrendered_data_list(self) -> list[str]:
        """
        Get raw data without rendering.

        Returns:
            List of unformatted data strings
        """
        return self.preliminary_data

    def get_rendered_data_list(self) -> list[str]:
        """
        Get rendered data lines without headers or delimiters.

        Returns:
            List of formatted data strings
        """
        self._prerender()
        return self.final_data

    def get_data_as_line(self) -> str:
        """
        Get all data as a single line with separators.

        Returns:
            Single string with all data joined by separators
        """
        return self.separator.join(self.preliminary_data) + self.separator

    def render_horizontal(self) -> str:
        """
        Render data horizontally (constructed vertically).

        Returns:
            Multi-line string with header, delimiter, and data lines
        """
        if not self.preliminary_data:
            return ""

        lines = [
            f"{self.dc.pad()}{self.separator}",
            self.construct_delimiter_line(),
            *[f"{line}\n" for line in self.get_rendered_data_list()],
        ]
        return "\n".join(lines)

    def render_vertical(self) -> str:
        """
        Render data vertically (constructed horizontally).

        Returns:
            Multi-line string with data arranged in vertical columns
        """
        if not self.preliminary_data:
            return ""

        self.update_justification(Justify.LEFT)
        lines = []
        header_data = f"{self.dc.front_pad()}{self.horz_delimiter}"
        header_spaces = " " * len(header_data)

        for i, line in enumerate(self.get_rendered_data_list()):
            prefix = header_data if i == 0 else header_spaces
            lines.append(f"{prefix}{line}")

        return "\n".join(lines)
