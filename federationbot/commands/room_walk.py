"""
Provide room history walking functionality.

This module implements commands for walking through a Matrix room's event history,
discovering and analyzing events in both forward and backward directions. It supports
progress tracking and detailed event analysis.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
import asyncio
import time

from maubot.handlers import command
from mautrix.errors.request import MatrixRequestError
from mautrix.types import EventID, PaginatedMessages, PaginationDirection, RoomID, SyncToken
from mautrix.util.logging import TraceLogger

from federationbot.constants import BACKOFF_MULTIPLIER, SECONDS_BETWEEN_EDITS
from federationbot.errors import FedBotException
from federationbot.utils.formatting import wrap_in_code_block_markdown
from federationbot.utils.matrix import get_domain_from_id, make_into_text_event

from .common import FederationBotCommandBase

if TYPE_CHECKING:
    from .common import MessageEvent

logger = TraceLogger("federationbot.commands.room_walk")


class RoomWalkCommand(FederationBotCommandBase):
    """
    Add room history walking capabilities to FederationBot.

    This mixin provides commands for walking through a room's event history,
    supporting both forward discovery and backward walking phases. It includes
    progress tracking, event analysis, and detailed reporting.
    """

    @command.new(name="test", help="playing", arg_fallthrough=True)
    async def test_command(self, command_event: MessageEvent) -> None:
        """
        Handle test command for development purposes.

        Args:
            command_event: The event that triggered this command
        """
        await command_event.respond(f"Received Test Command on: {self.client.mxid}")

    async def _room_walk_inner_fetcher(
        self,
        for_direction: PaginationDirection,
        queue: asyncio.Queue[tuple[float, SyncToken | None]],
        room_to_check: str,
        per_iteration_int: int,
        response_list: list[tuple[float, PaginatedMessages]],
    ) -> None:
        """
        Fetch room events in a specified direction with backoff handling.

        Continuously fetches events from the room, handling rate limiting through
        exponential backoff. Results are stored in the provided response list.

        Args:
            for_direction: Direction to fetch events (forward/backward)
            queue: Queue managing fetch tokens and backoff timing
            room_to_check: Room ID to fetch events from
            per_iteration_int: Number of events to fetch per iteration
            response_list: List to store fetched responses with timing information
        """
        retry_token = False
        back_off_time = 0.0
        next_token = None

        while True:
            if not retry_token:
                back_off_time, next_token = await queue.get()

            if back_off_time > 1.0:
                self.log.warning("Backing off for %s", back_off_time)
                await asyncio.sleep(back_off_time)

            try:
                iter_start_time = time.time()
                worker_response = await self.client.get_messages(
                    room_id=RoomID(room_to_check),
                    direction=for_direction,
                    from_token=next_token,
                    limit=per_iteration_int,
                )
                iter_finish_time = time.time()
            except MatrixRequestError as e:
                self.log.warning("%s", e)
                retry_token = True
            else:
                retry_token = False
                iter_time_spent = iter_finish_time - iter_start_time
                response_list.extend([(iter_time_spent, worker_response)])

                if worker_response.end:
                    queue.put_nowait((iter_time_spent * BACKOFF_MULTIPLIER, worker_response.end))

                queue.task_done()

    @staticmethod
    async def _room_walk_update_progress(
        command_event: MessageEvent,
        pinned_message: EventID,
        header_lines: list[str],
        static_lines: list[str],
        collection_of_event_ids: set[EventID],
        cumulative_time: float,
        iterations: int,
        progress_line: str = "",
        extra_lines: list[str] | None = None,
    ) -> None:
        """
        Update the progress message with current status.

        Updates a pinned message in the room with the current progress of the
        room walk operation, including timing and event count information.

        Args:
            command_event: Event that triggered the command
            pinned_message: ID of message to update
            header_lines: Header lines for the progress message
            static_lines: Static lines that don't change
            collection_of_event_ids: Set of event IDs found
            cumulative_time: Total time spent processing
            iterations: Number of iterations completed
            progress_line: Optional progress indicator line
            extra_lines: Optional additional status lines
        """
        lines = [
            *header_lines,
            *static_lines,
            f"Events found: {len(collection_of_event_ids)}",
            f"  Time taken: {cumulative_time:.3f} seconds (iter# {iterations})",
        ]

        if progress_line:
            lines.append(progress_line)
        if extra_lines:
            lines.extend(extra_lines)

        await command_event.respond(
            make_into_text_event(
                wrap_in_code_block_markdown("\n".join(lines)),
            ),
            edits=pinned_message,
        )

    async def _room_walk_handle_processing_loop(
        self,
        direction: PaginationDirection,
        room_to_check: str,
        per_iteration_int: int,
        command_event: MessageEvent,
        pinned_message: EventID,
        header_lines: list[str],
        static_lines: list[str],
        discovery_collection_of_event_ids: set[EventID] | None = None,
        room_depth: int | None = None,
        is_final_phase: bool = False,
    ) -> set[EventID]:
        """
        Process room events in a loop for discovery or backwalk phases.

        Manages the event processing loop, handling both the initial discovery phase
        and the subsequent backwalk phase. Updates progress and collects event statistics.

        Args:
            direction: Direction to walk events (forward/backward)
            room_to_check: Room ID to process
            per_iteration_int: Number of events per iteration
            command_event: Original command event
            pinned_message: Message ID to update with progress
            header_lines: Header lines for progress display
            static_lines: Static lines for progress display
            discovery_collection_of_event_ids: Optional set of discovered events
            room_depth: Optional room depth for progress calculation
            is_final_phase: Whether this is the final phase

        Returns:
            Set of event IDs found during this processing phase
        """
        iterations = 0
        cumulative_iter_time = 0.0
        collection_of_event_ids: set[EventID] = set()
        count_of_new_event_ids = 0
        response_list: list[tuple[float, PaginatedMessages]] = []
        fetch_queue: asyncio.Queue[tuple[float, SyncToken | None]] = asyncio.Queue()

        task = asyncio.create_task(
            self._room_walk_inner_fetcher(
                direction,
                fetch_queue,
                room_to_check,
                per_iteration_int,
                response_list,
            ),
        )
        fetch_queue.put_nowait((0.0, None))

        try:
            while True:
                new_responses_to_work_on = response_list.copy()
                response_list.clear()

                # Process responses
                new_event_ids = {
                    event.event_id for time_spent, response in new_responses_to_work_on for event in response.events
                }
                cumulative_iter_time += sum(time_spent for time_spent, _ in new_responses_to_work_on)
                iterations += len(new_responses_to_work_on)
                finish = any(not response.end for _, response in new_responses_to_work_on)

                if discovery_collection_of_event_ids is not None:
                    # Backwalk phase
                    new_events_this_iteration = new_event_ids - collection_of_event_ids
                    new_events_vs_discovery = new_event_ids - discovery_collection_of_event_ids
                    count_of_new_event_ids += len(new_events_vs_discovery)
                    collection_of_event_ids.update(new_event_ids)

                    progress_line = f"{len(collection_of_event_ids)} of {room_depth}"
                    extra_lines = [
                        f"Received Events during backwalk: {len(collection_of_event_ids)}",
                        f"New Events found during backwalk: {count_of_new_event_ids}",
                        f"  Time taken: {cumulative_iter_time:.3f} seconds (iter# {iterations})",
                        f"  Events found this iter: ({len(new_events_this_iteration)})",
                    ]
                    if is_final_phase and finish:
                        header_lines = ["Room Back-walking Procedure: Done"]
                        extra_lines.append("Done")
                else:
                    # Discovery phase
                    collection_of_event_ids.update(new_event_ids)
                    progress_line = ""
                    extra_lines = None

                if new_responses_to_work_on or finish:
                    await self._room_walk_update_progress(
                        command_event,
                        pinned_message,
                        header_lines,
                        static_lines,
                        collection_of_event_ids,
                        cumulative_iter_time,
                        iterations,
                        progress_line,
                        extra_lines,
                    )

                if finish:
                    break

                await asyncio.sleep(SECONDS_BETWEEN_EDITS)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        return collection_of_event_ids

    @test_command.subcommand(
        name="room_walk",
        help="Use the /message client endpoint to force fresh state download(beta).",
    )
    @command.argument(name="room_id_or_alias", required=False)
    @command.argument(name="per_iteration", required=False)
    async def room_walk_command(
        self,
        command_event: MessageEvent,
        room_id_or_alias: str | None,
        per_iteration: str = "1000",
    ) -> None:
        """
        Walk through a room's history to discover and analyse events.

        Performs a two-phase room walk:
        1. Discovery phase: Forward walk to find known events
        2. Backwalk phase: Backward walk to find any missed events

        Progress is displayed through a continuously updated message showing:
        - Room depth and current progress
        - Number of events discovered
        - Time taken for each phase
        - New events found during backwalk

        Args:
            command_event: The event that triggered this command
            room_id_or_alias: Optional room ID or alias to walk through
            per_iteration: Number of events to fetch per iteration (default: 1000)

        Note:
            The command can only be run by users on the same server as the bot,
            and requires proper server signing keys to be configured.
        """
        await command_event.mark_read()

        # Validate command context
        origin_server = get_domain_from_id(self.client.mxid)

        # Check user is on same server as bot
        if get_domain_from_id(command_event.sender) != get_domain_from_id(self.client.mxid):
            await command_event.reply(
                "I'm sorry, running this command from a user not on the same server as the bot will not help",
            )
            return

        # Verify bot has necessary signing keys
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first.",
            )
            return

        # Resolve room ID and validate per_iteration
        room_to_check, _ = await self.resolve_room_id_or_alias(
            room_id_or_alias or command_event.room_id,
            command_event,
            origin_server,
        )
        if not room_to_check:
            return

        try:
            per_iteration_int = int(per_iteration)
        except ValueError:
            await command_event.reply("per_iteration must be an integer")
            return

        # Need:
        # *1. to get the current depth for the room, so we have an idea how many events
        # we need to collect
        # *2. progress bars for backwalk based on depth
        # *3. total count of:
        #    a. discovered events
        #    b. new events found on backwalk
        # 4. itemized display of type of events found, and that are new
        # 5. total time spent on discovery and backwalk
        # 6. rolling time spent on backwalk requests, or maybe just fastest and longest
        # 7. event count of what is new

        # Want it to look like:
        #
        # Room depth reported as: 45867
        # Events found during discovery: 38757
        #   Time taken: 50 seconds
        # [|||||||||||||||||||||||||||||||||||||||||||||||||] 100%
        # New Events found during backwalk: 0(0 State)
        #   Time taken: 120 seconds

        # Get room depth
        try:
            room_depth = await self.get_room_depth(origin_server, room_to_check, command_event)
        except FedBotException as e:
            await command_event.respond(str(e))
            return

        # Setup initial progress display
        header_lines = ["Room Back-walking Procedure: Running"]
        static_lines = [
            "--------------------------",
            f"Room Depth reported as: {room_depth}",
        ]

        pinned_message = cast(
            "EventID",
            await command_event.respond(
                make_into_text_event(
                    wrap_in_code_block_markdown("\n".join(header_lines + static_lines)),
                ),
            ),
        )

        # Run discovery phase
        discovery_collection_of_event_ids = await self._room_walk_handle_processing_loop(
            PaginationDirection.FORWARD,
            room_to_check,
            per_iteration_int,
            command_event,
            pinned_message,
            header_lines,
            static_lines,
        )

        # Run backwalk phase with final update
        await self._room_walk_handle_processing_loop(
            PaginationDirection.BACKWARD,
            room_to_check,
            per_iteration_int,
            command_event,
            pinned_message,
            header_lines,
            static_lines,
            discovery_collection_of_event_ids,
            room_depth,
            is_final_phase=True,
        )
