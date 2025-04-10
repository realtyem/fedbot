from typing import Protocol

from aiohttp import ClientSession
from mautrix.types import EventID, RoomID, TextMessageEventContent


class MessageEvent(Protocol):
    """
    Type protocol for maubot's MessageEvent class.

    This protocol defines the expected interface for message events that commands
    will receive and interact with.
    """

    room_id: RoomID
    event_id: EventID
    sender: str
    client: ClientSession

    async def respond(
        self,
        content: str | TextMessageEventContent,
        edits: EventID | str | None = None,
        allow_html: bool = False,
    ) -> EventID:
        """Send a response message to the room where this event was received."""
        ...

    async def mark_read(self) -> None:
        """Mark this event as read."""
        ...

    async def reply(self, content: str | TextMessageEventContent, allow_html: bool = False) -> EventID:
        """Send a reply to this event."""
        ...
