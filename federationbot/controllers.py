from typing import Dict, Set
from enum import Enum

from maubot.matrix import MaubotMatrixClient
from mautrix.types import EventID, MessageEvent, ReactionEvent

from federationbot.errors import MessageAlreadyHasReactions, MessageNotWatched


class ReactionCommandStatus(Enum):
    # Notice the extra space, this obfuscates the reaction slightly so as not to pick up
    # stray commands from other rooms. I hope.
    START = "Start "
    PAUSE = "Pause "
    STOP = "Stop "


class ReactionControlEntry:
    """
    Attributes:
        current_status:
        related_command_event:
        client:
    """

    current_status: ReactionCommandStatus
    related_command_event: MessageEvent
    client: MaubotMatrixClient
    reaction_collection_of_event_ids: Set[EventID]

    def __init__(
        self,
        command_event: MessageEvent,
        client: MaubotMatrixClient,
        default_starting_status: ReactionCommandStatus = ReactionCommandStatus.START,
    ) -> None:
        self.client = client
        self.related_command_event = command_event
        self.current_status = default_starting_status
        self.reaction_collection_of_event_ids = set()

    async def setup(self, pinned_message: EventID) -> None:
        stop_reaction_event = await self.client.react(
            self.related_command_event.room_id,
            pinned_message,
            ReactionCommandStatus.STOP.value,
        )
        pause_reaction_event = await self.client.react(
            self.related_command_event.room_id,
            pinned_message,
            ReactionCommandStatus.PAUSE.value,
        )
        start_reaction_event = await self.client.react(
            self.related_command_event.room_id,
            pinned_message,
            ReactionCommandStatus.START.value,
        )
        self.reaction_collection_of_event_ids.add(stop_reaction_event)
        self.reaction_collection_of_event_ids.add(pause_reaction_event)
        self.reaction_collection_of_event_ids.add(start_reaction_event)

    async def cancel(self) -> None:
        self.stop()
        for reaction_event_id in self.reaction_collection_of_event_ids:
            await self.client.redact(
                self.related_command_event.room_id,
                reaction_event_id,
                "Remove reaction control",
            )
        self.reaction_collection_of_event_ids.clear()

    def start(self) -> None:
        self.current_status = ReactionCommandStatus.START

    def stop(self) -> None:
        self.current_status = ReactionCommandStatus.STOP

    def pause(self) -> None:
        self.current_status = ReactionCommandStatus.PAUSE

    def get_status(self) -> ReactionCommandStatus:
        return self.current_status


class ReactionTaskController:
    """
    Attributes:
        tracked_reactions: Map of EventID of the response of a command that will have the reactions attached to the current
            Status of the task
    """

    tracked_reactions: Dict[EventID, ReactionControlEntry]

    def __init__(self):
        self.tracked_reactions = {}

    async def setup(
        self,
        pinned_message: EventID,
        client: MaubotMatrixClient,
        command_event: MessageEvent,
        default_starting_status: ReactionCommandStatus = ReactionCommandStatus.START,
    ) -> None:
        if pinned_message in self.tracked_reactions:
            raise MessageAlreadyHasReactions

        # The creation will place the starting status as STOP, make it a start instead
        control_entry = ReactionControlEntry(
            command_event, client, default_starting_status
        )
        await control_entry.setup(pinned_message)
        self.tracked_reactions[pinned_message] = control_entry

    def start(self, pinned_message: EventID) -> None:
        if pinned_message not in self.tracked_reactions:
            raise MessageNotWatched
        self.tracked_reactions[pinned_message].start()

    def stop(self, pinned_message: EventID) -> None:
        if pinned_message not in self.tracked_reactions:
            raise MessageNotWatched
        self.tracked_reactions[pinned_message].stop()

    def is_started(self, pinned_message: EventID) -> bool:
        if (
            pinned_message in self.tracked_reactions
            and self.tracked_reactions[pinned_message].get_status()
            == ReactionCommandStatus.START
        ):
            return True
        return False

    def is_paused(self, pinned_message: EventID) -> bool:
        if (
            pinned_message in self.tracked_reactions
            and self.tracked_reactions[pinned_message].get_status()
            == ReactionCommandStatus.PAUSE
        ):
            return True
        return False

    def is_running(self, pinned_message: EventID) -> bool:
        if self.is_started(pinned_message) or self.is_paused(pinned_message):
            return True
        return False

    def is_stopped(self, pinned_message: EventID) -> bool:
        if (
            pinned_message in self.tracked_reactions
            and self.tracked_reactions[pinned_message].get_status()
            == ReactionCommandStatus.STOP
        ):
            return True
        return False

    async def cancel(self, pinned_message: EventID) -> None:
        if pinned_message not in self.tracked_reactions:
            raise MessageNotWatched
        await self.tracked_reactions[pinned_message].cancel()

    async def react_control_handler(self, react_evt: ReactionEvent) -> None:
        reaction_data = react_evt.content.relates_to
        # The second condition makes sure that the initial placement of the reactions is not registered
        if (
            reaction_data.event_id in self.tracked_reactions
            and react_evt.sender
            != self.tracked_reactions[reaction_data.event_id].client.mxid
        ):
            if reaction_data.key == ReactionCommandStatus.STOP.value:
                self.tracked_reactions[reaction_data.event_id].stop()
            elif reaction_data.key == ReactionCommandStatus.PAUSE.value:
                self.tracked_reactions[reaction_data.event_id].pause()
            elif reaction_data.key == ReactionCommandStatus.START.value:
                self.tracked_reactions[reaction_data.event_id].start()

        return
