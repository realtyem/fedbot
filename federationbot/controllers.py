from __future__ import annotations

from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    Generic,
    Hashable,
    List,
    Literal,
    Optional,
    Sequence,
    Set,
    Tuple,
    TypeVar,
    overload,
)
from asyncio import AbstractEventLoop, Future, Task
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
import asyncio
import functools
import logging
import random
import time

from maubot.matrix import MaubotMatrixClient
from mautrix.types import EventID, ReactionEvent, RoomID

from federationbot.errors import (
    MessageAlreadyHasReactions,
    MessageNotWatched,
    ReferenceKeyAlreadyExists,
    ReferenceKeyNotFound,
)
from federationbot.protocols import MessageEvent

T = TypeVar("T")
task_control_logger = logging.getLogger("task_control")


class ReactionCommandStatus(Enum):
    # Notice the extra space, this obfuscates the reaction slightly so as not to pick up
    # stray commands from other rooms. I hope.
    START = "Start "
    PAUSE = "Pause "
    STOP = "Stop "
    CLEANUP = "Remove "


class EmojiReactionCommandStatus(Enum):
    # Notice the extra space, this obfuscates the reaction slightly so as not to pick up
    # stray commands from other rooms. I hope.
    # Alternatives: ❌ ⏹ 🚮 🛑 ⏸ ▶
    START = "▶ "
    PAUSE = "⏸ "
    STOP = "⏹ "
    CLEANUP = "❌ "


class ReactionControlEntry:
    """
    The object for adding control reactions to messages made by the bot. Adds 'Start', 'Stop', and 'Pause'.
    Intended for use on long running commands that update the message with fresh information.

    This is keyed by the EventID in ReactionTaskController, so it doesn't know what Event it is on. Used by the
    Reaction handler to query/set what a running command status should be.

    Attributes:
        current_status:
        related_command_event: The MessageEvent containing data about the original event used to start the command
        client: The MaubotMatrixClient, used to add/cleanup control reactions to the response messages
    """

    current_status: ReactionCommandStatus | EmojiReactionCommandStatus
    related_command_event: MessageEvent
    client: MaubotMatrixClient
    reaction_collection_of_event_ids: Set[EventID]
    emoji: bool

    def __init__(
        self,
        command_event: MessageEvent,
        client: MaubotMatrixClient,
        emoji: bool,
        default_starting_status: ReactionCommandStatus | EmojiReactionCommandStatus = ReactionCommandStatus.START,
    ) -> None:
        self.client = client
        self.related_command_event = command_event
        self.current_status = default_starting_status
        self.emoji = emoji
        self.reaction_collection_of_event_ids = set()

    async def setup(self, pinned_message: EventID) -> None:
        """
        Initialize the control reactions so the handler can find them later if needed.
        Args:
            pinned_message:

        Returns:

        """
        stop_reaction_event = await self.client.react(
            self.related_command_event.room_id,
            pinned_message,
            EmojiReactionCommandStatus.STOP.value if self.emoji else ReactionCommandStatus.STOP.value,
        )
        pause_reaction_event = await self.client.react(
            self.related_command_event.room_id,
            pinned_message,
            EmojiReactionCommandStatus.PAUSE.value if self.emoji else ReactionCommandStatus.PAUSE.value,
        )
        start_reaction_event = await self.client.react(
            self.related_command_event.room_id,
            pinned_message,
            EmojiReactionCommandStatus.START.value if self.emoji else ReactionCommandStatus.START.value,
        )
        self.reaction_collection_of_event_ids = {
            stop_reaction_event,
            pause_reaction_event,
            start_reaction_event,
        }

    async def cancel(self) -> None:
        """
        This will change the status to 'Stop', then redact the reactions placed by the bot as a visual indicator that
        the command is finished.

        """
        self.stop()
        for reaction_event_id in self.reaction_collection_of_event_ids:
            await self.client.redact(
                self.related_command_event.room_id,
                reaction_event_id,
                "Remove reaction control",
            )
        self.reaction_collection_of_event_ids.clear()

    def start(self) -> None:
        self.current_status = EmojiReactionCommandStatus.START if self.emoji else ReactionCommandStatus.START

    def stop(self) -> None:
        self.current_status = EmojiReactionCommandStatus.STOP if self.emoji else ReactionCommandStatus.STOP

    def pause(self) -> None:
        self.current_status = EmojiReactionCommandStatus.PAUSE if self.emoji else ReactionCommandStatus.PAUSE

    async def add_cleanup_control(self, pinned_message: EventID, emoji: bool = False) -> None:
        """
        Adds a cleanup reaction to the command response that the reaction handler will be able to redact the command
        response to save on screen real estate.
        Args:
            pinned_message: The EventID of the command response, for finding which reactions to remove
            emoji: True if using the Emoji reaction symbols instead of words

        """
        # Don't actually have to save this, as the handler will be capable of removing old stuff without saving it
        await self.client.react(
            self.related_command_event.room_id,
            pinned_message,
            EmojiReactionCommandStatus.CLEANUP.value if emoji else ReactionCommandStatus.CLEANUP.value,
        )

    def get_status(self) -> ReactionCommandStatus | EmojiReactionCommandStatus:
        return self.current_status


