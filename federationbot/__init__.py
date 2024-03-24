from typing import Collection, Dict, List, Optional, Set, Tuple, Type, Union, cast
from asyncio import Queue
from datetime import datetime
from enum import Enum
from itertools import chain
import asyncio
import hashlib
import json
import random
import time

from canonicaljson import encode_canonical_json
from maubot import MessageEvent, Plugin
from maubot.handlers import command
from mautrix.errors.request import MatrixRequestError, MForbidden, MTooLarge
from mautrix.types import (
    EventID,
    EventType,
    Format,
    MessageType,
    PaginatedMessages,
    PaginationDirection,
    ReactionEvent,
    RoomID,
    SyncToken,
    TextMessageEventContent,
)
from mautrix.util import markdown
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper
from more_itertools import partition
from unpaddedbase64 import encode_base64

from federationbot.events import (
    CreateRoomStateEvent,
    Event,
    EventBase,
    EventError,
    GenericStateEvent,
    RoomMemberStateEvent,
    construct_event_id_from_event_v3,
    determine_what_kind_of_event,
    redact_event,
)
from federationbot.federation import (
    FederationHandler,
    authorization_headers,
    filter_events_based_on_type,
    filter_state_events_based_on_membership,
    parse_list_response_into_list_of_event_bases,
)
from federationbot.responses import (
    FederationBaseResponse,
    FederationErrorResponse,
    FederationServerKeyResponse,
    FederationVersionResponse,
    ServerVerifyKeys,
)
from federationbot.server_result import (
    DiagnosticInfo,
    ResponseStatusType,
    ServerResult,
    ServerResultError,
)
from federationbot.utils import (
    BitmapProgressBar,
    BitmapProgressBarStyle,
    DisplayLineColumnConfig,
    Justify,
    ProgressBar,
    get_domain_from_id,
    pad,
    pretty_print_timestamp,
    round_half_up,
)

# An event is considered having a maximum size of 64K. Unfortunately, encryption uses
# more space than cleartext, so give some slack room
MAX_EVENT_SIZE_FOR_SENDING = 40000
# For 'whole room' commands, limit the maximum number of servers to even try
MAX_NUMBER_OF_SERVERS_TO_ATTEMPT = 400

# number of concurrent requests to a single server
MAX_NUMBER_OF_CONCURRENT_TASKS = 10
# number of servers to make requests to concurrently
MAX_NUMBER_OF_SERVERS_FOR_CONCURRENT_REQUEST = 100

SECONDS_BETWEEN_EDITS = 5.0
# Used to control that a suggested backoff should be ignored
SECONDS_BEFORE_IGNORE_BACKOFF = 1.0
# For response time, multiply the previous by this to get the suggested backoff for next
BACKOFF_MULTIPLIER = 0.5

# Column headers. Probably will remove these constants
SERVER_NAME = "Server Name"
SERVER_SOFTWARE = "Software"
SERVER_VERSION = "Version"
CODE = "Code"

NOT_IN_ROOM_ERROR = (
    "Cannot process for a room I'm not in. Invite this bot to that room and try again."
)


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("whitelist")
        helper.copy("server_signing_keys")


class CommandType(Enum):
    avoid_excess = "avoid_excess"
    all = "all"
    count = "count"


json_decoder = json.JSONDecoder()


def is_event_id(maybe_event_id: str) -> Optional[str]:
    if maybe_event_id.startswith("$"):
        return maybe_event_id
    else:
        return None


def is_room_id(maybe_room_id: str) -> Optional[str]:
    if maybe_room_id.startswith("!"):
        return maybe_room_id
    else:
        return None


def is_room_alias(maybe_room_alias: str) -> Optional[str]:
    if maybe_room_alias.startswith("#"):
        return maybe_room_alias
    else:
        return None


def is_room_id_or_alias(maybe_room: str) -> Optional[str]:
    result = is_room_id(maybe_room)
    if result:
        return result
    result = is_room_alias(maybe_room)
    return result


def is_mxid(maybe_mxid: str) -> Optional[str]:
    if maybe_mxid.startswith("@"):
        return maybe_mxid
    else:
        return None


def is_int(maybe_int: str) -> Optional[int]:
    try:
        result = int(maybe_int)
    except ValueError:
        return None
    else:
        return result


def is_command_type(maybe_subcommand: str) -> Optional[str]:
    if maybe_subcommand in CommandType:
        return maybe_subcommand
    else:
        return None


class ReactionCommandStatus(Enum):
    # Notice the extra space, this obfuscates the reaction slightly so as not to pick up
    # stray commands from other rooms. I hope.
    START = "Start "
    PAUSE = "Pause "
    STOP = "Stop "


