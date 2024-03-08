from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from enum import Enum
from mautrix.types import EventID


class Justify(Enum):
    RIGHT = "right"  # or front padded
    LEFT = "left"  # or just padded


class DisplayLineColumnConfig:
    """
    An object to define the name of a column to format and the size of the column width.

    This is a helper for vertical columns. Can be used to establish a vertical column
    for a horizontal display table

    Args:
        header_name: A string to use as the header_name. If none required, just use an
            empty string.
        initial_size: If the initial column width is known, it can be given. Defaults to
            size of header_name if None.
        justify:

    """

    header_name: str
    line_size: int
    justification: Justify
    horizontal_separator: str

    def __init__(
        self,
        header_name: str,
        initial_size: Optional[int] = None,
        justify: Justify = Justify.LEFT,
        horizontal_separator: str = ": ",
    ) -> None:
        self.header_name = header_name
        self.line_size = initial_size or len(header_name)
        self.justification = justify
        self.horizontal_separator = horizontal_separator

    def maybe_update_column_width(self, new_value: int) -> None:
        """
        Use to potentially update the column width. Uses max() under the hood.

        Args:
            new_value: To apply to the column width
        """
        self.line_size = max(self.line_size, new_value)

    @property
    def size(self) -> int:
        """
        Gets the line_size without accidentally setting it.
        :return:
        """
        return self.line_size

    def pad(self, data_piece: Optional[Union[str, int]] = None, additional_padding: int = 0) -> str:
        """
        Pad the data_piece string by the column size. No data_piece means use the
        header_name

        Args:
            data_piece:

        Returns: The formatted string
        """
        if isinstance(data_piece, int):
            data_piece = str(data_piece)
        what_to_display = data_piece if data_piece is not None else self.header_name
        if self.justification == Justify.RIGHT:
            return f"{pad(what_to_display, self.size + additional_padding, front=True)}"
        else:
            return f"{pad(what_to_display, self.size + additional_padding)}"

    def front_pad(self, data_piece: Optional[str] = None) -> str:
        """
        Pad the data_piece string by the column size from the front of the
        string(effectively a right justify). No data_piece means use the header_name

        Args:
            data_piece:

        Returns: The formatted string
        """

        return str(
            pad(data_piece if data_piece is not None else self.header_name, self.size, front=True)
        )

    def render_pretty_line(self, header: str, item_to_render: Any, force: bool = False) -> str:
        summary = ""
        if item_to_render is not None or force:
            summary += f"{self.front_pad(header)}"
            summary += f"{self.horizontal_separator}"
            # If the display is being forced, but there is no data just print an empty
            # string so the header will be printed anyways.
            summary += f"{item_to_render if item_to_render else ''}"
            summary += "\n"
        return summary

    def render_pretty_list(
        self, header: Optional[str], list_to_render: Sequence[Union[str, int, EventID]]
    ) -> str:
        summary = ""
        first_line = True
        for i in list_to_render:
            h = self.front_pad(header) if first_line else self.front_pad("")
            separator = (
                self.horizontal_separator
                if first_line
                else pad("", pad_to=len(self.horizontal_separator))
            )
            summary += f"{h}{separator}{i}\n"
            first_line = False
        return summary


