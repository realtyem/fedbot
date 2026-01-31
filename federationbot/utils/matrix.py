"""Matrix-specific utility functions."""

from __future__ import annotations

from mautrix.types import Format, MessageType, TextMessageEventContent
from mautrix.util import markdown


def get_domain_from_id(string: str) -> str:
    """
    Extract domain portion from a Matrix ID.

    Example:
        "@user:example.com" -> "example.com"

    Returns:
        Domain portion of the Matrix ID
    """
    return string.split(":", 1)[1]


def is_event_id(maybe_event_id: str) -> str | None:
    """
    Check if a string is a valid event ID.

    Returns:
        Event ID if valid, None otherwise
    """
    if maybe_event_id.startswith("$"):
        return maybe_event_id

    return None


def is_room_id(maybe_room_id: str) -> str | None:
    """
    Check if a string is a valid room ID.

    Returns:
        Room ID if valid, None otherwise
    """
    if maybe_room_id.startswith("!"):
        if ":" in maybe_room_id or len(maybe_room_id) == 44:
            return maybe_room_id

    return None


def is_room_alias(maybe_room_alias: str) -> str | None:
    """
    Check if a string is a valid room alias.

    Returns:
        Room alias if valid, None otherwise
    """
    if maybe_room_alias.startswith("#") and ":" in maybe_room_alias:
        return maybe_room_alias

    return None


def is_room_id_or_alias(maybe_room: str) -> str | None:
    """
    Check if a string is a valid room ID or alias.

    Returns:
        Room ID or alias if valid, None otherwise
    """
    result = is_room_id(maybe_room)
    if result:
        return result
    return is_room_alias(maybe_room)


def is_mxid(maybe_mxid: str) -> str | None:
    """
    Check if a string is a valid Matrix ID.

    Returns:
        Matrix ID if valid, None otherwise
    """
    return maybe_mxid if maybe_mxid.startswith("@") else None


def make_into_text_event(message: str, allow_html: bool = False, ignore_body: bool = False) -> TextMessageEventContent:
    """
    Create a TextMessageEventContent object.

    Returns:
        TextMessageEventContent object
    """
    return TextMessageEventContent(
        msgtype=MessageType.NOTICE,
        body=message if not ignore_body else "no alt text available",
        format=Format.HTML,
        formatted_body=markdown.render(message, allow_html=allow_html),
    )
