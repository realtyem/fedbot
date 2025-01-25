"""
Progress bar implementations using text and bitmap graphics.

Provides two types of progress bars:
- A simple text-based progress bar using ASCII characters
- A more sophisticated bitmap progress bar using Unicode braille patterns,
  supporting both linear and scatter display styles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from enum import Enum, auto

from .numbers import round_half_up

if TYPE_CHECKING:
    from collections.abc import Iterable


class BitmapProgressBarStyle(Enum):
    """
    Define the style of progress bar display.

    Attributes:
        LINEAR: Display progress as a continuous bar filling from left to right
        SCATTER: Display progress as scattered dots that fill in randomly
    """

    LINEAR = auto()
    SCATTER = auto()


class ProgressBar:
    """
    Create and manage a text-based progress bar.

    A simple progress bar implementation using ASCII characters to show progress.
    Displays as a bar filling from left to right with a percentage indicator.

    Attributes:
        max_size: Maximum value representing 100% progress
        current_value: Current progress value
        front: Character to use at start of progress bar
        rear: Character to use at end of progress bar
        fill: Character to use for filled portion
        blank: Character to use for unfilled portion
    """

    def __init__(
        self,
        max_size: int,
        current_value: int = 0,
        front: str = "{",
        rear: str = "}",
        fill: str = "|",
        blank: str = " ",
    ) -> None:
        """
        Initialize a new progress bar.

        Args:
            max_size: Maximum value representing 100% progress
            current_value: Initial progress value (defaults to 0)
            front: Character to use at start of bar (defaults to '{')
            rear: Character to use at end of bar (defaults to '}')
            fill: Character for filled portion (defaults to '|')
            blank: Character for unfilled portion (defaults to space)

        Raises:
            ValueError: If max_size is less than or equal to 0
        """
        if max_size <= 0:
            msg = "max_size must be greater than 0"
            raise ValueError(msg)
        self.max_size = max_size
        self.current_value = current_value
        self.front = front
        self.rear = rear
        self.fill = fill
        self.blank = blank

    def update(self, current_value: int) -> None:
        """
        Update the current progress value.

        Args:
            current_value: New progress value to set

        Raises:
            ValueError: If current_value is negative or exceeds max_size
        """
        if current_value < 0 or current_value > self.max_size:
            msg = f"current_value must be between 0 and {self.max_size}"
            raise ValueError(msg)
        self.current_value = current_value

    def render_progress_bar(self) -> str:
        """
        Generate a progress bar string 52 chars long.

        Creates a visual representation of progress using ASCII characters,
        with a percentage indicator at the end.

        Returns:
            A string containing the rendered progress bar with percentage,
            formatted like: {||||||     } 50%
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


class BitmapProgressBar:
    """
    Create and manage a bitmap-based progress bar using Unicode braille patterns.

    A sophisticated progress bar that uses Unicode braille patterns to show progress.
    Supports both linear (continuous) and scatter (random-fill) display styles.

    The bar divides progress into segments, each represented by a braille character
    that can show 8 different levels of completion using dot patterns.

    Example:
        Linear:  {⡀⡄⡆⡇⣇⣧⣷⣿}
        Scatter: {⡀⣀⣄⣤⣦⣶⣷⣿}

    Attributes:
        line_size: Size of the displayed progress bar in characters
        max_size: Maximum values count to get to 100%
        front: Character to use at start of progress bar
        rear: Character to use at end of progress bar
        style: Style of progress bar display (linear or scatter)
        _map: The bitmap itself, marking values as True when updated
        _buckets: List of tuples defining start/end ranges for each segment
        _segment_size: Percentage each segment represents
        increment_size: Percentage each increase represents within a segment
        constants: Dictionary mapping progress levels to display characters
        blank: Character used for empty space
    """

    line_size: int
    max_size: int
    _map: dict[int, bool]
    _buckets: list[tuple[int, int]]
    _segment_size: float
    increment_size: float
    style: BitmapProgressBarStyle
    blank = "⠈"

    def __init__(
        self,
        line_size: int,
        max_size: int,
        front: str = "{",
        rear: str = "}",
        style: BitmapProgressBarStyle = BitmapProgressBarStyle.SCATTER,
    ) -> None:
        """
        Initialize a new bitmap progress bar.

        Args:
            line_size: Width of progress bar in characters
            max_size: Maximum value representing 100%
            front: Character for start of bar (defaults to '{')
            rear: Character for end of bar (defaults to '}')
            style: Display style (defaults to SCATTER)

        Raises:
            ValueError: If line_size or max_size is less than or equal to 0
        """
        if line_size <= 0 or max_size <= 0:
            msg = "line_size and max_size must be greater than 0"
            raise ValueError(msg)
        self.line_size = line_size
        self.max_size = max_size
        self.front = front
        self.rear = rear
        self.style = style
        self._map = dict.fromkeys(range(1, self.max_size + 1), False)

        # Pre-calculate constants based on style
        self.constants = (
            {  # LINEAR
                1: "⡀",
                2: "⡄",
                3: "⡆",
                4: "⡇",
                5: "⣇",
                6: "⣧",
                7: "⣷",
                8: "⣿",
            }
            if style == BitmapProgressBarStyle.LINEAR
            else {  # SCATTER
                1: "⡀",
                2: "⣀",
                3: "⣄",
                4: "⣤",
                5: "⣦",
                6: "⣶",
                7: "⣷",
                8: "⣿",
            }
        )

        # Pre-calculate segment sizes and buckets
        self.increment_size = 1 / len(self.constants)
        self._buckets = []

        # Calculate bucket ranges
        if self.max_size <= self.line_size:
            # When line_size > max_size, calculate segments per bit
            segment_size = self.line_size / self.max_size
            bit_counter = 1

            for s in range(1, self.line_size + 1):
                self._buckets.append((bit_counter, bit_counter + 1))
                if s >= round_half_up(bit_counter * segment_size):
                    bit_counter += 1
        else:
            # When max_size > line_size, calculate bits per segment
            segment_size = self.max_size / self.line_size
            start_bit = 1

            for s in range(1, self.line_size + 1):
                end_bit = int(round_half_up(s * segment_size))
                self._buckets.append((start_bit, end_bit))
                start_bit = end_bit

    def update(self, values: Iterable[int]) -> None:
        """
        Update progress bar with new values.

        For SCATTER style, marks each provided value as complete.
        For LINEAR style, marks all values up to the maximum provided value as complete.

        Args:
            values: Collection of progress values to update
        """
        if self.style == BitmapProgressBarStyle.SCATTER:
            for value in values:
                self._map[value] = True
        else:
            max_value = max(values)
            for i in range(1, max_value + 1):
                self._map[i] = True

    def render_bitmap_bar(self) -> str:
        """
        Render the current state of the progress bar.

        Generates a visual representation of progress using Unicode braille patterns,
        with the display style determined by self.style.

        Returns:
            A string containing the rendered progress bar using Unicode braille patterns
            to show completion levels for each segment
        """
        segments = []
        segments.append(self.front)

        for start, end in self._buckets:
            count = end - start
            tally = sum(1 for i in range(start, end) if self._map[i])
            segment_percent = tally / count

            # Find highest matching constant ratio
            next_segment = self.blank
            for constant, value in self.constants.items():
                if segment_percent >= constant * self.increment_size:
                    next_segment = value

            segments.append(next_segment)

        segments.append(self.rear)
        return "".join(segments)