class DataSet:
    """
    Used to hold and format a given set of data into line(s), optionally with a header
    and delimiter
    """

    dc: DisplayLineColumnConfig
    vert_delimiter: str = "-"
    horz_delimiter: str = ": "
    separator: str = " "
    data_tuple_collection: List[Tuple[str, List[str]]] = []
    preliminary_data: List[str]
    final_data: List[str]

    def __init__(
        self,
        header_name: Optional[str],
        vert_delimiter: Optional[str],
        horz_delimiter: Optional[str],
        separator: Optional[str],
        justification: Optional[Justify],
        preliminary_data: Optional[List[str]],
    ) -> None:
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
        Should only be used when doing a horizontal render.

        Returns: a string of self.vert_delimiter of column width in length
        """
        return f"{pad('', pad_to=self.dc.size, pad_with=self.vert_delimiter)}"

    def _recalculate_column_width(self) -> None:
        for line in self.preliminary_data:
            self.dc.maybe_update_column_width(len(line))

    def update_justification(self, justify: Justify) -> None:
        self.dc.justification = justify
        self._prerender()

    def _prerender(self) -> None:
        self._recalculate_column_width()
        self.final_data = []
        for line in self.preliminary_data:
            self.final_data.extend([f"{self.dc.pad(line)}"])

    def load_fresh_data(self, new_header: str, new_data: List[str]) -> None:
        new_data_tuple = (new_header, new_data)  # noqa: F841, pylint: disable=unused-variable
        self.preliminary_data = new_data
        self._recalculate_column_width()

    def get_unrendered_data_list(self) -> List[str]:
        return self.preliminary_data

    def get_rendered_data_list(self) -> List[str]:
        """
        Retrieve all the data lines(with no header, new lines or delimiter) with
        rendering done(which at the moment is just space padding based on justification)
        """
        self._prerender()
        return self.final_data

    def get_data_as_line(self) -> str:
        data = ""
        for line in self.preliminary_data:
            data += f"{line}{self.separator}"
        return data

    def render_horizontal(self) -> str:
        """
        Counterintuitive for the name, this renders the data to be displayed
        horizontally, which means it is constructed vertically.

        Returns: string formatted vertically with header, delimiter and newlines
        """
        # This is the final render, so include the header and delimiter lines
        final_data = f"{self.dc.pad()}{self.separator}\n"
        final_data += f"{self.construct_delimiter_line()}\n"
        prerendered_data_lines = self.get_rendered_data_list()
        for line in prerendered_data_lines:
            final_data += f"{line}\n"
        return final_data

    def render_vertical(self) -> str:
        """
        Counterintuitive for the name, this renders the data to be displayed vertically,
            which means it is constructed horizontally. Ignores justification, as vertical
            data should be centered on delimiter

        Returns: string formatted horizontally with header, delimiter and newlines
        """
        # This is the final render, so include the header and the delimiter
        final_data = ""
        self.update_justification(Justify.LEFT)
        prerendered_data_lines = self.get_rendered_data_list()
        first_line = True
        for line in prerendered_data_lines:
            if first_line:
                header_data = f"{self.dc.front_pad()}{self.horz_delimiter}"
            else:
                header_data = f"{self.dc.pad('', additional_padding=len(self.horz_delimiter))}"
            final_data += header_data
            final_data += f"{line}\n"
            first_line = False
        return final_data


def pad(
    orig_string: str,
    pad_to: int,
    pad_with: str = " ",
    front: bool = False,
    trim_backend: bool = False,
) -> str:
    length_of_orig = len(orig_string)
    to_pad = ""
    final_result = orig_string
    if length_of_orig < pad_to:
        amount_of_spaces_to_add = pad_to - length_of_orig
        for _ in range(amount_of_spaces_to_add):
            to_pad += pad_with

        if front:
            final_result = to_pad + orig_string
        else:
            if not trim_backend:
                final_result = orig_string + to_pad

    return final_result


def get_domain_from_id(string: str) -> str:
    # from the Synapse code base
    idx = string.find(":")
    # if idx == -1:
    #     raise (400, "Invalid ID: %r" % (string,))
    return string[idx + 1 :]


def extract_max_key_len_from_dict(data: Dict[str, Any]) -> int:
    max_len = 0
    for key_item in data.keys():
        max_len = max(max_len, len(key_item))
    return max_len


def extract_max_value_len_from_dict(data: Dict[str, Any]) -> int:
    max_len = 0
    for value_item in data.values():
        max_len = max(max_len, len(str(value_item)))
    return max_len