class TaskSetEntry(Generic[T]):
    """
    A collection object for holding and grouping Task's.

    Mainly used for cleaning up any running Task coroutines if they need to be stopped, like during a bot restart.
    """

    # Signatures
    # Callable[[ArgType1, ArgType2,...], T]  used to specify the typing of the function to create another function
    #                                       a Callable can return an Awaitable
    # Coroutine[Any, Any, T]
    # Awaitable[T]     the typing for a thing that will produce T as a result
    tasks: List[Task[T]]
    coros: List[Future[T]]
    loop: AbstractEventLoop

    def __init__(self) -> None:
        self.tasks = []
        self.coros = []
        self.loop = asyncio.get_event_loop()
        self.loop.set_debug(False)

    def add_tasks(
        self,
        new_task: Callable[..., Coroutine[Any, Any, T | None]],
        *args,
        limit: int = 1,
        **kwargs,
    ) -> None:
        """
        Add this Callable and its args 'limit' number of times to the grouping of Tasks.

        Args:
            new_task: The function name without the ()
            *args: all the bits that are passed to the function
            limit: How many copies of this Task should be created. Multiple copies implies either a worker model or a
                large grouping of one-off requests.

        """
        for _ in range(limit):
            self.tasks.append(asyncio.create_task(new_task(*args, **kwargs)))  # type: ignore[arg-type]

    async def add_threaded_tasks(
        self,
        new_task,
        *args,
        executor: ThreadPoolExecutor,
        limit: int = 1,
        **kwargs,
    ) -> None:
        """
        Add this Callable and its args 'limit' number of times to the grouping of Tasks.
        NOTE: This currently does not work as expected

        Args:
            new_task: The function name without the ()
            *args: all the bits that are passed to the function
            limit: How many copies of this Task should be created. Multiple copies implies either a worker model or a
                large grouping of one-off requests.
            executor:

        """
        for _ in range(limit):
            self.coros.append(await self.loop.run_in_executor(executor, functools.partial(new_task, *args, **kwargs)))

    async def gather_results(self, return_exceptions: bool = True) -> Sequence[T | BaseException]:
        """
        If you have elected for your Task to return a result, this will get them as a Sequence.

        gather() returns something that is, not stricly speaking, a List. However, it is Typed as a Tuple as
        it's elements do not have to homogenous. Therefore, we will use a Sequence and a Union, allowing
        to collect Exceptions as well.

        Args:
            return_exceptions: Recommend False for most instances. See asyncio.gather docstring for details

        Returns: Tuple of results collected

        """
        thing = await asyncio.gather(*self.tasks, return_exceptions=return_exceptions)
        return thing

    async def gather_threaded_results(self, return_exceptions: bool = True) -> Tuple[T | BaseException, ...]:
        """
        If you have elected for your Task to return a result, this will get them as a Sequence.

        gather() returns something that is, not striclty speaking, a List. However, it is Typed as a Tuple as
        it's elements do not have to homogenous. Therefore, we will use a Sequence and a Union, allowing
        to collect Exceptions as well.

        Args:
            return_exceptions: Recommend False for most instances. See asyncio.gather docstring for details

        Returns: Tuple of results collected

        """

        result = tuple(await asyncio.gather(*self.coros, return_exceptions=return_exceptions))
        return result

    async def clear_all_tasks(self) -> None:
        """
        Cancel all the tasks in this grouping. Mostly used when finished with a command or on restart to avoid orphans

        """
        for task in self.tasks:
            task.cancel()
        for coro in self.coros:
            coro.cancel()
        # Use return_exceptions set to True so all tasks actually are finished before exiting the system(or some
        # get left behind and keep running as orphans)
        await self.gather_results()
        await self.gather_threaded_results()