class FederationBot(Plugin):
    task_control: Dict[EventID, ReactionCommandStatus]
    reaction_handler_count: int

    cached_servers: Dict[str, str]
    server_signing_keys: Dict[str, str]
    federation_handler: FederationHandler

    @classmethod
    def get_config_class(cls) -> Union[Type[BaseProxyConfig], None]:
        return Config

    async def start(self) -> None:
        await super().start()
        self.server_signing_keys = {}
        self.task_control: Dict[EventID, ReactionCommandStatus] = {}
        self.reaction_handler_count = 0
        self.client.add_event_handler(
            EventType.REACTION, self.react_control_handler, True
        )

        if self.config:
            self.config.load_and_update()
            # self.log.info(str(self.config["server_signing_keys"]))
            for server, key_data in self.config["server_signing_keys"].items():
                self.server_signing_keys[server] = key_data
        self.federation_handler = FederationHandler(
            self.http,
            self.log,
            get_domain_from_id(self.client.mxid),
            self.server_signing_keys,
        )

    async def pre_stop(self) -> None:
        self.client.remove_event_handler(EventType.REACTION, self.react_control_handler)
        # To stop any caching cleanup tasks
        await self.federation_handler.stop()

    async def react_control_handler(self, react_evt: ReactionEvent) -> None:
        reaction_data = react_evt.content.relates_to
        if (
            react_evt.sender != self.client.mxid
            and reaction_data.event_id in self.task_control
        ):
            if reaction_data.key == ReactionCommandStatus.STOP.value:
                self.task_control[reaction_data.event_id] = ReactionCommandStatus.STOP
            elif reaction_data.key == ReactionCommandStatus.PAUSE.value:
                self.task_control[reaction_data.event_id] = ReactionCommandStatus.PAUSE
            elif reaction_data.key == ReactionCommandStatus.START.value:
                self.task_control[reaction_data.event_id] = ReactionCommandStatus.START

        return

    @command.new(
        name="status",
        help="playing",
        arg_fallthrough=True,
    )
    async def status_command(
        self,
        command_event: MessageEvent,
    ) -> None:
        pinned_message = await command_event.respond(
            f"Received Status Command on: {self.client.mxid}"
        )
        await self.client.react(
            command_event.room_id, pinned_message, ReactionCommandStatus.STOP.value
        )
        await self.client.react(
            command_event.room_id, pinned_message, ReactionCommandStatus.PAUSE.value
        )
        await self.client.react(
            command_event.room_id, pinned_message, ReactionCommandStatus.START.value
        )
        self.task_control[pinned_message] = ReactionCommandStatus.START
        header_line = "Bot status(wip)\n"
        finish_on_this_round = False
        while True:
            if self.task_control.get(pinned_message) == ReactionCommandStatus.STOP:
                finish_on_this_round = True
            # TODO: Lose this after Tom saw
            good_server_results, bad_server_results = partition(
                lambda x: x.cache_value.unhealthy is not None,
                self.federation_handler._server_discovery_cache._cache.values(),
            )
            buffered_line = (
                f"Event Cache size: {len(self.federation_handler._events_cache)}\n"
                f"New server_result cache: {len(self.federation_handler._server_discovery_cache)}\n"
                # TODO: Lose this after Tom seen it(engrish is grate)
                f" Good server results: {len([1 for _ in good_server_results])}\n"
                f" Bad server results: {len([1 for _ in bad_server_results])}"
            )
            await command_event.respond(
                make_into_text_event(wrap_in_code_block_markdown(buffered_line)),
                edits=pinned_message,
            )

            if finish_on_this_round:
                return
            await asyncio.sleep(5.0)

    @command.new(
        name="test",
        help="playing",
        arg_fallthrough=True,
    )
    async def test_command(
        self,
        command_event: MessageEvent,
    ) -> None:
        await command_event.respond(f"Received Test Command on: {self.client.mxid}")

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
        stuff = await self.client.get_event_context(
            room_id=RoomID(room_id_or_alias),
            event_id=EventID(event_id),
            limit=int(limit),
        )
        await command_event.respond(stuff.json())

    @test_command.subcommand(
        name="room_walk",
        help="Use the /message client endpoint to force fresh state download(beta).",
    )
    @command.argument(name="room_id_or_alias", required=False)
    @command.argument(name="per_iteration", required=False)
    async def room_walk_command(
        self,
        command_event: MessageEvent,
        room_id_or_alias: Optional[str],
        per_iteration: str = "1000",
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        if get_domain_from_id(command_event.sender) != get_domain_from_id(
            self.client.mxid
        ):
            await command_event.reply(
                "I'm sorry, running this command from a user not on the same server as the bot will not help"
            )
            return

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        # Sort out the room id
        if room_id_or_alias:
            room_to_check = await self._resolve_room_id_or_alias(
                room_id_or_alias, command_event, origin_server
            )
            if not room_to_check:
                # Don't need to actually display an error, that's handled in the above
                # function
                return
        else:
            # with server_to_check being set, this will be ignored any way
            room_to_check = command_event.room_id

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
        #

        # Get the last event that was in the room, for it's depth
        now = int(time.time() * 1000)
        time_to_check = now
        room_depth = 0
        ts_response = await self.federation_handler.get_timestamp_to_event_from_server(
            origin_server=origin_server,
            destination_server=origin_server,
            room_id=room_to_check,
            utc_time_at_ms=time_to_check,
        )
        if isinstance(ts_response, FederationErrorResponse):
            await command_event.respond(
                "Something went wrong while getting last event in room("
                f"{ts_response.reason}"
                "). Please supply an event_id instead at the place in time of query"
            )
            return
        else:
            event_id = ts_response.response_dict.get("event_id", None)
            assert isinstance(event_id, str)
            event_result = await self.federation_handler.get_event_from_server(
                origin_server, origin_server, event_id
            )
            event = event_result.get(event_id, None)
            assert event is not None
            room_depth = event.depth

        # Initial messages and lines setup. Never end in newline, as the helper handles
        header_lines = ["Room Back-walking Procedure: Running"]
        static_lines = []
        static_lines.extend(["--------------------------"])
        static_lines.extend([f"Room Depth reported as: {room_depth}"])

        discovery_lines: List[str] = []
        progress_line = ""
        backwalk_lines: List[str] = []

        def _combine_lines_for_backwalk() -> str:
            combined_lines = ""
            for line in header_lines:
                combined_lines += line + "\n"
            for line in static_lines:
                combined_lines += line + "\n"
            for line in discovery_lines:
                combined_lines += line + "\n"
            combined_lines += progress_line + "\n"
            for line in backwalk_lines:
                combined_lines += line + "\n"

            return combined_lines

        pinned_message = await command_event.respond(
            make_into_text_event(
                wrap_in_code_block_markdown(_combine_lines_for_backwalk())
            )
        )

        async def _inner_walking_fetcher(
            for_direction: PaginationDirection, queue: Queue
        ) -> None:
            retry_token = False
            retry_count = 0
            back_off_time = 0.0
            next_token = None
            while True:
                if not retry_token:
                    back_off_time, next_token = await queue.get()

                if back_off_time > 1.0:
                    self.log.warning(f"Backing off for {back_off_time}")
                    await asyncio.sleep(back_off_time)

                try:
                    iter_start_time = time.time()
                    worker_response = await self.client.get_messages(
                        room_id=RoomID(room_to_check),
                        direction=for_direction,
                        from_token=next_token,
                        limit=per_iteration_int,
                        # filter_json=,
                    )
                    iter_finish_time = time.time()
                except MatrixRequestError as e:
                    self.log.warning(f"{e}")
                    retry_token = True
                else:
                    retry_token = False
                    _time_spent = iter_finish_time - iter_start_time
                    response_list.extend([(_time_spent, worker_response)])

                    # prep for next iteration
                    if getattr(worker_response, "end"):
                        # The queue item is (new_back_off_time, pagination_token
                        queue.put_nowait((_time_spent * 0.5, worker_response.end))  # type: ignore[attr-defined]

                    # Don't want this behind a 'finally', as it should only run if not retrying the request
                    queue.task_done()

        discovery_iterations = 0
        discovery_cumulative_iter_time = 0.0
        discovery_collection_of_event_ids = set()
        response_list: List[Tuple[float, PaginatedMessages]] = []
        discovery_fetch_queue: Queue[Tuple[float, Optional[SyncToken]]] = Queue()

        task = asyncio.create_task(
            _inner_walking_fetcher(PaginationDirection.FORWARD, discovery_fetch_queue)
        )
        discovery_fetch_queue.put_nowait((0.0, None))
        finish = False

        while True:
            self.log.warning(f"discovery: size of response list: {len(response_list)}")
            new_responses_to_work_on = response_list.copy()
            response_list = []

            new_event_ids = set()
            for time_spent, response in new_responses_to_work_on:
                discovery_cumulative_iter_time += time_spent
                discovery_iterations = discovery_iterations + 1

                # prep for next iteration
                if getattr(response, "end"):
                    finish = False
                    # backwalk_fetch_queue.put_nowait((time_spent*0.5, response.end))
                else:
                    finish = True

                for event in response.events:  # type: ignore[attr-defined]
                    assert isinstance(event, EventBase)
                    new_event_ids.add(event.event_id)

            discovery_collection_of_event_ids.update(new_event_ids)

            # give a status update
            discovery_total_events_received = len(discovery_collection_of_event_ids)
            discovery_lines = []
            discovery_lines.extend(
                [f"Events found during discovery: {discovery_total_events_received}"]
            )
            discovery_lines.extend(
                [
                    f"  Time taken: {discovery_cumulative_iter_time:.3f} seconds (iter# {discovery_iterations})"
                ]
            )

            # for event_type, count in discovery_event_types_count.items():
            #     discovery_lines.extend([f"{event_type}: {count}"])
            if new_responses_to_work_on or finish:
                # Only print something if there is something to say
                await command_event.respond(
                    make_into_text_event(
                        wrap_in_code_block_markdown(_combine_lines_for_backwalk()),
                    ),
                    edits=pinned_message,
                )
            # prep for next iteration
            if finish:
                break

            await asyncio.sleep(SECONDS_BETWEEN_EDITS)

        # Cancel our worker tasks.
        task.cancel()
        # Wait until all worker tasks are cancelled.
        await asyncio.gather(task, return_exceptions=True)

        backwalk_iterations = 0
        backwalk_fetch_queue: Queue[Tuple[float, Optional[SyncToken]]] = Queue()
        # List of tuples, (time_spent float, NamedTuple of data)
        response_list = []

        task = asyncio.create_task(
            _inner_walking_fetcher(PaginationDirection.BACKWARD, backwalk_fetch_queue)
        )

        backwalk_collection_of_event_ids: Set[EventID] = set()
        backwalk_count_of_new_event_ids = 0
        backwalk_cumulative_iter_time = 0.0
        finish = False
        from_token = None
        # prime the queue
        backwalk_fetch_queue.put_nowait((0.0, from_token))

        while True:
            # if this isn't needed to move the queue along, then lose it
            # await backwalk_fetch_queue.join()

            # pinch off the list of things to work on
            new_responses_to_work_on = response_list.copy()
            response_list = []

            new_event_ids = set()
            for time_spent, response in new_responses_to_work_on:
                backwalk_cumulative_iter_time += time_spent
                backwalk_iterations = backwalk_iterations + 1

                # prep for next iteration
                if getattr(response, "end"):
                    finish = False
                    # backwalk_fetch_queue.put_nowait((time_spent*0.5, response.end))
                else:
                    finish = True

                for event in response.events:  # type: ignore[attr-defined]
                    assert event is not None
                    new_event_ids.add(event.event_id)

            # collect stats
            difference_of_b_and_a = new_event_ids.difference(
                backwalk_collection_of_event_ids
            )

            difference_of_bw_to_discovery = new_event_ids.difference(
                discovery_collection_of_event_ids
            )
            backwalk_count_of_new_event_ids = backwalk_count_of_new_event_ids + len(
                difference_of_bw_to_discovery
            )

            backwalk_collection_of_event_ids.update(new_event_ids)

            # setup backwalk status lines to respond
            # New Events found during backwalk: 0(0 State)
            #   Time taken: 120 seconds
            backwalk_lines = []
            progress_line = f"{len(backwalk_collection_of_event_ids)} of {room_depth}"
            backwalk_lines.extend(
                [
                    f"Received Events during backwalk: {len(backwalk_collection_of_event_ids)}"
                ]
            )
            backwalk_lines.extend(
                [f"New Events found during backwalk: {backwalk_count_of_new_event_ids}"]
            )
            backwalk_lines.extend(
                [
                    f"  Time taken: {backwalk_cumulative_iter_time:.3f} seconds (iter# {backwalk_iterations})"
                ]
            )
            backwalk_lines.extend(
                [f"  Events found this iter: ({len(difference_of_b_and_a)})"]
            )

            if new_responses_to_work_on or finish:
                # Only print something if there is something to say
                await command_event.respond(
                    make_into_text_event(
                        wrap_in_code_block_markdown(_combine_lines_for_backwalk()),
                    ),
                    edits=pinned_message,
                )
            # prep for next iteration
            if finish:
                break

            await asyncio.sleep(SECONDS_BETWEEN_EDITS)

        # Cancel our worker tasks.
        task.cancel()
        # Wait until all worker tasks are cancelled.
        await asyncio.gather(task, return_exceptions=True)
        header_lines = ["Room Back-walking Procedure: Done"]

        backwalk_lines.extend(["Done"])
        await command_event.respond(
            make_into_text_event(
                wrap_in_code_block_markdown(_combine_lines_for_backwalk()),
            ),
            edits=pinned_message,
        )

    @test_command.subcommand(
        name="room_walk2",
        help="Use the federation api to try and selectively download events that are "
        "missing(beta).",
    )
    @command.argument(
        name="room_id_or_alias", parser=is_room_id_or_alias, required=False
    )
    @command.argument(name="server_to_fix", required=False)
    async def room_walk_2_command(
        self,
        command_event: MessageEvent,
        room_id_or_alias: Optional[str],
        server_to_fix: Optional[str],
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        # if get_domain_from_id(command_event.sender) != get_domain_from_id(
        #     self.client.mxid
        # ):
        #     await command_event.reply(
        #         "I'm sorry, running this command from a user not on the same "
        #         "server as the bot will not help"
        #     )
        #     return

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        if server_to_fix:
            destination_server = server_to_fix
        else:
            destination_server = get_domain_from_id(command_event.sender)

        # Sort out the room id
        if room_id_or_alias:
            room_id = await self._resolve_room_id_or_alias(
                room_id_or_alias, command_event, origin_server
            )
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
        # *2. progress bars based on depth reversal, as this is going backwards. Depth
        #    and prev_events won't always have a correlation(remember the ping room)
        # 3. Count of:
        #    *a. All Events seen
        #    *b. Events just with errors
        #    *c. Events that had errors but were resolved later(these count as 'probably
        #       new')
        #    d. Events that had errors but were found on other hosts in the room
        # 4. Times for:
        #    *a. Time spent on backwalk as a whole(incremental=so far)
        #    b. Time spent on last request
        #    c. Time spent in backoff
        # 5. Allow-list for users to block bad actors

        # The process heuristic:
        # 1. backfill with limit=1(variable?) for event_id
        # 2. Take that event(s) and parse out prev_events and auth_events
        #    A. Try and pull prev_events and auth_events from the local server
        #       1. This acts as a prefetch(and will probably be very iterative, caching)
        #          and a basic check that the next event is not a failure
        #       2. If this is a failure, use backfill directly on that Event ID to try
        #          and get the Events from federation. Add to Error List, if found also
        #          add to Resolved Errors List
        #       3. If this continues to fail, check the hosts in the room to see if any
        #          other hosts have it.
        # Notes: If an Event has multiple prev_events, check them all before moving on.
        # They should all collectively have the same prev_event as a batch

        event_id_analyzed: Set[str] = set()
        event_id_ok_list: Set[str] = set()
        event_id_error_list: Set[str] = set()
        event_id_resolved_error_list: Set[str] = set()
        event_id_retries_exhausted: Set[str] = set()

        async def _filter_ancestor_events(
            worker_name: str,
            event_id_list_of_ancestors: List[str],
            _event_id_ok_list: Set[str],
            _event_id_error_list: Set[str],
            _event_id_resolved_error_list: Set[str],
            _event_id_retries_exhausted: Set[str],
        ) -> Tuple[Set[str], Set[str], Set[int]]:
            """
            Condense and filter given Event IDs from the OK list of Events already
            checked then check that any remaining are retrievable.
            Args:
                worker_name: string name for logging
                event_id_list_of_ancestors: Event ID's to look for
                _event_id_ok_list:
                _event_id_error_list:
                _event_id_resolved_error_list:
                _event_id_retries_exhausted:

            Returns: a Tuple of (events that are good, events that are bad)

            """
            next_batch: Set[str] = set()
            error_batch: Set[str] = set()
            depths_seen: Set[int] = set()
            # Do not add the error list to these, as they may be getting retried
            sorted_event_id_set = set(event_id_list_of_ancestors)
            sorted_event_id_set.difference_update(_event_id_ok_list)
            if not sorted_event_id_set:
                # These events have already all been seen, waste no time on it
                return next_batch, error_batch, depths_seen

            pulled_event_map = await self.federation_handler.get_events_from_server(
                origin_server=origin_server,
                destination_server=destination_server,
                events_list=sorted_event_id_set,
            )
            for _event_id in sorted_event_id_set:
                pulled_event = pulled_event_map.get(_event_id)
                if isinstance(pulled_event, EventError):
                    # Had an error, keep a tally
                    _event_id_error_list.add(_event_id)
                    error_batch.add(_event_id)
                    # self.log.warning(
                    #     f"{worker_name}: "
                    #     f"Error on event_id: {_event_id}\n {pulled_event.error}"
                    # )
                else:
                    # Got one, make a note
                    next_batch.add(_event_id)
                    _event_id_ok_list.add(_event_id)
                    assert pulled_event is not None
                    depths_seen.add(pulled_event.depth)
                    # self.log.warning(
                    #     f"{worker_name}: Depth on event_id: {_event_id}\n"
                    #     f" current: {pulled_event.depth}\n"
                    #     f" max iter: {max_depth}"
                    # )

            return next_batch, error_batch, depths_seen

        # Get the last event that was in the room, for its depth and as a starting spot
        now = int(time.time() * 1000)
        ts_response = await self.federation_handler.get_timestamp_to_event_from_server(
            origin_server=origin_server,
            destination_server=origin_server,
            room_id=room_id,
            utc_time_at_ms=now,
        )
        if isinstance(ts_response, FederationErrorResponse):
            await command_event.respond(
                "Something went wrong while getting last event in room\n"
                f"* {ts_response.reason}"
            )
            return

        event_id = ts_response.response_dict.get("event_id", None)
        assert isinstance(event_id, str)
        event_result = await self.federation_handler.get_event_from_server(
            origin_server, origin_server, event_id
        )
        event = event_result.get(event_id, None)
        assert event is not None
        room_depth = event.depth
        seen_depths_for_progress = set()

        # Initial messages and lines setup. Never end in newline, as the helper handles
        header_lines = ["Room Back-walking Procedure: Running"]
        static_lines = []
        static_lines.extend(["------------------------------------"])
        static_lines.extend([f"Room Depth reported as: {room_depth}"])

        discovery_lines: List[str] = []
        progress_bar = BitmapProgressBar(30, room_depth)
        progress_line = progress_bar.render_bitmap_bar()
        roomwalk_lines: List[str] = []

        def _combine_lines_for_backwalk() -> str:
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

        pinned_message = await command_event.respond(
            make_into_text_event(
                wrap_in_code_block_markdown(_combine_lines_for_backwalk())
            )
        )
        await self.client.react(
            command_event.room_id, pinned_message, ReactionCommandStatus.STOP.value
        )
        await self.client.react(
            command_event.room_id, pinned_message, ReactionCommandStatus.PAUSE.value
        )
        await self.client.react(
            command_event.room_id, pinned_message, ReactionCommandStatus.START.value
        )

        # The initial command
        self.task_control.setdefault(pinned_message, ReactionCommandStatus.START)

        bot_working: Dict[str, bool] = dict()

        async def _event_walking_fetcher(
            worker_name: str,
            _event_fetch_queue: Queue[Tuple[float, bool, Set[str]]],
            _backfill_queue: Queue[str],
        ) -> None:
            while True:
                if self.task_control.get(pinned_message) == ReactionCommandStatus.STOP:
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

                # nonlocal event_id_analyzed
                # If this event_id has already been through analysis, no need to
                # again. A good example of how this helps: In the ping room, each bot
                # that sends a 'pong' will have the invocation as a prev_event. The
                # next thing sent into the room will have all those 'pong' responses
                # as prev_events. However, if this is a retry because it wasn't
                # available the first time around...
                # if not is_this_a_retry:
                #    Filter out already analyzed event_ids
                #    next_event_ids.difference_update(event_id_analyzed)

                # But still add it to the pile so that we don't do it twice
                # event_id_analyzed.update(next_event_ids)

                if back_off_time > 5.0:  # SECONDS_BEFORE_IGNORE_BACKOFF:
                    self.log.info(f"{worker_name}: Backing off for {back_off_time}")
                    await asyncio.sleep(back_off_time)

                nonlocal seen_depths_for_progress
                iter_start_time = time.time()
                pulled_event_map = await self.federation_handler.get_events_from_server(
                    origin_server=origin_server,
                    destination_server=destination_server,
                    events_list=next_event_ids,
                )
                for next_event_id in next_event_ids:

                    # self.log.info(f"{worker_name} on {next_event_id}")
                    pulled_event = pulled_event_map.get(next_event_id)
                    # pulled_event should never be None, but mypy doesn't know that
                    assert pulled_event is not None
                    if not isinstance(pulled_event, EventError):
                        if is_this_a_retry:
                            # This should only be hit after a backfill attempt, and
                            # means the second try succeeded.
                            self.log.info(
                                f"{worker_name}: "
                                f"Hit a Resolved Error on {next_event_id}"
                            )
                            event_id_resolved_error_list.add(next_event_id)

                        seen_depths_for_progress.add(pulled_event.depth)
                        prev_events = pulled_event.prev_events
                        auth_events = pulled_event.auth_events
                        # _event_id_ok_list: Set[str],
                        # _event_id_error_list: Set[str],
                        # _event_id_resolved_error_list: Set[str],
                        # _event_id_retries_exhausted: Set[str],

                        # These have filters inside so as not to duplicate work too,
                        # hence why passing all these Set's in
                        (
                            prev_good_events,
                            prev_bad_events,
                            prev_depths,
                        ) = await _filter_ancestor_events(
                            worker_name,
                            prev_events,
                            event_id_ok_list,
                            event_id_error_list,
                            event_id_resolved_error_list,
                            event_id_retries_exhausted,
                        )
                        # seen_depths_for_progress.add(prev_depths)
                        # self.log.warning(
                        #     f"{worker_name}: Depth from prev_depth\n {prev_depths}"
                        # )

                        # We don't iterate the walk based on auth_events themselves,
                        # eventually we'll find them in prev_events. At worst, this is a
                        # prefetch for the cache.
                        (
                            auth_good_events,
                            auth_bad_events,
                            auth_depths,
                        ) = await _filter_ancestor_events(
                            worker_name,
                            auth_events,
                            event_id_ok_list,
                            event_id_error_list,
                            event_id_resolved_error_list,
                            event_id_retries_exhausted,
                        )
                        # seen_depths_for_progress.add(auth_depths)

                        for _event_id in chain(prev_bad_events, auth_bad_events):
                            _backfill_queue.put_nowait(_event_id)
                        # Prep for next iteration. Don't worry about adding auth events
                        # to this, as they will come along in due time
                        next_list_to_get.update(prev_good_events)

                    # else is an EventError. Chances of hitting this are extremely low,
                    # in fact it may only happen on the initial pull to start a walk.
                    # All other opportunities to hit this will have been handled in the
                    # above filter function.
                _time_spent = time.time() - iter_start_time
                total_time_spent += _time_spent

                if next_list_to_get:
                    # The queue item is:
                    # (new_back_off_time, is_this_a_retry, next_event_ids)
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

        async def _backfill_fetcher(
            worker_name: str,
            _event_fetch_queue: Queue[Tuple[float, bool, Set[str]]],
            _backfill_queue: Queue[str],
        ) -> None:
            while True:
                if self.task_control.get(pinned_message) == ReactionCommandStatus.STOP:
                    break

                next_event_id = await _backfill_queue.get()
                bot_working[worker_name] = True

                if next_event_id in event_id_ok_list:
                    self.log.warning(
                        f"{worker_name}: Unexpectedly found backfill fetch event in OK list {next_event_id}"
                    )
                if next_event_id in event_id_resolved_error_list:
                    self.log.warning(
                        f"{worker_name}: Unexpectedly found backfill fetch event in RESOLVED list {next_event_id}"
                    )
                if next_event_id in event_id_retries_exhausted:
                    _backfill_queue.task_done()
                    continue

                # self.log.warning(f"{worker_name}: (Retry count: {retry_count}) Trying a backfill on {_event_id}")
                start_time = time.time()
                backfilled_event = await self._get_event_from_backfill(
                    origin_server=origin_server,
                    destination_server=destination_server,
                    room_id=room_id,
                    event_id=next_event_id,
                )
                request_time = time.time() - start_time

                # Go ahead and add to this tally, just in case somehow there ends up
                # being another duplicate of this in the queue
                event_id_retries_exhausted.add(next_event_id)

                # Maintain the tally. We don't remove Events that were resolved
                # from the Errors List
                if isinstance(backfilled_event, EventError):
                    # Fed error, really not a lot we can do here, if getting an
                    # event worked but backfilled didn't, there are bigger
                    # problems. Just log it and move on, nothing will be added to the
                    # queue in this case(as there is nothing to work with)
                    self.log.warning(
                        f"{worker_name}: Error after backfill event_id: {next_event_id}\n {backfilled_event.to_json()}"
                    )

                elif backfilled_event is None:
                    # The event wasn't found. Drop it for now(might add something neat
                    # later, like look for this event on other hosts in the room)
                    self.log.warning(
                        f"{worker_name}: No Event found on target server after backfill event_id: {next_event_id}"
                    )

                else:
                    # found the event, send it back to the event fetch queue for retry
                    self.log.warning(
                        f"{worker_name}: Potentially found event on backfill, testing {next_event_id}"
                    )
                    _event_fetch_queue.put_nowait(
                        (request_time * BACKOFF_MULTIPLIER, True, {next_event_id})
                    )

                _backfill_queue.task_done()
                bot_working[worker_name] = False

        # Tuple[suggested_backoff, is_this_a_retry, event_ids_to_fetch]
        roomwalk_fetch_queue: Queue[Tuple[float, bool, Set[str]]] = Queue()
        # Tuple[suggested_backoff, event_id_to_fetch]
        backfill_fetch_queue: Queue[str] = Queue()

        tasks = []
        i = 0
        tasks.append(
            asyncio.create_task(
                _event_walking_fetcher(
                    f"event_worker_{i}", roomwalk_fetch_queue, backfill_fetch_queue
                )
            )
        )
        bot_working.setdefault(f"event_worker_{i}", False)
        tasks.append(
            asyncio.create_task(
                _backfill_fetcher(
                    f"backfill_worker_{i}",
                    roomwalk_fetch_queue,
                    backfill_fetch_queue,
                )
            )
        )
        bot_working.setdefault(f"backfill_worker_{i}", False)

        roomwalk_cumulative_iter_time = 0.0
        # Number of times we've re-rendered status
        roomwalk_iterations = 0
        # List of tuples, (time_spent float, bool if we are done)
        render_list: List[Tuple[float, bool]] = []
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
            if self.task_control.get(pinned_message) == ReactionCommandStatus.STOP:
                finish_on_this_round = True

            if self.task_control.get(pinned_message) == ReactionCommandStatus.PAUSE:
                # A pause just means not adding anything to the screen, until restarted
                await asyncio.sleep(1)
                continue

            if roomwalk_fetch_queue.qsize() == 0 and backfill_fetch_queue.qsize() == 0:
                if all([not x for x in bot_working.values()]):
                    retry_for_finish += 1
                    self.log.warning(
                        f"Unexpectedly found no work being processed. Retry count: {retry_for_finish}"
                    )
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
            current_count_of_events_processed = len(
                event_id_ok_list.union(event_id_error_list)
            )
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

            self.log.info(
                f"mid-render, room_depth difference: {len(seen_depths_for_progress)} / {room_depth}"
            )
            progress_bar.update(seen_depths_for_progress)
            progress_line = progress_bar.render_bitmap_bar()

            roomwalk_lines = [
                f"(Updating every {SECONDS_BETWEEN_EDITS} seconds)",
                f"Total Events processed: {current_count_of_events_processed} ({current_count_of_events_processed - last_count_of_events_processed} / update)",
                f"  Errors: {len(event_id_error_list)}",
                f"  Resolved Errors: ({len(event_id_resolved_error_list)})",
                f"  Time taken: {roomwalk_cumulative_iter_time:.3f} seconds (iter# {roomwalk_iterations})",
                f"  (Items currently in backlog event queue: {roomwalk_fetch_queue.qsize()})",
                f"  (Items currently in backlog backfill queue: {backfill_fetch_queue.qsize()})",
                f"Total number of workers: {len(tasks)}",
            ]
            if retry_for_finish:
                roomwalk_lines.extend(
                    [f"Might be out of work, retry count:{retry_for_finish}"]
                )

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

        # Cancel our worker tasks.
        for task in tasks:
            task.cancel()
        # Wait until all worker tasks are cancelled.
        await asyncio.gather(*tasks, return_exceptions=True)

        # Clean up the task controller
        self.task_control.pop(pinned_message)

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
            )
        )

    @test_command.subcommand(
        name="room_hosts", help="List all hosts in a room, in order from earliest"
    )
    @command.argument(
        name="room_id_or_alias", parser=is_room_id_or_alias, required=False
    )
    @command.argument(name="event_id", parser=is_event_id, required=False)
    @command.argument(name="limit", parser=is_int, required=False)
    @command.argument(name="server_to_request_from", required=False)
    async def room_host_command(
        self,
        command_event: MessageEvent,
        room_id_or_alias: Optional[str],
        event_id: Optional[str],
        limit: Optional[int],
        server_to_request_from: Optional[str] = None,
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        if server_to_request_from:
            destination_server = server_to_request_from
        else:
            destination_server = origin_server

        discovered_info = await self._discover_event_ids_and_room_ids(
            origin_server, destination_server, command_event, room_id_or_alias, event_id
        )
        if not discovered_info:
            # The user facing error message was already sent
            return

        room_id, event_id, origin_server_ts = discovered_info

        if origin_server_ts:
            # A nice little addition for the status updated before the command runs
            special_time_formatting = (
                "\n  * which took place at: "
                f"{datetime.fromtimestamp(float(origin_server_ts / 1000))} UTC"
            )
        else:
            special_time_formatting = ""

        # One way or another, we have a room id by now
        # assert room_id is not None

        await command_event.respond(
            f"Retrieving Hosts for \n"
            f"* Room: {room_id_or_alias or room_id}\n"
            f"* at Event ID: {event_id}{special_time_formatting}\n"
            f"* From {destination_server} using {origin_server}"
        )

        # This will be assigned by now
        assert event_id is not None

        host_list = await self.get_hosts_in_room_ordered(
            origin_server=origin_server,
            destination_server=destination_server,
            room_id=room_id,
            event_id_in_timeline=event_id,
        )

        # Time to start rendering. Build the header lines first
        header_message = "Hosts in order of state membership joins\n"

        list_of_buffer_lines = []

        if limit:
            # if limit is more than the number of hosts, fix it
            limit = min(limit, len(host_list))
            for host_number in range(0, limit):
                list_of_buffer_lines.extend(
                    [f"{host_list[host_number:host_number+1]}\n"]
                )
        else:
            for host in host_list:
                list_of_buffer_lines.extend([f"['{host}']\n"])

        # Chunk the data as there may be a few 'pages' of it
        final_list_of_data = combine_lines_to_fit_event(
            list_of_buffer_lines, header_message
        )

        for chunk in final_list_of_data:
            await command_event.respond(
                make_into_text_event(
                    wrap_in_code_block_markdown(chunk), ignore_body=True
                ),
            )

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
        max_size: Optional[str],
        style: Optional[str],
        seconds_to_run_for: Optional[str],
    ) -> None:
        if not max_size:
            max_size = "50"
        if not seconds_to_run_for:
            seconds_to_run_for = "60"
        max_size_int = int(max_size)
        seconds_float = float(seconds_to_run_for)
        interval_float = 5.0
        num_of_intervals = seconds_float / interval_float
        if style == "linear":
            style_type = BitmapProgressBarStyle.linear
        else:
            style_type = BitmapProgressBarStyle.scatter
        how_many_to_pull = max(int(max_size_int / num_of_intervals), 1)
        progress_bar = BitmapProgressBar(30, max_size_int, style=style_type)
        range_list = []

        constants_display_string = ""
        for digit, value in progress_bar.constants.items():
            constants_display_string += f"'{value}', "
        spaces_display_string = f"' ', ' ', ' ', ' ', ' '"
        await command_event.respond(
            wrap_in_code_block_markdown(
                f"fullb char: {constants_display_string}\n"
                f"other char: '{progress_bar.blank}'\n"
                f"space char: {spaces_display_string}\n"
                f"segment_size: {progress_bar._segment_size}\n"
            )  #
        )

        if style_type == BitmapProgressBarStyle.scatter:
            for i in range(1, max_size_int + 1):
                range_list.extend([i])
        else:
            for i in range(1, int(round_half_up(num_of_intervals)) + 1):
                range_list.extend([i * how_many_to_pull])
        pinned_message = await command_event.respond(
            wrap_in_code_block_markdown(
                progress_bar.render_bitmap_bar()
                + f"\n size of range_list: {len(range_list)}\n"
                f" how many to pull: {how_many_to_pull}\n"
            )
        )
        finish = False
        while True:
            set_to_pull = set()
            start_time = time.time()
            if style_type == BitmapProgressBarStyle.scatter:
                for j in range(0, min(int(how_many_to_pull), len(range_list))):
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
                    f" time to render: {finished_time - start_time}\n"
                ),
                edits=pinned_message,
            )
            if finish:
                break
            await asyncio.sleep(interval_float)

    @test_command.subcommand(
        name="find_event", help="Search all hosts in a given room for a given Event"
    )
    @command.argument(name="event_id", parser=is_event_id, required=True)
    @command.argument(
        name="room_id_or_alias", parser=is_room_id_or_alias, required=True
    )
    async def find_event_command(
        self,
        command_event: MessageEvent,
        event_id: Optional[str],
        room_id_or_alias: Optional[str],
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        room_id = await self._resolve_room_id_or_alias(
            room_id_or_alias, command_event, origin_server
        )
        if not room_id:
            # Don't need to actually display an error, that's handled in the above
            # function
            return

        await command_event.respond(
            f"Checking all hosts:\n"
            f"* from Room: {room_id_or_alias or room_id}\n\n"
            f"for:\n"
            f"* Event ID: {event_id}\n"
            f"* Using {origin_server}"
        )

        # This will be assigned by now
        assert event_id is not None

        # Can not assume that the event_id supplied is in the room requested to search
        # hosts of. Get the current hosts in the room
        ts_response = await self.federation_handler.get_timestamp_to_event_from_server(
            origin_server, origin_server, room_id, int(time.time() * 1000)
        )
        if isinstance(ts_response, FederationErrorResponse):
            host_list = []
        else:
            event_id_from_room_right_now: Optional[str] = ts_response.response_dict.get(
                "event_id", None
            )
            assert event_id_from_room_right_now is not None
            self.log.debug(
                f"Timestamp to event responded with event_id: {event_id_from_room_right_now}"
            )
            # Get all the hosts in the supplied room
            host_list = await self.get_hosts_in_room_ordered(
                origin_server, origin_server, room_id, event_id_from_room_right_now
            )

        use_ordered_list = True
        if not host_list:
            use_ordered_list = False
            # Either the origin server doesn't have the state, or some other problem
            # occurred. Fall back to the client api with current state. Obviously there
            # are problems with this, but it will allow forward progress.
            await command_event.respond(
                "Failed getting hosts from State over federation, "
                "falling back to client API"
            )
            try:
                joined_members = await self.client.get_joined_members(RoomID(room_id))

            except MForbidden:
                await command_event.respond(NOT_IN_ROOM_ERROR)
                return
            else:
                for member in joined_members:
                    host = get_domain_from_id(member)
                    if host not in host_list:
                        host_list.extend([host])

        started_at = time.time()
        host_to_event_status_map = await self._find_event_on_servers(
            origin_server, event_id, host_list
        )
        total_time = time.time() - started_at

        # Begin the render
        dc_host_config = DisplayLineColumnConfig("Hosts", justify=Justify.RIGHT)
        dc_result_config = DisplayLineColumnConfig("Results")

        for host in host_to_event_status_map:
            dc_host_config.maybe_update_column_width(len(host))

        header_message = (
            f"Hosts{'(in oldest order)' if use_ordered_list else ''} that found "
            f"event '{event_id}'\n"
        )
        list_of_result_data = []
        for host in host_list:
            result = host_to_event_status_map.get(host)
            buffered_message = ""
            if result:
                if isinstance(result, EventError):
                    buffered_message += (
                        f"{dc_host_config.pad(host)}"
                        f"{dc_host_config.horizontal_separator}"
                        f"{dc_result_config.pad('Fail')}"
                        f"{dc_host_config.horizontal_separator}{result.error}"
                    )
                else:
                    buffered_message += (
                        f"{dc_host_config.pad(host)}"
                        f"{dc_host_config.horizontal_separator}"
                        f"{dc_result_config.pad('OK')}"
                    )
            else:
                # The "unlikely to ever be hit" error
                buffered_message += (
                    f"{dc_host_config.pad(host)}"
                    f"{dc_host_config.horizontal_separator}"
                    f"{dc_result_config.pad('Fail')}"
                    f"{dc_host_config.horizontal_separator}"
                    "Plugin error(Host not contacted)"
                )

            list_of_result_data.extend([f"{buffered_message}\n"])

        footer_message = f"\nTotal time for retrieval: {total_time:.3f} seconds\n"
        list_of_result_data.extend([footer_message])

        # For a single server test, the response will fit into a single message block.
        # However, for a roomful it could be several pages long. Chunk those responses
        # to fit into the size limit of an Event.
        final_list_of_data = combine_lines_to_fit_event(
            list_of_result_data, header_message
        )

        for chunk in final_list_of_data:
            await command_event.respond(
                make_into_text_event(
                    wrap_in_code_block_markdown(chunk), ignore_body=True
                ),
            )

    async def _find_event_on_servers(
        self, origin_server: str, event_id: str, servers_to_check: Collection[str]
    ) -> Dict[str, EventBase]:
        host_to_event_status_map: Dict[str, EventBase] = {}

        host_queue: Queue[str] = Queue()
        for host in servers_to_check:
            host_queue.put_nowait(host)

        async def _event_finding_worker(queue: Queue) -> None:
            while True:
                worker_host = await queue.get()
                returned_events = await self.federation_handler.get_event_from_server(
                    origin_server=origin_server,
                    destination_server=worker_host,
                    event_id=event_id,
                )
                inner_returned_event = returned_events.get(event_id)
                assert inner_returned_event is not None
                host_to_event_status_map[worker_host] = inner_returned_event
                queue.task_done()

        tasks_list = []
        for _ in range(
            min(len(servers_to_check), MAX_NUMBER_OF_SERVERS_FOR_CONCURRENT_REQUEST)
        ):
            tasks_list.append(asyncio.create_task(_event_finding_worker(host_queue)))

        await host_queue.join()

        # Cancel our worker tasks.
        for task in tasks_list:
            task.cancel()

        # Wait until all worker tasks are cancelled.
        await asyncio.gather(*tasks_list, return_exceptions=True)

        return host_to_event_status_map

    @test_command.subcommand(
        name="room_version", help="experiment to get room version from room id"
    )
    @command.argument(
        name="room_id_or_alias", parser=is_room_id_or_alias, required=True
    )
    async def room_version_command(
        self, command_event: MessageEvent, room_id_or_alias: Optional[str]
    ) -> None:
        await command_event.mark_read()

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        room_id = await self._resolve_room_id_or_alias(
            room_id_or_alias, command_event, origin_server
        )
        if not room_id:
            # Don't need to actually display an error, that's handled in the above
            # function
            return

        room_version = await self.federation_handler.discover_room_version(
            origin_server=origin_server,
            destination_server=origin_server,
            room_id=room_id,
        )
        await command_event.reply(f"{room_id} version is {room_version}")

    @test_command.subcommand(
        name="discover_event_id", help="experiment to get event id from PDU event"
    )
    @command.argument(name="event_id", parser=is_event_id, required=True)
    async def discover_event_id_command(
        self, command_event: MessageEvent, event_id: Optional[str]
    ) -> None:
        await command_event.mark_read()

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        if not event_id:
            await command_event.respond(
                "I need you to supply an actual existing event_id to use as a reference for this experiment."
            )
            return
        event_map = await self.federation_handler.get_event_from_server(
            origin_server=origin_server,
            destination_server=origin_server,
            event_id=event_id,
        )
        event = event_map[event_id]
        if isinstance(event, EventError):
            await command_event.respond(
                f"I don't think {event_id} is a legitimate event, or {origin_server} is"
                f" not in that room, so I can not access it.\n\n{event.errcode}"
            )
            return
        else:
            assert event is not None and isinstance(event, EventBase)

        room_id = event.room_id

        room_version = int(
            await self.federation_handler.discover_room_version(
                origin_server=origin_server,
                destination_server=origin_server,
                room_id=room_id,
            )
        )

        await command_event.respond(
            f"Original:\n{wrap_in_code_block_markdown(event.to_json())}"
        )
        redacted_data = redact_event(room_version, event.raw_data)
        redacted_data.pop("signatures", None)
        redacted_data.pop("unsigned", None)
        await command_event.respond(
            f"Redacted:\n{wrap_in_code_block_markdown(json.dumps(redacted_data, indent=4))}"
        )
        encoded_redacted_event_bytes = encode_canonical_json(redacted_data)
        reference_content = hashlib.sha256(encoded_redacted_event_bytes)
        reference_hash = encode_base64(reference_content.digest(), True)

        await command_event.respond(
            f"Supplied: {event_id}\n\nResolved: {'$' + reference_hash}"
        )

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

    @command.new(name="fed", help="`!fed`: Federation requests for information")
    async def fed_command(self, command_event: MessageEvent) -> None:
        pass

    @fed_command.subcommand(
        name="summary", help="Print summary of the delegation portion of the spec"
    )
    async def summary(self, command_event: MessageEvent) -> None:
        await command_event.mark_read()

        await command_event.respond(
            "Summary of how Delegation is processed for a Matrix homeserver.\n"
            "The process to determine the ultimate final host:port is defined in "
            "the [spec](https://spec.matrix.org/v1.9/server-server-api/#resolving-"
            "server-names)\n"
            + wrap_in_code_block_markdown(
                "Basically:\n"
                "1. If it's a literal IP, then use that either with the port supplied "
                "or 8448\n"
                "2. If it's a hostname with an explicit port, resolve with DNS to an "
                "A, AAAA or CNAME record\n"
                "3. If it's a hostname with no explicit port, request from\n"
                "   <server_name>/.well-known/matrix/server and parse the json. "
                "Anything\n"
                "   wrong, skip to step 4. Want "
                "<delegated_server_name>[:<delegated_port>]\n"
                "   3a. Same as 1 above, except don't just use 8448(step 3e)\n"
                "   3b. Same as 2 above.\n"
                "   3c. If no explicit port, check for a SRV record at\n"
                "       _matrix-fed._tcp.<delegated_server_name> to get the port "
                "number.\n"
                "       Resolve with A or AAAA(but not CNAME) record\n"
                "   3d. (deprecated) Check _matrix._tcp.<delegated_server_name> "
                "instead\n"
                "   3e. (there was no port, remember), resolve using provided "
                "delegated\n"
                "       hostname and use port 8448\n"
                "4. (no well-known) Check SRV record(same as 3c above)\n"
                "5. (deprecated) Check other SRV record(same as 3d above)\n"
                "6. Use the supplied server_name and try port 8448\n"
            )
        )

    @command.new(
        name="delegation",
        help="Some simple diagnostics around federation server discovery",
    )
    @command.argument(name="server_to_check", label="Server To Check", required=True)
    async def delegation_command(
        self, command_event: MessageEvent, server_to_check: Optional[str]
    ) -> None:
        if not server_to_check:
            # Only sub commands display the 'help' text field(for now at least). Tell
            # them how it works.
            await command_event.reply(
                "**Usage**: !delegation <server_name>\n - Some simple diagnostics "
                "around federation server discovery"
            )
            return

        await self._delegations(command_event, server_to_check)

    async def _delegations(
        self,
        command_event: MessageEvent,
        server_to_check: str,
    ) -> None:
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
            room_to_check = await self._resolve_room_id_or_alias(
                maybe_room_id, command_event, origin_server
            )
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
                joined_members = await self.client.get_joined_members(
                    RoomID(room_to_check)
                )

            except MForbidden:
                await command_event.respond(NOT_IN_ROOM_ERROR)
                return
            else:
                for member in joined_members:
                    list_of_servers_to_check.add(get_domain_from_id(member))

        # The first of the 'entire room' limitations
        number_of_servers = len(list_of_servers_to_check)
        if number_of_servers > MAX_NUMBER_OF_SERVERS_TO_ATTEMPT:
            await command_event.respond(
                f"To many servers in this room: {number_of_servers}. Please select "
                "a specific server instead.\n\n(This command can have a very large"
                f" response. Max supported is {MAX_NUMBER_OF_SERVERS_TO_ATTEMPT})"
            )
            return

        # Some quality of life niceties
        await command_event.respond(
            f"Retrieving data from federation for {number_of_servers} "
            f"server{'s.' if number_of_servers > 1 else '.'}\n"
            "This may take up to 30 seconds to complete."
        )

        # map of server name -> (server brand, server version)
        server_to_server_data: Dict[str, FederationBaseResponse] = {}

        async def _delegation_worker(queue: Queue) -> None:
            while True:
                worker_server_name = await queue.get()
                try:
                    # The 'get_server_version' function was written with the capability of
                    # collecting diagnostic data.
                    server_to_server_data[
                        worker_server_name
                    ] = await self.federation_handler.get_server_version(
                        worker_server_name,
                        force_rediscover=True,
                        diagnostics=True,
                        timeout_seconds=10.0,
                    )
                except asyncio.TimeoutError:
                    self.log.warning(
                        f"HIT TIMEROUT ERROR WHEN SHOULD NOT: {worker_server_name}"
                    )
                    server_to_server_data[worker_server_name] = FederationErrorResponse(
                        status_code=0,
                        status_reason="Request timed out",
                        response_dict={},
                        server_result=ServerResultError(
                            error_reason="Timeout err", diag_info=DiagnosticInfo(True)
                        ),
                    )
                except Exception as e:
                    server_to_server_data[worker_server_name] = FederationErrorResponse(
                        status_code=0,
                        status_reason=f"Plugin Error: {e}",
                        response_dict={},
                        server_result=ServerResultError(
                            error_reason=f"Plugin err: {e}",
                            diag_info=DiagnosticInfo(True),
                        ),
                    )

                finally:
                    queue.task_done()

        delegation_queue: Queue[str] = asyncio.Queue()
        for server_name in list_of_servers_to_check:
            await delegation_queue.put(server_name)

        tasks = []
        for i in range(MAX_NUMBER_OF_SERVERS_FOR_CONCURRENT_REQUEST):
            task = asyncio.create_task(_delegation_worker(delegation_queue))
            tasks.append(task)

        started_at = time.monotonic()
        await delegation_queue.join()
        # await asyncio.gather(
        #     *[_delegation(server_name) for server_name in list_of_servers_to_check]
        # )
        total_time = time.monotonic() - started_at
        # Cancel our worker tasks.
        for task in tasks:
            task.cancel()
        # Wait until all worker tasks are cancelled.
        await asyncio.gather(*tasks, return_exceptions=True)

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

        # Iterate through the server names to widen the column, if necessary.
        for server_name, server_results in server_to_server_data.items():
            server_name_col.maybe_update_column_width(len(server_name))
            if not isinstance(server_results, FederationErrorResponse):
                if server_results.headers is not None:
                    tls_server = server_results.headers.get("server", None)
                    if tls_server:
                        tls_served_by_col.maybe_update_column_width(len(tls_server))

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
            f"Errors\n"
        )

        # Need the total of the width for the code block table to make the delimiter
        header_line_size = len(header_message)

        # Create the delimiter line under the header
        header_message += f"{pad('', header_line_size, pad_with='-')}\n"

        list_of_result_data = []
        # Use the sorted list from earlier, alphabetical looks nicer
        for server_name in server_results_sorted:
            server_response = server_to_server_data.get(server_name, None)

            if server_response:
                # Shortcut reference the diag_info to cut down line length
                diag_info = server_response.server_result.diag_info

                # The server name column
                buffered_message = f"{server_name_col.front_pad(server_name)} | "
                # The well-known status column
                buffered_message += (
                    f"{well_known_status_col.pad(diag_info.get_well_known_status())} | "
                )

                # the SRV record status column
                buffered_message += (
                    f"{srv_status_col.pad(diag_info.get_srv_record_status())} | "
                )

                # the DNS record status column
                buffered_message += (
                    f"{dns_status_col.pad(diag_info.get_dns_record_status())} | "
                )

                # The connectivity status column
                connectivity_status = diag_info.get_connectivity_test_status()
                buffered_message += (
                    f"{connective_test_status_col.pad(connectivity_status)} | "
                )
                if not isinstance(server_response, FederationErrorResponse):
                    error_reason = None
                    assert server_response.headers is not None
                    reverse_proxy = server_response.headers.get("server", None)
                else:
                    error_reason = server_response.reason
                    reverse_proxy = None

                buffered_message += (
                    f"{tls_served_by_col.pad(reverse_proxy if reverse_proxy else '')}"
                    " | "
                )
                buffered_message += f"{error_reason if error_reason else ''}"

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
        final_list_of_data = combine_lines_to_fit_event(
            list_of_result_data, header_message
        )

        for chunk in final_list_of_data:
            await command_event.respond(
                make_into_text_event(
                    wrap_in_code_block_markdown(chunk), ignore_body=True
                ),
            )

    @fed_command.subcommand(name="event_raw")
    @command.argument(name="event_id", parser=is_event_id, required=False)
    @command.argument(name="server_to_request_from", required=False)
    async def event_command(
        self,
        command_event: MessageEvent,
        event_id: Optional[str],
        server_to_request_from: Optional[str],
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        if server_to_request_from:
            destination_server = server_to_request_from
        else:
            destination_server = origin_server

        # Sometimes have to just make things a little more useful
        extra_info = ""
        if not event_id:
            event_id = command_event.event_id
            extra_info = " last event in this room"

        await command_event.respond(
            f"Retrieving{extra_info}: {event_id} from "
            f"{destination_server} using {origin_server}"
        )

        # Collect all the Federation Responses as well as the EventBases.
        # Errors can be found in the Responses.
        returned_events = await self.federation_handler.get_event_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            event_id=event_id,
        )

        buffered_message = ""
        returned_event = returned_events.get(event_id)
        if isinstance(returned_event, EventError):
            buffered_message += (
                f"received an error\n{returned_event.errcode}:{returned_event.error}"
            )

        else:
            assert isinstance(returned_event, EventBase)
            buffered_message += f"{returned_event.event_id}\n"
            # EventBase.to_json() does not have a trailing new line, add one
            buffered_message += returned_event.to_json() + "\n"

        # It is extremely unlikely that an Event will be larger than can be displayed.
        # Don't bother chunking the response.
        try:
            await command_event.respond(wrap_in_code_block_markdown(buffered_message))
        except MTooLarge:
            await command_event.respond("Somehow, Event is to large to display")

    @fed_command.subcommand(name="event")
    @command.argument(name="event_id", parser=is_event_id, required=False)
    @command.argument(name="server_to_request_from", required=False)
    async def event_command_pretty(
        self,
        command_event: MessageEvent,
        event_id: Optional[str],
        server_to_request_from: Optional[str],
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        if server_to_request_from:
            destination_server = server_to_request_from
        else:
            destination_server = origin_server

        # Sometimes have to just make things a little more useful
        extra_info = ""
        if not event_id:
            event_id = command_event.event_id
            extra_info = " last event in this room"

        await command_event.respond(
            f"Retrieving{extra_info}: {event_id} from "
            f"{destination_server} using {origin_server}"
        )

        returned_event_dict = await self.federation_handler.get_event_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            event_id=event_id,
        )

        buffered_message = ""
        returned_event = returned_event_dict.get(event_id)
        if isinstance(returned_event, EventError):
            buffered_message += (
                f"received an error\n{returned_event.errcode}:{returned_event.error}\n"
            )

        else:
            assert isinstance(returned_event, Event)
            # a_event will stand for ancestor event
            # A mapping of 'a_event_id' to the string of short data about the a_event to
            # be shown
            a_event_data_map: Dict[str, str] = {}
            # Recursively retrieve events that are in the immediate past. This
            # allows for some annotation to the events when they are displayed in
            # the 'footer' section of the rendered response. For example: auth
            # events will have their event type displayed, such as 'm.room.create'
            # and the room version.
            assert isinstance(returned_event, EventBase)
            list_of_a_event_ids = returned_event.auth_events.copy()
            list_of_a_event_ids.extend(returned_event.prev_events)

            # For the verification display, grab the room version in these events
            found_room_version = 1
            a_returned_events = await self.federation_handler.get_events_from_server(
                origin_server=origin_server,
                destination_server=destination_server,
                events_list=list_of_a_event_ids,
            )
            for a_event_id in list_of_a_event_ids:
                a_event_base = a_returned_events.get(a_event_id)
                if a_event_base:
                    a_event_data_map[a_event_id] = a_event_base.to_short_type_summary()
                    if isinstance(a_event_base, CreateRoomStateEvent):
                        found_room_version = a_event_base.room_version

            # Begin rendering
            # TODO: test by modifying the object. Have to reach into the raw_data and
            #  modify that, as the attrib versions will have already been parsed and
            #  won't be read by the verifier. Spoiler alert: works as intended.
            # returned_event.raw_data["depth"] += 1
            await self.federation_handler.verify_signatures_and_annotate_event(
                returned_event, found_room_version
            )

            # It may be, but is unlikely outside of connection errors, that room_version
            # was not found. This is handled gracefully inside of to_pretty_summary()
            buffered_message += returned_event.to_pretty_summary(
                room_version=found_room_version
            )
            # Add a little gap at the bottom of the previous for better separation
            buffered_message += "\n"
            buffered_message += returned_event.to_pretty_summary_content()
            buffered_message += returned_event.to_pretty_summary_unrecognized()
            buffered_message += returned_event.to_pretty_summary_footer(
                event_data_map=a_event_data_map
            )

        await command_event.respond(wrap_in_code_block_markdown(buffered_message))

    @fed_command.subcommand(
        name="state", help="Request state over federation for a room."
    )
    @command.argument(
        name="room_id_or_alias", parser=is_room_id_or_alias, required=False
    )
    @command.argument(name="event_id", parser=is_event_id, required=False)
    @command.argument(name="server_to_request_from", required=False)
    async def state_command(
        self,
        command_event: MessageEvent,
        room_id_or_alias: Optional[str],
        event_id: Optional[str],
        server_to_request_from: Optional[str],
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        if server_to_request_from:
            destination_server = server_to_request_from
        else:
            destination_server = origin_server

        discovered_info = await self._discover_event_ids_and_room_ids(
            origin_server, destination_server, command_event, room_id_or_alias, event_id
        )
        if not discovered_info:
            # The user facing error message was already sent
            return

        room_id, event_id, origin_server_ts = discovered_info

        if origin_server_ts:
            # A nice little addition for the status updated before the command runs
            special_time_formatting = (
                "\n  * which took place at: "
                f"{datetime.fromtimestamp(float(origin_server_ts / 1000))} UTC"
            )
        else:
            special_time_formatting = ""
        await command_event.respond(
            f"Retrieving State for:\n"
            f"* Room: {room_id_or_alias or room_id}\n"
            f"* at Event ID: {event_id}{special_time_formatting}\n"
            f"* From {destination_server} using {origin_server}"
        )

        # This will be assigned by now
        assert event_id is not None

        # This will retrieve the events and the auth chain, we only use the former here
        (pdu_list, _,) = await self.federation_handler.get_state_ids_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            room_id=room_id,
            event_id=event_id,
        )

        await command_event.respond(
            f"Retrieving {len(pdu_list)} events from {destination_server}"
        )

        # Keep both the response and the actual event, if there was an error it will be
        # in the response and the event won't exist here
        event_to_event_base: Dict[str, EventBase]

        started_at = time.monotonic()
        event_to_event_base = await self.federation_handler.get_events_from_server(
            origin_server, destination_server, pdu_list
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
        list_of_event_ids: List[Tuple[int, EventID]] = []
        for event_id, event_id_entry in event_to_event_base.items():
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
        for (_, event_id) in list_of_event_ids:
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
        final_list_of_data = combine_lines_to_fit_event(
            list_of_buffer_lines, header_message
        )

        for chunk in final_list_of_data:
            await command_event.respond(
                make_into_text_event(
                    wrap_in_code_block_markdown(chunk), ignore_body=True
                ),
            )

    @fed_command.subcommand(
        name="version",
        aliases=["versions"],
        help="Check a server in the room for version info",
    )
    @command.argument(name="server_to_check", label="Server to check", required=True)
    async def version_command(
        self, command_event: MessageEvent, server_to_check: Optional[str]
    ) -> None:
        if not server_to_check:
            await command_event.reply(
                "**Usage**: !fed version <server_name>\n - Check a server in the room "
                "for version info"
            )
            return

        # Let the user know the bot is paying attention
        await command_event.mark_read()

        list_of_servers_to_check = set()

        # It may be that they are using their mxid as the server to check, parse that
        maybe_user_mxid = is_mxid(server_to_check)
        if maybe_user_mxid:
            server_to_check = get_domain_from_id(maybe_user_mxid)

        # As an undocumented option, allow passing in a room_id to check an entire room.
        # This can be rather long(and time consuming) so we'll place limits later.
        maybe_room_id = is_room_id_or_alias(server_to_check)
        if maybe_room_id:
            origin_server = get_domain_from_id(self.client.mxid)
            room_to_check = await self._resolve_room_id_or_alias(
                maybe_room_id, command_event, origin_server
            )
            # Need to cancel server_to_check, but can't use None
            server_to_check = ""
            if not maybe_room_id:
                # Don't need to actually display an error, that's handled in the above
                # function
                return
        else:
            room_to_check = command_event.room_id

        # If the room id was passed in, then this will turn into None
        if not server_to_check:
            # Get the members this bot knows about in this room
            # TODO: try and find a way to not use the client API for this
            try:
                assert isinstance(room_to_check, str)
                joined_members = await self.client.get_joined_members(
                    RoomID(room_to_check)
                )

            except MForbidden:
                await command_event.respond(NOT_IN_ROOM_ERROR)
                return
            else:
                for member in joined_members:
                    list_of_servers_to_check.add(get_domain_from_id(member))
        else:
            list_of_servers_to_check.add(server_to_check)

        # Guard against there being to many servers on the response
        number_of_servers = len(list_of_servers_to_check)
        if number_of_servers > MAX_NUMBER_OF_SERVERS_TO_ATTEMPT:
            await command_event.respond(
                f"To many servers in this room: {number_of_servers}. Please select "
                "a specific server instead.\n\n(This command can have a very large"
                f" response. Max supported is {MAX_NUMBER_OF_SERVERS_TO_ATTEMPT})"
            )
            return

        await command_event.respond(
            f"Retrieving data from federation for {number_of_servers} server"
            f"{'s' if number_of_servers > 1 else ''}"
        )

        started_at = time.monotonic()
        server_to_version_data = await self._get_versions_from_servers(
            list_of_servers_to_check
        )
        total_time = time.monotonic() - started_at

        # Establish the initial size of the padding for each row
        server_name_col = DisplayLineColumnConfig(SERVER_NAME)
        server_software_col = DisplayLineColumnConfig(SERVER_SOFTWARE)
        server_version_col = DisplayLineColumnConfig(SERVER_VERSION)

        # Iterate over all the data to collect the column sizes
        for server, result in server_to_version_data.items():
            server_name_col.maybe_update_column_width(len(server))

            if isinstance(result, FederationErrorResponse):
                server_software_col.maybe_update_column_width(
                    len(str(result.status_code))
                )
                server_version_col.maybe_update_column_width(len(str(result.reason)))
            else:
                assert isinstance(result, FederationVersionResponse)
                server_software_col.maybe_update_column_width(
                    len(result.server_software)
                )
                server_version_col.maybe_update_column_width(len(result.server_version))

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
        header_message = (
            f"{server_name_col.front_pad()} | "
            f"{server_software_col.pad()} | "
            f"{server_version_col.pad()}\n"
        )

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
            # Federation request may have had an error, handle those errors here
            if isinstance(server_data, FederationErrorResponse):
                # Pad the software column with spaces, so the error and the code end up in the version column
                buffered_message += f"{server_software_col.pad('')} | "

                # status codes of 0 represent the kind of error that doesn't have an
                # http code, like an SSL error.
                if server_data.status_code > 0:
                    buffered_message += f"{server_data.status_code}:"

                buffered_message += f"{server_data.reason}\n"
            else:
                assert isinstance(server_data, FederationVersionResponse)
                buffered_message += (
                    f"{server_software_col.pad(server_data.server_software)} | "
                    f"{server_data.server_version}\n"
                )

            list_of_result_data.extend([buffered_message])

        footer_message = f"\nTotal time for retrieval: {total_time:.3f} seconds\n"
        list_of_result_data.extend([footer_message])

        final_list_of_data = combine_lines_to_fit_event(
            list_of_result_data, header_message
        )

        # Wrap in code block markdown before sending
        for chunk in final_list_of_data:
            await command_event.respond(
                make_into_text_event(
                    wrap_in_code_block_markdown(chunk), ignore_body=True
                ),
            )

    async def _get_versions_from_servers(
        self,
        servers_to_check: Collection[str],
    ) -> Dict[str, FederationBaseResponse]:

        # map of server name -> (server brand, server version)
        # Return this at the end
        server_to_version_data: Dict[str, FederationBaseResponse] = {}

        async def _version_worker(queue: Queue) -> None:
            while True:
                worker_server_name = await queue.get()
                try:
                    server_to_version_data[worker_server_name] = await asyncio.wait_for(
                        self.federation_handler.get_server_version(
                            worker_server_name,
                        ),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError:
                    server_to_version_data[
                        worker_server_name
                    ] = FederationErrorResponse(
                        status_code=0,
                        status_reason="Timed out waiting for response",
                        response_dict={},
                        server_result=ServerResultError(
                            error_reason="Timeout err", diag_info=DiagnosticInfo(True)
                        ),
                    )
                except Exception as e:
                    server_to_version_data[
                        worker_server_name
                    ] = FederationErrorResponse(
                        status_code=0,
                        status_reason="Plugin Error",
                        response_dict={},
                        server_result=ServerResultError(
                            error_reason=f"Plugin err: {e}",
                            diag_info=DiagnosticInfo(True),
                        ),
                    )

                finally:
                    queue.task_done()

        version_queue: Queue[str] = asyncio.Queue()
        for server_name in servers_to_check:
            await version_queue.put(server_name)

        tasks = []
        for i in range(
            min(len(servers_to_check), MAX_NUMBER_OF_SERVERS_FOR_CONCURRENT_REQUEST)
        ):
            task = asyncio.create_task(_version_worker(version_queue))
            tasks.append(task)

        await version_queue.join()

        # Cancel our worker tasks.
        for task in tasks:
            task.cancel()
        # Wait until all worker tasks are cancelled.
        await asyncio.gather(*tasks, return_exceptions=True)

        return server_to_version_data

    @fed_command.subcommand(name="server_keys")
    @command.argument(name="server_to_check", required=True)
    async def server_keys_command(
        self, command_event: MessageEvent, server_to_check: Optional[str]
    ) -> None:
        if not server_to_check:
            await command_event.reply(
                "**Usage**: !fed server_keys <server_name>\n - Check a server in the "
                "room for version info"
            )
            return
        await self._server_keys(command_event, server_to_check)

    @fed_command.subcommand(name="server_keys_raw")
    @command.argument(name="server_to_check", required=True)
    async def server_keys_raw_command(
        self, command_event: MessageEvent, server_to_check: Optional[str]
    ) -> None:
        if not server_to_check:
            await command_event.reply(
                "**Usage**: !fed server_keys <server_name>\n - Check a server in the "
                "room for version info"
            )
            return
        await self._server_keys(command_event, server_to_check, display_raw=True)

    async def _server_keys(
        self,
        command_event: MessageEvent,
        server_to_check: str,
        display_raw: bool = False,
    ) -> None:
        # Let the user know the bot is paying attention
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
            room_to_check = await self._resolve_room_id_or_alias(
                maybe_room_id, command_event, origin_server
            )
            # Need to cancel server_to_check, but can't use None
            server_to_check = ""
            if not maybe_room_id:
                # Don't need to actually display an error, that's handled in the above
                # function
                return
        else:
            room_to_check = command_event.room_id

        list_of_servers_to_check = set()
        # If the room id was passed in, then this will turn into None
        if not server_to_check:
            try:
                assert isinstance(room_to_check, str)
                joined_members = await self.client.get_joined_members(
                    RoomID(room_to_check)
                )

            except MForbidden:
                await command_event.respond(NOT_IN_ROOM_ERROR)
                return
            else:
                for member in joined_members:
                    list_of_servers_to_check.add(get_domain_from_id(member))
        else:
            list_of_servers_to_check.add(server_to_check)

        number_of_servers = len(list_of_servers_to_check)
        if number_of_servers > 1 and display_raw:
            await command_event.respond(
                "Only can see raw JSON data if a single server is selected(as the "
                "response would be super spammy)."
            )
            return

        if number_of_servers > MAX_NUMBER_OF_SERVERS_TO_ATTEMPT:
            await command_event.respond(
                f"To many servers in this room: {number_of_servers}. Please select "
                "a specific server instead.\n\n(This command can have a very large"
                f" response. Max supported is {MAX_NUMBER_OF_SERVERS_TO_ATTEMPT})"
            )
            return

        server_to_server_data: Dict[
            str, Union[FederationErrorResponse, FederationServerKeyResponse]
        ] = dict()
        await command_event.respond(
            f"Retrieving data from federation for {number_of_servers} server"
            f"{'s' if number_of_servers > 1 else ''}"
        )

        async def _server_keys_worker(queue: Queue[str]) -> None:
            while True:
                worker_server_name = await queue.get()
                try:
                    server_to_server_data[
                        worker_server_name
                    ] = await self.federation_handler._get_server_keys(
                        worker_server_name,
                        timeout=10.0,
                    )

                except Exception as e:
                    server_to_server_data[worker_server_name] = FederationErrorResponse(
                        status_code=0,
                        status_reason=f"Plugin Error: {e}",
                        response_dict={},
                        server_result=ServerResultError(
                            error_reason=f"Plugin err: {e}",
                            diag_info=DiagnosticInfo(True),
                        ),
                    )

                finally:
                    queue.task_done()

        keys_queue: Queue[str] = asyncio.Queue()
        for server_name in list_of_servers_to_check:
            await keys_queue.put(server_name)

        tasks = []
        for i in range(
            min(
                len(list_of_servers_to_check),
                MAX_NUMBER_OF_SERVERS_FOR_CONCURRENT_REQUEST,
            )
        ):
            task = asyncio.create_task(_server_keys_worker(keys_queue))
            tasks.append(task)

        started_at = time.monotonic()
        await keys_queue.join()

        total_time = time.monotonic() - started_at
        # Cancel our worker tasks.
        for task in tasks:
            task.cancel()
        # Wait until all worker tasks are cancelled.
        await asyncio.gather(*tasks, return_exceptions=True)

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
            # Don't care about widening columns for errors
            if isinstance(server_results, FederationServerKeyResponse):

                server_name_col.maybe_update_column_width(len(server_name))

                for (
                    key_id,
                    key_data,
                ) in server_results.server_verify_keys.verify_keys.items():
                    server_key_col.maybe_update_column_width(len(key_id))
                    valid_until_ts_col.maybe_update_column_width(
                        len(str(key_data.valid_until_ts))
                    )

        # Begin constructing the message

        # Build the header line
        header_message = (
            f"{server_name_col.front_pad()} | "
            f"{server_key_col.pad()} | "
            f"{valid_until_ts_col.header_name}\n"
        )

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

            if isinstance(server_results, FederationErrorResponse):
                buffered_message += f"{server_results.reason}\n"

            else:
                # This will be a ServerVerifyKeys
                time_now = int(time.time() * 1000)
                verify_keys = server_results.server_verify_keys.verify_keys

                # There will not be more than a single key.
                for key_id, key_data in verify_keys.items():
                    valid_until_pretty = "None Found"
                    valid_until_ts = key_data.valid_until_ts
                    if valid_until_ts > 0:
                        valid_until_pretty = pretty_print_timestamp(valid_until_ts)

                    # This will mark the display with a * to visually express expired
                    pretty_expired_marker = "*" if valid_until_ts < time_now else ""
                    buffered_message += f"{server_key_col.pad(key_id)} | "
                    buffered_message += f"{pretty_expired_marker}{valid_until_pretty}\n"

            list_of_result_data.extend([buffered_message])

            # Only if there was a single server because of the above condition
            if display_raw:
                if isinstance(server_results, FederationServerKeyResponse):
                    list_of_result_data.extend(
                        [
                            f"{json.dumps(server_results.server_verify_keys._raw_data, indent=4)}\n"
                        ]
                    )
                else:
                    list_of_result_data.extend(
                        [f"{json.dumps(server_results.response_dict, indent=4)}\n"]
                    )

        footer_message = f"\nTotal time for retrieval: {total_time:.3f} seconds\n"
        list_of_result_data.extend([footer_message])

        final_list_of_data = combine_lines_to_fit_event(
            list_of_result_data, header_message
        )

        for chunk in final_list_of_data:
            await command_event.respond(
                make_into_text_event(
                    wrap_in_code_block_markdown(chunk), ignore_body=True
                ),
            )

    @fed_command.subcommand(name="notary_server_keys")
    @command.argument(name="server_to_check", required=True)
    @command.argument(name="notary_server_to_use", required=False)
    async def notary_server_keys_command(
        self,
        command_event: MessageEvent,
        server_to_check: Optional[str],
        notary_server_to_use: Optional[str],
    ) -> None:
        if not server_to_check:
            await command_event.reply(
                "**Usage**: !fed notary_server_keys <server_name> [notary_to_ask]\n"
                " - Check a server in the room for version info"
            )
            return
        await self._server_keys_from_notary(
            command_event, server_to_check, notary_server_to_use=notary_server_to_use
        )

    @fed_command.subcommand(name="notary_server_keys_raw")
    @command.argument(name="server_to_check", required=True)
    @command.argument(name="notary_server_to_use", required=False)
    async def notary_server_keys_raw_command(
        self,
        command_event: MessageEvent,
        server_to_check: Optional[str],
        notary_server_to_use: Optional[str],
    ) -> None:
        if not server_to_check:
            await command_event.reply(
                "**Usage**: !fed notary_server_keys <server_name> [notary_to_ask]\n"
                " - Check a server in the room for version info"
            )
            return
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
        notary_server_to_use: Optional[str],
        display_raw: bool = False,
    ) -> None:
        # Let the user know the bot is paying attention
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
            room_to_check = await self._resolve_room_id_or_alias(
                maybe_room_id, command_event, origin_server
            )
            # Need to cancel server_to_check, but can't use None
            server_to_check = ""
            if not maybe_room_id:
                # Don't need to actually display an error, that's handled in the above
                # function
                return
        else:
            room_to_check = command_event.room_id

        list_of_servers_to_check = set()
        # If the room id was passed in, then this will turn into None
        if not server_to_check:
            try:
                assert isinstance(room_to_check, str)
                joined_members = await self.client.get_joined_members(
                    RoomID(room_to_check)
                )

            except MForbidden:
                await command_event.respond(NOT_IN_ROOM_ERROR)
                return
            else:
                for member in joined_members:
                    list_of_servers_to_check.add(get_domain_from_id(member))
        else:
            list_of_servers_to_check.add(server_to_check)

        number_of_servers = len(list_of_servers_to_check)
        if number_of_servers > 1 and display_raw:
            await command_event.respond(
                "Only can see raw JSON data if a single server is selected(as the "
                "response would be super spammy)."
            )
            return

        if number_of_servers > MAX_NUMBER_OF_SERVERS_TO_ATTEMPT:
            await command_event.respond(
                f"To many servers in this room: {number_of_servers}. Please select "
                "a specific server instead.\n\n(This command can have a very large"
                f" response. Max supported is {MAX_NUMBER_OF_SERVERS_TO_ATTEMPT})"
            )
            return

        if number_of_servers > 1 and display_raw:
            await command_event.respond(
                f"Only can see raw JSON data if a single server is selected(as the "
                "response would be super spammy)."
            )
            return

        if not notary_server_to_use:
            notary_server_to_use = get_domain_from_id(command_event.sender)
        await command_event.respond(
            f"Retrieving data from federation for {number_of_servers} server"
            f"{'s' if number_of_servers > 1 else ''}\n"
            f"Using {notary_server_to_use}"
        )

        server_to_server_data: Dict[str, FederationBaseResponse] = {}

        async def _server_keys_from_notary_worker(queue: Queue) -> None:
            while True:
                worker_server_name = await queue.get()
                try:
                    server_to_server_data[worker_server_name] = await asyncio.wait_for(
                        self.federation_handler.get_server_keys_from_notary(
                            worker_server_name, notary_server_to_use
                        ),
                        timeout=10.0,
                    )

                except asyncio.TimeoutError:
                    server_to_server_data[worker_server_name] = FederationErrorResponse(
                        status_code=0,
                        status_reason="Request timed out",
                        response_dict={},
                        server_result=ServerResultError(
                            error_reason="Timeout err", diag_info=DiagnosticInfo(True)
                        ),
                    )

                except Exception as e:
                    server_to_server_data[worker_server_name] = FederationErrorResponse(
                        status_code=0,
                        status_reason="Plugin Error",
                        response_dict={},
                        server_result=ServerResultError(
                            error_reason=f"Plugin Error: {e}",
                            diag_info=DiagnosticInfo(True),
                        ),
                    )

                finally:
                    queue.task_done()

        keys_queue: Queue[str] = asyncio.Queue()
        for server_name in list_of_servers_to_check:
            await keys_queue.put(server_name)

        tasks = []
        for i in range(MAX_NUMBER_OF_SERVERS_FOR_CONCURRENT_REQUEST):
            task = asyncio.create_task(_server_keys_from_notary_worker(keys_queue))
            tasks.append(task)

        started_at = time.monotonic()
        await keys_queue.join()

        total_time = time.monotonic() - started_at
        # Cancel our worker tasks.
        for task in tasks:
            task.cancel()
        # Wait until all worker tasks are cancelled.
        await asyncio.gather(*tasks, return_exceptions=True)

        # Preprocess the data to get the column sizes
        # Want it to look like this for now, for the whole room version. Obviously a
        # single line of the same for the 'one server' version.
        #
        #      Server Name | Key ID | Valid until(UTC)
        # ---------------------------------------
        # littlevortex.net | aRGvs  | Pretty formatted DateTime
        #       matrix.org | aYp3g  | Pretty formatted DateTime
        #                  | 0ldK3y | EXPIRED: Expired DateTime

        server_name_col = DisplayLineColumnConfig("Server Name")
        server_key_col = DisplayLineColumnConfig("Key ID")
        valid_until_ts_col = DisplayLineColumnConfig("Valid until(UTC)")

        for server_name, server_results in server_to_server_data.items():
            for server_keys in server_results.response_dict.get("server_keys", []):
                server_name_col.maybe_update_column_width(len(server_name))

                verify_keys = server_keys.get("verify_keys", {})
                old_verify_keys = server_keys.get("old_verify_keys", {})
                valid_until: Optional[int] = server_keys.get("valid_until_ts", None)
                valid_until_pretty = "None Found"

                if valid_until:
                    valid_until_pretty = str(
                        datetime.fromtimestamp(float(valid_until / 1000))
                    )

                for key_id in verify_keys.keys():
                    server_key_col.maybe_update_column_width(len(key_id))

                valid_until_ts_col.maybe_update_column_width(len(valid_until_pretty))
                for key_id, old_key_data in old_verify_keys.items():
                    server_key_col.maybe_update_column_width(len(key_id))

                    if old_key_data:
                        valid_until_ts_col.maybe_update_column_width(
                            len(str(old_key_data.get("expired_ts", 0)))
                        )

        # Begin constructing the message

        # Build the header line
        header_message = (
            f"{server_name_col.front_pad()} | "
            f"{server_key_col.pad()} | "
            f"{valid_until_ts_col.header_name}\n"
        )

        # Need the total of the width for the code block table to make the delimiter
        total_srv_line_size = len(header_message)

        # Create the delimiter line under the header
        header_message += f"{pad('', total_srv_line_size, pad_with='-')}\n"

        # The collection of lines to be chunked later
        list_of_result_data = []
        # Use a sorted list of server names, so it displays in alphabetical order.
        for server_name, server_response in sorted(server_to_server_data.items()):
            # There will only be data for servers that didn't time out
            first_line = True
            test_new_object = ServerVerifyKeys({})
            buffered_message = f"{server_name_col.front_pad(server_name)} | "
            if isinstance(server_response, FederationErrorResponse):
                buffered_message += f"{server_response.reason}\n"

            else:
                test_new_object.update_key_data_from_list(server_response.response_dict)
                time_now = int(time.time() * 1000)
                for v_key_id, server_data in test_new_object.verify_keys.items():
                    valid_until_ts = server_data.valid_until_ts
                    valid_until_pretty = "None Found"

                    if valid_until_ts:
                        valid_until_pretty = pretty_print_timestamp(valid_until_ts)
                    pretty_expired_mark = "*" if valid_until_ts < time_now else ""

                    if not first_line:
                        buffered_message += f"{pad('', server_name_col.size)} | "
                    buffered_message += f"{server_key_col.pad(v_key_id)} | "
                    buffered_message += f"{pretty_expired_mark}{valid_until_pretty}\n"

                    first_line = False

            list_of_result_data.extend([buffered_message])

            # Only if there was a single server because of the above condition
            if display_raw:
                list_of_result_data.extend(
                    [f"{json.dumps(server_response.response_dict, indent=4)}\n"]
                )

        footer_message = f"\nTotal time for retrieval: {total_time:.3f} seconds\n"
        list_of_result_data.extend([footer_message])

        final_list_of_data = combine_lines_to_fit_event(
            list_of_result_data, header_message
        )

        for chunk in final_list_of_data:
            await command_event.respond(
                make_into_text_event(
                    wrap_in_code_block_markdown(chunk), ignore_body=True
                ),
            )

    @fed_command.subcommand(
        name="backfill", help="Request backfill over federation for a room."
    )
    @command.argument(
        name="room_id_or_alias", parser=is_room_id_or_alias, required=False
    )
    @command.argument(name="event_id", parser=is_event_id, required=False)
    @command.argument(name="limit", required=False)
    @command.argument(name="server_to_request_from", required=False)
    async def backfill_command(
        self,
        command_event: MessageEvent,
        room_id_or_alias: Optional[str],
        event_id: Optional[str],
        limit: Optional[str],
        server_to_request_from: Optional[str] = None,
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        if not limit:
            limit = "10"

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        if server_to_request_from:
            destination_server = server_to_request_from
        else:
            destination_server = origin_server

        discovered_info = await self._discover_event_ids_and_room_ids(
            origin_server, destination_server, command_event, room_id_or_alias, event_id
        )
        if not discovered_info:
            # The user facing error message was already sent
            return

        room_id, event_id, origin_server_ts = discovered_info

        if origin_server_ts:
            # A nice little addition for the status updated before the command runs
            special_time_formatting = (
                "\n  * which took place at: "
                f"{datetime.fromtimestamp(float(origin_server_ts / 1000))} UTC"
            )
        else:
            special_time_formatting = ""
        await command_event.respond(
            f"Retrieving last {limit} Events for \n"
            f"* Room: {room_id_or_alias or room_id}\n"
            f"* at Event ID: {event_id}{special_time_formatting}\n"
            f"* From {destination_server} using {origin_server}"
        )

        # This will be assigned by now
        assert event_id is not None

        response = await self.federation_handler.get_backfill_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            room_id=room_id,
            event_id=event_id,
            limit=limit,
        )

        if isinstance(response, FederationErrorResponse):
            await command_event.respond(
                f"Some kind of error\n{response.status_code}:{response.reason}"
            )
            return

        # The response should contain all the pdu data inside 'pdus'
        pdu_list_from_response = response.response_dict.get("pdus", [])

        # Time to start rendering. Build the header lines first
        header_message = ""
        dc_depth = DisplayLineColumnConfig("Depth")
        dc_etype = DisplayLineColumnConfig("Event Type")
        dc_sender = DisplayLineColumnConfig("Sender")
        dc_extras = DisplayLineColumnConfig("Extras")

        pdu_list: List[Tuple[int, EventBase]] = []
        for event in pdu_list_from_response:
            event_base = determine_what_kind_of_event(event_id=None, data_to_use=event)
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
        header_message += f"{dc_etype.pad()} "
        header_message += f"{dc_sender.pad()} "
        header_message += f"{dc_extras.pad()}\n"

        # ...and the delimiter
        header_message += f"{pad('', pad_to=len(header_message), pad_with='-')}\n"
        list_of_buffer_lines = []

        # Begin the render, first construct the template list
        template_list = [
            (["depth"], dc_depth),
            (["event_type"], dc_etype),
            (["sender"], dc_sender),
        ]
        for (_, event_base) in pdu_list:
            buffered_message = ""
            line_summary = event_base.to_template_line_summary(template_list)
            line_summary += " "
            line_summary += event_base.to_extras_summary()

            buffered_message += f"{line_summary}\n"

            list_of_buffer_lines.extend([buffered_message])

        # Chunk the data as there may be a few 'pages' of it
        final_list_of_data = combine_lines_to_fit_event(
            list_of_buffer_lines, header_message
        )

        for chunk in final_list_of_data:
            await command_event.respond(
                make_into_text_event(
                    wrap_in_code_block_markdown(chunk), ignore_body=True
                ),
            )

    @fed_command.subcommand(
        name="event_auth", help="Request the auth chain for an event over federation"
    )
    @command.argument(
        name="room_id_or_alias", parser=is_room_id_or_alias, required=False
    )
    @command.argument(name="event_id", parser=is_event_id, required=True)
    @command.argument(name="server_to_request_from", required=False)
    async def event_auth_command(
        self,
        command_event: MessageEvent,
        room_id_or_alias: Optional[str],
        event_id: Optional[str],
        server_to_request_from: Optional[str] = None,
    ) -> None:
        # Unlike some of the other commands, this one *requires* an event_id passed in.

        # Let the user know the bot is paying attention
        await command_event.mark_read()

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        if server_to_request_from:
            destination_server = server_to_request_from
        else:
            destination_server = origin_server

        discovered_info = await self._discover_event_ids_and_room_ids(
            origin_server, destination_server, command_event, room_id_or_alias, event_id
        )
        if not discovered_info:
            # The user facing error message was already sent
            return

        room_id, event_id, origin_server_ts = discovered_info

        if origin_server_ts:
            # A nice little addition for the status updated before the command runs
            special_time_formatting = (
                "\n  * which took place at: "
                f"{datetime.fromtimestamp(float(origin_server_ts / 1000))} UTC"
            )
        else:
            special_time_formatting = ""

        await command_event.respond(
            "Retrieving the chain of Auth Events for:\n"
            f"* Event ID: {event_id}{special_time_formatting}\n"
            f"* in Room: {room_id_or_alias or room_id}\n"
            f"* From {destination_server} using {origin_server}"
        )

        # This will be assigned by now
        assert event_id is not None

        started_at = time.monotonic()
        response = await self.federation_handler.get_event_auth_for_event_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            room_id=room_id,
            event_id=event_id,
        )
        total_time = time.monotonic() - started_at

        if isinstance(response, FederationErrorResponse):
            await command_event.respond(
                f"Some kind of error\n{response.status_code}:{response.reason}"
            )
            return

        # The response should contain all the pdu data inside 'pdus'
        list_from_response = response.response_dict.get("auth_chain", [])
        list_of_event_bases = parse_list_response_into_list_of_event_bases(
            list_from_response
        )
        # Time to start rendering. Build the header lines first
        header_message = ""
        dc_depth = DisplayLineColumnConfig("Depth")
        dc_etype = DisplayLineColumnConfig("Event Type")
        dc_sender = DisplayLineColumnConfig("Sender")
        dc_extras = DisplayLineColumnConfig("Extras")

        ordered_list: List[Tuple[int, EventBase]] = []
        for event in list_of_event_bases:
            # Don't worry about resizing the 'Extras' Column,
            # it's on the end and variable length
            dc_depth.maybe_update_column_width(len(str(event.depth)))
            dc_etype.maybe_update_column_width(len(event.event_type))
            dc_sender.maybe_update_column_width(len(event.sender))

            ordered_list.append((event.depth, event))

        # Sort the list in place by the first of the tuples, which is the depth
        ordered_list.sort(key=lambda x: x[0])

        # Build the header line...
        header_message += f"{dc_depth.pad()} "
        header_message += f"{dc_etype.pad()} "
        header_message += f"{dc_sender.pad()} "
        header_message += f"{dc_extras.pad()}\n"

        # ...and the delimiter
        header_message += f"{pad('', pad_to=len(header_message), pad_with='-')}\n"
        list_of_buffer_lines = []

        # Begin the render, first construct the template list
        template_list = [
            (["depth"], dc_depth),
            (["event_type"], dc_etype),
            (["sender"], dc_sender),
        ]
        for (_, event_base) in ordered_list:
            buffered_message = ""
            line_summary = event_base.to_template_line_summary(template_list)
            line_summary += " "
            line_summary += event_base.to_extras_summary()

            buffered_message += f"{line_summary}\n"

            list_of_buffer_lines.extend([buffered_message])

        footer_message = f"\nTotal time for retrieval: {total_time:.3f} seconds\n"
        list_of_buffer_lines.extend([footer_message])

        # Chunk the data as there may be a few 'pages' of it
        final_list_of_data = combine_lines_to_fit_event(
            list_of_buffer_lines, header_message
        )

        for chunk in final_list_of_data:
            await command_event.respond(
                make_into_text_event(
                    wrap_in_code_block_markdown(chunk), ignore_body=True
                ),
            )

    @fed_command.subcommand(
        name="user_devices", help="Request user devices over federation for a user."
    )
    @command.argument(name="user_mxid", parser=is_mxid, required=True)
    async def user_devices_command(
        self,
        command_event: MessageEvent,
        user_mxid: str,
    ) -> None:
        # Let the user know the bot is paying attention
        await command_event.mark_read()

        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        origin_server = get_domain_from_id(self.client.mxid)
        if origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first."
            )
            return

        _, destination_server = user_mxid.split(":", maxsplit=1)

        await command_event.respond(
            f"Retrieving user devices for {user_mxid}\n"
            f"* From {destination_server} using {origin_server}"
        )

        response = await self.federation_handler.get_user_devices_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            user_mxid=user_mxid,
        )

        if isinstance(response, FederationErrorResponse):
            await command_event.respond(
                f"Some kind of error\n{response.status_code}:{response.reason}\n\n"
                f"{json.dumps(response.response_dict, indent=4)}"
            )
            return

        await command_event.respond(
            f"```json\n{json.dumps(response.response_dict, indent=4)}\n```\n"
        )
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

    async def _resolve_room_id_or_alias(
        self,
        room_id_or_alias: Optional[str],
        command_event: MessageEvent,
        origin_server: str,
    ) -> Optional[str]:
        if room_id_or_alias:
            # Sort out if the room id or alias passed in is valid and resolve the alias
            # to the room id if it is.
            if room_id_or_alias.startswith("#"):
                # look up the room alias. The server is extracted from the alias itself.
                alias_result = await self.federation_handler.get_room_alias_from_server(
                    origin_server=origin_server,
                    # destination_server=destination_server,
                    room_alias=room_id_or_alias,
                )
                if isinstance(alias_result, FederationErrorResponse):
                    await command_event.reply(
                        "Received an error while querying for room alias: "
                        f"{alias_result.status_code}: {alias_result.reason}"
                    )
                    # self.log.warning(f"alias_result: {alias_result}")
                    return None
                else:
                    room_id = alias_result.response_dict.get("room_id")
            elif room_id_or_alias.startswith("!"):
                room_id = room_id_or_alias
            else:
                # Probably won't ever hit this, as it will be prefiltered at the command
                # invocation.
                await command_event.reply(
                    "Room ID or Alias supplied doesn't have the appropriate sigil"
                    f"(either a `!` or a `#`), '{room_id_or_alias}'"
                )
                return None
        else:
            # When not supplied a room id, we assume they want the room the command was
            # issued from.
            room_id = str(command_event.room_id)
        return room_id

    async def get_hosts_in_room_ordered(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        event_id_in_timeline: str,
    ) -> List[str]:
        # Should be a faithful recreation of what Synapse does.
        sql = """
            SELECT
                /* Match the domain part of the MXID */
                substring(c.state_key FROM '@[^:]*:(.*)$') as server_domain
            FROM current_state_events c
            /* Get the depth of the event from the events table */
            INNER JOIN events AS e USING (event_id)
            WHERE
                /* Find any join state events in the room */
                c.type = 'm.room.member'
                AND c.membership = 'join'
                AND c.room_id = ?
            /* Group all state events from the same domain into their own buckets (groups) */
            GROUP BY server_domain
            /* Sorted by lowest depth first */
            ORDER BY min(e.depth) ASC;
        """
        # (Given the toolbox at the time of writing) I think the best way to simulate
        # this will be to use get_state_ids_from_server(), which returns a tuple of the
        # current state ids and the auth chain ids. The state ids should have all the
        # data from the room up to that point already layered to be current. Pull those
        # events, then sort them based on above.
        # Update for 0.0.5: Taking Tom's suggestion, going to use the alternative,
        # get_state_from_server() instead. It will at the very least save some
        # processing steps.
        state_events, _ = await self.federation_handler.get_state_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            room_id=room_id,
            event_id=event_id_in_timeline,
        )
        converted_state_events = []
        for state_event in state_events:
            converted_state_events.append(
                determine_what_kind_of_event(None, data_to_use=state_event)
            )

        filtered_room_member_events = cast(
            List[RoomMemberStateEvent],
            filter_events_based_on_type(converted_state_events, "m.room.member"),
        )
        joined_member_events = cast(
            List[RoomMemberStateEvent],
            filter_state_events_based_on_membership(
                filtered_room_member_events, "join"
            ),
        )
        joined_member_events.sort(key=lambda x: x.depth)
        hosts_ordered = []
        for member in joined_member_events:
            host = get_domain_from_id(member.state_key)
            if host not in hosts_ordered:
                hosts_ordered.extend([host])

        return hosts_ordered

    async def _discover_event_ids_and_room_ids(
        self,
        origin_server: str,
        destination_server: str,
        command_event: MessageEvent,
        room_id_or_alias: Optional[str],
        event_id: Optional[str],
    ) -> Optional[Tuple[str, str, int]]:
        room_id = await self._resolve_room_id_or_alias(
            room_id_or_alias, command_event, origin_server
        )
        if not room_id:
            # Don't need to actually display an error, that's handled in the above
            # function
            return None

        origin_server_ts = None
        if not event_id:
            # No event id was supplied, find out what the last event in the room was
            now = int(time.time() * 1000)
            ts_response = (
                await self.federation_handler.get_timestamp_to_event_from_server(
                    origin_server=origin_server,
                    destination_server=destination_server,
                    room_id=room_id,
                    utc_time_at_ms=now,
                )
            )
            if isinstance(ts_response, FederationErrorResponse):
                await command_event.respond(
                    "Something went wrong while getting last event in room("
                    f"{ts_response.reason}"
                    "). Please supply an event_id instead at the place in time of query"
                )
                return None
            else:
                event_id = ts_response.response_dict.get("event_id", None)

        assert event_id is not None
        event_result = await self.federation_handler.get_event_from_server(
            origin_server, destination_server, event_id
        )
        event = event_result.get(event_id, None)
        if event:
            if isinstance(event, EventError):
                await command_event.reply(
                    "The Event ID supplied doesn't appear to be on the origin "
                    f"server({origin_server}). Try query a different server for it."
                )
                return None

            if isinstance(event, (Event, GenericStateEvent)):
                room_id = event.room_id
                origin_server_ts = event.origin_server_ts

        assert isinstance(origin_server_ts, int)
        return room_id, event_id, origin_server_ts

    async def _get_event_from_backfill(
        self, origin_server: str, destination_server: str, room_id: str, event_id: str
    ) -> Optional[EventBase]:
        """
        Retrieve a single event from the backfill mechanism. This will have 3 types of
        return values(listed below)

        Args:
            origin_server: The server to make the request from(applies auth to request)
            destination_server: The server being asked
            room_id: The room the Event ID should be part of
            event_id: The actual Event ID to look up

        Returns:
            * EventBase in question
            * None(for when the event isn't on this server)
            * Error from federation response in the EventError custom class

        """
        response = await self.federation_handler.get_backfill_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            room_id=room_id,
            event_id=event_id,
            limit="1",
        )
        if isinstance(response, FederationErrorResponse):
            return EventError(
                event_id=EventID(event_id),
                data={
                    "error": f"{response.reason}",
                    "errcode": f"{response.status_code}",
                },
            )

        pdus_list = response.response_dict.get("pdus", [])

        event = None
        # Even though this is a list, there should be only one
        for pdu in pdus_list:
            event = determine_what_kind_of_event(
                event_id=EventID(event_id), data_to_use=pdu
            )
        return event


