from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union
from datetime import datetime
from enum import Enum, auto
import json
import math

from mautrix.types import EventID

json_decoder = json.JSONDecoder()

# An event is considered having a maximum size of 64K. Unfortunately, encryption uses
# more space than cleartext, so give some slack room
MAX_EVENT_SIZE_FOR_SENDING = 40000


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

    def maybe_update_column_width(self, new_value: Optional[Union[int, str]]) -> None:
        """
        Use to potentially update the column width. Uses max() under the hood.

        Args:
            new_value: To apply to the column width
        """
        if new_value:
            if isinstance(new_value, str):
                self.line_size = max(self.line_size, len(new_value))
            else:
                self.line_size = max(self.line_size, new_value)

    @property
    def size(self) -> int:
        """
        Gets the line_size without accidentally setting it.
        :return:
        """
        return self.line_size

    def pad(
        self, data_piece: Optional[Union[str, int]] = None, additional_padding: int = 0
    ) -> str:
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

        return f"{pad(what_to_display, self.size + additional_padding)}"

    def front_pad(self, data_piece: Optional[str] = None) -> str:
        """
        Pad the data_piece string by the column size from the front of the
        string(effectively a right justify). No data_piece means use the header_name

        Args:
            data_piece:

        Returns: The formatted string
        """

        return f"{pad(data_piece if data_piece is not None else self.header_name, self.size, front=True)}"

    def render_pretty_line(
        self, header: str, item_to_render: Any, force: bool = False
    ) -> str:
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

    def load_fresh_data(self, new_data: List[str]) -> None:
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
                header_data = (
                    f"{self.dc.pad('', additional_padding=len(self.horz_delimiter))}"
                )
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


class ProgressBar:
    def __init__(
        self,
        max_size: int,
        current_value: int = 0,
        front: str = "{",
        rear: str = "}",
        fill: str = "|",
        blank: str = " ",
    ) -> None:
        self.max_size = max_size
        self.current_value = current_value
        self.front = front
        self.rear = rear
        self.fill = fill
        self.blank = blank

    def update(self, current_value: int) -> None:
        self.current_value = current_value

    def render_progress_bar(self) -> str:
        """
        Generate a progress bar string 52 chars long, like:
        {|||||||||||||||||||                               } 38%
        based on the(mostly) simple math of current_value / max_size

        Returns: string with no newlines

        """
        buffered_message = self.front
        calculated_percent = (self.current_value / self.max_size) * 100
        adjusted_percent = int(calculated_percent)
        # Since we are using range() to calculate our positions, there are some
        # oddities around the step argument and how it deals with odd numbers. The
        # progress bar's accuracy is less important than representing forward
        # progress so just making the odd number even is close enough.
        is_odd_number = (adjusted_percent % 2) != 0
        if is_odd_number:
            adjusted_percent += 1

        for _ in range(0, adjusted_percent, 2):
            buffered_message += self.fill
        for _ in range(0, 100 - adjusted_percent, 2):
            buffered_message += self.blank

        buffered_message += self.rear + " " + str(adjusted_percent) + "%"
        return buffered_message


class BitmapProgressBarStyle(Enum):
    LINEAR = auto()
    SCATTER = auto()


