from typing import Any, Callable, Dict, Hashable, List, Optional, Set
from asyncio import Task
from enum import Enum
import asyncio
import random

from maubot.matrix import MaubotMatrixClient
from mautrix.types import EventID, MessageEvent, ReactionEvent, RoomID

from federationbot.errors import (
    MessageAlreadyHasReactions,
    MessageNotWatched,
    ReferenceKeyAlreadyExists,
    ReferenceKeyNotFound,
)


class ReactionCommandStatus(Enum):
    # Notice the extra space, this obfuscates the reaction slightly so as not to pick up
    # stray commands from other rooms. I hope.
    START = "Start "
    PAUSE = "Pause "
    STOP = "Stop "
    CLEANUP = "Remove "


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

    async def add_cleanup_control(self, pinned_message: EventID) -> None:
        cleanup_reaction_event = await self.client.react(
            self.related_command_event.room_id,
            pinned_message,
            ReactionCommandStatus.CLEANUP.value,
        )
        self.reaction_collection_of_event_ids.add(cleanup_reaction_event)

    def get_status(self) -> ReactionCommandStatus:
        return self.current_status


class TaskSetEntry:
    tasks: List[Task]

    def __init__(self) -> None:
        self.tasks = []

    def add_tasks(self, new_task: Callable, *args, limit: int = 1) -> None:
        for _ in range(0, limit):
            self.tasks.append(asyncio.create_task(new_task(*args)))

    async def gather_results(self, return_exceptions: bool = False):
        return await asyncio.gather(*self.tasks, return_exceptions=return_exceptions)

    async def clear_all_tasks(self) -> None:
        for task in self.tasks:
            task.cancel()
        # Use return_exceptions set to True so all tasks actually are finished before exiting the system(or some
        # get left behind and keep running as orphans)
        await self.gather_results(True)


class ReactionTaskController:
    """
    Attributes:
        tracked_reactions: Map of EventID of the response of a command that will have the reactions attached to the current
            Status of the task
        tasks_sets: Map of Hashable key for reference to a TaskSetEntry of associated Task objects used by the
            command
    """

    tracked_reactions: Dict[EventID, ReactionControlEntry]
    tasks_sets: Dict[Hashable, TaskSetEntry]
    client: MaubotMatrixClient

    def __init__(self, client: MaubotMatrixClient) -> None:
        self.tracked_reactions = {}
        self.tasks_sets = {}
        self.client = client

    async def setup_control_reactions(
        self,
        pinned_message: EventID,
        command_event: MessageEvent,
        default_starting_status: ReactionCommandStatus = ReactionCommandStatus.START,
    ) -> None:
        if pinned_message in self.tracked_reactions:
            raise MessageAlreadyHasReactions

        # The creation will place the starting status as STOP, make it a start instead
        control_entry = ReactionControlEntry(
            command_event, self.client, default_starting_status
        )
        await control_entry.setup(pinned_message)
        self.tracked_reactions[pinned_message] = control_entry

    def setup_task_set(self, reference_key: Optional[Hashable] = None) -> Hashable:
        if reference_key is None:
            # Use a nice large base for the random key
            reference_key = random.randint(0, 1024 * 1024)
        if reference_key in self.tasks_sets:
            raise ReferenceKeyAlreadyExists
        self.tasks_sets.setdefault(reference_key, TaskSetEntry())
        return reference_key

    async def shutdown(self) -> None:
        for task_set in self.tasks_sets.values():
            await task_set.clear_all_tasks()
        for reaction_control_entry in self.tracked_reactions.values():
            await reaction_control_entry.cancel()

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

    async def cancel(
        self,
        pinned_message: Any,
        add_cleanup_control: bool = False,
    ) -> None:
        # TODO: I don't like the Any up there, but I wasn't able to find a better typing that Unioned Hashable and
        #  EventID(since EventID is a 'NewType')
        if pinned_message in self.tracked_reactions:
            # The cancel() includes a built-in stop()
            await self.tracked_reactions[pinned_message].cancel()
            if add_cleanup_control:
                await self.tracked_reactions[pinned_message].add_cleanup_control(
                    pinned_message
                )
            self.tracked_reactions.pop(pinned_message, None)
        if isinstance(pinned_message, Hashable) and pinned_message in self.tasks_sets:
            await self.tasks_sets[pinned_message].clear_all_tasks()
            self.tasks_sets.pop(pinned_message)

    async def remove_last_display_of(
        self, event_id_to_remove: EventID, room_id: RoomID
    ) -> None:
        await self.client.redact(
            room_id,
            event_id_to_remove,
            "Cleaning up screen real estate",
        )
        if event_id_to_remove in self.tracked_reactions:
            del self.tracked_reactions[event_id_to_remove]

    def add_tasks(
        self, reference_key: Hashable, new_task: Callable, *args, limit: int = 1
    ) -> None:
        if reference_key not in self.tasks_sets:
            raise ReferenceKeyNotFound("Need to run setup_task_set() first")

        self.tasks_sets[reference_key].add_tasks(new_task, *args, limit=limit)

    async def get_task_results(self, reference_key: Hashable):
        return await self.tasks_sets[reference_key].gather_results()

    async def react_control_handler(self, react_evt: ReactionEvent) -> None:
        reaction_data = react_evt.content.relates_to
        # The first condition makes sure that the initial placement of the reactions is not registered
        if react_evt.sender != self.client.mxid and reaction_data.event_id is not None:
            if reaction_data.event_id in self.tracked_reactions:
                if reaction_data.key == ReactionCommandStatus.STOP.value:
                    self.tracked_reactions[reaction_data.event_id].stop()
                elif reaction_data.key == ReactionCommandStatus.PAUSE.value:
                    self.tracked_reactions[reaction_data.event_id].pause()
                elif reaction_data.key == ReactionCommandStatus.START.value:
                    self.tracked_reactions[reaction_data.event_id].start()

            if reaction_data.key == ReactionCommandStatus.CLEANUP.value:
                await self.remove_last_display_of(
                    reaction_data.event_id, react_evt.room_id
                )

        return
