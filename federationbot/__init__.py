"""The main module for the FederationBot plugin."""

from __future__ import annotations

from typing import Any, Collection, Sequence, cast
from asyncio import QueueEmpty
from contextlib import suppress
from itertools import chain
import asyncio
import hashlib
import json
import random
import time

from canonicaljson import encode_canonical_json
from maubot.handlers import command
from mautrix.errors.request import MForbidden, MTooLarge
from mautrix.types import EventID, RoomID
from more_itertools import partition
from unpaddedbase64 import encode_base64

from federationbot.commands.room_walk import RoomWalkCommand
from federationbot.constants import (
    BACKOFF_MULTIPLIER,
    MAX_NUMBER_OF_SERVERS_TO_ATTEMPT,
    NOT_IN_ROOM_ERROR,
    SECONDS_BETWEEN_EDITS,
    SERVER_NAME,
    SERVER_SOFTWARE,
    SERVER_VERSION,
)
from federationbot.controllers import EmojiReactionCommandStatus
from federationbot.events import CreateRoomStateEvent, Event, EventBase, EventError, redact_event
from federationbot.protocols import MessageEvent
from federationbot.resolver import (
    Diagnostics,
    ServerDiscoveryDnsResult,
    ServerDiscoveryResult,
    WellKnownDiagnosticResult,
    WellKnownLookupFailure,
    WellKnownLookupResult,
)
from federationbot.responses import MatrixError, MatrixFederationResponse, MatrixResponse, RoomHeadData
from federationbot.utils.bitmap_progress import BitmapProgressBar, BitmapProgressBarStyle
from federationbot.utils.colors import Colors
from federationbot.utils.display import DisplayLineColumnConfig, Justify, pad
from federationbot.utils.formatting import (
    add_color,
    bold,
    combine_lines_to_fit_event,
    combine_lines_to_fit_event_html,
    wrap_in_code_block_markdown,
    wrap_in_details,
)
from federationbot.utils.matrix import (
    get_domain_from_id,
    is_event_id,
    is_mxid,
    is_room_id,
    is_room_id_or_alias,
    make_into_text_event,
)
from federationbot.utils.numbers import is_int, round_half_up
from federationbot.utils.time import pretty_print_timestamp

json_decoder = json.JSONDecoder()