class BitmapProgressBar:
    # Size of the displayed progress bar, in characters
    line_size: int

    # Maximum values count to get to 100%
    max_size: int

    # The bit map itself. As a value is updated it will be marked True.
    _map: Dict[int, bool]

    # Let's do buckets to divide the segments of the bar. There will be the same
    # number of buckets as a count of line_size, so line_size of 50 means 50 buckets.
    # Each bucket has two integers as a Tuple, one for the start item in the bitmap
    # and one for the end(which is exclusive). Sometimes a bucket will overlap with
    # two items for the bitmap(should only happen for bitmap counts less than line_size)
    _buckets: List[Tuple[int, int]]

    # Segment size should reflect what % each segment(of line_size) is responsible for.
    # For example:
    #  with a line_size of 50(so 50 segments total) and a max_size of 100 units:
    #  100 / 50 = 2%
    #  So each segment would be responsible for 2% of the displayable value. By
    #  extension, max_sizes less than line_size will reflect smaller percents.
    #  with a line_size of 50 and a max_size of 20:
    #  20 / 50 = 0.4%
    # TLDR, above 1.0 for line_size>max_size, below 1.0 for line_size<max_size.
    # (buckets per bit vs bits per bucket)
    _segment_size: float

    # The graphics to use for displaying items on the bar, each is considered a segment
    # on the line. In theory could have any number of elements(and the math is taken
    # care of), 8 works pretty good.
    # Work ups, tried(all had some kind of problem):
    # constants = {1: "▏", 2: "▎", 3: "▍", 4: "▌", 5: "▋", 6: "▊", 7: "▉", 8: "█"}
    # constants = {1: "▁", 2: "▂", 3: "▃", 4: "▄", 5: "▅", 6: "▆", 7: "▇", 8: "█"}
    # constants = {1: "▖", 2: "▄", 3: "▙", 4: "█"}
    # constants = {1: "╷", 2: "╻", 3: "╽", 4: "┃", 5: "┫", 6: "╋"}

    # (3, 4), (7, 8) appear same
    # constants = {1: "╷", 2: "╻", 3: "┒", 4: "┓", 5: "┪", 6: "┫", 7: "╉", 8: "╋"}

    # Unfortunately, because of the fonts used in various clients, they look like trash
    # due to misformed/inconsistent-formed glyphs. The braille sequence seems best. Set
    # these in the __init__() function, allowing flexibility of scatter vs linear.
    # constants: Dict[int, str] = {
    #     1: "⡀",
    #     2: "⣀",
    #     3: "⣄",
    #     4: "⣤",
    #     5: "⣦",
    #     6: "⣶",
    #     7: "⣷",
    #     8: "⣿",
    # }  # "⠈"
    # constants = {
    #     1: "⡀", 2: "⡄", 3: "⡆", 4: "⡇", 5: "⣇", 6: "⣧", 7: "⣷", 8: "⣿"
    # }  # , 2: "⠀" "⠈"

    # The blank spacing. Take care must be taken to verify that the pixel width is close
    # to the glyph size(again, inconsistency in fonts)
    blank = "⠈"  # other fractional spaces:"    "

    # Percent of a segment that each increase represents. For example:
    # a line_size of 50, and max_size of 100 and 8 constants(just above)
    # 1 / 8 = 0.125 or 12.5%
    # so if the bar is started at empty and we update positions 1-3 to True,
    # a segment size is 2%(so segment 1 would be responsible for values 1-2 and segment
    # 2 would be responsible for 3-4),
    # segment 1 would be constant[8] and segment 2 would be constant[4]
    increment_size: float
    style: BitmapProgressBarStyle

    def __init__(
        self,
        line_size: int,
        max_size: int,
        front: str = "{",
        rear: str = "}",
        style: BitmapProgressBarStyle = BitmapProgressBarStyle.SCATTER,
    ) -> None:
        self.line_size = line_size
        self.max_size = max_size
        self.front = front
        self.rear = rear
        self.style = style
        # Create the empty bitmap and fill it with False
        self._map = {}
        for i in range(1, self.max_size + 1):
            self._map[i] = False

        if self.style == BitmapProgressBarStyle.SCATTER:
            self.constants = {
                1: "⡀",
                2: "⣀",
                3: "⣄",
                4: "⣤",
                5: "⣦",
                6: "⣶",
                7: "⣷",
                8: "⣿",
            }
        elif self.style == BitmapProgressBarStyle.LINEAR:
            self.constants = {
                1: "⡀",
                2: "⡄",
                3: "⡆",
                4: "⡇",
                5: "⣇",
                6: "⣧",
                7: "⣷",
                8: "⣿",
            }
        # Percent(as float)
        self.increment_size = 1 / len(self.constants)

        self._buckets = []
        if self.max_size <= self.line_size:
            # When the line_size is larger than the max_size, this will be a count of
            # the segments that make up a given bit in the map. That is a convenient
            # measure of how many buckets to assign to that bit, so:
            # Buckets / bit
            self._segment_size = self.line_size / self.max_size  # 50 / 21 = 2.5
            bucket_counter = 0
            bit_counter = 1

            for s in range(1, self.line_size + 1):
                bucket_counter += 1
                self._buckets.append((bit_counter, bit_counter + 1))
                if s >= round_half_up(bit_counter * self._segment_size):
                    bit_counter += 1
                    bucket_counter = 0

        elif self.max_size > self.line_size:
            # Bits / bucket(see above)
            self._segment_size = self.max_size / self.line_size  # 105 / 50 = 2.1
            start_bit_counter = 1

            for s in range(1, self.line_size + 1):
                end_bit_counter = int(round_half_up(s * self._segment_size))
                self._buckets.append((start_bit_counter, end_bit_counter))
                start_bit_counter = end_bit_counter

    def update(self, values: Iterable[int]) -> None:
        """
        Scatter bar: Add given iterable integers to the bit map
        Linear bar: Advance all bits up to given integer to the bit map, leaving no
            gaps(recommend only submitting a single integer inside the iterable,
            otherwise only the largest will apply)
        Args:
            values: An iterable of integers(largest of submitted will be used for linear
                bar)

        Returns: None

        """
        if self.style == BitmapProgressBarStyle.SCATTER:
            for value in values:
                self._map[value] = True
        else:
            max_value = max(values)
            for i in range(1, max_value + 1):
                self._map[i] = True

    def render_bitmap_bar(self) -> str:
        result_bar = self.front
        for start, end in self._buckets:
            count = end - start
            tally = 0
            next_segment = self.blank
            for i in range(start, end):
                tally += 1 if self._map[i] else 0

            segment_percent = tally / count

            for constant, value in self.constants.items():
                constant_ratio = constant * self.increment_size
                if segment_percent >= constant_ratio:
                    next_segment = value

            result_bar += next_segment

        result_bar += self.rear
        return result_bar


