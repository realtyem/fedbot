"""
HTML and text formatting utilities.

Provides functions for formatting text with HTML tags and handling Matrix message
size limitations, including:
- Basic HTML formatting (bold, code blocks, etc.)
- Color application
- Message chunking for large content
- Details/summary tag wrapping
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .colors import Colors

MAX_EVENT_SIZE_FOR_SENDING = 40000


def br(message: str) -> str:
    """
    Add a HTML line break at end of line.

    Returns:
        String with <br> tag and newline appended
    """
    return f"{message}<br>\n"


def bold(message: str) -> str:
    """
    Wrap a string with HTML bold tags.

    Returns:
        String wrapped in <b> tags
    """
    return f"<b>{message}</b>"


def wrap_in_code_tags(message: str) -> str:
    """
    Wrap a string with HTML code tags.

    Returns:
        String wrapped in <code> tags
    """
    return f"<code>{message}</code>"


def wrap_in_details(message: str, summary: str | None = None) -> str:
    """
    Wrap content in HTML details/summary tags.

    Returns:
        String wrapped in <details> tags, with optional summary
    """
    return f"<details>{'<summary>' + summary + '</summary>' if summary else ''}{message}</details>"


def add_color(
    message: str,
    foreground: Colors | None = None,
    background: Colors | None = None,
) -> str:
    """
    Add HTML color formatting to text.

    Returns:
        String wrapped in <font> tag with color attributes
    """
    attrs = []
    if foreground:
        attrs.append(f'color="#{foreground.value}"')
    if background:
        attrs.append(f'data-mx-bg-color="#{background.value}"')
    return f"<font{' ' + ' '.join(attrs) if attrs else ''}>{message}</font>"


def combine_lines_to_fit_event(
    list_of_all_lines: list[str],
    header_line: str | None,
    insert_new_lines: bool = False,
) -> list[str]:
    """
    Combine strings into message-sized chunks for event sending.

    Splits content into chunks that fit within Matrix message size limits,
    optionally adding headers and newlines.

    Returns:
        List of strings, each within message size limit
    """
    if not list_of_all_lines:
        return []

    newline = "\n" if insert_new_lines else ""
    header = f"{header_line}{newline}" if header_line else ""
    result = []
    current_chunk = [header] if header else []
    current_size = len(header)

    for line in list_of_all_lines:
        line_with_nl = f"{line}{newline}"
        line_size = len(line_with_nl)

        if current_size + line_size > MAX_EVENT_SIZE_FOR_SENDING:
            result.append("".join(current_chunk))
            current_chunk = [header] if header else []
            current_size = len(header)

        current_chunk.append(line_with_nl)
        current_size += line_size

    if current_chunk:
        result.append("".join(current_chunk))
    return result


def combine_lines_to_fit_event_html(
    list_of_all_lines: list[str],
    header_lines: list[str] | None = None,
    add_code_tags: bool = True,
    apply_pre_tags: bool = False,
    insert_new_lines: bool = True,
) -> list[str]:
    """
    Combine strings into message-sized chunks with HTML formatting.

    Like combine_lines_to_fit_event but with HTML formatting options
    including code tags, pre tags, and line breaks.

    Returns:
        List of HTML-formatted strings, each within message size limit
    """
    if not list_of_all_lines:
        return []

    pre_start = "<pre>" if apply_pre_tags else ""
    pre_end = "</pre>" if apply_pre_tags else ""

    # Pre-render header once
    rendered_headers = ""
    if header_lines:
        headers = []
        for header_line in header_lines:
            rendered_header = header_line
            if add_code_tags:
                rendered_header = wrap_in_code_tags(rendered_header)
            if insert_new_lines:
                rendered_header = br(rendered_header)
            headers.append(rendered_header)
        rendered_headers = "".join(headers)

    result = []
    current_chunk = [pre_start, rendered_headers]
    current_size = len(pre_start) + len(rendered_headers) + len(pre_end)

    for line in list_of_all_lines:
        rendered_line = line
        if add_code_tags:
            rendered_line = wrap_in_code_tags(rendered_line)
        if insert_new_lines:
            rendered_line = br(rendered_line)
        line_size = len(rendered_line)

        if current_size + line_size > MAX_EVENT_SIZE_FOR_SENDING:
            current_chunk.append(pre_end)
            result.append("".join(current_chunk))
            current_chunk = [pre_start, rendered_headers]
            current_size = len(pre_start) + len(rendered_headers) + len(pre_end)

        current_chunk.append(rendered_line)
        current_size += line_size

    if current_chunk:
        current_chunk.append(pre_end)
        result.append("".join(current_chunk))
    return result