class FederationBot(RoomWalkCommand):
    """The main class for the FederationBot plugin."""

    @command.new(
        name="status",
        help="playing",
        arg_fallthrough=True,
    )
    async def status_command(
        self,
        command_event: MessageEvent,
    ) -> None:
        """
        Display bot status information.

        Shows cache sizes, task counts, and other runtime statistics.
        Updates the display periodically until stopped.

        Args:
            command_event: The event that triggered the command
        """
        pinned_message = cast("EventID", await command_event.respond(f"Received Status Command on: {self.client.mxid}"))
        await self.reaction_task_controller.setup_control_reactions(
            pinned_message,
            command_event,
            emoji=True,
            default_starting_status=EmojiReactionCommandStatus.START,
        )

        finish_on_this_round = False
        while True:
            if self.reaction_task_controller.is_stopped(pinned_message):
                finish_on_this_round = True

            if self.reaction_task_controller.is_paused(pinned_message):
                # A pause just means not adding anything to the screen, until restarted
                await asyncio.sleep(1)
                continue

            # TODO: Lose this after Tom saw
            good_server_results, bad_server_results = partition(
                lambda x: x.cache_value.unhealthy is not None,
                self.federation_handler.api.server_discovery_cache._cache.values(),
            )
            buffered_line = (
                f"Event Cache size: {len(self.federation_handler._events_cache)}\n"
                f"Room Version Cache size: {len(self.federation_handler.room_version_cache)}\n"
                f"New server_result cache: {len(self.federation_handler.api.server_discovery_cache)}\n"
                # TODO: Lose this after Tom seen it(engrish is grate)
                f" Good server results: {len([1 for _ in good_server_results])}\n"
                f" Bad server results: {len([1 for _ in bad_server_results])}\n"
                f"Server Signing keys: {len(self.federation_handler._server_keys_cache)}\n"
                f"Reaction Task Controller:\n"
                f" Number of task sets: {len(self.reaction_task_controller.tasks_sets)}\n"
                f" Number of commands with tracked reactions: {len(self.reaction_task_controller.tracked_reactions)}\n"
            )
            await command_event.respond(
                make_into_text_event(wrap_in_code_block_markdown(buffered_line)),
                edits=pinned_message,
            )

            if finish_on_this_round:
                break
            await asyncio.sleep(5.0)

        await self.reaction_task_controller.cancel(pinned_message, True)

    @command.new(
        name="test",
        help="playing",
        arg_fallthrough=True,
    )
    async def test_command(
        self,
        command_event: MessageEvent,
    ) -> None:
        """
        Handle test commands.

        A simple test command that confirms the bot is responding.

        Args:
            command_event: The event that triggered the command
        """
        await command_event.respond(f"Received Test Command on: {self.client.mxid}")

    @command.new(name="fed", help="`!fed`: Federation requests for information")
    async def fed_command(self, command_event: MessageEvent) -> None:
        """
        Handle federation info commands.

        Parent command for federation-related subcommands.
        Does nothing on its own.

        Args:
            command_event: The event that triggered the command
        """
        pass

    @test_command.subcommand(name="color", help="Test color palette and layout")
    async def color_subcommand(self, command_event: MessageEvent) -> None:
        """
        Test color formatting and display.

        Shows test messages with different color combinations to verify
        color formatting is working correctly.

        Args:
            command_event: The event that triggered the command
        """
        await command_event.mark_read()
        test_message_list = []
        test_message_list.extend(["OKAY"])
        test_message_list.extend(["WARN"])
        test_message_list.extend(["ERROR"])

        await command_event.respond(
            make_into_text_event(
                combine_lines_to_fit_event_html(test_message_list, [""])[0],
                allow_html=True,
            ),
            allow_html=True,
        )
        test_message_list = []
        test_message_list.extend([add_color(bold("OKAY"), foreground=Colors.WHITE, background=Colors.GREEN)])
        test_message_list.extend([add_color(bold("WARN"), foreground=Colors.BLACK, background=Colors.YELLOW)])
        test_message_list.extend([add_color(bold("ERROR"), foreground=Colors.WHITE, background=Colors.RED)])

        await command_event.respond(
            make_into_text_event(
                combine_lines_to_fit_event_html(test_message_list, [""])[0],
                allow_html=True,
            ),
            allow_html=True,
        )

    @test_command.subcommand(
        name="context",
        help="test /context federation command",
    )
    @command.argument(name="room_id_or_alias", parser=is_room_id, required=True)
    @command.argument(name="event_id", parser=is_event_id, required=True)
    @command.argument(name="limit", required=False)
    async def context_subcommand(
        self,
        command_event: MessageEvent,
        room_id_or_alias: str,
        event_id: str,
        limit: str,
    ) -> None:
        """
        Get event context from federation API.

        Retrieves events before and after the specified event.

        Args:
            command_event: The event that triggered the command
            room_id_or_alias: Room ID or alias to query
            event_id: Event ID to get context around
            limit: Maximum number of events to retrieve
        """
        stuff = await self.client.get_event_context(
            room_id=RoomID(room_id_or_alias),
            event_id=EventID(event_id),
            limit=int(limit),
        )
        await command_event.respond(stuff.json())

    @test_command.subcommand(name="alias", help="Get raw room alias json from another server")
    @command.argument(name="room_id_or_alias", parser=is_room_id_or_alias, required=True)
    @command.argument(name="target_server", required=False)
    async def alias_subcommand(
        self,
        command_event: MessageEvent,
        room_id_or_alias: str,
        target_server: str | None,
    ) -> None:
        """
        Look up room alias information.

        Gets room alias directory information from a target server.

        Args:
            command_event: The event that triggered the command
            room_id_or_alias: Room alias to look up
            target_server: Optional server to query, defaults to alias's server
        """
        origin_server = self.federation_handler.hosting_server
        destination_server = target_server or room_id_or_alias.split(":", maxsplit=1)[1]

        # The parser in the command should have caught this, but it may have bumped it to the target_server(unlikely)
        if not room_id_or_alias.startswith("#"):
            await command_event.reply("I need a room alias not a room id")
            return

        stuff = await self.federation_handler.api.get_room_alias_from_directory(
            origin_server,
            destination_server,
            room_id_or_alias,
        )
        await command_event.respond(wrap_in_code_block_markdown(json.dumps(stuff.json_response, indent=4)))

    @test_command.subcommand(
        name="room_walk2",
        help="Use the federation api to try and selectively download events that are missing(beta).",
    )
    @command.argument(name="room_id_or_alias", parser=is_room_id_or_alias, required=False)
    @command.argument(name="server_to_fix", required=False)
    async def room_walk_2_command(
        self,
        command_event: MessageEvent,
        room_id_or_alias: str | None,
        server_to_fix: str | None,
    ) -> None:
        """
        Use federation API to selectively download missing events.

        Args:
            command_event: The event that triggered the command
            room_id_or_alias: Room ID or alias to walk through
            server_to_fix: Optional server to target for fixes

        Raises:
            ValueError: If the room ID or alias is not valid
            TypeError: If the event ID is not a string
        """
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        if get_domain_from_id(command_event.sender) != get_domain_from_id(self.client.mxid):
            await command_event.reply(
                "I'm sorry, running this command from a user not on the same server as the bot will not help",
            )
            return

        origin_server = await self.get_origin_server_and_assert_key_exists(command_event)
        if not origin_server:
            return

        destination_server = server_to_fix or get_domain_from_id(command_event.sender)

        # Sort out the room id
        if room_id_or_alias:
            room_id, _ = await self.resolve_room_id_or_alias(room_id_or_alias, command_event, origin_server)
            if not room_id:
                # Don't need to actually display an error, that's handled in the above
                # function
                return
        else:
            # with server_to_check being set, this will be ignored any way
            room_id = command_event.room_id

        # Need:
        # *1. Initial Event ID to start at. Want the depth from that Event to base
        #    progress bars on
        # *2. progress bars based on depth progress, note this is going backwards. Depth
        #    and prev_events won't always have a correlation(remember the ping room)
        # 3. Count of:
        #    *a. All Events seen
        #    *b. Events just with errors
        #    *c. Events that had errors but were found on other hosts in the room
        # 4. Times for:
        #    *a. Time spent on roomwalk as a whole
        #    b. Time spent on last request
        #    c. Time spent in backoff
        # 5. Allow-list for users to block bad actors

        # Definitions for below:
        # targeted server: The server that is missing events
        # donor server: The arbitrary server that has the events
        # The process heuristic:
        # 1. Retrieve Event for event_id from target server
        # 2. Parse out prev_events and auth_events from that original Event
        #    A. Attempt retrieval of these ancestor Events from target server, add new
        #       prev_events(but not auth_events) to Queue and back to (1) if found.
        #    B. Otherwise:
        #       i. Use roomwalk directly on that Event ID to try and get the Events from
        #          federation.
        #          A. Verify signatures
        #          B. Pull prev_events from THAT Event to verify the targeted server has
        #             those, otherwise check federation again
        #          C. Add to List of Events to send to target server.
        #          D. Repeat until all prev_events have been found and verified
        #       ii. Reverse the List(so the oldest is seen first) then use the
        #           federation /send endpoint on the target server to receive the Event.
        #       iii. Re-add original Event to Queue to retry(Back to (1)). It should
        #            succeed this time.
        #       iv. Failure at that last stage means there is probably a state auth
        #           problem.
        # Notes: If an Event has multiple prev_events, check them all before moving on.
        # They should all collectively have the same prev_event as a batch

        event_id_ok_list: set[str] = set()
        event_id_error_list: set[str] = set()
        event_id_resolved_error_list: set[str] = set()
        event_id_attempted_once: set[str] = set()

        async def _parse_ancestor_events(
            worker_name: str,
            event_id_list_of_ancestors: list[str],
        ) -> tuple[set[str], set[str]]:
            """
            Condense and parse given Event IDs from the prev_event and auth_event fields
            into batches representing found and not found events on target server.

            Args:
                worker_name: String name for logging
                event_id_list_of_ancestors: Event IDs to look for

            Returns:
                Tuple of (events_found, events_not_found) as sets of event IDs
            """
            next_batch: set[str] = set()
            error_batch: set[str] = set()

            pulled_event_map = await self.federation_handler.get_events_from_server(
                origin_server=origin_server,
                destination_server=destination_server,
                events_list=event_id_list_of_ancestors,
            )
            # This is to provide a nice reference, in case an event was not found
            for _event_id in event_id_list_of_ancestors:
                pulled_event = pulled_event_map.get(_event_id)
                if isinstance(pulled_event, EventError):
                    error_batch.add(_event_id)

                else:
                    next_batch.add(_event_id)

            return next_batch, error_batch

        # Get the last event that was in the room, for its depth and as a starting spot
        now = int(time.time() * 1000)
        # TODO: swap this out for 'fed head'
        ts_response = await self.federation_handler.api.get_timestamp_to_event(
            origin_server=origin_server,
            destination_server=origin_server,
            room_id=room_id,
            utc_time_at_ms=now,
        )
        if ts_response.http_code != 200:
            await command_event.respond(
                f"Something went wrong while getting last event in room\n* {ts_response.reason}",
            )
            return

        pinned_message = cast(
            "EventID",
            await command_event.respond(
                make_into_text_event(wrap_in_code_block_markdown("Just a moment while I prepare a few things\n")),
            ),
        )
        # The initial starting point for the room walk
        event_id = ts_response.json_response.get("event_id", None)
        if not isinstance(event_id, str):
            msg = "event_id must be a string"
            raise TypeError(msg)
        event_result = await self.federation_handler.get_event_from_server(origin_server, origin_server, event_id)
        event = event_result.get(event_id, None)
        if not event:
            msg = "event must be present in result"
            raise ValueError(msg)
        room_depth = event.depth
        seen_depths_for_progress = set()

        # Prep the host list, just in case we need it later on so the worker doesn't
        # have to do it on-demand, increasing its complexity
        host_list = await self.federation_handler.get_hosts_in_room_ordered(
            origin_server,
            destination_server,
            room_id,
            event_id,
        )

        good_host_list: list[str] = []
        # This will act as a prefetch to prime the server result cache, which can be
        # then checked directly for hosts which are not online
        _ = await self._get_versions_from_servers(host_list)

        for host in host_list:
            server_check = self.federation_handler.api.server_discovery_cache.get(host, None)
            if server_check and server_check.unhealthy is None:
                good_host_list.extend((host,))
            else:
                self.log.warning("not using %s for room_walk2", host)
        # Can now use good_host_list as an ordered list of servers to check for Events

        # Initial messages and lines setup. Never end in newline, as the helper handles
        header_lines = ["Room Back-walking Procedure: Running"]
        static_lines = []
        static_lines.extend(["------------------------------------"])
        static_lines.extend([f"Room Depth reported as: {room_depth}"])

        discovery_lines: list[str] = []
        progress_bar = BitmapProgressBar(30, room_depth)
        progress_line = progress_bar.render_bitmap_bar()
        roomwalk_lines: list[str] = []

        def _combine_lines_for_backwalk() -> str:
            """
            Combine different sections of lines into a single formatted string.

            Combines header lines, static lines, discovery lines, progress bar,
            and roomwalk lines with appropriate newlines.

            Returns:
                Combined string with all sections formatted with newlines
            """
            combined_lines = ""
            for line in header_lines:
                combined_lines += line + "\n"
            for line in static_lines:
                combined_lines += line + "\n"
            for line in discovery_lines:
                combined_lines += line + "\n"
            combined_lines += progress_line + "\n"
            nonlocal roomwalk_lines
            for line in roomwalk_lines:
                combined_lines += line + "\n"

            return combined_lines

        # Begin the render, replace the original message
        await command_event.respond(
            make_into_text_event(wrap_in_code_block_markdown(_combine_lines_for_backwalk())),
            edits=pinned_message,
        )

        await self.reaction_task_controller.setup_control_reactions(pinned_message, command_event)

        bot_working: dict[str, bool] = {}

        async def _event_walking_fetcher(
            worker_name: str,
            _event_fetch_queue: asyncio.Queue[tuple[float, bool, set[str]]],
            _event_error_queue: asyncio.Queue[str],
        ) -> None:
            while True:
                if self.reaction_task_controller.is_stopped(pinned_message):
                    break

                # Use a set for this, as sometimes prev_events ARE auth_events
                next_list_to_get = set()
                total_time_spent = 0.0
                # next_event_ids is the previous iterations next_list_to_get. Usually
                # there will be only one item in that sequence
                (
                    back_off_time,
                    is_this_a_retry,
                    next_event_ids,
                ) = await _event_fetch_queue.get()

                bot_working[worker_name] = True

                # If this event_id has already been through analysis, no need to
                # again. A good example of how this helps: In the ping room, each bot
                # that sends a 'pong' will have the invocation as a prev_event. The
                # next thing sent into the room will have all those 'pong' responses
                # as prev_events. However, if this is a retry because it wasn't
                # available the first time around...
                if not is_this_a_retry:
                    # Filter out already analyzed event_ids
                    next_event_ids.difference_update(event_id_attempted_once)

                # But still add it to the pile so that we don't do it twice
                event_id_attempted_once.update(next_event_ids)

                iter_start_time = time.time()
                if next_event_ids:
                    pulled_event_map = await self.federation_handler.get_events_from_server(
                        origin_server=origin_server,
                        destination_server=destination_server,
                        events_list=next_event_ids,
                    )
                else:
                    # This way, if there was nothing to do after being filtered out, it
                    # should just fall through to task_done()
                    pulled_event_map = {}

                for next_event_id in next_event_ids:
                    pulled_event = pulled_event_map.get(next_event_id, None)

                    # pulled_event should never be None, but mypy doesn't know that
                    if pulled_event is not None and not isinstance(pulled_event, EventError):
                        if is_this_a_retry:
                            # This should only be hit after a backfill attempt, and
                            # means the second try succeeded.
                            self.log.info("%s: Hit a Resolved Error on %s", worker_name, next_event_id)
                            event_id_resolved_error_list.add(next_event_id)

                        seen_depths_for_progress.add(pulled_event.depth)
                        prev_events = pulled_event.prev_events
                        auth_events = pulled_event.auth_events
                        event_id_ok_list.add(next_event_id)

                        (
                            prev_good_events,
                            prev_bad_events,
                        ) = await _parse_ancestor_events(
                            worker_name,
                            prev_events,
                        )

                        # We don't iterate the walk based on auth_events themselves,
                        # eventually we'll find them in prev_events. At worst, this is a
                        # prefetch for the cache.
                        (
                            auth_good_events,
                            auth_bad_events,
                        ) = await _parse_ancestor_events(
                            worker_name,
                            auth_events,
                        )

                        for _event_id in chain(prev_bad_events, auth_bad_events):
                            if _event_id not in event_id_error_list:
                                # Because it's already been tried in this case
                                _event_error_queue.put_nowait(_event_id)
                                event_id_error_list.add(_event_id)
                        # Prep for next iteration. Don't worry about adding auth events
                        # to this, as they will come along in due time
                        # TODO: maybe add a backfill of two at this point, to skip over gaps?
                        if not prev_good_events:
                            self.log.warning(
                                "%s: Unexpectedly found an empty prev_good_events on %s", worker_name, next_event_id
                            )
                        next_list_to_get.update(prev_good_events)
                        next_list_to_get.update(auth_good_events)

                    else:
                        # is an EventError. Chances of hitting this are extremely low,
                        # in fact it may only happen on the initial pull to start a walk.
                        # All other opportunities to hit this will have been handled in the
                        # above filter function.
                        # TODO: not any more they aren't
                        self.log.warning("hit an EventError when shouldn't have: %s", next_event_id)

                _time_spent = time.time() - iter_start_time
                total_time_spent += _time_spent

                if next_list_to_get:
                    # The queue item is:
                    # (new_back_off_time, is_this_a_retry, next_event_ids)
                    # Note that BACKOFF_MULTIPLIER is a reducing multiplier
                    _event_fetch_queue.put_nowait(
                        (
                            _time_spent * BACKOFF_MULTIPLIER,
                            False,
                            next_list_to_get,
                        )
                    )

                # Tuple of time spent(for calculating backoff) and if we are done
                render_list.extend([(total_time_spent, False)])

                _event_fetch_queue.task_done()
                bot_working[worker_name] = False

        room_version_of_found_event = "1"
        local_set_of_events_already_tried = set()

        async def _room_repair_worker(
            worker_name: str,
            room_version: str,
            _event_fetch_queue: asyncio.Queue[tuple[float, bool, set[str]]],
            _event_error_queue: asyncio.Queue[str],
        ) -> None:
            """
            Responsible for hunting down an Event(by its ID) and walking its
            prev_events looking for additionals that are missing. Then sending these in
            reverse order to the target server.

            Args:
                worker_name:
                room_version:
                _event_fetch_queue:
                _event_error_queue:

            Returns:

            """
            while True:
                if self.reaction_task_controller.is_stopped(pinned_message):
                    break

                next_event_id = await _event_error_queue.get()
                bot_working[worker_name] = True

                if next_event_id in event_id_ok_list:
                    self.log.warning(
                        "%s: Unexpectedly found room walk fetch event in OK list %s", worker_name, next_event_id
                    )
                if next_event_id in event_id_resolved_error_list:
                    self.log.warning(
                        "%s: Unexpectedly found room walk fetch event in RESOLVED list %s", worker_name, next_event_id
                    )

                # start_time = time.time()

                list_of_server_and_event_id_to_send = []
                already_searching_for_event_id = set()

                # These will be local queues to organize what this worker is currently working on.
                event_ids_to_try_next: asyncio.Queue[str] = asyncio.Queue()
                event_ids_to_try_next.put_nowait(next_event_id)
                done = False
                while not done:
                    try:
                        popped_event_id = event_ids_to_try_next.get_nowait()
                    except QueueEmpty:
                        # Nothing new, must be done
                        break

                    if self.reaction_task_controller.is_stopped(pinned_message):
                        break

                    # Let's not repeat something locally
                    if popped_event_id in local_set_of_events_already_tried:
                        continue
                    local_set_of_events_already_tried.add(popped_event_id)
                    # self.log.info(f"{worker_name}: looking at {popped_event_id}")
                    # 4. Check the found Event for ancestor Events that are not on the
                    #    target server
                    server_to_event_result_map = await self.federation_handler.find_event_on_servers(
                        origin_server,
                        popped_event_id,
                        good_host_list,
                    )

                    for server_name, event_base in server_to_event_result_map.items():
                        # But first, verify the events are valid
                        if isinstance(event_base, Event):
                            if not room_version:
                                try:
                                    room_version = await self.federation_handler.discover_room_version(
                                        origin_server,
                                        server_name,
                                        event_base.room_id,
                                    )
                                except MatrixError:
                                    pass  # until find something better. Should move this up anyways
                            await self.federation_handler.verify_signatures_and_annotate_event(event_base, room_version)

                    count_of_how_many_servers_tried = 0
                    for server_name in good_host_list:
                        _event_base = server_to_event_result_map.get(server_name)
                        count_of_how_many_servers_tried += 1
                        if isinstance(_event_base, Event):
                            # Only use it if it's verified, otherwise it will fail on send
                            if _event_base.signatures_verified:
                                # 5. Add to a List, so we can clearly walk backwards
                                # (filling them in order)
                                list_of_server_and_event_id_to_send.append((_event_base,))
                                self.log.info(
                                    "%s: found %s on " "%s after " "%s other servers",
                                    worker_name,
                                    popped_event_id,
                                    server_name,
                                    count_of_how_many_servers_tried - 1,
                                )
                                # But, do we need more

                                (
                                    prev_good_events,
                                    prev_bad_events,
                                ) = await _parse_ancestor_events(worker_name, _event_base.prev_events)
                                (
                                    auth_good_events,
                                    auth_bad_events,
                                ) = await _parse_ancestor_events(worker_name, _event_base.auth_events)
                                for _ancestor_event_id in prev_bad_events:
                                    if _ancestor_event_id in already_searching_for_event_id:
                                        # Already tried searching for this
                                        continue
                                    self.log.warning(
                                        "%s: for: " "%s, need prev_event: %s",
                                        worker_name,
                                        _event_base.event_id,
                                        _ancestor_event_id,
                                    )
                                    already_searching_for_event_id.add(_ancestor_event_id)

                                    event_ids_to_try_next.put_nowait(_ancestor_event_id)

                                for _ancestor_event_id in auth_bad_events:
                                    if _ancestor_event_id in already_searching_for_event_id:
                                        # Already tried searching for this
                                        continue
                                    self.log.warning(
                                        "%s: for: " "%s, need auth_event: %s",
                                        worker_name,
                                        _event_base.event_id,
                                        _ancestor_event_id,
                                    )
                                    already_searching_for_event_id.add(_ancestor_event_id)

                                    event_ids_to_try_next.put_nowait(_ancestor_event_id)

                                if not prev_bad_events and not auth_bad_events:
                                    self.log.info(
                                        "%s: All events for %s were found locally", worker_name, _event_base.event_id
                                    )
                                # The event was found, we can skip the rest of the host list on this iteration

                            else:
                                self.log.warning(
                                    "%s: Event did not pass signature check, %s", worker_name, _event_base.event_id
                                )
                        # else:
                        #     self.log.warning(
                        #         f"{worker_name}: Event in server_to_event_result_map was unexpectedly an EventError "
                        #         f"from {server_name}"
                        #     )

                    event_ids_to_try_next.task_done()
                    # 6. Repeat from 3 until no more ancestor Events are found that are missing
                    if event_ids_to_try_next.qsize() == 0:
                        # should be done
                        done = False

                event_sent = False
                # Need to do these in reverse, or the destination server will barf
                list_of_server_and_event_id_to_send.reverse()
                self.log.info(
                    "%s: Size of PDU list about to send: %d for %s",
                    worker_name,
                    len(list_of_server_and_event_id_to_send),
                    next_event_id,
                )
                list_of_pdus_to_send = []
                for (event_base,) in list_of_server_and_event_id_to_send:
                    assert isinstance(event_base, Event)
                    list_of_pdus_to_send.extend([event_base.raw_data])

                while list_of_pdus_to_send:
                    limited_list_of_pdus = list_of_pdus_to_send[:50]
                    list_of_pdus_to_send = list_of_pdus_to_send[50:]
                    self.log.info("Size of PDU list about to send: %d", len(limited_list_of_pdus))
                    response = await self.federation_handler.send_events_to_server(
                        origin_server,
                        destination_server,
                        limited_list_of_pdus,
                    )
                    event_sent = True

                    response_break_down = response.json_response.get("pdus", {})
                    # The response from a federation send transaction has a dictionary at 'pdus' with
                    # each key being the 'event_id' and the value being an empty {} for ok, but some
                    # string value if there was an error of some kind. Only log the errors
                    for (
                        pdu_event_id,
                        pdu_received_result,
                    ) in response_break_down.items():
                        if pdu_received_result:
                            self.log.info(
                                "%s: Received error from %s got response of %s",
                                worker_name,
                                pdu_event_id,
                                pdu_received_result,
                            )

                # Update for the render
                # end_time = time.time() - start_time
                render_list.extend([(0.0, False)])
                _event_error_queue.task_done()

                # Set up the retry task, but only if an event was actually sent
                if event_sent:
                    new_worker_id = len(self.reaction_task_controller.tasks_sets[pinned_message].tasks)
                    self.reaction_task_controller.add_tasks(
                        pinned_message,
                        _waiting_retry_worker,
                        f"_waiting_retry_worker_{new_worker_id}",
                        _event_fetch_queue,
                        bot_working,
                        next_event_id,
                        len(list_of_server_and_event_id_to_send),
                    )

                else:
                    self.log.warning("%s: Nothing to do, as no events were sent out for %s", worker_name, next_event_id)

                bot_working[worker_name] = False

        async def _waiting_retry_worker(
            worker_name: str,
            _event_fetch_queue: asyncio.Queue[tuple[float, bool, set[str]]],
            _bot_working_status_counter: dict[str, bool],
            event_id_to_check: str,
            num_of_events_prev_sent: int,
        ) -> None:
            """
            Responsible for retrying the 'fixed' Event ID before being sent back to the
            normal event fetching worker. Given a count of the events sent in order to
            repair an Event, base a sleep timer on that. See notes inside function for
            rationale.
            Args:
                worker_name:
                _event_fetch_queue:
                event_id_to_check:
                num_of_events_prev_sent:

            Returns:

            """
            retry_counter = 0
            not_found = True
            _bot_working_status_counter[worker_name] = True

            # We'll use a pretty healthy number of retries, as huge rooms have complex
            # state to work through and may have some delays
            for retry_counter in range(0, 10):
                if not not_found:
                    break
                if self.reaction_task_controller.is_stopped(pinned_message):
                    return

                # We'll give slow servers a little bit of time to work through the
                # stack of events that already were sent. Depending on state resolution
                # (looking at you, Synapse) this may take up to a minute to fully
                # resolve before the Event ID is actually available on the server.
                sleep_time = max(1.0 * num_of_events_prev_sent, 5.0)

                await asyncio.sleep(sleep_time)

                retry_check_on_event = await self.federation_handler.get_event_from_server(
                    origin_server,
                    destination_server,
                    event_id_to_check,
                )
                retry_counter += 1

                retried_event = retry_check_on_event.get(event_id_to_check, None)
                if isinstance(retried_event, Event):
                    # found the event, send it back to the event fetch queue for retry
                    not_found = False
                    self.log.info(
                        "%s: Potentially found event on roomwalk(after "
                        "%d attempts), sending %s back "
                        "to event fetcher",
                        worker_name,
                        retry_counter,
                        event_id_to_check,
                    )
                    # The back off mech shouldn't need to wait in this instance, as the
                    # event will already be in the cache. This is considered a 'retry'
                    # for the event fetcher
                    _event_fetch_queue.put_nowait((0.0, True, {event_id_to_check}))

            if not_found:
                self.log.warning(
                    "%s: Not found after %d tries, giving up on %s", worker_name, retry_counter, event_id_to_check
                )
            # Register the bot as not working, so the render loop knows it's not waiting
            # for anything.
            _bot_working_status_counter[worker_name] = False

        # tuple[suggested_backoff, is_this_a_retry, event_ids_to_fetch]
        roomwalk_fetch_queue: asyncio.Queue[tuple[float, bool, set[str]]] = asyncio.Queue()
        # The Queue of errors, matches with _event_error_queue on workers
        # tuple[suggested_backoff, event_id_to_fetch]
        roomwalk_error_queue: asyncio.Queue[str] = asyncio.Queue()

        # This is a grouping of one-off fired tasks, specifically the ones with a long
        # wait timer. The room walker worker will create these
        self.reaction_task_controller.setup_task_set(pinned_message)

        for i in range(0, 1):
            self.reaction_task_controller.add_tasks(
                pinned_message,
                _event_walking_fetcher,
                f"event_worker_{i}",
                roomwalk_fetch_queue,
                roomwalk_error_queue,
            )
            bot_working.setdefault(f"event_worker_{i}", False)
        for i in range(0, 1):
            self.reaction_task_controller.add_tasks(
                pinned_message,
                _room_repair_worker,
                f"roomwalk_worker_{i}",
                room_version_of_found_event,
                roomwalk_fetch_queue,
                roomwalk_error_queue,
            )
            bot_working.setdefault(f"roomwalk_worker_{i}", False)

        roomwalk_cumulative_iter_time = 0.0
        # Number of times we've re-rendered status
        roomwalk_iterations = 0
        # List of tuples, (time_spent float, bool if we are done)
        render_list: list[tuple[float, bool]] = []
        last_count_of_events_processed = 0
        # prime the queue
        await roomwalk_fetch_queue.put((0.0, False, {event_id}))

        # Want it to look like:
        #
        # Room depth reported as: 45867
        # [|||||||||||||||||||||||||||||||||||||||||||||||||] 100%
        # (Updating every x seconds)
        # Total Events processed: 0
        #   Errors: 0(0 found on other servers)
        #   Resolved Errors: 0
        #   Time taken: 120 seconds
        #

        # Don't use the finish bool below to avoid the last sleep()
        retry_for_finish = 0
        while True:
            finish_on_this_round = False
            if self.reaction_task_controller.is_stopped(pinned_message):
                finish_on_this_round = True

            if self.reaction_task_controller.is_paused(pinned_message):
                # A pause just means not adding anything to the screen, until restarted
                await asyncio.sleep(1)
                continue

            if roomwalk_fetch_queue.qsize() == 0 and roomwalk_error_queue.qsize() == 0:
                if all({not x for x in bot_working.values()}):
                    retry_for_finish += 1
                    self.log.warning("Unexpectedly found no work being processed. Retry count: %d", retry_for_finish)
                    if retry_for_finish > 5:
                        finish_on_this_round = True
                else:
                    retry_for_finish = 0

            # pinch off the list of things to work on
            new_items_to_render = render_list.copy()
            render_list = []
            # self.log.info(f"LENGTH of render_list: {len(new_items_to_render)}")

            # self.log.info(f"SIZE of queue: {roomwalk_fetch_queue.qsize()}")

            for time_spent, finish in new_items_to_render:
                if finish:
                    finish_on_this_round = True
                roomwalk_cumulative_iter_time += time_spent
            roomwalk_iterations = roomwalk_iterations + 1
            current_count_of_events_processed = len(event_id_ok_list.union(event_id_error_list))
            # Want it to look like:
            #
            # Room depth reported as: 45867
            # [|||||||||||||||||||||||||||||||||||||||||||||||||] 100%
            # (Updating every x seconds)
            # Total Events processed: 0
            #   Errors: 0(0 found on other servers)
            #   Resolved Errors: 0
            #   Time taken: 120 seconds
            #
            # TODO: Fix this too
            # if new_items_to_render or finish_on_this_round:

            self.log.info("mid-render, room_depth difference: %d / %d", len(seen_depths_for_progress), room_depth)
            progress_bar.update(seen_depths_for_progress)
            progress_line = progress_bar.render_bitmap_bar()

            roomwalk_lines = [
                f"(Updating every {SECONDS_BETWEEN_EDITS} seconds)",
                f"Total Events processed: {current_count_of_events_processed} ({current_count_of_events_processed - last_count_of_events_processed} / update)",
                f"  Errors: {len(event_id_error_list)}",
                f"  Resolved Errors: ({len(event_id_resolved_error_list)})",
                f"  Time taken: {roomwalk_cumulative_iter_time:.3f} seconds (iter# {roomwalk_iterations})",
                f"  (Items currently in backlog event queue: {roomwalk_fetch_queue.qsize()})",
                f"  (Items currently in backlog roomwalk queue: {roomwalk_error_queue.qsize()})",
                f"Total number of workers: {len(self.reaction_task_controller.tasks_sets[pinned_message].tasks)}",
            ]
            if retry_for_finish:
                roomwalk_lines.extend([f"Might be out of work, retry count:{retry_for_finish}"])

            # Only print something if there is something to say
            await command_event.respond(
                make_into_text_event(
                    wrap_in_code_block_markdown(_combine_lines_for_backwalk()),
                ),
                edits=pinned_message,
            )
            last_count_of_events_processed = current_count_of_events_processed
            if finish_on_this_round:
                break

            await asyncio.sleep(SECONDS_BETWEEN_EDITS)

        # Clean up the task controller
        await self.reaction_task_controller.cancel(pinned_message, True)

        header_lines = ["Room Back-walking Procedure: Done"]

        roomwalk_lines.extend(["Done"])
        await command_event.respond(
            make_into_text_event(
                wrap_in_code_block_markdown(_combine_lines_for_backwalk()),
            ),
            edits=pinned_message,
        )
        event_ids_that_errored_message = ""
        for event_id in event_id_error_list:
            event_ids_that_errored_message += event_id + "\n"
        if not event_ids_that_errored_message:
            event_ids_that_errored_message = "No event ids were found to have errored\n"
        await command_event.respond(
            make_into_text_event(
                wrap_in_code_block_markdown(event_ids_that_errored_message),
            ),
        )

    @test_command.subcommand(
        name="repair_event",
        help="Find a given Event ID in a room and inject any necessary ancestors into the server_to_fix",
    )
    @command.argument(name="room_id_or_alias", parser=is_room_id_or_alias, required=True)
    @command.argument(name="event_id", parser=is_event_id, required=True)
    @command.argument(name="server_to_fix", required=True)
    async def repair_event_command(
        self,
        command_event: MessageEvent,
        room_id_or_alias: str,
        event_id: str,
        server_to_fix: str,
    ) -> None:
        """
        Find an event and inject any necessary ancestors into the target server.

        The process:
        1. Verify event isn't already on target server
        2. Get hosts in room, filter out unresponsive ones
        3. Hunt for event on remaining hosts
        4. Check found event for missing ancestor events
        5. Add to ordered list for backfilling
        6. Repeat until no more missing ancestors found
        7. Walk backwards through list sending events to target

        Notes: Maybe make a change to always check the origin server first, as if it is present there we only need to
            send that event and the server_to_fix will automatically try and pull the rest of the auth_events.

            When the server_to_fix receives these events on their `/send` endpoint, it(or at least Synapse) will use the
            authorization header to try and ask *that* server for the other events, even if they are not present.

            This means the local server will be polled and if the events are not present the whole process will fail.
        Args:
            command_event: The event that triggered the command
            room_id_or_alias: Room ID or alias containing the event
            event_id: Event ID to repair
            server_to_fix: Optional target server to fix, defaults to origin server

        Raises:
            ValueError: If the event ID is not a string
        """
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        origin_server = await self.get_origin_server_and_assert_key_exists(command_event)
        if not origin_server:
            return

        destination_server = server_to_fix or origin_server

        # 1. The event ID that was provided, prove it's not already on the target server
        if not event_id:
            await command_event.reply(f"I need you to provide me with an event_id, got {event_id}")
            return

        await command_event.respond("Making sure event is not already in room")
        event_map = await self.federation_handler.get_event_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            event_id=event_id,
        )
        sampled_event = event_map.get(event_id, None)
        if not isinstance(sampled_event, EventError):
            await command_event.reply(
                f"It appears that the Event referenced by '{event_id}' is already "
                f"on the target server: {destination_server}",
            )
            return

        await command_event.respond("Resolving room alias(if it was one) and retrieving list of hosts in room")
        room_data = await self.get_room_data(
            origin_server, destination_server, command_event, room_id_or_alias, get_servers_in_room=True
        )
        if room_data is None:
            return

        good_host_list: list[str] = []
        # This will act as a prefetch to prime the server result cache, which can be
        # then checked directly
        await command_event.respond("Filtering out dead/unresponsive hosts")
        assert isinstance(room_data.list_of_servers_in_room, list)
        version_results = await self._get_versions_from_servers(room_data.list_of_servers_in_room)
        for host in room_data.list_of_servers_in_room:
            server_result = version_results.get(host)
            if server_result and not isinstance(server_result, MatrixError):
                good_host_list.append(host)

        # 3. Hunt for the event on those hosts, start at the top. Make a note when
        #   you've passed the 5th one that didn't have it.
        await command_event.respond("Hunting for event on list of good hosts")

        list_of_server_and_event_id_to_send = []
        event_ids_to_try_next: asyncio.Queue[str] = asyncio.Queue()
        event_ids_to_try_next.put_nowait(event_id)

        while True:
            popped_event_id = await event_ids_to_try_next.get()
            # 4. Check the found Event for ancestor Events that are not on the target server
            server_to_event_result_map = await self.federation_handler.find_event_on_servers(
                origin_server, popped_event_id, good_host_list
            )

            await command_event.respond(f"Found event {popped_event_id} on {len(server_to_event_result_map)} servers")
            for event_base in server_to_event_result_map.values():
                # But first, verify the events are valid
                # TODO: what happens if it is not? Where is the handling for that?
                #  Are we just hoping that one of them is?
                if isinstance(event_base, Event):
                    await self.federation_handler.verify_signatures_and_annotate_event(
                        event_base, room_data.room_version
                    )

            # count_of_how_many_servers_tried = 0
            for server_name, event_base in server_to_event_result_map.items():
                if isinstance(event_base, Event):
                    if event_base.signatures_verified:
                        # 5. Add to a List, so we can clearly walk backwards(filling them in order)
                        list_of_server_and_event_id_to_send.append(event_base)
                        # But, do we need more
                        for _prev_event_id in event_base.prev_events:
                            response_check_for_this_event = await self.federation_handler.get_event_from_server(
                                origin_server,
                                destination_server,
                                _prev_event_id,
                            )
                            _inner_event_base_check = response_check_for_this_event.get(_prev_event_id)
                            if isinstance(_inner_event_base_check, EventError):
                                # We hit an error, that's what we want to keep looking
                                self.log.warning(
                                    "event retrieved during inner check: %s", _inner_event_base_check.error
                                )

                                event_ids_to_try_next.put_nowait(_prev_event_id)
                                self.log.info("repair_event: adding event_id to next to try: %s", _prev_event_id)
                            elif isinstance(_inner_event_base_check, Event):
                                self.log.warning(
                                    "event retrieved during inner check: %s", _inner_event_base_check.raw_data
                                )

                        break

            # 6. Repeat from 3 until no more ancestor Events are found that are missing
            if event_ids_to_try_next.qsize() == 0:
                # should be done
                break

        # 7. Walk backwards through that list 'send'ing those events to the target
        # server. Check each result for errors, only do one at a time to wait for
        # responses.
        await command_event.respond("Attempting to send event to destination")

        list_of_buffer_lines = [
            "Done for now, check logs\n",
            f"found {len(room_data.list_of_servers_in_room)} servers in that room\n",
            f"found {len(good_host_list)} of good servers\n",
        ]

        # Need to do these in reverse, or the destination server will barf
        list_of_server_and_event_id_to_send.reverse()
        for event_base in list_of_server_and_event_id_to_send:
            if isinstance(event_base, Event):
                response = await self.federation_handler.send_events_to_server(
                    origin_server,
                    destination_server,
                    [event_base.raw_data],
                )
                self.log.info("SENT, got response of %s", response.json_response)
                list_of_buffer_lines.extend(
                    [
                        f"response from server_to_fix:\n{json.dumps(response.json_response, indent=4)}",
                    ]
                )
            else:
                self.log.info("Unexpectedly not sent %s", event_base)

        # Time to start rendering. Build the header lines first
        header_message = f"Attempting to send {event_id} and it's ancestors into {destination_server}\n\n"

        # Chunk the data as there may be a few 'pages' of it
        final_list_of_data = combine_lines_to_fit_event(list_of_buffer_lines, header_message)

        for chunk in final_list_of_data:
            await command_event.respond(
                make_into_text_event(wrap_in_code_block_markdown(chunk), ignore_body=True),
            )

    @test_command.subcommand(name="room_hosts", help="List all hosts in a room")
    @command.argument(name="room_id_or_alias", parser=is_room_id_or_alias, required=False)
    @command.argument(name="limit", parser=is_int, required=False)
    @command.argument(name="server_to_request_from", required=False)
    async def room_host_command(
        self,
        command_event: MessageEvent,
        room_id_or_alias: str | None,
        limit: int | None,
        server_to_request_from: str | None = None,
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        origin_server = await self.get_origin_server_and_assert_key_exists(command_event)
        if not origin_server:
            return

        destination_server = server_to_request_from or origin_server

        room_data = await self.get_room_data(
            origin_server,
            destination_server,
            command_event,
            room_id_or_alias,
            get_servers_in_room=True,
            use_origin_room_as_fallback=True,
        )
        if not room_data:
            # The user facing error message was already sent
            return

        preresponse_message = await command_event.respond(
            f"Retrieving Hosts for \n"
            f"* Room: {room_id_or_alias or room_data.room_id}\n"
            f"* at {pretty_print_timestamp(room_data.timestamp_of_last_event_id)}\n"
            f"* From {destination_server} using {origin_server}",
        )
        list_of_message_ids: list[EventID] = [preresponse_message]

        assert room_data.list_of_servers_in_room is not None
        host_list = room_data.list_of_servers_in_room.copy()

        list_of_buffer_lines = []

        if limit:
            # if limit is more than the number of hosts, fix it
            limit = min(limit, len(host_list))
            for host_number in range(0, limit):
                list_of_buffer_lines.extend([f"{host_list[host_number : host_number + 1]}\n"])
        else:
            for host in host_list:
                list_of_buffer_lines.extend([f"['{host}']\n"])

        list_of_buffer_lines.extend([f"Process took: {room_data.processing_time} milliseconds"])
        # Chunk the data as there may be a few 'pages' of it
        final_list_of_data = combine_lines_to_fit_event(list_of_buffer_lines, None)

        for chunk in final_list_of_data:
            current_message_id = await command_event.respond(
                make_into_text_event(wrap_in_code_block_markdown(chunk), ignore_body=True),
            )
            list_of_message_ids.extend([current_message_id])
        for message_id in list_of_message_ids:
            await self.reaction_task_controller.add_cleanup_control(message_id, command_event.room_id)

    @test_command.subcommand(
        name="demo_bar",
        help="test bitmap progress bar",
    )
    @command.argument(name="max_size", parser=is_int, required=False)
    @command.argument(name="style", required=False)
    @command.argument(name="seconds_to_run_for", parser=is_int, required=False)
    async def demo_progress_bar_subcommand(
        self,
        command_event: MessageEvent,
        max_size: str | None,
        style: str | None,
        seconds_to_run_for: str | None,
    ) -> None:
        """
        Demonstrate progress bar rendering.

        Shows an animated progress bar with configurable parameters.

        Args:
            command_event: The event that triggered the command
            max_size: Maximum size of the progress bar
            style: Progress bar style ('linear' or 'scatter')
            seconds_to_run_for: How long to run the demo
        """
        if not max_size:
            max_size = "50"
        if not seconds_to_run_for:
            seconds_to_run_for = "60"
        max_size_int = int(max_size)
        seconds_float = float(seconds_to_run_for)
        interval_float = 5.0
        num_of_intervals = seconds_float / interval_float
        style_type = BitmapProgressBarStyle.LINEAR if style == "linear" else BitmapProgressBarStyle.SCATTER
        how_many_to_pull = max(int(max_size_int / num_of_intervals), 1)
        progress_bar = BitmapProgressBar(30, max_size_int, style=style_type)
        range_list = []

        constants_display_string = ""
        for value in progress_bar.constants.values():
            constants_display_string += f"'{value}', "
        spaces_display_string = "' ', ' ', ' ', ' ', ' '"

        debug_message = await command_event.respond(
            wrap_in_code_block_markdown(
                f"fullb char: {constants_display_string}\n"
                f"other char: '{progress_bar.blank}'\n"
                f"space char: {spaces_display_string}\n"
                f"segment_size: {progress_bar._segment_size}\n",
            ),
        )
        list_of_message_ids: list[EventID] = [debug_message]

        if style_type == BitmapProgressBarStyle.SCATTER:
            for i in range(1, max_size_int + 1):
                range_list.extend([i])
        else:
            for i in range(1, int(round_half_up(num_of_intervals)) + 1):
                range_list.extend([i * how_many_to_pull])
        pinned_message = cast(
            "EventID",
            await command_event.respond(
                wrap_in_code_block_markdown(
                    progress_bar.render_bitmap_bar() + f"\n size of range_list: {len(range_list)}\n"
                    f" how many to pull: {how_many_to_pull}\n",
                ),
            ),
        )
        list_of_message_ids.extend([pinned_message])

        finish = False
        while True:
            set_to_pull = set()
            start_time = time.time()
            if style_type == BitmapProgressBarStyle.SCATTER:
                for _ in range(min(int(how_many_to_pull), len(range_list))):
                    entry_index = random.randint(0, len(range_list) - 1)
                    entry = range_list.pop(entry_index)
                    set_to_pull.add(entry)
            else:
                entry = range_list.pop(0)
                set_to_pull.add(entry)

            if len(range_list) == 0:
                finish = True
            progress_bar.update(set_to_pull)
            rendered_bar = progress_bar.render_bitmap_bar()
            finished_time = time.time()
            await command_event.respond(
                wrap_in_code_block_markdown(
                    rendered_bar + f"\n size of range_list: {len(range_list)}\n"
                    f" how many to pull: {how_many_to_pull}\n"
                    f" time to render: {finished_time - start_time}\n",
                ),
                edits=pinned_message,
            )
            if finish:
                break
            await asyncio.sleep(interval_float)

        for message_id in list_of_message_ids:
            await self.reaction_task_controller.add_cleanup_control(message_id, command_event.room_id)

    @test_command.subcommand(name="room_version", help="experiment to get room version from room id")
    @command.argument(name="room_id_or_alias", parser=is_room_id_or_alias, required=True)
    @command.argument(name="target_server", required=False)
    async def room_version_command(
        self, command_event: MessageEvent, room_id_or_alias: str, target_server: str | None = None
    ) -> None:
        """
        Get room version information.

        Retrieves and displays the Matrix protocol version used by a room.

        Args:
            command_event: The event that triggered the command
            room_id_or_alias: Room ID or alias to check version for
            target_server: optional server to target, if omitted the origin will be used
        """
        await command_event.mark_read()

        origin_server = await self.get_origin_server_and_assert_key_exists(command_event)
        if not origin_server:
            return

        if not target_server:
            target_server = origin_server
        room_data = await self.get_room_data(
            origin_server,
            target_server,
            command_event,
            room_id_or_alias,
            get_servers_in_room=False,
            use_origin_room_as_fallback=False,
        )
        if not room_data:
            return

        pinned_message = cast(
            "EventID", await command_event.reply(f"{room_data.room_id} version is {room_data.room_version}")
        )
        await self.reaction_task_controller.add_cleanup_control(pinned_message, command_event.room_id)

    @test_command.subcommand(name="discover_event_id", help="experiment to get event id from PDU event")
    @command.argument(name="event_id", parser=is_event_id, required=True)
    @command.argument(name="from_server", required=False)
    @command.argument(name="room_version", required=False)
    async def discover_event_id_command(
        self,
        command_event: MessageEvent,
        event_id: str,
        from_server: str | None,
        room_version: str | None,
    ) -> None:
        """
        Discover information about an event ID.

        Gets event details and calculates reference hashes for verification.

        Args:
            command_event: The event that triggered the command
            event_id: Event ID to look up
            from_server: Optional server to query
            room_version: Optional room version to use for hash calculation
        """
        await command_event.mark_read()

        origin_server = await self.get_origin_server_and_assert_key_exists(command_event)
        if not origin_server:
            return

        if not event_id:
            await command_event.respond(
                "I need you to supply an actual existing event_id to use as a reference for this experiment.",
            )
            return

        if not from_server:
            from_server = str(origin_server)
        else:
            # in case they skipped a from_server and just used a room_version
            with suppress(ValueError):
                room_version = str(from_server)

        event_map = await self.federation_handler.get_event_from_server(
            origin_server=origin_server,
            destination_server=from_server,
            event_id=event_id,
        )
        event = event_map[event_id]
        if isinstance(event, EventError):
            await command_event.respond(
                f"I don't think {event_id} is a legitimate event, or {origin_server} is"
                f" not in that room, so I can not access it.\n\n{event.errcode}",
            )
            return

        assert event is not None and isinstance(event, EventBase)

        room_id = event.room_id

        if not room_version:
            room_version = await self.federation_handler.discover_room_version(
                origin_server=origin_server,
                destination_server=origin_server,
                room_id=room_id,
            )

        assert room_version is not None

        current_message = await command_event.respond(f"Original:\n{wrap_in_code_block_markdown(event.to_json())}")
        list_of_message_ids: list[EventID] = [current_message]

        redacted_data = redact_event(room_version, event.raw_data)
        redacted_data.pop("signatures", None)
        redacted_data.pop("unsigned", None)
        current_message = await command_event.respond(
            f"Redacted:\n{wrap_in_code_block_markdown(json.dumps(redacted_data, indent=4))}",
        )
        list_of_message_ids.extend([current_message])

        encoded_redacted_event_bytes = encode_canonical_json(redacted_data)
        reference_content = hashlib.sha256(encoded_redacted_event_bytes)
        reference_hash = encode_base64(reference_content.digest(), True)

        current_message = await command_event.respond(f"Supplied: {event_id}\n\nResolved: {'$' + reference_hash}")
        list_of_message_ids.extend([current_message])
        for message_id in list_of_message_ids:
            await self.reaction_task_controller.add_cleanup_control(message_id, command_event.room_id)

    @fed_command.subcommand(name="head", help="experiment for retrieving information about a room")
    @command.argument(name="room_id_or_alias", parser=is_room_id_or_alias, required=False)
    @command.argument(name="target_server", required=False)
    async def head_command(
        self, command_event: MessageEvent, room_id_or_alias: str | None, target_server: str | None
    ) -> None:
        if target_server:
            await self._head_command_single(command_event, room_id_or_alias, target_server)
        else:
            await self._head_command(command_event, room_id_or_alias)

    async def _head_command_single(
        self, command_event: MessageEvent, room_id_or_alias: str | None, target_server: str
    ) -> None:
        """
        Get room head information from a single server with more verbose display.

        Retrieves information about the current head of a room's event graph.

        Args:
            command_event: The event that triggered the command
            room_id_or_alias: Room ID or alias to get head for
            target_server: If provided, just pull the data from this one server instead of the whole room
        """
        await command_event.mark_read()
        origin_server = await self.get_origin_server_and_assert_key_exists(command_event)
        if not origin_server:
            return
        room_data = await self.get_room_data(
            origin_server,
            target_server,
            command_event,
            room_id_or_alias,
            get_servers_in_room=False,
            use_origin_room_as_fallback=True,
        )
        if not room_data:
            return

        destination_server = target_server
        list_of_message_ids: list[EventID] = []
        list_of_buffered_messages: list[str] = []
        try:
            room_head_data = await self.federation_handler.get_room_head(
                origin_server, destination_server, room_data.room_id, self.client.mxid
            )
        except MatrixError as e:
            list_of_buffered_messages.append(f"{e.errcode}: {e.error}")
            list_of_buffered_messages.append(f"{json.dumps(e.json_response, indent=2)}")

        else:
            list_of_buffered_messages.append(json.dumps(room_head_data.make_join_response.json_response, indent=2))
            list_of_buffered_messages.extend(room_head_data.print_detailed_lines())
        final_buffer_messages = combine_lines_to_fit_event(list_of_buffered_messages, None, True)
        for message in final_buffer_messages:
            current_message = await command_event.respond(
                make_into_text_event(wrap_in_code_block_markdown(message), ignore_body=True),
            )
            list_of_message_ids.extend([current_message])
        for message_id in list_of_message_ids:
            await self.reaction_task_controller.add_cleanup_control(message_id, command_event.room_id)

    async def _head_command(self, command_event: MessageEvent, room_id_or_alias: str | None) -> None:
        """
        Get room head information.

        Retrieves information about the current head of a room's event graph.

        Args:
            command_event: The event that triggered the command
            room_id_or_alias: Room ID or alias to get head for
        """
        await command_event.mark_read()
        origin_server = await self.get_origin_server_and_assert_key_exists(command_event)
        if not origin_server:
            return
        destination_server = origin_server
        room_data = await self.get_room_data(
            origin_server,
            destination_server,
            command_event,
            room_id_or_alias,
            get_servers_in_room=True,
            use_origin_room_as_fallback=True,
        )
        if not room_data:
            return

        assert isinstance(room_data.list_of_servers_in_room, list), "List of servers was None, they must be escaping"
        await command_event.respond(f"Found {len(room_data.list_of_servers_in_room)} servers")

        async def _head_task(_host: str) -> tuple[str, RoomHeadData | MatrixError]:
            try:
                join_response = await self.federation_handler.get_room_head(
                    origin_server,
                    _host,
                    room_data.room_id,
                    str(self.client.mxid),
                )
            except MatrixError as _e:
                return _host, _e

            return _host, join_response

        reference_task_key = self.reaction_task_controller.setup_task_set()
        start_time = time.time()

        set_of_server_names = set(room_data.list_of_servers_in_room)
        # These are one-off tasks, not workers. Create as many as we have servers to check
        for host in room_data.list_of_servers_in_room:
            self.reaction_task_controller.add_tasks(
                reference_task_key,
                _head_task,
                host,
                limit=1,
            )

        results: Sequence[tuple[str, RoomHeadData | MatrixError]] = (
            await self.reaction_task_controller.get_task_results(reference_task_key, return_exceptions=False)
        )

        await self.reaction_task_controller.cancel(reference_task_key)
        end_time = time.time()
        list_of_buffered_messages: list[str] = []
        list_of_bad_responses: list[str] = []

        server_name_dc = DisplayLineColumnConfig("Server Name")

        # Preprocess the results for the column widths
        for result in results:
            _host_sort, _result_sort = result
            server_name_dc.maybe_update_column_width(_host_sort)

        # Split the results
        for result in results:
            try:
                _host_sort, _result_sort = result
                set_of_server_names.discard(_host_sort)
                if isinstance(_result_sort, MatrixError):
                    list_of_bad_responses.append(
                        f"{server_name_dc.pad(_host_sort)}: {_result_sort.http_code}, {_result_sort.reason or _result_sort.error}"
                    )

                else:
                    assert isinstance(
                        _result_sort, RoomHeadData
                    ), f"while splitting results, _result_sort was not RoomHeadData: {_result_sort}"
                    list_of_buffered_messages.append(
                        f"{server_name_dc.pad(_host_sort)}: {_result_sort.print_summary_line()}"
                    )

            except BaseException as e:
                self.log.warning("Found an exception: %r", e)

        list_of_buffered_messages = sorted(list_of_buffered_messages)
        list_of_bad_responses = sorted(list_of_bad_responses)
        list_of_buffered_messages.extend(list_of_bad_responses)
        list_of_buffered_messages.append(
            f"\nprocessing time: {(end_time - start_time):.3f}\nservers unaccounted for: {set_of_server_names}"
        )

        final_buffer_messages = combine_lines_to_fit_event(list_of_buffered_messages, None, True)
        for message in final_buffer_messages:
            current_message = await command_event.respond(
                make_into_text_event(wrap_in_code_block_markdown(message), ignore_body=True),
            )
            await self.reaction_task_controller.add_cleanup_control(current_message, command_event.room_id)

    # I think the command map should look a little like this:
    # (defaults will be marked with * )
    # !fed
    #   - state
    #       - avoid_excess* [room_id][event_id]  - retrieve state at this specific
    #                                      event_id(or last event) from room_id(or
    #                                      current room) but do not include similar
    #                                      events if they count more than 10 of each
    #       - all   [room_id][event_id]
    #       - count
    #   - event   [event_id] - retrieve a specific event(or last in room)
    #   - events  [room_id][how_many]   - retrieve the last how_many(or 10) events from
    #                                       room_id(or current room)

    @command.new(
        name="delegation",
        help="Some simple diagnostics around federation server discovery",
    )
    @command.argument(name="server_to_check", label="Server To Check", required=True)
    async def delegation_command(self, command_event: MessageEvent, server_to_check: str) -> None:
        """
        Check server delegation information.

        Checks server discovery and delegation setup including well-known
        records and SRV records.

        Args:
            command_event: Event that triggered the command
            server_to_check: Server name to check delegation for
        """
        list_of_servers_to_check = set()

        await command_event.mark_read()

        # It may be that they are using their mxid as the server to check, parse that
        maybe_user_mxid = is_mxid(server_to_check)
        if maybe_user_mxid:
            server_to_check = get_domain_from_id(maybe_user_mxid)

        # As an undocumented option, allow passing in a room_id to check an entire room.
        # This can be rather long(and time consuming) so we'll place limits later.
        maybe_room_id = is_room_id_or_alias(server_to_check)
        if maybe_room_id:
            origin_server = get_domain_from_id(self.client.mxid)
            room_to_check, _ = await self.resolve_room_id_or_alias(maybe_room_id, command_event, origin_server)
            # Need to cancel server_to_check, but can't use None
            server_to_check = ""
            if not room_to_check:
                # Don't need to actually display an error, that's handled in the above
                # function
                return
        else:
            # with server_to_check being set, this will be ignored any way
            room_to_check = command_event.room_id

        # server_to_check has survived this far, add it to the set of servers to search
        # for. Since we allow for searching an entire room, it will be the only server
        # in the set.
        if server_to_check:
            list_of_servers_to_check.add(server_to_check)

        # The list of servers was empty. This implies that a room_id was provided,
        # let's check.
        if not list_of_servers_to_check:
            try:
                assert room_to_check is not None
                joined_members = await self.client.get_joined_members(RoomID(room_to_check))

            except MForbidden:
                await command_event.respond(NOT_IN_ROOM_ERROR)
                return

            for member in joined_members:
                list_of_servers_to_check.add(get_domain_from_id(member))

        number_of_servers = len(list_of_servers_to_check)

        # Some quality of life niceties
        prerender_message = await command_event.respond(
            f"Retrieving data from federation for {number_of_servers} "
            f"server{'s.' if number_of_servers > 1 else '.'}\n"
            "This may take up to 30 seconds to complete.",
        )
        list_of_message_ids: list[EventID] = [prerender_message]

        # map of server name -> (server brand, server version)
        server_to_server_data: dict[str, MatrixResponse] = {}

        async def _delegation_worker(queue: asyncio.Queue[str]) -> None:
            while True:
                worker_server_name = await queue.get()

                # The 'get_server_version' function was written with the capability of
                # collecting diagnostic data.
                try:
                    server_to_server_data[worker_server_name] = await self.federation_handler.api.get_server_version(
                        worker_server_name,
                        force_rediscover=True,
                        diagnostics=True,
                    )
                except Exception as e:
                    self.log.debug("delegation worker error: %r", e)
                queue.task_done()

        delegation_queue: asyncio.Queue[str] = asyncio.Queue()
        for server_name in list_of_servers_to_check:
            delegation_queue.put_nowait(server_name)

        reference_key = self.reaction_task_controller.setup_task_set(command_event.event_id)

        self.reaction_task_controller.add_tasks(
            reference_key,
            _delegation_worker,
            delegation_queue,
            limit=MAX_NUMBER_OF_SERVERS_TO_ATTEMPT,
        )

        started_at = time.monotonic()
        await delegation_queue.join()
        total_time = time.monotonic() - started_at

        # Cancel our worker tasks.
        await self.reaction_task_controller.cancel(reference_key, False)

        # Want the full room version it to look like this for now
        #
        #   Server Name | WK   | SRV  | DNS  | Test  | SNI | TLS served by  |
        # ------------------------------------------------------------------
        #   example.org | OK   | None | OK   | OK    |     | Synapse 1.92.0 |
        # somewhere.net | None | None | None | Error |     | resty          | Long error....
        #   maunium.net | OK   | OK   | OK   | OK    | SNI | Caddy          |

        # The single server version will be the same in that a single line like above
        # will be printed, then the rendered diagnostic data

        # Create the columns to be used
        server_name_col = DisplayLineColumnConfig("Server Name")
        well_known_status_col = DisplayLineColumnConfig("WK")
        srv_status_col = DisplayLineColumnConfig("SRV")
        dns_status_col = DisplayLineColumnConfig("DNS")
        connective_test_status_col = DisplayLineColumnConfig("Test")
        tls_served_by_col = DisplayLineColumnConfig("TLS served by")
        retries_col = DisplayLineColumnConfig("Retries")

        # Iterate through the server names to widen the column, if necessary.
        for server_name, response in server_to_server_data.items():
            server_name_col.maybe_update_column_width(len(server_name))
            if response.diag_info:
                maybe_tls_server = response.diag_info.tls_handled_by
                if maybe_tls_server:
                    tls_served_by_col.maybe_update_column_width(len(maybe_tls_server))

        # Just use a fixed width for the results. Should never be larger than 5 for most
        well_known_status_col.maybe_update_column_width(5)
        srv_status_col.maybe_update_column_width(5)
        dns_status_col.maybe_update_column_width(5)
        connective_test_status_col.maybe_update_column_width(5)

        # Begin constructing the message
        #
        # Use a sorted list of server names, so it displays in alphabetical order.
        server_results_sorted = sorted(server_to_server_data.keys())

        # Build the header line
        header_message = (
            f"{server_name_col.front_pad()} | "
            f"{well_known_status_col.pad()} | "
            f"{srv_status_col.pad()} | "
            f"{dns_status_col.pad()} | "
            f"{connective_test_status_col.pad()} | "
            f"{tls_served_by_col.pad()} | "
            f"{retries_col.pad()} | "
            f"Errors\n"
        )

        # Need the total of the width for the code block table to make the delimiter
        header_line_size = len(header_message)

        # Create the delimiter line under the header
        header_message += f"{pad('', header_line_size, pad_with='-')}\n"

        list_of_result_data = []
        # Use the sorted list from earlier, alphabetical looks nicer
        for server_name in server_results_sorted:
            response = server_to_server_data[server_name]

            if response:
                # Shortcut reference the diag_info to cut down line length
                assert response.diag_info is not None
                diag_info = response.diag_info

                # The server name column
                buffered_message = f"{server_name_col.front_pad(server_name)} | "
                # The well-known status column
                buffered_message += f"{well_known_status_col.pad(diag_info.get_well_known_status())} | "

                # the SRV record status column
                buffered_message += f"{srv_status_col.pad(diag_info.get_srv_record_status())} | "

                # the DNS record status column
                buffered_message += f"{dns_status_col.pad(diag_info.get_dns_record_status())} | "

                # The connectivity status column
                connectivity_status = diag_info.get_connectivity_test_status()
                buffered_message += f"{connective_test_status_col.pad(connectivity_status)} | "
                maybe_tls_server = diag_info.tls_handled_by

                buffered_message += f"{tls_served_by_col.pad(maybe_tls_server if maybe_tls_server else '')} | "

                num_of_retries = diag_info.retries
                buffered_message += f"{retries_col.pad(num_of_retries)} | "

                if response.http_code != 200:
                    buffered_message += f"{response.reason}"

                buffered_message += "\n"
                if number_of_servers == 1:
                    # Print the diagnostic summary, since there is only one server there
                    # is no need to be brief.
                    buffered_message += f"{pad('', header_line_size, pad_with='-')}\n"
                    for line in diag_info.list_of_results:
                        buffered_message += f"{pad('', 3)}{line}\n"

                    buffered_message += f"{pad('', header_line_size, pad_with='-')}\n"

                list_of_result_data.extend([buffered_message])

        footer_message = f"\nTotal time for retrieval: {total_time:.3f} seconds\n"
        list_of_result_data.extend([footer_message])

        # For a single server test, the response will fit into a single message block.
        # However, for a roomful it could be several pages long. Chunk those responses
        # to fit into the size limit of an Event.
        final_list_of_data = combine_lines_to_fit_event(list_of_result_data, header_message)

        for chunk in final_list_of_data:
            current_message = await command_event.respond(
                make_into_text_event(wrap_in_code_block_markdown(chunk), ignore_body=True),
            )
            list_of_message_ids.extend([current_message])

        for current_message in list_of_message_ids:
            await self.reaction_task_controller.add_cleanup_control(current_message, command_event.room_id)

    @fed_command.subcommand(name="event_raw")
    @command.argument(name="event_id", parser=is_event_id, required=False)
    @command.argument(name="server_to_request_from", required=False)
    async def event_command(
        self,
        command_event: MessageEvent,
        event_id: str | None,
        server_to_request_from: str | None,
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        origin_server = await self.get_origin_server_and_assert_key_exists(command_event)
        if not origin_server:
            return

        destination_server = server_to_request_from or origin_server

        # Sometimes have to just make things a little more useful
        extra_info = ""
        if not event_id:
            event_id = command_event.event_id
            extra_info = " last event in this room"

        prerender_message = await command_event.respond(
            f"Retrieving{extra_info}: {event_id} from {destination_server} using {origin_server}",
        )
        list_of_message_ids: list[EventID] = [prerender_message]

        returned_event = await self.federation_handler.get_raw_pdu(
            origin_server=origin_server,
            destination_server=destination_server,
            event_id=event_id,
        )

        buffered_message = json.dumps(returned_event, indent=2) + "\n"

        # It is extremely unlikely that an Event will be larger than can be displayed.
        # Don't bother chunking the response.
        try:
            current_message = await command_event.respond(wrap_in_code_block_markdown(buffered_message))
        except MTooLarge:
            current_message = await command_event.respond("Somehow, Event is to large to display")
        list_of_message_ids.extend([current_message])
        for message_id in list_of_message_ids:
            await self.reaction_task_controller.add_cleanup_control(message_id, command_event.room_id)

    @fed_command.subcommand(name="event")
    @command.argument(name="event_id", parser=is_event_id, required=False)
    @command.argument(name="server_to_request_from", required=False)
    @command.argument(name="test_json_to_inject_or_keys_to_pop", required=False, pass_raw=True)
    async def event_command_pretty(
        self,
        command_event: MessageEvent,
        event_id: str | None,
        server_to_request_from: str | None,
        test_json_to_inject_or_keys_to_pop: str | None,
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        origin_server = await self.get_origin_server_and_assert_key_exists(command_event)
        if not origin_server:
            return

        if server_to_request_from:
            # deal with the case where pulling from origin as destination but also testing json/keys
            if "{" in server_to_request_from or "," in server_to_request_from:
                test_json_to_inject_or_keys_to_pop = server_to_request_from
                destination_server = origin_server
            else:
                destination_server = server_to_request_from
        else:
            destination_server = origin_server

        # Sometimes have to just make things a little more useful
        extra_info = ""
        if not event_id:
            event_id = command_event.event_id
            extra_info = " last event in this room"

        prerender_message = await command_event.respond(
            f"Retrieving{extra_info}: {event_id} from {destination_server} using {origin_server}",
        )
        list_of_message_ids: list[EventID] = [prerender_message]

        # TODO: test by modifying the object. Have to reach into the data as it comes in and
        #  modify that, as the attrib versions will have already been parsed and
        #  won't be read by the verifier. Spoiler alert: works as intended.
        right_most_bracket = None
        json_dumped = None
        remove_bit = None
        if (
            test_json_to_inject_or_keys_to_pop
            and "{" in test_json_to_inject_or_keys_to_pop
            and "}" in test_json_to_inject_or_keys_to_pop
        ):
            self.log.info("test_json_to_inject_or_keys_to_pop: %r", test_json_to_inject_or_keys_to_pop)
            right_most_bracket = test_json_to_inject_or_keys_to_pop.rindex("}")
            json_bit = test_json_to_inject_or_keys_to_pop[: right_most_bracket + 1]
            self.log.info("incoming inject: %s", json_bit)
            test_json_dumped = json.loads(json_bit)
            self.log.info("after json.loads: %r", test_json_dumped)

            remove_bit = test_json_to_inject_or_keys_to_pop[right_most_bracket + 1 :]
            self.log.info("remove_bit: %r", remove_bit)
            # returned_event.raw_data.update(test_json_dumped)
            # self.log.info(f"dumped raw_data:\n{json.dumps(returned_event.raw_data, indent=4)}")

        returned_event_dict = await self.federation_handler.get_event_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            event_id=event_id,
            inject_new_data=json_dumped,
            keys_to_pop=remove_bit,
        )

        buffered_message = ""
        returned_event = returned_event_dict.get(event_id)
        if isinstance(returned_event, EventError):
            buffered_message += f"received an error\n{returned_event.errcode}:{returned_event.error}\n"

        else:
            assert isinstance(returned_event, Event)
            # a_event will stand for ancestor event
            # A mapping of 'a_event_id' to the string of short data about the a_event to
            # be shown
            a_event_data_map: dict[str, str] = {}
            # Recursively retrieve events that are in the immediate past. This
            # allows for some annotation to the events when they are displayed in
            # the 'footer' section of the rendered response. For example: auth
            # events will have their event type displayed, such as 'm.room.create'
            # and the room version.
            assert isinstance(returned_event, EventBase)
            list_of_a_event_ids = returned_event.auth_events.copy()
            list_of_a_event_ids.extend(returned_event.prev_events)

            # For the verification display, grab the room version in these events
            # TODO: might be better to just get the room version directly
            found_room_version = "1"
            a_returned_events = await self.federation_handler.get_events_from_server(
                origin_server=origin_server,
                destination_server=destination_server,
                events_list=list_of_a_event_ids,
            )
            for a_event_id in list_of_a_event_ids:
                a_event_base = a_returned_events.get(a_event_id)
                if a_event_base:
                    a_event_data_map[a_event_id] = a_event_base.to_summary()
                    if isinstance(a_event_base, CreateRoomStateEvent):
                        found_room_version = a_event_base.room_version

            # Begin rendering
            await self.federation_handler.verify_signatures_and_annotate_event(returned_event, found_room_version)

            # It may be, but is unlikely outside of connection errors, that room_version
            # was not found. This is handled gracefully inside of to_pretty_summary()
            buffered_message += returned_event.to_pretty_summary(found_room_version)
            # Add a little gap at the bottom of the previous for better separation
            buffered_message += "\n"
            buffered_message += returned_event.to_pretty_summary_content()
            buffered_message += returned_event.to_pretty_summary_unrecognized()
            buffered_message += returned_event.to_pretty_summary_footer(a_event_data_map)

        current_message = await command_event.respond(wrap_in_code_block_markdown(buffered_message))
        list_of_message_ids.extend([current_message])
        for message_id in list_of_message_ids:
            await self.reaction_task_controller.add_cleanup_control(message_id, command_event.room_id)

    @fed_command.subcommand(name="state", help="Request state over federation for a room.")
    @command.argument(name="room_id_or_alias", parser=is_room_id_or_alias, required=False)
    @command.argument(name="server_to_request_from", required=False)
    async def state_command(
        self,
        command_event: MessageEvent,
        room_id_or_alias: str | None,
        server_to_request_from: str | None,
    ) -> None:
        await self._state_command(command_event, room_id_or_alias, server_to_request_from)

    @fed_command.subcommand(name="state_no_members", help="Request state over federation for a room.")
    @command.argument(name="room_id_or_alias", parser=is_room_id_or_alias, required=False)
    @command.argument(name="server_to_request_from", required=False)
    async def state_no_members_command(
        self,
        command_event: MessageEvent,
        room_id_or_alias: str | None,
        server_to_request_from: str | None,
    ) -> None:
        await self._state_command(command_event, room_id_or_alias, server_to_request_from, no_members=True)

    async def _state_command(
        self,
        command_event: MessageEvent,
        room_id_or_alias: str | None,
        server_to_request_from: str | None,
        no_members: bool = False,
    ) -> None:
        """
        Get and display room state information.

        Fetches state events for a room and displays them in a formatted table,
        optionally filtering out member events.

        Args:
            command_event: Event that triggered the command
            room_id_or_alias: Room ID or alias to get state for
            server_to_request_from: Server to query, defaults to origin server
            no_members: Whether to filter out member events
        """
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        origin_server = await self.get_origin_server_and_assert_key_exists(command_event)
        if not origin_server:
            return

        destination_server = server_to_request_from or origin_server

        room_data = await self.get_room_data(
            origin_server,
            destination_server,
            command_event,
            room_id_or_alias,
            get_servers_in_room=False,
            use_origin_room_as_fallback=True,
        )
        if not room_data:
            # The user facing error message was already sent
            return

        room_id = room_data.room_id
        event_id = room_data.detected_last_event_id
        origin_server_ts = room_data.timestamp_of_last_event_id

        prerender_message = await command_event.respond(
            f"Retrieving State for:\n"
            f"* Room: {room_id_or_alias or room_id}\n"
            f"* which took place at: {pretty_print_timestamp(origin_server_ts)} UTC\n"
            f"* From {destination_server} using {origin_server}",
        )
        list_of_message_ids: list[EventID] = [prerender_message]

        # This will retrieve the events and the auth chain, we only use the former here
        (
            pdu_list,
            _,
        ) = await self.federation_handler.get_state_ids_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            room_id=room_id,
            event_id=event_id,
        )

        prerender_message_2 = await command_event.respond(
            f"Retrieving {len(pdu_list)} events from {destination_server}",
        )
        list_of_message_ids.extend([prerender_message_2])

        # Keep both the response and the actual event, if there was an error it will be
        # in the response and the event won't exist here
        event_to_event_base: dict[str, EventBase]

        started_at = time.monotonic()
        event_to_event_base = await self.federation_handler.get_events_from_server(
            origin_server,
            destination_server,
            pdu_list,
        )
        total_time = time.monotonic() - started_at

        # Time to start rendering. Build the header lines first
        header_message = ""
        dc_depth = DisplayLineColumnConfig("Depth")
        dc_eid = DisplayLineColumnConfig("Event ID")
        dc_etype = DisplayLineColumnConfig("Event Type")
        dc_sender = DisplayLineColumnConfig("Sender")

        # Preprocessing:
        # 1. Set the column widths
        # 2. Get the depth's for row ordering
        list_of_event_ids: list[tuple[int, EventID]] = []
        for event_id, event_id_entry in event_to_event_base.items():
            # Use the about to be constructed list to curate what will be displayed later
            if no_members and event_id_entry.event_type == "m.room.member":
                continue

            list_of_event_ids.append((event_id_entry.depth, EventID(event_id)))

            dc_depth.maybe_update_column_width(len(str(event_id_entry.depth)))
            dc_eid.maybe_update_column_width(len(event_id))
            dc_etype.maybe_update_column_width(len(event_id_entry.event_type))
            dc_sender.maybe_update_column_width(len(event_id_entry.sender))

        # Sort the list in place by the first of the tuples, which is the depth
        list_of_event_ids.sort(key=lambda x: x[0])

        # Build the header line...
        header_message += f"{dc_depth.pad()} "
        header_message += f"{dc_eid.pad()} "
        header_message += f"{dc_etype.pad()} "
        header_message += f"{dc_sender.pad()}\n"

        # ...and the delimiter
        header_message += f"{pad('', pad_to=len(header_message), pad_with='-')}\n"
        list_of_buffer_lines = []

        # Use the sorted list to pull the events in order and begin the render
        for _, event_id in list_of_event_ids:
            buffered_message = ""
            event_base = event_to_event_base.get(event_id, None)
            if event_base:
                line_summary = event_base.to_line_summary(
                    dc_depth=dc_depth,
                    dc_eid=dc_eid,
                    dc_etype=dc_etype,
                    dc_sender=dc_sender,
                )
                buffered_message += f"{line_summary}\n"
            else:
                buffered_message += f"{event_id} was not found(unknown reason)\n"

            list_of_buffer_lines.extend([buffered_message])

        footer_message = f"\nTotal time for retrieval: {total_time:.3f} seconds\n"
        list_of_buffer_lines.extend([footer_message])
        # Chunk the data as there may be a few 'pages' of it
        final_list_of_data = combine_lines_to_fit_event(list_of_buffer_lines, header_message)

        for chunk in final_list_of_data:
            current_message = await command_event.respond(
                make_into_text_event(wrap_in_code_block_markdown(chunk), ignore_body=True),
            )
            list_of_message_ids.extend([current_message])

        for current_message in list_of_message_ids:
            await self.reaction_task_controller.add_cleanup_control(current_message, command_event.room_id)

    @fed_command.subcommand(
        name="ping",
        help="Check a server(or room) for application response time",
    )
    @command.argument(name="room_or_server", label="Server or Room to test", required=False)
    async def ping_command(self, command_event: MessageEvent, room_or_server: str | None) -> None:
        """
        Check federation response time for a server or room's servers.

        Args:
            command_event: The event that triggered the command
            room_or_server: Server name or room ID/alias to test
        """
        list_of_servers_to_check: list[str] = []
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        if not room_or_server:
            room_or_server = str(command_event.room_id)

        # As an undocumented option, allow passing in a room_id to check an entire room.
        if is_room_id_or_alias(room_or_server):
            origin_server = await self.get_origin_server_and_assert_key_exists(command_event)
            if origin_server is None:
                return
            destination_server = origin_server
            room_data = await self.get_room_data(
                origin_server,
                destination_server,
                command_event,
                # We determined above that this is actually a room_id or alias
                room_or_server,
                get_servers_in_room=True,
                use_origin_room_as_fallback=False,
            )
            if not room_data:
                # Don't need to actually display an error, that's handled in the above
                # function
                return
            assert room_data.list_of_servers_in_room is not None
            list_of_servers_to_check = room_data.list_of_servers_in_room

        # If the whole room was to be searched, this will already be filled in. Otherwise, there is our target
        if not list_of_servers_to_check:
            list_of_servers_to_check.append(room_or_server)

        number_of_servers = len(list_of_servers_to_check)

        current_message_id = await command_event.respond(
            f"Testing federation response time of `/version` for {number_of_servers} server"
            f"{'s' if number_of_servers > 1 else ''}",
        )
        list_of_message_ids: list[EventID] = [current_message_id]

        started_at = time.monotonic()
        server_to_version_data = await self._get_versions_from_servers(list_of_servers_to_check)
        total_time = time.monotonic() - started_at

        # Establish the initial size of the padding for each row
        server_name_col = DisplayLineColumnConfig("Server Name", horizontal_separator=" | ")
        ok_or_fail_col = DisplayLineColumnConfig("Result", justify=Justify.RIGHT, horizontal_separator=" | ")
        response_time_col = DisplayLineColumnConfig("Response time")

        # Iterate over all the data to collect the column sizes
        for server in server_to_version_data:
            server_name_col.maybe_update_column_width(len(server))

        # Construct the message response now
        #
        # Want it to look like
        # Server Name         | Result | Application Response Time
        # ---------------------------------------------------------
        # example.org         |   Fail | ClientConnectorError
        # matrix.org          |   Pass | 1.234 ms
        # dendrite.matrix.org |   Pass | 0.13.5+13c5173

        # Create the header lines
        header_messages = [f"{server_name_col.pad()} | {ok_or_fail_col.pad()} | {response_time_col.pad()}"]

        # Create the delimiter line
        header_message_line_size = len(header_messages[0])
        header_messages.extend([f"{pad('', header_message_line_size, pad_with='-')}"])

        # Alphabetical looks nicer
        sorted_list_of_servers = sorted(server_to_version_data.keys())

        # Collect all the output lines for chunking afterward
        list_of_result_data = []

        for server_name in sorted_list_of_servers:
            buffered_message = ""
            server_data = server_to_version_data[server_name]

            buffered_message += f"{server_name_col.pad(server_name)}{server_name_col.horizontal_separator}"
            # Federation request may have had an error, handle those errors here
            if server_data.http_code != 200:
                # Pad the software column with spaces, so the error and the code end up in the version column
                # Additionally, since this is the error clause, don't include the vertical line to separate
                # the column, giving a more distinctive visual indicator.
                buffered_message += f"{add_color(bold(ok_or_fail_col.pad('ERROR')), foreground=Colors.RED)}"
                buffered_message += f"{ok_or_fail_col.horizontal_separator}"

                buffered_message += f"{str(server_data.http_code) + ': ' if server_data.http_code > 0 else ''}"
                buffered_message += f"{server_data.reason}"

            else:
                buffered_message += f"{add_color(bold(ok_or_fail_col.pad('PASS')), foreground=Colors.GREEN)}"
                buffered_message += f"{ok_or_fail_col.horizontal_separator}"
                calculated_time = -1.0
                if server_data.tracing_context:
                    context = server_data.tracing_context
                    if context:
                        end_time = context.request_end
                        # if context.request_chunk_sent:
                        start_time = context.request_chunk_sent
                        # else:
                        #     start_time =
                        calculated_time = (end_time - start_time) * 1000
                buffered_message += f"{calculated_time:.3f} ms"
            list_of_result_data.extend([buffered_message])

        footer_message = f"\nTotal time for retrieval: {total_time:.3f} seconds"
        list_of_result_data.extend([footer_message])

        final_list_of_data = combine_lines_to_fit_event_html(
            list_of_result_data,
            header_messages,
            insert_new_lines=True,
        )

        # Wrap in code block markdown before sending
        count = 0
        for chunk in final_list_of_data:
            count += 1
            current_message_id = await command_event.respond(
                make_into_text_event(wrap_in_details(chunk, f" ⏩ Page {count} ⏪"), allow_html=True, ignore_body=True),
                allow_html=True,
            )
            list_of_message_ids.extend([current_message_id])
        for message_id in list_of_message_ids:
            await self.reaction_task_controller.add_cleanup_control(message_id, command_event.room_id, emoji=True)

    @fed_command.subcommand(
        name="version",
        aliases=["versions"],
        help="Check a server in the room for version info",
    )
    @command.argument(name="room_or_server", label="Server to check", required=True)
    async def version_command(self, command_event: MessageEvent, room_or_server: str) -> None:
        """
        Retrieves and displays information about the version of a server's software

        Args:
            command_event: The event that triggered the command
            room_or_server: Server name or room ID/alias to test
        """
        list_of_servers_to_check: list[str] = []
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        # As an undocumented option, allow passing in a room_id to check an entire room.
        if is_room_id_or_alias(room_or_server):
            origin_server = await self.get_origin_server_and_assert_key_exists(command_event)
            if origin_server is None:
                return
            destination_server = origin_server
            room_data = await self.get_room_data(
                origin_server,
                destination_server,
                command_event,
                # We determined above that this is actually a room_id or alias
                room_or_server,
                get_servers_in_room=True,
                use_origin_room_as_fallback=False,
            )
            if not room_data:
                # Don't need to actually display an error, that's handled in the above
                # function
                return
            assert room_data.list_of_servers_in_room is not None
            list_of_servers_to_check = room_data.list_of_servers_in_room

        # If the whole room was to be searched, this will already be filled in. Otherwise, there is our target
        if not list_of_servers_to_check:
            list_of_servers_to_check.append(room_or_server)

        number_of_servers = len(list_of_servers_to_check)

        current_message_id = await command_event.respond(
            f"Retrieving data from federation for {number_of_servers} server{'s' if number_of_servers > 1 else ''}",
        )
        list_of_message_ids: list[EventID] = [current_message_id]

        started_at = time.monotonic()
        server_to_version_data = await self._get_versions_from_servers(list_of_servers_to_check)
        total_time = time.monotonic() - started_at

        # Establish the initial size of the padding for each row
        server_name_col = DisplayLineColumnConfig(SERVER_NAME)
        server_software_col = DisplayLineColumnConfig(SERVER_SOFTWARE)
        server_version_col = DisplayLineColumnConfig(SERVER_VERSION)

        # Iterate over all the data to collect the column sizes
        for server, result in server_to_version_data.items():
            server_name_col.maybe_update_column_width(len(server))

            if result.http_code == 200:
                server_block = result.json_response.get("server", {})
                server_software: str = server_block.get("name", "")
                server_software_col.maybe_update_column_width(server_software)
                server_version = server_block.get("version", "")
                server_version_col.maybe_update_column_width(server_version)

        # Construct the message response now
        #
        # Want it to look like
        #         Server Name | Software | Version
        # -------------------------------------------------------------------------
        #         example.org | Synapse  | 1.98.0
        #          matrix.org | Synapse  | 1.99.0rc1 (b=matrix-org-hotfixes,4d....)
        # dendrite.matrix.org | Dendrite | 0.13.5+13c5173

        # Obviously, a single server will have only one line

        # Create the header line
        header_message = f"{server_name_col.front_pad()} | {server_software_col.pad()} | {server_version_col.pad()}\n"

        # Create the delimiter line
        header_message_line_size = len(header_message)
        header_message += f"{pad('', header_message_line_size, pad_with='-')}\n"

        # Alphabetical looks nicer
        sorted_list_of_servers = sorted(list_of_servers_to_check)

        # Collect all the output lines for chunking afterward
        list_of_result_data = []

        for server_name in sorted_list_of_servers:
            buffered_message = ""
            server_data = server_to_version_data.get(server_name, None)
            buffered_message += f"{server_name_col.front_pad(server_name)} | "
            if server_data:
                # Federation request may have had an error, handle those errors here
                if server_data.http_code != 200:
                    # Don't include the vertical line to separate the column, giving a more distinctive visual
                    # indicator.
                    buffered_message += "❌ "

                    buffered_message += f"{str(server_data.http_code) + ': ' if server_data.http_code > 0 else ''}{server_data.reason}\n"

                else:
                    server_block = server_data.json_response.get("server", {})
                    server_software = server_block.get("name")
                    server_version = server_block.get("version")
                    buffered_message += f"{server_software_col.pad(server_software)} | {server_version}\n"
            else:
                buffered_message += "Probably a threading error(WIP) sorry bout that"

            list_of_result_data.extend([buffered_message])

        footer_message = f"\nTotal time for retrieval: {total_time:.3f} seconds\n"
        list_of_result_data.extend([footer_message])

        final_list_of_data = combine_lines_to_fit_event(list_of_result_data, header_message)

        # Wrap in code block markdown before sending
        for chunk in final_list_of_data:
            current_message_id = await command_event.respond(
                make_into_text_event(wrap_in_code_block_markdown(chunk), ignore_body=True),
            )
            list_of_message_ids.extend([current_message_id])
        for message_id in list_of_message_ids:
            await self.reaction_task_controller.add_cleanup_control(message_id, command_event.room_id)

    async def _get_versions_from_servers(
        self,
        servers_to_check: Collection[str],
    ) -> dict[str, MatrixResponse]:
        """
        Get version information from a collection of servers in parallel.

        Makes concurrent /version requests to each server and collects responses.

        Args:
            servers_to_check: Collection of server names to query

        Returns:
            Dict mapping server names to their version response objects
        """
        # map of server name -> (server brand, server version)
        # Return this at the end
        server_to_version_data: dict[str, MatrixResponse] = {}

        async def _version_worker(queue: asyncio.Queue[str]) -> None:
            while True:
                worker_server_name = await queue.get()
                try:
                    result = await self.federation_handler.api.get_server_version_new(
                        worker_server_name,
                        diagnostics=True,
                    )

                    server_to_version_data[worker_server_name] = result
                except Exception as e:
                    self.log.warning("_version_worker: %s: %r", worker_server_name, e)
                    pass
                queue.task_done()

        version_queue: asyncio.Queue[str] = asyncio.Queue()
        for server_name in servers_to_check:
            version_queue.put_nowait(server_name)

        reference_key = self.reaction_task_controller.setup_task_set()
        self.reaction_task_controller.add_tasks(
            reference_key,
            _version_worker,
            version_queue,
            limit=MAX_NUMBER_OF_SERVERS_TO_ATTEMPT,
        )
        await version_queue.join()

        await self.reaction_task_controller.cancel(reference_key)

        return server_to_version_data

    @fed_command.subcommand(name="server_keys")
    @command.argument(name="server_to_check", required=True)
    async def server_keys_command(self, command_event: MessageEvent, server_to_check: str) -> None:
        await self._server_keys(command_event, server_to_check)

    @fed_command.subcommand(name="server_keys_raw")
    @command.argument(name="server_to_check", required=True)
    async def server_keys_raw_command(self, command_event: MessageEvent, server_to_check: str) -> None:
        await self._server_keys(command_event, server_to_check, display_raw=True)

    async def _server_keys(
        self,
        command_event: MessageEvent,
        server_to_check: str,
        display_raw: bool = False,
    ) -> None:
        """
        Get and display server key information.

        Fetches server signing keys and displays them in a formatted table,
        optionally showing raw JSON.

        Args:
            command_event: Event that triggered the command
            server_to_check: Server name to check keys for
            display_raw: Whether to display raw JSON response
        """
        list_of_servers_to_check: list[str] = []
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        if not server_to_check:
            server_to_check = str(command_event.room_id)

        # As an undocumented option, allow passing in a room_id to check an entire room.
        if is_room_id_or_alias(server_to_check):
            origin_server = await self.get_origin_server_and_assert_key_exists(command_event)
            if origin_server is None:
                return
            destination_server = origin_server
            room_data = await self.get_room_data(
                origin_server,
                destination_server,
                command_event,
                # We determined above that this is actually a room_id or alias
                server_to_check,
                get_servers_in_room=True,
                use_origin_room_as_fallback=False,
            )
            if not room_data:
                # Don't need to actually display an error, that's handled in the above
                # function
                return
            assert room_data.list_of_servers_in_room is not None
            list_of_servers_to_check = room_data.list_of_servers_in_room

        # If the whole room was to be searched, this will already be filled in. Otherwise, there is our target
        if not list_of_servers_to_check:
            list_of_servers_to_check.append(server_to_check)

        number_of_servers = len(list_of_servers_to_check)
        if number_of_servers > 1 and display_raw:
            await command_event.respond(
                "Only can see raw JSON data if a single server is selected(as the response would be super spammy).",
            )
            return

        server_to_server_data: dict[str, MatrixResponse] = {}

        about_statement = ""
        if number_of_servers == 1:
            about_statement = f"from {list_of_servers_to_check[0]} "
        else:
            about_statement = f"from {number_of_servers} server{'s' if number_of_servers > 1 else ''}"
        prerender_message = await command_event.respond(
            f"Retrieving server keys {about_statement}\n\n"
            # Because we may or may not have processed the origin_server above
            f"Using {self.federation_handler.hosting_server}",
        )

        list_of_message_ids: list[EventID] = [prerender_message]

        async def _server_keys_worker(queue: asyncio.Queue[str]) -> None:
            while True:
                worker_server_name = await queue.get()
                try:
                    server_to_server_data[worker_server_name] = await self.federation_handler.api.get_server_keys(
                        worker_server_name,
                    )
                except Exception as e:
                    self.log.error("_server_keys_worker: %r", e, stack_info=True)
                    pass
                queue.task_done()

        keys_queue: asyncio.Queue[str] = asyncio.Queue()
        for server_name in list_of_servers_to_check:
            await keys_queue.put(server_name)

        reference_key = self.reaction_task_controller.setup_task_set(command_event.event_id)
        self.reaction_task_controller.add_tasks(
            reference_key,
            _server_keys_worker,
            keys_queue,
            limit=MAX_NUMBER_OF_SERVERS_TO_ATTEMPT,
        )

        started_at = time.monotonic()
        await keys_queue.join()

        total_time = time.monotonic() - started_at
        # Cancel our worker tasks.
        await self.reaction_task_controller.cancel(reference_key)

        # Want it to look like this for now
        #
        #      Server Name | Key ID | Valid until(UTC)
        # ---------------------------------------
        # littlevortex.net | aRGvs  | Pretty formatted DateTime
        #       matrix.org | aYp3g  | Pretty formatted DateTime
        #                  | 0ldK3y | EXPIRED: Expired DateTime

        server_name_col = DisplayLineColumnConfig("Server Name", justify=Justify.RIGHT)
        server_key_col = DisplayLineColumnConfig("Key ID")
        valid_until_ts_col = DisplayLineColumnConfig("Valid until(UTC)")

        for server_name, server_results in server_to_server_data.items():
            # For sizing columns, don't care about errors
            server_name_col.maybe_update_column_width(len(server_name))
            keyid_block = server_results.json_response.get("verify_keys", {})
            oldkeyid_block = server_results.json_response.get("old_verify_keys", {})
            for key_id in keyid_block:
                server_key_col.maybe_update_column_width(key_id)
            for key_id in oldkeyid_block:
                server_key_col.maybe_update_column_width(key_id)

        # Begin constructing the message

        # Build the header line
        header_message = f"{server_name_col.pad()} | {server_key_col.pad()} | {valid_until_ts_col.header_name}\n"

        # Need the total of the width for the code block table to make the delimiter
        total_srv_line_size = len(header_message)

        # Create the delimiter line under the header
        header_message += f"{pad('', total_srv_line_size, pad_with='-')}\n"

        # The collection of rendered lines. This will be chunked into a paged response
        list_of_result_data = []
        # Begin the data render. Use the sorted list, alphabetical looks nicer. Even
        # if there were errors, there will be data available.
        for server_name, server_results in sorted(server_to_server_data.items()):
            buffered_message = ""
            buffered_message += f"{server_name_col.pad(server_name)} | "
            first_line = True
            if server_results.http_code != 200:
                buffered_message += f"{server_results.reason}\n"

            else:
                time_now = int(time.time() * 1000)
                keyid_block = server_results.json_response.get("verify_keys", {})
                oldkeyid_block = server_results.json_response.get("old_verify_keys", {})
                # Probably don't care much about this one, as it's the end of a line
                valid_until_ts: int = server_results.json_response.get("valid_until_ts", 0)

                # There will not be more than a single key.
                for key_id in keyid_block:
                    valid_until_pretty = "None Found"

                    if valid_until_ts > 0:
                        valid_until_pretty = pretty_print_timestamp(valid_until_ts)

                    if not first_line:
                        buffered_message += f"{server_name_col.pad('')} | "

                    # This will mark the display with a * to visually express expired
                    pretty_expired_marker = "*" if valid_until_ts < time_now else ""
                    buffered_message += f"{server_key_col.pad(key_id)} | "
                    buffered_message += f"{pretty_expired_marker}{valid_until_pretty}\n"
                    first_line = False

                for key_id, key_data in oldkeyid_block.items():
                    expired_pretty = "None Found"
                    expired_ts = key_data.get("expired_ts", 0)
                    if expired_ts > 0:
                        expired_pretty = pretty_print_timestamp(expired_ts)

                    if not first_line:
                        buffered_message += f"{server_name_col.pad('')} | "

                    # This will mark the display with a * to visually express expired
                    pretty_expired_marker = "*" if expired_ts < time_now else ""
                    buffered_message += f"{server_key_col.pad(key_id)} | "
                    buffered_message += f"{pretty_expired_marker}{expired_pretty}\n"
                    # extremely unlikely this is needed
                    # first_line = False

            list_of_result_data.extend([buffered_message])

            # Only if there was a single server because of the above condition
            if display_raw:
                list_of_result_data.extend([f"{json.dumps(server_results.json_response, indent=4)}\n"])

        footer_message = f"\nTotal time for retrieval: {total_time:.3f} seconds\n"
        list_of_result_data.extend([footer_message])

        final_list_of_data = combine_lines_to_fit_event(list_of_result_data, header_message)

        for chunk in final_list_of_data:
            current_message = await command_event.respond(
                make_into_text_event(wrap_in_code_block_markdown(chunk), ignore_body=True),
            )
            list_of_message_ids.extend([current_message])
        for message_id in list_of_message_ids:
            await self.reaction_task_controller.add_cleanup_control(message_id, command_event.room_id)

    @fed_command.subcommand(name="notary_server_keys")
    @command.argument(name="server_to_check", required=True)
    @command.argument(name="notary_server_to_use", required=False)
    async def notary_server_keys_command(
        self,
        command_event: MessageEvent,
        server_to_check: str,
        notary_server_to_use: str | None,
    ) -> None:

        await self._server_keys_from_notary(command_event, server_to_check, notary_server_to_use=notary_server_to_use)

    @fed_command.subcommand(name="notary_server_keys_raw")
    @command.argument(name="server_to_check", required=True)
    @command.argument(name="notary_server_to_use", required=False)
    async def notary_server_keys_raw_command(
        self,
        command_event: MessageEvent,
        server_to_check: str,
        notary_server_to_use: str | None,
    ) -> None:

        await self._server_keys_from_notary(
            command_event,
            server_to_check,
            notary_server_to_use=notary_server_to_use,
            display_raw=True,
        )

    async def _server_keys_from_notary(
        self,
        command_event: MessageEvent,
        server_to_check: str,
        notary_server_to_use: str | None,
        display_raw: bool = False,
    ) -> None:
        """
        Get and display server key information from a notary server.

        Fetches server signing keys via a notary server and displays them
        in a formatted table, optionally showing raw JSON.

        Args:
            command_event: Event that triggered the command
            server_to_check: Server name to check keys for
            notary_server_to_use: Notary server to query, defaults to sender's server
            display_raw: Whether to display raw JSON response
        """
        list_of_servers_to_check: list[str] = []
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        if not server_to_check:
            server_to_check = str(command_event.room_id)

        # As an undocumented option, allow passing in a room_id to check an entire room.
        if is_room_id_or_alias(server_to_check):
            origin_server = await self.get_origin_server_and_assert_key_exists(command_event)
            if origin_server is None:
                return
            destination_server = origin_server
            room_data = await self.get_room_data(
                origin_server,
                destination_server,
                command_event,
                # We determined above that this is actually a room_id or alias
                server_to_check,
                get_servers_in_room=True,
                use_origin_room_as_fallback=False,
            )
            if not room_data:
                # Don't need to actually display an error, that's handled in the above
                # function
                return
            assert room_data.list_of_servers_in_room is not None
            list_of_servers_to_check = room_data.list_of_servers_in_room

        # If the whole room was to be searched, this will already be filled in. Otherwise, there is our target
        if not list_of_servers_to_check:
            list_of_servers_to_check.append(server_to_check)

        number_of_servers = len(list_of_servers_to_check)
        if number_of_servers > 1 and display_raw:
            await command_event.respond(
                "Only can see raw JSON data if a single server is selected(as the response would be super spammy).",
            )
            return

        if not notary_server_to_use:
            notary_server_to_use = get_domain_from_id(command_event.sender)

        about_statement = ""
        if number_of_servers == 1:
            about_statement = f"about {list_of_servers_to_check} "
        prerender_message = await command_event.respond(
            f"Retrieving data {about_statement}from {notary_server_to_use} for "
            f"{number_of_servers} server{'s' if number_of_servers > 1 else ''}\n"
            # Because we may or may not have processed the origin_server above
            f"Using {self.federation_handler.hosting_server}",
        )
        list_of_message_ids: list[EventID] = [prerender_message]

        server_to_server_data: dict[str, MatrixResponse] = {}
        minimum_valid_until_ts = int(time.time() * 1000) + (30 * 60 * 1000)  # Add 30 minutes

        async def _server_keys_from_notary_worker(
            _queue: asyncio.Queue[str],
        ) -> None:
            while True:
                worker_server_name = await _queue.get()

                result = await self.federation_handler.api.get_server_notary_keys(
                    worker_server_name,
                    notary_server_to_use,
                    minimum_valid_until_ts,
                )

                server_to_server_data[worker_server_name] = result
                _queue.task_done()

        keys_queue: asyncio.Queue[str] = asyncio.Queue()
        for server_name in list_of_servers_to_check:
            await keys_queue.put(server_name)

        # Setup the task into the controller
        reference_key = self.reaction_task_controller.setup_task_set(command_event.event_id)

        self.reaction_task_controller.add_tasks(
            reference_key,
            _server_keys_from_notary_worker,
            keys_queue,
            limit=MAX_NUMBER_OF_SERVERS_TO_ATTEMPT,
        )

        started_at = time.monotonic()

        await keys_queue.join()

        total_time = time.monotonic() - started_at
        # Cancel our worker tasks.
        await self.reaction_task_controller.cancel(reference_key)

        # Preprocess the data to get the column sizes
        # Want it to look like this for now, for the whole room version. Obviously a
        # single line of the same for the 'one server' version.
        #
        #      Server Name | Key ID | Valid until(UTC)
        # ---------------------------------------
        # littlevortex.net | aRGvs  | Pretty formatted DateTime
        #       matrix.org | aYp3g  | Pretty formatted DateTime
        #                  | 0ldK3y | EXPIRED: Expired DateTime

        server_name_col = DisplayLineColumnConfig("Server Name", justify=Justify.RIGHT)
        server_key_col = DisplayLineColumnConfig("Key ID")
        valid_until_ts_col = DisplayLineColumnConfig("Valid until(UTC)")

        for server_name, server_results in server_to_server_data.items():
            server_name_col.maybe_update_column_width(len(server_name))

            # Not worried about column size for errors
            if server_results.http_code == 200:
                server_key_list: list[dict[str, Any]] = server_results.json_response.get("server_keys", [])
                for server_key_entry in server_key_list:
                    key_ids: dict[str, Any] = server_key_entry.get("verify_keys", {})
                    old_verify_keys: dict[str, Any] = server_key_entry.get("old_verify_keys", {})
                    for key_id in chain(key_ids, old_verify_keys):
                        # cast this to a str explicitly, in case someone gets funny ideas
                        server_key_col.maybe_update_column_width(str(key_id))

        # Begin constructing the message

        # Build the header line
        header_message = f"{server_name_col.pad()} | {server_key_col.pad()} | {valid_until_ts_col.header_name}\n"

        # Need the total of the width for the code block table to make the delimiter
        total_srv_line_size = len(header_message)

        # Create the delimiter line under the header
        header_message += f"{pad('', total_srv_line_size, pad_with='-')}\n"

        # The collection of lines to be chunked later
        list_of_result_data = []
        # Use a sorted list of server names, so it displays in alphabetical order.
        for server_name, server_results in sorted(server_to_server_data.items()):
            # There will only be data for servers that didn't time out
            first_line = True
            buffered_message = f"{server_name_col.pad(server_name)} | "
            if server_results.http_code != 200:
                buffered_message += f"{server_results.http_code}: {server_results.reason}\n"

            else:
                time_now = int(time.time() * 1000)
                server_key_list = server_results.json_response.get("server_keys", [])
                for server_key_entry in server_key_list:
                    key_ids = server_key_entry.get("verify_keys", {})
                    valid_until_ts = server_key_entry.get("valid_until_ts", 0)
                    valid_until_pretty = pretty_print_timestamp(valid_until_ts)
                    pretty_expired_mark = "*" if valid_until_ts < time_now else ""
                    old_verify_keys = server_key_entry.get("old_verify_keys", {})
                    for key_id in key_ids:
                        if not first_line:
                            buffered_message += f"{server_name_col.pad('')} | "
                        buffered_message += f"{server_key_col.pad(key_id)} | "
                        # Don't care about padding, as this is end of line
                        buffered_message += f"{pretty_expired_mark}{valid_until_pretty}\n"

                        first_line = False

                    for old_key_id, old_key_data in old_verify_keys.items():
                        expired_ts = old_key_data.get("expired_ts", 0)
                        expired_pretty = pretty_print_timestamp(expired_ts)
                        pretty_expired_mark = "*" if expired_ts < time_now else ""
                        if not first_line:
                            buffered_message += f"{server_name_col.pad('')} | "
                        buffered_message += f"{server_key_col.pad(old_key_id)} | "
                        # Don't care about padding, as this is end of line
                        buffered_message += f"{pretty_expired_mark}{expired_pretty}\n"
                        first_line = False

            list_of_result_data.extend([buffered_message])

            # Only if there was a single server because of the above condition
            if display_raw:
                list_of_result_data.extend([f"{json.dumps(server_results.json_response, indent=4)}\n"])

        footer_message = f"\nTotal time for retrieval: {total_time:.3f} seconds\n"
        list_of_result_data.extend([footer_message])

        final_list_of_data = combine_lines_to_fit_event(list_of_result_data, header_message)

        for chunk in final_list_of_data:
            current_message = await command_event.respond(
                make_into_text_event(wrap_in_code_block_markdown(chunk), ignore_body=True),
            )
            list_of_message_ids.extend([current_message])

        for message_id in list_of_message_ids:
            await self.reaction_task_controller.add_cleanup_control(message_id, command_event.room_id)

    @fed_command.subcommand(
        name="backfill",
        help="Request backfill over federation for a room. Provide a room ID/Alias to get latest from that room. "
        "Provide an Event ID to backfill from that place in time",
    )
    @command.argument(name="room_id_or_alias", parser=is_room_id_or_alias, required=False)
    @command.argument(name="event_id", parser=is_event_id, required=False)
    @command.argument(name="limit", required=False)
    @command.argument(name="server_to_request_from", required=False)
    async def backfill_command(
        self,
        command_event: MessageEvent,
        *,
        room_id_or_alias: str | None,
        event_id: str | None,
        limit: str | None = None,
        server_to_request_from: str | None = None,
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        if not limit:
            limit = "10"
        try:
            limit_int = int(limit)
        except ValueError:
            await command_event.reply(f"I got a limit number that could not be converted into an integer: {limit}")
            return

        origin_server = await self.get_origin_server_and_assert_key_exists(command_event)
        if not origin_server:
            return

        destination_server = server_to_request_from or origin_server

        room_data = await self.get_room_data(
            origin_server,
            destination_server,
            command_event,
            room_id_or_alias,
            get_servers_in_room=False,
            use_origin_room_as_fallback=True,
        )

        if not event_id and room_data:
            event_id = room_data.detected_last_event_id
            timestamp = room_data.timestamp_of_last_event_id
            room_id = room_data.room_id
        else:
            assert event_id is not None, "Event ID can not be None here"
            event = await self.federation_handler.get_event(origin_server, destination_server, event_id)
            timestamp = event.origin_server_ts
            room_id = event.room_id

        prerender_message = await command_event.respond(
            f"Retrieving last {limit} Events for \n"
            f"* Room: {room_id}\n"
            f"* at Event ID: {event_id}\n"
            f"\n  * which took place at: {pretty_print_timestamp(timestamp)} UTC"
            f"* From {destination_server} using {origin_server}",
        )
        list_of_message_ids: list[EventID] = [prerender_message]

        pdu_list_from_response = await self.federation_handler.get_events_from_backfill(
            origin_server,
            destination_server,
            room_id,
            event_id,
            limit=limit_int,
        )

        # Time to start rendering. Build the header lines first
        header_message = ""
        dc_depth = DisplayLineColumnConfig("Depth")
        dc_eid = DisplayLineColumnConfig("Event ID", initial_size=44)
        dc_etype = DisplayLineColumnConfig("Event Type")
        dc_sender = DisplayLineColumnConfig("Sender")
        dc_extras = DisplayLineColumnConfig("Extras")

        # Reconstruct the list so it can be sorted by depth
        pdu_list: list[tuple[int, EventBase]] = []
        for event_base in pdu_list_from_response:
            # Don't worry about resizing the 'Extras' Column,
            # it's on the end and variable length
            dc_depth.maybe_update_column_width(len(str(event_base.depth)))
            dc_etype.maybe_update_column_width(len(event_base.event_type))
            dc_sender.maybe_update_column_width(len(event_base.sender))

            pdu_list.append((event_base.depth, event_base))

        # Sort the list in place by the first of the tuples, which is the depth
        pdu_list.sort(key=lambda x: x[0])

        # Build the header line...
        header_message += f"{dc_depth.pad()} "
        header_message += f"{dc_eid.pad()} "
        header_message += f"{dc_etype.pad()} "
        header_message += f"{dc_sender.pad()} "
        header_message += f"{dc_extras.pad()}\n"

        # ...and the delimiter
        header_message += f"{pad('', pad_to=len(header_message), pad_with='-')}\n"
        list_of_buffer_lines = []

        # Begin the render, first construct the template list
        template_list = [
            (["depth"], dc_depth),
            (["event_id"], dc_eid),
            (["event_type"], dc_etype),
            (["sender"], dc_sender),
        ]
        for _, event_base in pdu_list:
            line_summary = event_base.to_template_line_summary(template_list)
            line_summary += " "
            line_summary += event_base.to_extras_summary()
            line_summary += "\n"

            list_of_buffer_lines.extend([line_summary])

        # Chunk the data as there may be a few 'pages' of it
        final_list_of_data = combine_lines_to_fit_event(list_of_buffer_lines, header_message)

        for chunk in final_list_of_data:
            current_message = await command_event.respond(
                make_into_text_event(wrap_in_code_block_markdown(chunk), ignore_body=True),
            )
            list_of_message_ids.extend([current_message])
        for message_id in list_of_message_ids:
            await self.reaction_task_controller.add_cleanup_control(message_id, command_event.room_id)

    @fed_command.subcommand(name="event_auth", help="Request the auth chain for an event over federation")
    @command.argument(name="event_id", parser=is_event_id, required=True)
    @command.argument(name="server_to_request_from", required=False)
    async def event_auth_command(
        self,
        command_event: MessageEvent,
        event_id: str,
        server_to_request_from: str | None = None,
    ) -> None:
        # Unlike some of the other commands, this one *requires* an event_id passed in.

        # Let the user know the bot is paying attention
        await command_event.mark_read()

        origin_server = await self.get_origin_server_and_assert_key_exists(command_event)
        if not origin_server:
            return

        destination_server = server_to_request_from or origin_server

        event_mapping = await self.federation_handler.get_event_from_server(origin_server, destination_server, event_id)
        event = event_mapping.get(event_id)
        if isinstance(event, EventError):
            await self.log_to_client(
                command_event, f"The event ID supplied produced an error\n\n{event.errcode}:{event.error}"
            )
            return

        assert isinstance(event, EventBase)
        room_id = event.room_id

        prerender_message = await command_event.respond(
            "Retrieving the chain of Auth Events for:\n"
            f"* Event ID: {event_id}\n"
            f"* in Room: {room_id}\n"
            f"\n  * which took place at: {pretty_print_timestamp(event.origin_server_ts)} UTC"
            f"* From {destination_server} using {origin_server}",
        )
        list_of_message_ids: list[EventID] = [prerender_message]

        started_at = time.monotonic()
        try:
            list_of_event_bases = await self.federation_handler.get_event_auth(
                origin_server,
                destination_server,
                room_id,
                event_id,
            )
        except MatrixError as e:
            await command_event.respond(f"Some kind of error\n{e.http_code}:{e.reason}")
            return

        total_time = time.monotonic() - started_at

        # Time to start rendering. Build the header lines first
        header_message = ""
        dc_depth = DisplayLineColumnConfig("Depth")
        dc_eid = DisplayLineColumnConfig("Event ID")
        dc_etype = DisplayLineColumnConfig("Event Type")
        dc_sender = DisplayLineColumnConfig("Sender")
        dc_extras = DisplayLineColumnConfig("Extras")

        ordered_list: list[tuple[int, EventBase]] = []
        for event in list_of_event_bases:
            # Don't worry about resizing the 'Extras' Column,
            # it's on the end and variable length
            dc_depth.maybe_update_column_width(len(str(event.depth)))
            dc_eid.maybe_update_column_width(len(str(event.event_id)))
            dc_etype.maybe_update_column_width(len(event.event_type))
            dc_sender.maybe_update_column_width(len(event.sender))

            ordered_list.append((event.depth, event))

        # Sort the list in place by the first of the tuples, which is the depth
        ordered_list.sort(key=lambda x: x[0])

        # Build the header line...
        header_message += f"{dc_depth.pad()} "
        header_message += f"{dc_eid.pad()}"
        header_message += f"{dc_etype.pad()} "
        header_message += f"{dc_sender.pad()} "
        header_message += f"{dc_extras.pad()}\n"

        # ...and the delimiter
        header_message += f"{pad('', pad_to=len(header_message), pad_with='-')}\n"
        list_of_buffer_lines = []

        # Begin the render, first construct the template list
        template_list = [
            (["depth"], dc_depth),
            (["event_id"], dc_eid),
            (["event_type"], dc_etype),
            (["sender"], dc_sender),
        ]
        for _, event_base in ordered_list:
            buffered_message = ""
            line_summary = event_base.to_template_line_summary(template_list)
            line_summary += " "
            line_summary += event_base.to_extras_summary()

            buffered_message += f"{line_summary}\n"

            list_of_buffer_lines.extend([buffered_message])

        footer_message = f"\nTotal time for retrieval: {total_time:.3f} seconds\n"
        list_of_buffer_lines.extend([footer_message])

        # Chunk the data as there may be a few 'pages' of it
        final_list_of_data = combine_lines_to_fit_event(list_of_buffer_lines, header_message)

        for chunk in final_list_of_data:
            current_message = await command_event.respond(
                make_into_text_event(wrap_in_code_block_markdown(chunk), ignore_body=True),
            )
            list_of_message_ids.extend([current_message])

        for message_id in list_of_message_ids:
            await self.reaction_task_controller.add_cleanup_control(message_id, command_event.room_id)

    @fed_command.subcommand(name="user_devices", help="Request user devices over federation for a user.")
    @command.argument(name="user_mxid", parser=is_mxid, required=True)
    async def user_devices_command(
        self,
        command_event: MessageEvent,
        user_mxid: str,
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        origin_server = await self.get_origin_server_and_assert_key_exists(command_event)
        if not origin_server:
            return

        _, destination_server = user_mxid.split(":", maxsplit=1)

        prerender_message = await command_event.respond(
            f"Retrieving user devices for {user_mxid}\n* From {destination_server} using {origin_server}",
        )
        list_of_message_ids: list[EventID] = [prerender_message]

        response = await self.federation_handler.api.get_user_devices(
            origin_server,
            destination_server,
            user_mxid,
        )

        if response.http_code != 200:
            await command_event.respond(
                f"Some kind of error\n{response.http_code}:{response.reason}\n\n"
                f"{json.dumps(response.json_response, indent=4)}",
            )
            return

        message_id = await command_event.respond(f"```json\n{json.dumps(response.json_response, indent=4)}\n```\n")
        list_of_message_ids.extend([message_id])

        for message_id in list_of_message_ids:
            await self.reaction_task_controller.add_cleanup_control(message_id, command_event.room_id)

        # # Chunk the data as there may be a few 'pages' of it
        # final_list_of_data = combine_lines_to_fit_event(
        #     list_of_buffer_lines, header_message
        # )
        #
        # for chunk in final_list_of_data:
        #     await command_event.respond(
        #         make_into_text_event(
        #             wrap_in_code_block_markdown(chunk), ignore_body=True
        #         ),
        #     )

    @fed_command.subcommand(name="find_event", help="Search all hosts in a given room for a given Event")
    @command.argument(name="event_id", parser=is_event_id, required=True)
    @command.argument(name="room_id_or_alias", parser=is_room_id_or_alias, required=True)
    async def find_event_command(
        self,
        command_event: MessageEvent,
        event_id: str,
        room_id_or_alias: str,
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        origin_server = await self.get_origin_server_and_assert_key_exists(command_event)
        if not origin_server:
            return

        room_data = await self.get_room_data(
            origin_server,
            origin_server,
            command_event,
            room_id_or_alias,
            get_servers_in_room=True,
            use_origin_room_as_fallback=False,
        )
        if not room_data:
            # Don't need to actually display an error, that's handled in the above
            # function
            return

        prerender_message = await command_event.respond(
            f"Checking all hosts:\n"
            f"* from Room: {room_data.room_id}\n\n"
            f"for:\n"
            f"* Event ID: {event_id}\n"
            f"* Using {origin_server}\n\n"
            "Note: if there are more than 1,000 servers in this room, this may fail or take a long time.",
        )
        list_of_message_ids: list[EventID] = [prerender_message]

        started_at = time.time()
        assert isinstance(room_data.list_of_servers_in_room, list), "list of servers in room should be available"
        host_to_event_status_map = await self.federation_handler.find_event_on_servers(
            origin_server,
            event_id,
            room_data.list_of_servers_in_room,
        )
        total_time = time.time() - started_at

        # Begin the render
        dc_host_config = DisplayLineColumnConfig("Hosts", justify=Justify.RIGHT)
        dc_result_config = DisplayLineColumnConfig("Results")

        for host in host_to_event_status_map:
            dc_host_config.maybe_update_column_width(len(host))

        header_message = f"Hosts that found event '{event_id}'\n"
        list_of_result_data = []
        servers_had = 0
        servers_not_had = 0
        for host in room_data.list_of_servers_in_room:
            result = host_to_event_status_map.get(host)
            buffered_message = ""
            if result:
                if isinstance(result, EventError):
                    buffered_message += (
                        f"{dc_host_config.pad(host)}"
                        f"{dc_host_config.horizontal_separator}"
                        f"{dc_result_config.pad('Fail')}"
                        # f"{dc_result_config.pad(add_color(bold('Fail'), foreground=Colors.WHITE, background=Colors.RED))}"
                        f"{dc_host_config.horizontal_separator}{result.error}"
                    )
                    servers_not_had += 1
                else:
                    buffered_message += (
                        f"{dc_host_config.pad(host)}{dc_host_config.horizontal_separator}{dc_result_config.pad('OK')}"
                        # f"{dc_result_config.pad(add_color(bold('OK'), foreground=Colors.WHITE, background=Colors.GREEN))}"
                    )
                    servers_had += 1
            else:
                # The "unlikely to ever be hit" error
                buffered_message += (
                    f"{dc_host_config.pad(host)}"
                    f"{dc_host_config.horizontal_separator}"
                    f"{dc_result_config.pad('Fail')}"
                    f"{dc_host_config.horizontal_separator}"
                    "Plugin error(Host not contacted)"
                )

            # remove the new line for <code> tags
            list_of_result_data.extend([f"{buffered_message}\n"])

        # remove the new line for <code> tags
        footer_message = (
            f"\nTotal time for retrieval: {total_time:.3f} seconds\n"
            f"Servers Good: {servers_had}\n"
            f"Servers Fail: {servers_not_had}\n"
        )
        list_of_result_data.extend([footer_message])

        # For a single server test, the response will fit into a single message block.
        # However, for a roomful it could be several pages long. Chunk those responses
        # to fit into the size limit of an Event.
        final_list_of_data = combine_lines_to_fit_event(list_of_result_data, header_message)

        for chunk in final_list_of_data:
            message_id = await command_event.respond(
                make_into_text_event(wrap_in_code_block_markdown(chunk), ignore_body=True),
            )
            list_of_message_ids.extend([message_id])

        for message_id in list_of_message_ids:
            await self.reaction_task_controller.add_cleanup_control(message_id, command_event.room_id)

    @fed_command.subcommand(name="publicrooms_raw")
    @command.argument(name="target_server", required=True)
    @command.argument(name="since", required=False)
    async def publicrooms_raw_subcommand(
        self,
        command_event: MessageEvent,
        target_server: str,
        since: str | None,
    ) -> None:
        """
        Get raw public rooms list.

        Retrieves and displays raw JSON response of public rooms list.

        Args:
            command_event: The event that triggered the command
            target_server: Server to query public rooms from
            since: Optional pagination token
        """
        await command_event.mark_read()

        public_room_result = await self.federation_handler.get_public_rooms_from_server(
            None,
            target_server,
            since=since,
        )
        await command_event.respond(wrap_in_code_block_markdown(json.dumps(public_room_result.json_response, indent=4)))

    @fed_command.subcommand(name="publicrooms")
    @command.argument(name="target_server", required=True)
    async def publicrooms_subcommand(self, command_event: MessageEvent, target_server: str) -> None:
        """
        Get formatted public rooms list.

        Retrieves and displays public rooms list in a formatted table.

        Args:
            command_event: The event that triggered the command
            target_server: Server to query public rooms from
        """
        await command_event.mark_read()

        # DisplayConfig objects, per column
        alias_dc = DisplayLineColumnConfig("canonical_alias")
        room_id_dc = DisplayLineColumnConfig("room_id")
        name_dc = DisplayLineColumnConfig("name")
        num_joined_members_dc = DisplayLineColumnConfig("num_joined_members")
        join_rule_dc = DisplayLineColumnConfig("join_rule")
        world_readable_dc = DisplayLineColumnConfig("world_readable")
        guest_can_join_dc = DisplayLineColumnConfig("guest_can_join")
        avatar_url_dc = DisplayLineColumnConfig("avatar_url")

        done = False
        since = None
        collected_chunks_fragments = []
        retry_count = 0
        while not done:
            public_room_result = await self.federation_handler.get_public_rooms_from_server(
                None,
                target_server,
                since=since,
            )
            if public_room_result.http_code != 200:
                self.log.warning(
                    "Hit an error on public rooms: %d %s", public_room_result.http_code, public_room_result.reason
                )
                if retry_count > 3:
                    await command_event.respond(
                        f"Hit an error on public rooms after {retry_count + 1} retry attempts: {public_room_result.http_code}: {public_room_result.reason} on {target_server}",
                    )
                    return
                retry_count += 1
                await asyncio.sleep(3)
                continue

            since = public_room_result.json_response.get("next_batch", None)
            self.log.info("Next batch: %r", since)
            if not since:
                done = True

            chunks: list[dict[str, Any]] = public_room_result.json_response.get("chunk", [])
            for entry in chunks:
                alias_dc.maybe_update_column_width(entry.get("canonical_alias", None))
                room_id_dc.maybe_update_column_width(entry.get("room_id", None))
                name_dc.maybe_update_column_width(entry.get("name", None))
                # This next one gives up an int, which means the column will be huge. string it
                num_joined_members_dc.maybe_update_column_width(str(entry.get("num_joined_members", None)))
                join_rule_dc.maybe_update_column_width(entry.get("join_rule", None))
                world_readable_dc.maybe_update_column_width(entry.get("world_readable", None))
                guest_can_join_dc.maybe_update_column_width(entry.get("guest_can_join", None))
                avatar_url_dc.maybe_update_column_width(entry.get("avatar_url", None))

                collected_chunks_fragments.append(entry)
            # TODO: after implementing auto-retry lose this
            await asyncio.sleep(0.5)

        list_of_buffered_lines = []
        for chunk in collected_chunks_fragments:
            list_of_buffered_lines.append(
                f"{room_id_dc.pad(chunk.get('room_id', ''))}: "
                f"{alias_dc.pad(chunk.get('canonical_alias', ''))} "
                f"{name_dc.pad(chunk.get('name', ''))} "
                f"{num_joined_members_dc.pad(chunk.get('num_joined_members', ''))} "
                f"{join_rule_dc.pad(chunk.get('join_rule', ''))} "
                f"{guest_can_join_dc.pad(chunk.get('guest_can_join', ''))} "
                f"{world_readable_dc.pad(chunk.get('world_readable', ''))} "
                f"{avatar_url_dc.pad(chunk.get('avatar_url', ''))}",
            )

        header_message = (
            f"{room_id_dc.pad()}: "
            f"{alias_dc.pad()} "
            f"{name_dc.pad()} "
            f"{num_joined_members_dc.pad()} "
            f"{join_rule_dc.pad()} "
            f"{guest_can_join_dc.pad()} "
            f"{world_readable_dc.pad()} "
            f"{avatar_url_dc.pad()}"
        )
        final_lines = combine_lines_to_fit_event(list_of_buffered_lines, header_message, insert_new_lines=True)
        list_of_message_ids: list[EventID] = []
        for line in final_lines:
            message_id = await command_event.respond(
                make_into_text_event(wrap_in_code_block_markdown(line), ignore_body=True),
            )
            list_of_message_ids.extend([message_id])
        for message_id in list_of_message_ids:
            await self.reaction_task_controller.add_cleanup_control(message_id, command_event.room_id)

    @test_command.subcommand(name="wellknown")
    @command.argument(name="server_to_check", label="Server To Check", required=True)
    async def well_know_command(self, command_event: MessageEvent, server_to_check: str) -> None:
        """
        Check server delegation information.

        Checks server discovery and delegation setup including well-known
        records and SRV records.

        Args:
            command_event: Event that triggered the command
            server_to_check: Server name to check delegation for
        """
        list_of_servers_to_check = set()

        await command_event.mark_read()

        # It may be that they are using their mxid as the server to check, parse that
        maybe_user_mxid = is_mxid(server_to_check)
        if maybe_user_mxid:
            server_to_check = get_domain_from_id(maybe_user_mxid)

        # As an undocumented option, allow passing in a room_id to check an entire room.
        # This can be rather long(and time consuming) so we'll place limits later.
        maybe_room_id = is_room_id_or_alias(server_to_check)
        if maybe_room_id:
            origin_server = get_domain_from_id(self.client.mxid)
            room_to_check, _ = await self.resolve_room_id_or_alias(maybe_room_id, command_event, origin_server)
            # Need to cancel server_to_check, but can't use None
            server_to_check = ""
            if not room_to_check:
                # Don't need to actually display an error, that's handled in the above
                # function
                return
        else:
            # with server_to_check being set, this will be ignored any way
            room_to_check = command_event.room_id

        # server_to_check has survived this far, add it to the set of servers to search
        # for. Since we allow for searching an entire room, it will be the only server
        # in the set.
        if server_to_check:
            list_of_servers_to_check.add(server_to_check)

        # The list of servers was empty. This implies that a room_id was provided,
        # let's check.
        if not list_of_servers_to_check:
            try:
                assert room_to_check is not None
                joined_members = await self.client.get_joined_members(RoomID(room_to_check))

            except MForbidden:
                await command_event.respond(NOT_IN_ROOM_ERROR)
                return

            for member in joined_members:
                list_of_servers_to_check.add(get_domain_from_id(member))

        number_of_servers = len(list_of_servers_to_check)

        # Some quality of life niceties
        prerender_message = await command_event.respond(
            f"Retrieving data from federation for {number_of_servers} "
            f"server{'s.' if number_of_servers > 1 else '.'}\n"
            "This may take up to 30 seconds to complete.",
        )
        list_of_message_ids: list[EventID] = [prerender_message]

        # map of server name -> (server brand, server version)
        server_to_server_data: dict[str, WellKnownLookupResult] = {}
        set_of_server_names = set(list_of_servers_to_check)

        async def _delegation_worker(queue: asyncio.Queue[str]) -> None:
            while True:
                worker_server_name = await queue.get()

                # The 'get_server_version' function was written with the capability of
                # collecting diagnostic data.
                try:
                    server_to_server_data[worker_server_name] = (
                        await self.federation_handler.api.federation_transport.server_discovery.get_well_known(
                            worker_server_name, [], Diagnostics()
                        )
                    )
                    set_of_server_names.discard(worker_server_name)
                except Exception as e:
                    self.log.warning("delegation worker error on %s: %r", worker_server_name, e, exc_info=True)
                    raise
                queue.task_done()

        delegation_queue: asyncio.Queue[str] = asyncio.Queue()
        for server_name in list_of_servers_to_check:
            delegation_queue.put_nowait(server_name)

        reference_key = self.reaction_task_controller.setup_task_set(command_event.event_id)

        self.reaction_task_controller.add_tasks(
            reference_key,
            _delegation_worker,
            delegation_queue,
            limit=MAX_NUMBER_OF_SERVERS_TO_ATTEMPT,
        )

        started_at = time.monotonic()
        await delegation_queue.join()
        await self.reaction_task_controller.cancel(reference_key)
        total_time = time.monotonic() - started_at

        # Want the full room version it to look like this for now
        #
        #   Server Name | Status | Host                 | Port  | TLS served by  |
        # ------------------------------------------------------------------
        #   example.org |    200 | matrix.example.org   | 8448  | Synapse 1.92.0 |
        # somewhere.net |    404 | None                 | Error | resty          | Long error....
        #   maunium.net |    200 | meow.host.mau.fi     | 443   | Caddy          |

        # The single server version will be the same in that a single line like above
        # will be printed, then the rendered diagnostic data

        # Create the columns to be used
        server_name_col = DisplayLineColumnConfig("Server Name")
        status_col = DisplayLineColumnConfig("Status")
        host_col = DisplayLineColumnConfig("Host")
        port_col = DisplayLineColumnConfig("Port")
        tls_served_by_col = DisplayLineColumnConfig("TLS served by")
        # errors_col = DisplayLineColumnConfig("Errors")

        # Iterate through the server names to widen the column, if necessary.
        for server_name, response in server_to_server_data.items():
            # Only if it's not obnoxiously long, saw an over 66 once(you know who you are)
            if not len(server_name) > 30:
                server_name_col.maybe_update_column_width(len(server_name))
            if isinstance(response, WellKnownDiagnosticResult):
                host_col.maybe_update_column_width(response.host)
                port_col.maybe_update_column_width(response.port)
                if maybe_tls_server := response.headers.get("server", None):
                    tls_served_by_col.maybe_update_column_width(len(maybe_tls_server))

        # Just use a fixed width for the results. Should never be larger than 5 for most
        status_col.maybe_update_column_width(6)

        # Begin constructing the message
        #
        # Use a sorted list of server names, so it displays in alphabetical order.
        server_results_sorted = sorted(server_to_server_data.keys())

        servers_missing = list_of_servers_to_check - set(server_results_sorted)
        # Build the header line
        header_message = (
            f"{server_name_col.front_pad()} | "
            f"{status_col.pad()} | "
            f"{host_col.pad()} | "
            f"{port_col.pad()} | "
            f"{tls_served_by_col.pad()} | "
            f"Errors\n"
        )

        # Need the total of the width for the code block table to make the delimiter
        header_line_size = len(header_message)

        # Create the delimiter line under the header
        header_message += f"{pad('', header_line_size, pad_with='-')}\n"

        list_of_result_data = []
        # Use the sorted list from earlier, alphabetical looks nicer
        for server_name in server_results_sorted:
            response = server_to_server_data[server_name]

            # Want the full room version it to look like this for now
            #
            #   Server Name | Status | Host                 | Port  | TLS served by  | Errors
            # ------------------------------------------------------------------
            #   example.org |    200 | matrix.example.org   | 8448  | Synapse 1.92.0 |
            # somewhere.net |    404 | None                 | Error | resty          | Long error....
            #   maunium.net |    200 | meow.host.mau.fi     | 443   | Caddy          |

            buffered_message = f"{server_name_col.front_pad(server_name)} | "
            if isinstance(response, WellKnownDiagnosticResult):
                buffered_message += f"{status_col.pad(response.status_code)} | "
                buffered_message += f"{host_col.pad(response.host)} | "
                buffered_message += f"{port_col.pad(response.port)} | "

                maybe_tls_server = response.headers.get("server")
                buffered_message += f"{tls_served_by_col.pad(maybe_tls_server if maybe_tls_server else '')} | "

                buffered_message += "\n"

            else:
                assert isinstance(response, WellKnownLookupFailure)
                # Sometimes status_code can be None, but if we let that ride it shows up as the header name of the column
                buffered_message += f"{status_col.pad(response.status_code or '')} | "
                buffered_message += f"{response.reason}\n"

            list_of_result_data.extend([buffered_message])

        footer_message = f"\nTotal time for retrieval: {total_time:.3f} seconds\nservers missing: {servers_missing}\n"
        list_of_result_data.extend([footer_message])

        # For a single server test, the response will fit into a single message block.
        # However, for a roomful it could be several pages long. Chunk those responses
        # to fit into the size limit of an Event.
        final_list_of_data = combine_lines_to_fit_event(list_of_result_data, header_message)

        for chunk in final_list_of_data:
            current_message = await command_event.respond(
                make_into_text_event(wrap_in_code_block_markdown(chunk), ignore_body=True),
            )
            list_of_message_ids.extend([current_message])

        for current_message in list_of_message_ids:
            await self.reaction_task_controller.add_cleanup_control(current_message, command_event.room_id)

    @test_command.subcommand(name="dns")
    @command.argument(name="server_to_check", label="Server To Check", required=True)
    async def dns_command(self, command_event: MessageEvent, server_to_check: str) -> None:
        """
        Check server delegation information.

        Checks server discovery and delegation setup including well-known
        records and SRV records.

        Args:
            command_event: Event that triggered the command
            server_to_check: Server name to check delegation for
        """
        list_of_servers_to_check: list[str] = []

        await command_event.mark_read()

        # It may be that they are using their mxid as the server to check, parse that
        maybe_user_mxid = is_mxid(server_to_check)
        if maybe_user_mxid:
            server_to_check = get_domain_from_id(maybe_user_mxid)

        if not server_to_check:
            server_to_check = str(command_event.room_id)

        # As an undocumented option, allow passing in a room_id to check an entire room.
        # This can be rather long(and time consuming) so we'll place limits later.
        if is_room_id_or_alias(server_to_check):
            origin_server = await self.get_origin_server_and_assert_key_exists(command_event)
            if origin_server is None:
                return
            destination_server = origin_server
            room_data = await self.get_room_data(
                origin_server,
                destination_server,
                command_event,
                server_to_check,
                get_servers_in_room=True,
                use_origin_room_as_fallback=False,
            )
            if not room_data:
                # Don't need to actually display an error, that's handled in the above
                # function
                return
            assert room_data.list_of_servers_in_room is not None
            list_of_servers_to_check = room_data.list_of_servers_in_room

        # If the whole room was to be searched, this will already be filled in. Otherwise, there is our target
        if not list_of_servers_to_check:
            list_of_servers_to_check.append(server_to_check)

        number_of_servers = len(list_of_servers_to_check)

        # Some quality of life niceties
        prerender_message = await command_event.respond(
            f"Retrieving data from federation for {number_of_servers} "
            f"server{'s.' if number_of_servers > 1 else '.'}\n"
            "This may take up to 30 seconds to complete.",
        )
        list_of_message_ids: list[EventID] = [prerender_message]

        # map of server name -> (server brand, server version)
        server_to_server_data: dict[str, ServerDiscoveryDnsResult] = {}
        servers_to_contact = set(list_of_servers_to_check)

        async def _delegation_worker(queue: asyncio.Queue[str]) -> None:
            while True:
                worker_server_name = await queue.get()

                try:
                    server_to_server_data[worker_server_name] = (
                        await self.federation_handler.api.federation_transport.server_discovery.exp_dns_resolver.resolve_reg_records(
                            worker_server_name, Diagnostics()
                        )
                    )
                    servers_to_contact.discard(worker_server_name)
                except Exception as e:
                    self.log.warning("delegation worker error on %s: %r", worker_server_name, e, exc_info=True)
                    raise
                queue.task_done()

        delegation_queue: asyncio.Queue[str] = asyncio.Queue()
        for server_name in list_of_servers_to_check:
            delegation_queue.put_nowait(server_name)

        reference_key = self.reaction_task_controller.setup_task_set(command_event.event_id)

        self.reaction_task_controller.add_tasks(
            reference_key,
            _delegation_worker,
            delegation_queue,
            limit=MAX_NUMBER_OF_SERVERS_TO_ATTEMPT,
        )

        started_at = time.monotonic()
        await delegation_queue.join()
        await self.reaction_task_controller.cancel(reference_key)
        total_time = time.monotonic() - started_at

        # Create the columns to be used
        server_name_col = DisplayLineColumnConfig("Server Name")
        results_col = DisplayLineColumnConfig("Results")

        # Iterate through the server names to widen the column, if necessary.
        for server_name, response in server_to_server_data.items():
            # Only if it's not obnoxiously long, saw an over 66 once(you know who you are)
            if not len(server_name) > 30:
                server_name_col.maybe_update_column_width(len(server_name))

        # Begin constructing the message
        #
        # Use a sorted list of server names, so it displays in alphabetical order.
        server_results_sorted = sorted(server_to_server_data.keys())

        # Build the header line
        header_message = f"{server_name_col.front_pad()} | " f"{results_col.pad()} | \n"

        # Need the total of the width for the code block table to make the delimiter
        header_line_size = len(header_message)

        # Create the delimiter line under the header
        header_message += f"{pad('', header_line_size, pad_with='-')}\n"

        list_of_result_data = []
        # Use the sorted list from earlier, alphabetical looks nicer
        for server_name in server_results_sorted:
            response = server_to_server_data[server_name]
            buffered_message = f"{server_name_col.front_pad(server_name)} | "
            if response.a_result is not None:
                count = len(response.a_result.hosts)
                for a_address in response.a_result.hosts:
                    buffered_message += f"{results_col.pad(a_address)}"
                    if count > 1:
                        buffered_message += f"\n{server_name_col.front_pad('')} | "
                    count -= 1
            buffered_message += "\n"

            list_of_result_data.extend([buffered_message])

        footer_message = (
            f"\nTotal time for retrieval: {total_time:.3f} seconds\nservers missing: {servers_to_contact}\n"
        )
        list_of_result_data.extend([footer_message])

        # For a single server test, the response will fit into a single message block.
        # However, for a roomful it could be several pages long. Chunk those responses
        # to fit into the size limit of an Event.
        final_list_of_data = combine_lines_to_fit_event(list_of_result_data, header_message)

        for chunk in final_list_of_data:
            current_message = await command_event.respond(
                make_into_text_event(wrap_in_code_block_markdown(chunk), ignore_body=True),
            )
            list_of_message_ids.extend([current_message])

        for current_message in list_of_message_ids:
            await self.reaction_task_controller.add_cleanup_control(current_message, command_event.room_id)

    @test_command.subcommand(name="delegation")
    @command.argument(name="server_to_check", label="Server To Check", required=True)
    async def discover_server_command(self, command_event: MessageEvent, server_to_check: str) -> None:
        """
        Check server delegation information.

        Checks server discovery and delegation setup including well-known
        records and SRV records.

        Args:
            command_event: Event that triggered the command
            server_to_check: Server name to check delegation for
        """
        list_of_servers_to_check: list[str] = []

        await command_event.mark_read()

        # It may be that they are using their mxid as the server to check, parse that
        maybe_user_mxid = is_mxid(server_to_check)
        if maybe_user_mxid:
            server_to_check = get_domain_from_id(maybe_user_mxid)

        if not server_to_check:
            server_to_check = str(command_event.room_id)

        # As an undocumented option, allow passing in a room_id to check an entire room.
        # This can be rather long(and time consuming) so we'll place limits later.
        if is_room_id_or_alias(server_to_check):
            origin_server = await self.get_origin_server_and_assert_key_exists(command_event)
            if origin_server is None:
                return
            destination_server = origin_server
            room_data = await self.get_room_data(
                origin_server,
                destination_server,
                command_event,
                server_to_check,
                get_servers_in_room=True,
                use_origin_room_as_fallback=False,
            )
            if not room_data:
                # Don't need to actually display an error, that's handled in the above
                # function
                return
            assert room_data.list_of_servers_in_room is not None
            list_of_servers_to_check = room_data.list_of_servers_in_room

        # If the whole room was to be searched, this will already be filled in. Otherwise, there is our target
        if not list_of_servers_to_check:
            list_of_servers_to_check.append(server_to_check)

        number_of_servers = len(list_of_servers_to_check)

        # Some quality of life niceties
        prerender_message = await command_event.respond(
            f"Retrieving data from federation for {number_of_servers} "
            f"server{'s.' if number_of_servers > 1 else '.'}\n"
            "This may take up to 30 seconds to complete.",
        )
        list_of_message_ids: list[EventID] = [prerender_message]

        # map of server name -> (server brand, server version)
        server_to_server_data: dict[str, MatrixResponse] = {}
        # Use this to show which servers were uncontactable when rendering
        servers_to_contact = set(list_of_servers_to_check)

        async def _discover_worker(queue: asyncio.Queue[str]) -> None:
            while True:
                worker_server_name = await queue.get()

                try:
                    server_to_server_data[worker_server_name] = (
                        await self.federation_handler.api.get_server_version_new(
                            worker_server_name,
                            diagnostics=True,
                        )
                    )
                    servers_to_contact.discard(worker_server_name)
                except Exception as e:
                    self.log.warning("discover worker error on %s: %r", worker_server_name, e, exc_info=True)
                    pass
                queue.task_done()

        delegation_queue: asyncio.Queue[str] = asyncio.Queue()
        for server_name in list_of_servers_to_check:
            delegation_queue.put_nowait(server_name)

        reference_key = self.reaction_task_controller.setup_task_set(command_event.event_id)
        await self.client.set_typing(command_event.room_id, 1000 * 60 * 2)
        self.reaction_task_controller.add_tasks(
            reference_key,
            _discover_worker,
            delegation_queue,
            limit=100,
        )

        started_at = time.monotonic()
        await delegation_queue.join()
        await self.reaction_task_controller.cancel(reference_key)
        total_time = time.monotonic() - started_at
        await self.client.set_typing(command_event.room_id, 0)
        # Want the full room version to look like this for now
        #
        #   Server Name | WK   | SRV  | DNS  | Test  | SNI | SRT | TLS served by  |
        # ------------------------------------------------------------------
        #   example.org | OK   | None | OK   | OK    |     |     | Synapse 1.92.0 |
        # somewhere.net | None | None | None | Error |     |     | resty          | Long error....
        #   maunium.net | OK   | OK   | OK   | OK    | SNI |     | Caddy          |

        # The single server version will be the same in that a single line like above
        # will be printed, then the rendered diagnostic data

        # Create the columns to be used
        server_name_col = DisplayLineColumnConfig("Server Name")
        well_known_status_col = DisplayLineColumnConfig("WK", initial_size=5)
        srv_status_col = DisplayLineColumnConfig("SRV", initial_size=5)
        dns_status_col = DisplayLineColumnConfig("DNS", initial_size=5)
        connective_test_status_col = DisplayLineColumnConfig("Test", initial_size=5)
        sni_col = DisplayLineColumnConfig("SNI")
        srt_col = DisplayLineColumnConfig("SRT", 5, justify=Justify.RIGHT)
        crt_col = DisplayLineColumnConfig("CRT", 5, justify=Justify.RIGHT)
        tls_served_by_col = DisplayLineColumnConfig("TLS served by")

        # Iterate through the server names to widen the column, if necessary.
        for server_name, response in server_to_server_data.items():
            # Only if it's not obnoxiously long, saw an over 66 once(you know who you are)
            if not len(server_name) > 30:
                server_name_col.maybe_update_column_width(len(server_name))
            if isinstance(response, MatrixFederationResponse):
                well_known_status_col.maybe_update_column_width(response.diagnostics.status.well_known)
                srv_status_col.maybe_update_column_width(response.diagnostics.status.srv)
                dns_status_col.maybe_update_column_width(response.diagnostics.status.dns)
                connective_test_status_col.maybe_update_column_width(response.diagnostics.status.connection)

                # If this header is not present, just using "" means it will register as a zero length string
                tls_served_by = response.headers.get("server", "")
                tls_served_by_col.maybe_update_column_width(len(tls_served_by))

        # Just use a fixed width for the results. Should never be larger than 5 for most
        # status_col.maybe_update_column_width(6)

        # Begin constructing the message
        #
        # Use a sorted list of server names, so it displays in alphabetical order.
        server_results_sorted = sorted(server_to_server_data.keys())

        # Build the header line

        header_message = (
            f"{server_name_col.front_pad()} | "
            f"{well_known_status_col.pad()} | "
            f"{srv_status_col.pad()} | "
            f"{dns_status_col.pad()} | "
            f"{connective_test_status_col.pad()} | "
            f"{sni_col.pad()} | "
            f"{srt_col.pad()} | "
            f"{crt_col.pad()} | "
            f"{tls_served_by_col.pad()}\n"
        )

        # Need the total of the width for the code block table to make the delimiter
        header_line_size = len(header_message)

        # Create the delimiter line under the header
        header_message += f"{pad('', header_line_size, pad_with='-')}\n"

        list_of_result_data = []
        # Use the sorted list from earlier, alphabetical looks nicer
        for server_name in server_results_sorted:
            response = server_to_server_data[server_name]

            # Want the full room version it to look like this for now
            #
            #   Server Name | WK   | SRV  | DNS  | Test  | SNI | SRT | CRT | TLS served by  |
            # ------------------------------------------------------------------
            #   example.org | OK   | None | OK   | OK    |     |     |     | Synapse 1.92.0 |
            # somewhere.net | None | None | None | Error |     |     |     | resty          | Long error....
            #   maunium.net | OK   | OK   | OK   | OK    | SNI |     |     | Caddy          |

            buffered_message = f"{server_name_col.front_pad(server_name)} | "
            assert isinstance(response.diagnostics, Diagnostics)
            buffered_message += f"{well_known_status_col.pad(response.diagnostics.status.well_known)} | "
            buffered_message += f"{srv_status_col.pad(response.diagnostics.status.srv)} | "
            buffered_message += f"{dns_status_col.pad(response.diagnostics.status.dns)} | "
            buffered_message += f"{connective_test_status_col.pad(response.diagnostics.status.connection)} | "
            if isinstance(response, MatrixFederationResponse):
                assert isinstance(response.server_result, ServerDiscoveryResult)
                buffered_message += (
                    f"{sni_col.pad('X' if response.server_result.sni != response.server_result.hostname else '')} | "
                )
                buffered_message += f"{srt_col.pad('%.3f' % response.server_result.time_for_complete_delegation)} | "
                buffered_message += f"{crt_col.pad('%.3f' % response.time_taken)} | "
                buffered_message += f"{tls_served_by_col.pad(response.headers.get('server', ''))} | "
            else:
                assert isinstance(response, MatrixError)
                buffered_message += f"{response.reason}"

            # maybe_tls_server = response.headers.get("server")
            # buffered_message += f"{tls_served_by_col.pad(maybe_tls_server if maybe_tls_server else '')} | "

            # else:
            #     buffered_message += f"{response}"

            buffered_message += "\n"
            if number_of_servers == 1:
                # Print the diagnostic summary, since there is only one server there
                # is no need to be brief.
                buffered_message += f"{pad('', header_line_size, pad_with='-')}\n"
                for line in response.diagnostics.output_list:
                    buffered_message += f"{pad('', 3)}{line}\n"

                buffered_message += f"{pad('', header_line_size, pad_with='-')}\n"

            # else:
            #     assert isinstance(response, WellKnownLookupFailure)
            #     # Sometimes status_code can be None, but if we let that ride it shows up as the header name of the column
            #     buffered_message += f"{status_col.pad(response.status_code or '')} | "
            #     buffered_message += f"{response.reason}\n"

            list_of_result_data.extend([buffered_message])

        footer_message = (
            f"\nTotal time for retrieval: {total_time:.3f} seconds\nservers missing: {servers_to_contact}\n"
        )
        list_of_result_data.extend([footer_message])

        # For a single server test, the response will fit into a single message block.
        # However, for a roomful it could be several pages long. Chunk those responses
        # to fit into the size limit of an Event.
        final_list_of_data = combine_lines_to_fit_event(list_of_result_data, header_message)

        for chunk in final_list_of_data:
            current_message = await command_event.respond(
                make_into_text_event(wrap_in_code_block_markdown(chunk), ignore_body=True),
            )
            list_of_message_ids.extend([current_message])

        for current_message in list_of_message_ids:
            await self.reaction_task_controller.add_cleanup_control(current_message, command_event.room_id)