def round_up(n, decimals=0):
    multiplier = 10 ** decimals
    return math.ceil(n * multiplier) / multiplier


def round_down(n, decimals=0):
    multiplier = 10 ** decimals
    return math.floor(n * multiplier) / multiplier


def round_half_up(n, decimals=0):
    multiplier = 10 ** decimals
    return math.floor(n * multiplier + 0.5) / multiplier


def truncate(n, decimals=0):
    multiplier = 10 ** decimals
    return int(n * multiplier) / multiplier


def pretty_print_timestamp(timestamp: int) -> str:
    return str(datetime.fromtimestamp(float(timestamp / 1000)))


def full_dict_copy(data_to_copy: Dict[str, Any]) -> Dict[str, Any]:
    """
    Make a copy of a Dict, to avoid mutating any sub-keys
    Args:
        data_to_copy:

    Returns: A duplicate of the original Dict with all new reference objects

    """
    return json_decoder.decode(json.dumps(data_to_copy))


class Colors(Enum):
    BLACK = "000000"
    GREEN = "008000"
    RED = "F00000"
    WHITE = "e8e8e8"
    YELLOW = "FFFF00"


def br(message: str) -> str:
    """
    Add a HTML <br> at end of line
    Args:
        message: string to append to

    Returns: message string appended with <br> tag

    """
    return f"{message}<br>\n"


def bold(message: str) -> str:
    """
    Wrap a string with HTML <bold></bold> tags
    Args:
        message: string to wrap

    Returns: string wrapped with applicable tags

    """
    return f"<b>{message}</b>"


def wrap_in_code_tags(message: str) -> str:
    """
    Wrap a string with HTML <code></code> tags
    Args:
        message: string to wrap

    Returns: string wrapped with applicable tags

    """
    return f"<code>{message}</code>"


def wrap_in_details(message: str, summary: Optional[str] = None) -> str:
    summary_render = ""
    if summary:
        summary_render = f"<summary>{summary}</summary>"
    return f"<details>{summary_render}{message}</details>"