def format_result_lines(
    server_name: str,
    server_name_max_size: int,
    server_software: str,
    server_software_max_size: int,
    server_version: str,
    server_version_max_size: int,
) -> str:
    buffered_message = (
        f"{pad(server_name, server_name_max_size, front=True)} | "
        f"{pad(server_software, server_software_max_size)}"
        f" | {pad(server_version, server_version_max_size, trim_backend=True)}\n"
    )
    return buffered_message


def format_result_lines_var(line_segments: List[Tuple[str, int]]) -> str:
    buffered_message = ""
    count = len(line_segments)
    for line_data, column_size in line_segments:
        pass
    return buffered_message


def wrap_in_code_block_markdown(existing_buffer: str) -> str:
    prepend_string = "```text\n"
    append_string = "```\n"
    new_buffer = ""
    if existing_buffer != "":
        new_buffer = prepend_string + existing_buffer + append_string

    return new_buffer


def make_into_text_event(
    message: str, ignore_body: bool = False
) -> TextMessageEventContent:
    content = TextMessageEventContent(
        msgtype=MessageType.NOTICE,
        body=message if not ignore_body else "no alt text available",
        format=Format.HTML,
        formatted_body=markdown.render(message),
    )

    return content


def wrap_in_pre_tags(incoming: str) -> str:
    buffer = ""
    if incoming != "":
        buffer = f"<pre>\n{incoming}\n</pre>\n"
    return buffer