class ReactionTaskController(Generic[T]):
    """
    Now you can add reactions to a running command as a controlling mechanism, as well as group any Tasks that may be
    used for processing the command into a cancellable format.

    Add a ReactionTaskController() to your start() call on your plugin, and call shutdown() in the pre_stop() call.

    Attributes:
        tracked_reactions: Map of EventID of the response of a command that will have the reactions attached to the
            current Status of the task
        tasks_sets: Map of Hashable key for reference to a TaskSetEntry of associated Task objects used by the
            command
    """

    tracked_reactions: Dict[EventID, ReactionControlEntry]
    tasks_sets: Dict[Hashable, TaskSetEntry[T]]
    client: MaubotMatrixClient
    executor: ThreadPoolExecutor

    def __init__(self, client: MaubotMatrixClient, max_workers: int) -> None:
        self.tracked_reactions = {}
        self.tasks_sets = {}
        self.client = client
        self.executor = ThreadPoolExecutor(max_workers=max_workers)

    async def setup_control_reactions(
        self,
        pinned_message: EventID,
        command_event: MessageEvent,
        emoji: bool = False,
        default_starting_status: ReactionCommandStatus | EmojiReactionCommandStatus = ReactionCommandStatus.START,
    ) -> None:
        if pinned_message in self.tracked_reactions:
            raise MessageAlreadyHasReactions

        # The creation will place the starting status as STOP, make it a start instead
        control_entry = ReactionControlEntry(command_event, self.client, emoji, default_starting_status)
        await control_entry.setup(pinned_message)
        self.tracked_reactions[pinned_message] = control_entry

    def setup_task_set(
        self,
        reference_key: Optional[Hashable] = None,
    ) -> Hashable:
        if reference_key is None:
            # Use a nice large base for the random key
            reference_key = str(random.randint(0, 1024 * 1024)) + "-" + str(time.time())
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
        if pinned_message in self.tracked_reactions and (
            self.tracked_reactions[pinned_message].get_status() == ReactionCommandStatus.START
            or self.tracked_reactions[pinned_message].get_status() == EmojiReactionCommandStatus.START
        ):
            return True
        return False

    def is_paused(self, pinned_message: EventID) -> bool:
        if pinned_message in self.tracked_reactions and (
            self.tracked_reactions[pinned_message].get_status() == ReactionCommandStatus.PAUSE
            or self.tracked_reactions[pinned_message].get_status() == EmojiReactionCommandStatus.PAUSE
        ):
            return True
        return False

    def is_running(self, pinned_message: EventID) -> bool:
        if self.is_started(pinned_message) or self.is_paused(pinned_message):
            return True
        return False

    def is_stopped(self, pinned_message: EventID) -> bool:
        if pinned_message in self.tracked_reactions and (
            self.tracked_reactions[pinned_message].get_status() == ReactionCommandStatus.STOP
            or self.tracked_reactions[pinned_message].get_status() == EmojiReactionCommandStatus.STOP
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
                pinned_control_entry = self.tracked_reactions[pinned_message]
                await pinned_control_entry.add_cleanup_control(pinned_message, pinned_control_entry.emoji)
            self.tracked_reactions.pop(pinned_message, None)
        if isinstance(pinned_message, Hashable) and pinned_message in self.tasks_sets:
            await self.tasks_sets[pinned_message].clear_all_tasks()
            self.tasks_sets.pop(pinned_message)

    async def remove_last_display_of(self, event_id_to_remove: EventID, room_id: RoomID) -> None:
        await self.client.redact(
            room_id,
            event_id_to_remove,
            "Cleaning up screen real estate",
        )
        if event_id_to_remove in self.tracked_reactions:
            del self.tracked_reactions[event_id_to_remove]

    def add_tasks(
        self,
        reference_key: Hashable,
        new_task,
        *args,
        limit: int = 1,
        **kwargs,
    ) -> None:
        if reference_key not in self.tasks_sets:
            raise ReferenceKeyNotFound("Need to run setup_task_set() first")

        self.tasks_sets[reference_key].add_tasks(new_task, *args, limit=limit, **kwargs)

    async def add_threaded_tasks(
        self,
        reference_key: Hashable,
        new_task,
        *args,
        limit: int = 1,
        **kwargs,
    ) -> None:
        """
        Add a callable that can be awaited to a thread from the pool.

        Args:
            reference_key:
            new_task:
            *args:
            limit:

        Returns:

        """
        if reference_key not in self.tasks_sets:
            raise ReferenceKeyNotFound("Need to run setup_task_set() first")

        await self.tasks_sets[reference_key].add_threaded_tasks(
            new_task,
            *args,
            limit=limit,
            executor=self.executor,
            **kwargs,
        )

    @overload
    async def get_task_results(
        self,
        reference_key: Hashable,
        threaded: bool = False,
        return_exceptions: Literal[True] = True,
    ) -> Sequence[T | BaseException]:
        pass

    @overload
    async def get_task_results(
        self,
        reference_key: Hashable,
        threaded: bool = False,
        return_exceptions: Literal[False] = False,
    ) -> Sequence[T]:
        pass

    async def get_task_results(
        self,
        reference_key: Hashable,
        threaded: bool = False,
        return_exceptions: bool = True,
    ) -> Sequence[T | BaseException]:
        if threaded:
            return await self.tasks_sets[reference_key].gather_threaded_results(return_exceptions=return_exceptions)
        return await self.tasks_sets[reference_key].gather_results(return_exceptions=return_exceptions)

    async def add_cleanup_control(self, related_message: EventID, room_id: RoomID, emoji: bool = False) -> None:
        # Just sticking the reaction the handler will look for onto the message
        if emoji:
            await self.client.react(
                room_id,
                related_message,
                EmojiReactionCommandStatus.CLEANUP.value,
            )
        else:
            await self.client.react(
                room_id,
                related_message,
                ReactionCommandStatus.CLEANUP.value,
            )

    async def react_control_handler(self, react_evt: ReactionEvent) -> None:
        reaction_data = react_evt.content.relates_to
        # The first condition makes sure that the initial placement of the reactions is not registered
        if react_evt.sender != self.client.mxid and reaction_data.event_id is not None:
            if reaction_data.event_id in self.tracked_reactions:
                if reaction_data.key in (ReactionCommandStatus.STOP.value, EmojiReactionCommandStatus.STOP.value):
                    self.tracked_reactions[reaction_data.event_id].stop()
                elif reaction_data.key in (ReactionCommandStatus.PAUSE.value, EmojiReactionCommandStatus.PAUSE.value):
                    self.tracked_reactions[reaction_data.event_id].pause()
                elif reaction_data.key in (ReactionCommandStatus.START.value, EmojiReactionCommandStatus.START.value):
                    self.tracked_reactions[reaction_data.event_id].start()

            if reaction_data.key in (ReactionCommandStatus.CLEANUP.value, EmojiReactionCommandStatus.CLEANUP.value):
                await self.remove_last_display_of(reaction_data.event_id, react_evt.room_id)

        return