def add_color(
    message: str,
    foreground: Optional[Colors] = None,
    background: Optional[Colors] = None,
) -> str:
    buffered_message = "<font"
    if foreground:
        buffered_message += f' color="#{foreground.value}"'
    if background:
        buffered_message += f' data-mx-bg-color="#{background.value}"'
    buffered_message += f">{message}</font>"
    return buffered_message


def combine_lines_to_fit_event(
    list_of_all_lines: List[str],
    header_line: Optional[str],
    insert_new_lines: bool = False,
) -> List[str]:
    """
    The rendering system uses lists of strings to build a message response. This will append those strings(optionally
    with new line characters) to the correct size to send as a MessageEvent.

    Args:
        list_of_all_lines: strings to render(don't forget newlines)
        header_line: if you want a line at the top(description or whatever)
        insert_new_lines: bool if new line markdown should be inserted between each line

    Returns: List strings designed to fit into an Event's size restrictions

    """
    list_of_combined_lines = []
    buffered_line = ""
    if header_line:
        buffered_line += header_line
        buffered_line += "\n" if insert_new_lines else ""
    for line in list_of_all_lines:
        if len(buffered_line) + len(line) > MAX_EVENT_SIZE_FOR_SENDING:
            # This buffer is full, add it to the final list
            list_of_combined_lines.extend([buffered_line])
            # Don't forget to start the new buffer
            buffered_line = str(header_line)
            buffered_line += "\n" if insert_new_lines else ""

        buffered_line += line
        buffered_line += "\n" if insert_new_lines else ""

    # Grab the last buffer too
    list_of_combined_lines.extend([buffered_line])
    return list_of_combined_lines


def combine_lines_to_fit_event_html(
    list_of_all_lines: List[str],
    header_lines: Optional[List[str]],
    add_code_tags: bool = True,
    apply_pre_tags: bool = False,
    insert_new_lines: bool = True,
) -> List[str]:
    """
    The rendering system uses lists of strings to build a message response. This will append those strings(optionally
    with <br> html tags and/org <code> html tags) to the correct size to send as a MessageEvent.

    Args:
        list_of_all_lines: strings to render(don't forget newlines)
        header_lines: if you want a line at the top(description or whatever)
        add_code_tags: bool if lines should be wrapped in <code></code> tags
        apply_pre_tags: bool if entire finished combined lines should be wrapped in <pre></pre> tags
        insert_new_lines: bool if <br> html tags should be appended to each line

    Returns: List strings designed to fit into an Event's size restrictions

    """
    if header_lines is None:
        header_lines = []
    list_of_combined_lines = []
    buffered_line = "<pre>" if apply_pre_tags else ""
    rendered_header_lines = ""
    for header_line in header_lines:
        half_rendered_header_line = header_line
        if add_code_tags:
            half_rendered_header_line = wrap_in_code_tags(half_rendered_header_line)
        if insert_new_lines:
            half_rendered_header_line = br(half_rendered_header_line)
        # Take a copy, otherwise the reference passing plays games
        rendered_header_lines += str(half_rendered_header_line)
    buffered_line += rendered_header_lines

    for line in list_of_all_lines:
        if len(buffered_line) + len(line) > MAX_EVENT_SIZE_FOR_SENDING:
            # This buffer is full, add it to the final list
            buffered_line += "</pre>" if apply_pre_tags else ""
            list_of_combined_lines.extend([buffered_line])

            # Don't forget to start the new buffer
            buffered_line = "<pre>" if apply_pre_tags else ""
            buffered_line += rendered_header_lines

        half_rendered_line = line
        if add_code_tags:
            half_rendered_line = wrap_in_code_tags(half_rendered_line)
        if insert_new_lines:
            half_rendered_line = br(half_rendered_line)
        buffered_line += half_rendered_line

    buffered_line += "</pre>" if apply_pre_tags else ""
    # Grab the last buffer too
    list_of_combined_lines.extend([buffered_line])
    return list_of_combined_lines