def wrap_in_ul_tags(incoming: str) -> str:
    buffer = ""
    if incoming != "":
        buffer = f"<ul>\n{incoming}\n</ul>\n"
    return buffer


def wrap_in_li_tags(incoming: str) -> str:
    buffer = ""
    if incoming != "":
        buffer += f"<li>\n{incoming}\n</li>\n"
    return buffer


def wrap_in_details(incoming: str, summary_tag: str) -> str:
    buffer = ""
    if incoming != "":
        buffer = f"<details>\n<summary>{summary_tag}</summary>\n"
        buffer += f"{incoming}\n</details>\n"
    return buffer


def combine_lines_to_fit_event(
    list_of_all_lines: List[str], header_line: str
) -> List[str]:
    """
    bring your own newlines

    Args:
        list_of_all_lines: strings to render(don't forget newlines)
        header_line: if you want a line at the top(description or whatever)

    Returns: List strings designed to fit into an Event's size restrictions

    """
    list_of_combined_lines = []
    # Make sure it's a copy and not a reference
    buffered_line = str(header_line)
    for line in list_of_all_lines:
        if len(buffered_line) + len(line) > MAX_EVENT_SIZE_FOR_SENDING:
            # This buffer is full, add it to the final list
            list_of_combined_lines.extend([buffered_line])
            # Don't forget to start the new buffer
            buffered_line = str(header_line)

        buffered_line += line

    # Grab the last buffer too
    list_of_combined_lines.extend([buffered_line])
    return list_of_combined_lines
