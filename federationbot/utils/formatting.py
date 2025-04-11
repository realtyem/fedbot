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


def combine_lines_to_fit_event(
    list_of_all_lines: list[str],
    header_line: str | None,
    insert_new_lines: bool = False,
) -> list[str]:
    """
    Combine strings into message-sized chunks for event sending.

    Splits content into chunks that fit within Matrix message size limits,
    optionally adding headers and newlines.

    Args:
        list_of_all_lines: Lines of text to combine
        header_line: Optional header to prepend to each chunk
        insert_new_lines: Whether to add newlines between lines

    Returns:
        List of strings, each within message size limit
    """
    if not list_of_all_lines:
        return []

    newline = "\n" if insert_new_lines else ""
    header = f"{header_line}{newline}" if header_line else ""
    header_size = len(header)

    result = []
    current_chunk = [header] if header else []
    current_size = header_size

    for line in list_of_all_lines:
        line_with_nl = f"{line}{newline}"
        line_size = len(line_with_nl)

        if current_size + line_size > MAX_EVENT_SIZE_FOR_SENDING:
            result.append("".join(current_chunk))
            current_chunk = [header] if header else []
            current_size = header_size

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

    Optimizes message chunking by pre-calculating tag sizes and pre-rendering
    common elements to avoid repeated string operations.

    Args:
        list_of_all_lines: Lines of text to combine
        header_lines: Optional header lines to prepend to each chunk
        add_code_tags: Whether to wrap lines in <code> tags
        apply_pre_tags: Whether to wrap chunks in <pre> tags
        insert_new_lines: Whether to add HTML line breaks

    Returns:
        List of HTML-formatted strings, each within message size limit
    """
    if not list_of_all_lines:
        return []

    br_tag = "<br>\n" if insert_new_lines else ""

    # Pre-render header
    rendered_headers = ""
    if header_lines:
        headers = [(wrap_in_code_tags(line) if add_code_tags else line) + br_tag for line in header_lines]
        rendered_headers = "".join(headers)

    # Pre-render lines with their formatting
    rendered_lines = [(wrap_in_code_tags(line) if add_code_tags else line) + br_tag for line in list_of_all_lines]

    # Calculate chunk components
    pre_start = "<pre>" if apply_pre_tags else ""
    pre_end = "</pre>" if apply_pre_tags else ""
    chunk_overhead = len(pre_start) + len(rendered_headers) + len(pre_end)

    result = []
    current_chunk = [pre_start, rendered_headers]
    current_size = chunk_overhead

    for rendered_line in rendered_lines:
        line_size = len(rendered_line)

        if current_size + line_size > MAX_EVENT_SIZE_FOR_SENDING:
            current_chunk.append(pre_end)
            result.append("".join(current_chunk))
            current_chunk = [pre_start, rendered_headers]
            current_size = chunk_overhead

        current_chunk.append(rendered_line)
        current_size += line_size

    if current_chunk:
        current_chunk.append(pre_end)
        result.append("".join(current_chunk))

    return result


def wrap_in_code_block_markdown(existing_buffer: str) -> str:
    """
    Wrap a string with Markdown code block tags.

    Returns:
        String wrapped in ```text tags
    """
    return f"```text\n{existing_buffer}\n```\n" if existing_buffer else ""


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
    summary_tag = f"<summary>{summary}</summary>" if summary else ""
    return f"<details>{summary_tag}{message}</details>"


def wrap_in_pre_tags(incoming: str) -> str:
    """
    Wrap a string with HTML pre tags.

    Returns:
        String wrapped in <pre> tags
    """
    return f"<pre>\n{incoming}\n</pre>\n" if incoming else ""
