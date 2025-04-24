from __future__ import annotations

from typing import Any, Collection, Dict, List, Optional, Sequence, Set, Tuple, Union, cast
import asyncio
import json
import logging
import time

from mautrix.types import EventID, RoomID
from signedjson.key import decode_verify_key_bytes
from signedjson.sign import SignatureVerifyException, verify_signed_json

from federationbot.api import FederationApi
from federationbot.cache import LRUCache
from federationbot.controllers import ReactionTaskController
from federationbot.errors import FedBotException
from federationbot.events import (
    Event,
    EventBase,
    EventError,
    RoomMemberStateEvent,
    determine_what_kind_of_event,
    redact_event,
)
from federationbot.primitives import KeyID
from federationbot.responses import (
    MakeJoinResponse,
    MatrixError,
    MatrixFederationResponse,
    MatrixResponse,
    RoomHeadData,
    TimestampToEventResponse,
)
from federationbot.types import KeyContainer, RoomAlias, ServerVerifyKeys, SignatureVerifyResult
from federationbot.utils import full_dict_copy, get_domain_from_id

fed_handler_logger = logging.getLogger("federation_handler")


class FederationHandler:
    def __init__(
        self,
        bot_mxid: str,
        server_signing_keys: Dict[str, str],
        task_controller: ReactionTaskController,
    ):
        self.hosting_server = get_domain_from_id(bot_mxid)
        self.bot_mxid = bot_mxid
        self.server_signing_keys = server_signing_keys
        self.json_decoder = json.JSONDecoder()
        self.task_controller = task_controller
        # Map the key to (server_name, event_id) -> Event
        self._events_cache: LRUCache[Tuple[str, str], EventBase] = LRUCache()
        self._server_keys_cache: LRUCache[str, ServerVerifyKeys] = LRUCache()
        self.room_version_cache: LRUCache[str, str] = LRUCache(
            expire_after_seconds=float(60 * 60 * 6),
            cleanup_task_sleep_time_seconds=float(60 * 60),
        )
        self.api = FederationApi(self.server_signing_keys, self.task_controller)

    async def stop(self) -> None:
        # For stopping the cleanup task on these caches
        await self._events_cache.stop()
        await self._server_keys_cache.stop()
        await self.room_version_cache.stop()
        await self.api.shutdown()

    async def get_server_keys(self, server_name: str, **kwargs) -> ServerVerifyKeys:
        response = await self.api.get_server_keys(server_name=server_name, **kwargs)

        json_response = response.json_response
        return ServerVerifyKeys(json_response)

    async def get_server_keys_from_notary(
        self, fetch_server_name: str, from_server_name: str, **kwargs
    ) -> ServerVerifyKeys:
        minimum_valid_until_ts = int(time.time() * 1000) + (30 * 60 * 1000)  # Add 30 minutes

        response = await self.api.get_server_notary_keys(
            fetch_server_name=fetch_server_name,
            from_server_name=from_server_name,
            minimum_valid_until_ts=minimum_valid_until_ts,
        )

        server_verify_keys = ServerVerifyKeys({})

        server_verify_keys.update_key_data_from_list(response.json_response)

        return server_verify_keys

    async def get_server_key_by_id(
        self, for_server_name: str, key_id_needed: str, **kwargs
    ) -> Dict[KeyID, KeyContainer]:
        key_id_formatted = KeyID(key_id_needed)

        cached_server_keys = self._server_keys_cache.get(for_server_name)
        if cached_server_keys is not None:
            if key_id_formatted in cached_server_keys.verify_keys:
                return cached_server_keys.verify_keys

        server_verify_keys = await self.get_server_keys(for_server_name, **kwargs)

        if key_id_formatted not in server_verify_keys.verify_keys:
            server_verify_keys = await self.get_server_keys_from_notary(for_server_name, self.hosting_server, **kwargs)

        # At this point we know
        # 1. Wasn't in the cache before(or at least this one key id wasn't)
        # 2. We have some kind of result
        verify_keys = server_verify_keys.verify_keys

        self._server_keys_cache.set(for_server_name, server_verify_keys)

        return verify_keys

    async def verify_signatures_and_annotate_event(
        self,
        event: Event,
        room_version: str,
    ) -> None:
        # There are two places that discuss verifying the signatures:

        # 1. S2S section 27.2
        #  First the signature is checked. The event is redacted following the
        #  redaction algorithm, and the resultant object is checked for a signature
        #  from the originating server, following the algorithm described in Checking
        #  for a signature. Note that this step should succeed whether we have been
        #  sent the full event or a redacted copy.
        #
        #  The signatures expected on an event are:
        #
        #  The sender’s server, unless the invite was created as a result of 3rd party
        #  invite. The sender must already match the 3rd party invite, and the server
        #  which actually sends the event may be a different server.
        #
        #  For room versions 1 and 2, the server which created the event_id. Other
        #  room versions do not track the event_id over federation and therefore do
        #  not need a signature from those servers.

        # 2. Appendices 3.3

        # To check if an entity has signed a JSON object an implementation does the
        # following:
        #
        #  1. Checks if the signatures member of the object contains an entry with the
        #     name of the entity. If the entry is missing then the check fails.
        #  2. Removes any signing key identifiers from the entry with algorithms it
        #     doesn’t understand. If there are no signing key identifiers left then the
        #     check fails.
        #  3. Looks up verification keys for the remaining signing key identifiers
        #     either from a local cache or by consulting a trusted key server. If it
        #     cannot find a verification key then the check fails.
        #  4. Decodes the base64 encoded signature bytes. If base64 decoding fails then
        #     the check fails.
        #  5. Removes the signatures and unsigned members of the object.
        #  6. Encodes the remainder of the JSON object using the Canonical JSON
        #     encoding.
        #  7. Checks the signature bytes against the encoded object using the
        #     verification key. If this fails then the check fails. Otherwise the check
        #     succeeds.

        # So it looks like in summary:
        # 1. Redact the event to strip off keys that aren't needed
        # 2. Make sure we have the various servers that have signed the event's public
        #    keys. Should already have the key decoded from base64 in the KeyContainer
        # 3. Create the VerifyKey from the decoded server key
        # 4. Run each servers key's through verify_signed_json(), which will:
        #    * Strip off all signatures and the 'unsigned' section of an event
        #    * Does the actual verifying
        base_event = full_dict_copy(event.raw_data)
        signatures = base_event.get("signatures", {})
        redacted_base_event = redact_event(room_version, base_event)
        for server_name, server_key_set in signatures.items():
            for server_key_id in server_key_set.keys():
                remote_server_keys = await self.get_server_key_by_id(server_name, server_key_id)

                remote_server_key = remote_server_keys.get(KeyID(server_key_id), None)
                if remote_server_key is not None:
                    verify_key = decode_verify_key_bytes(server_key_id, remote_server_key.key.decoded_key)
                    try:
                        verify_signed_json(redacted_base_event, server_name, verify_key)
                    except SignatureVerifyException:
                        event.signatures_verified[server_name] = SignatureVerifyResult.FAIL
                    else:
                        event.signatures_verified[server_name] = SignatureVerifyResult.SUCCESS

    async def get_event_from_server(
        self,
        origin_server: str,
        destination_server: str,
        event_id: str,
        inject_new_data: Optional[Dict[str, Any]] = None,
        keys_to_pop: Optional[str] = None,
    ) -> Dict[str, EventBase]:
        """
        Retrieves a single Event from a server. Since the event id will be known, it can
         be included in the retrieved Event.
        Args:
            origin_server: The server placing the request
            destination_server: The server receiving the request
            event_id: The opaque string of the id given to the Event
            inject_new_data: Allow for injecting data into the structure for testing verification later
            keys_to_pop: Allow for removing data by key(s) from the structure for testing verification

        Returns: A tuple containing the FederationResponse received and the Event
            contained in a List

        """

        new_event_base = self._events_cache.get((destination_server, event_id))
        if new_event_base and not inject_new_data and not keys_to_pop:
            # Only successful events are cached
            return {event_id: new_event_base}

        response = await self.api.get_event(
            destination_server,
            origin_server,
            event_id,
        )

        if response.http_code != 200:
            new_event_base = EventError(
                EventID(event_id),
                {
                    "error": f"{response.error or response.reason}",
                    "errcode": f"{response.errcode or response.http_code}",
                },
            )

            return {event_id: new_event_base}

        pdu_list: List[Dict[str, Any]] = response.json_response.get("pdus", [])
        split_keys: List[str] = []
        if keys_to_pop:
            if "," in keys_to_pop:
                split_keys = keys_to_pop.split(",")
            else:
                split_keys = [keys_to_pop]

        for data in pdu_list:
            if inject_new_data:
                data.update(inject_new_data)
            for key_to_lose in split_keys:
                key_to_lose = key_to_lose.strip()

                data.pop(key_to_lose, None)
            # This path is only taken on success, errors are sorted above
            room_version: str = await self.discover_room_version(origin_server, destination_server, data["room_id"])
            new_event_base = determine_what_kind_of_event(EventID(event_id), room_version, data)
            if not (inject_new_data or keys_to_pop):
                self._events_cache.set((destination_server, event_id), new_event_base)

        assert new_event_base is not None
        return {event_id: new_event_base}

    async def get_events_from_server(
        self,
        origin_server: str,
        destination_server: str,
        events_list: Union[Sequence[str], Set[str]],
    ) -> Dict[str, EventBase]:
        """
        Retrieve multiple Events from a given server. Uses Async Tasks and a Queue to
        be efficient. Creates number of event_ids up to 3 Tasks for concurrency.

        Args:
            origin_server: The server to auth the request with
            destination_server: The server to ask about the Event
            events_list: Either a Sequence or a Set of Event ID strings

        Returns: A mapping of the Event ID to the Event(or EventError)

        """
        # Keep both the response and the actual event, if there was an error it will be
        # in the response and the event won't exist here
        event_to_event_base: Dict[str, EventBase] = {}

        async def _get_event_worker(queue: asyncio.Queue[str]) -> None:
            while True:
                worker_event_id: str = await queue.get()

                event_base_dict = await self.get_event_from_server(
                    origin_server=origin_server,
                    destination_server=destination_server,
                    event_id=worker_event_id,
                )

                for r_event_id, event_base in event_base_dict.items():
                    # Add this newly retrieved Event data to the outside dict that
                    # is being returned.
                    event_to_event_base[r_event_id] = event_base

                queue.task_done()

        fed_handler_logger.debug(
            "get_events_from_server: requesting %d events from %s", len(events_list), destination_server
        )
        event_queue: asyncio.Queue[str] = asyncio.Queue()
        for event_id in events_list:
            await event_queue.put(event_id)

        reference_key = self.task_controller.setup_task_set()

        # Limit this to no more than three. Synapse in particular isn't capable of pulling more than 3 events
        # from its database simultaneously. No sense is overloading it, and it's pretty quick usually.
        self.task_controller.add_tasks(reference_key, _get_event_worker, event_queue, limit=min(len(events_list), 3))

        await event_queue.join()

        # Cancel our worker tasks.
        await self.task_controller.cancel(reference_key)
        return event_to_event_base

    async def get_raw_pdu(
        self,
        origin_server: str,
        destination_server: str,
        event_id: str,
    ) -> dict[str, Any]:
        """
        Retrieves a single Event PDU from a server. This will be in the raw JSON converted format

        Args:
            origin_server: The server placing the request
            destination_server: The server receiving the request
            event_id: The opaque string of the id given to the Event

        Returns: A dict format json response of a single PDU

        """
        response = await self.api.get_event(
            destination_server,
            origin_server,
            event_id,
        )

        if isinstance(response, MatrixError):
            raise response

        if response.http_code != 200:
            raise MatrixError(**response.__dict__)

        pdu_list: List[Dict[str, Any]] = response.json_response.get("pdus", [])
        if len(pdu_list) < 1:
            # When retrieving and event, there should only be a single pdu in the list. Should never be more than one,
            # and sometimes there are none. WHat makes this problematic is when the response returns a 200, then has no
            # data. This case will be caught in get_event() below
            fed_handler_logger.debug(
                "get_raw_pdu: PDU list problem: %s: %s: code: %d", destination_server, event_id, response.http_code
            )
            fed_handler_logger.debug(
                "get_raw_pdu: PDU list problem %s: %s:\n%s",
                destination_server,
                event_id,
                json.dumps(response.json_response, indent=2),
            )
            return {}
        return pdu_list[0]

    async def get_event(self, origin_server: str, destination_server: str, event_id: str) -> EventBase:
        """

        Args:
            origin_server:
            destination_server:
            event_id:

        Returns:

        """
        try:
            pdu = await self.get_raw_pdu(origin_server, destination_server, event_id)
        except MatrixError as e:
            return EventError(EventID(event_id), {"errcode": e.errcode, "error": e.error})
        if not pdu:
            # I'm not sure the circumstances in this, but was found in one instance. I did check the rest of the
            # response returned before passing thru get_raw_pdu(), and it had the other required fields
            return EventError(
                EventID(event_id), {"errcode": "I_DONT_KNOW", "error": "HTTP returned 200, but no usable data"}
            )
        room_id = pdu.get("room_id")
        assert room_id is not None, f"get_event: {destination_server}: {event_id}: no room ID found"

        room_version = await self.discover_room_version(origin_server, destination_server, room_id)
        return determine_what_kind_of_event(event_id=None, room_version=room_version, data_to_use=pdu)

    async def find_event_on_servers(
        self, origin_server: str, event_id: str, servers_to_check: Collection[str]
    ) -> Dict[str, EventBase]:
        host_to_event_status_map: Dict[str, EventBase] = {}

        host_queue: asyncio.Queue[str] = asyncio.Queue()
        for host in servers_to_check:
            host_queue.put_nowait(host)

        async def _event_finding_worker(
            queue: asyncio.Queue[str],
        ) -> Tuple[str, EventBase]:
            worker_host = await queue.get()
            returned_events = await self.get_event_from_server(
                origin_server=origin_server,
                destination_server=worker_host,
                event_id=event_id,
            )
            inner_returned_event = returned_events.get(event_id)
            assert inner_returned_event is not None
            queue.task_done()
            return worker_host, inner_returned_event

        # Create a collection of Task's, to run the coroutine in
        reference_task_key = self.task_controller.setup_task_set()

        # These are one-off tasks, not workers. Create as many as we have servers to check
        self.task_controller.add_tasks(
            reference_task_key,
            _event_finding_worker,
            host_queue,
            limit=len(servers_to_check),
        )

        results = await self.task_controller.get_task_results(reference_task_key)

        for result in results:
            if isinstance(result, BaseException):
                raise result
            response: EventBase
            host, response = result
            host_to_event_status_map[host] = response

        # Make sure to cancel all tasks
        await self.task_controller.cancel(reference_task_key)

        return host_to_event_status_map

    async def get_state_ids_from_server(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        event_id: str,
    ) -> Tuple[List[str], List[str]]:
        response = await self.api.get_state_ids(
            origin_server,
            destination_server,
            room_id,
            event_id,
        )

        pdu_list = response.json_response.get("pdu_ids", [])
        auth_chain_list = response.json_response.get("auth_chain_ids", [])

        return pdu_list, auth_chain_list

    async def get_state_from_server(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        event_id: str,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        response = await self.api.get_state(
            origin_server,
            destination_server,
            room_id,
            event_id,
        )

        pdus_list = response.json_response.get("pdus", [])
        auth_chain_list = response.json_response.get("auth_chain", [])

        return pdus_list, auth_chain_list

    async def resolve_room_alias(
        self,
        room_alias: RoomAlias,
        origin_server: str,
    ) -> Tuple[str, List[str]]:
        """

        Args:
            room_alias:
            origin_server: The server that is sending the request

        Returns:
        Raises: ValueError if room_alias does not start with '#' or contain a ':'
        """
        # look up the room alias. The server is extracted from the alias itself.
        alias_result = await self.api.get_room_alias_from_directory(
            origin_server,
            room_alias.origin_server,
            room_alias,
        )

        if alias_result.http_code != 200 or isinstance(alias_result, MatrixError):
            raise FedBotException(
                summary_exception=f"{alias_result.errcode or alias_result.http_code}: {alias_result.error or alias_result.reason}"
            )

        room_id: str = alias_result.json_response["room_id"]
        list_of_servers: List[str] = alias_result.json_response.get("servers", [])

        return room_id, list_of_servers

    async def send_events_to_server(
        self,
        origin_server: str,
        destination_server: str,
        event_data: Sequence[Dict[str, Any]],
    ) -> MatrixResponse:
        response = await self.api.put_pdu_transaction(
            origin_server,
            destination_server,
            event_data,
        )

        return response

    async def get_public_rooms_from_server(
        self,
        origin_server: Optional[str],
        destination_server: Optional[str],
        include_all_networks: bool = False,
        limit: int = 10,
        since: Optional[str] = None,
        third_party_instance_id: Optional[str] = None,
    ) -> MatrixResponse:
        if not origin_server:
            origin_server = self.hosting_server
        if not destination_server:
            destination_server = origin_server

        response = await self.api.get_public_rooms(
            origin_server,
            destination_server,
            include_all_networks=include_all_networks,
            limit=limit,
            since=since,
            third_party_instance_id=third_party_instance_id,
        )

        return response

    async def discover_room_version(
        self,
        origin_server: Optional[str],
        destination_server: Optional[str],
        room_id: str,
    ) -> str:
        room_version = self.room_version_cache.get(room_id)
        if room_version:
            return room_version

        if not origin_server:
            origin_server = self.hosting_server
        if not destination_server:
            destination_server = origin_server

        try:
            response = await self.make_join_to_server(
                origin_server=origin_server,
                destination_server=destination_server,
                room_id=room_id,
                user_id=self.bot_mxid,
            )

        except MatrixError:
            # whine about it
            fed_handler_logger.warning(
                "discover_room_version: Failed to make_join to room: %s from %s", room_id, destination_server
            )

        else:
            if response.http_code == 200:
                room_version = response.room_version
                if room_id not in self.room_version_cache:
                    self.room_version_cache.set(room_id, room_version)

                return room_version

        try:
            # perhaps retrieve the last event in the room, and use its auth events to triangulate the create event
            timestamp_response = await self.get_last_event_id_in_room(origin_server, destination_server, room_id)
            last_event_id = timestamp_response.event_id
            fed_handler_logger.info("discover_room_version: trying ts2e response, found event_id: %s", last_event_id)
            last_event_raw_pdu = await self.get_raw_pdu(origin_server, destination_server, last_event_id)

            auth_event_id_list = last_event_raw_pdu.get("auth_events", [])
            for auth_event_id in auth_event_id_list:
                auth_event = await self.get_raw_pdu(origin_server, destination_server, auth_event_id)
                if room_version := auth_event.get("content", {}).get("room_version"):
                    # Found it
                    if room_id not in self.room_version_cache:
                        self.room_version_cache.set(room_id, room_version)
                    break
        except MatrixError as e:
            fed_handler_logger.warning(
                "discover_room_version: Hit exception pulling last event in room: %s from %s\n%r",
                room_id,
                destination_server,
                e,
            )

        else:
            if room_id not in self.room_version_cache:
                self.room_version_cache.set(room_id, room_version)

        # We still don't have a room version, federation is playing a rough game. Maybe just pull state?
        assert room_version is not None, f"room_version was unexpectedly None for {room_id}"
        return room_version

    async def filter_state_for_type(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        state_type_str: str,
    ) -> List[EventBase]:
        now = int(time.time() * 1000)
        event_id = None
        ts_response = await self.api.get_timestamp_to_event(
            origin_server,
            destination_server,
            room_id,
            now,
        )
        if isinstance(ts_response, MatrixFederationResponse):
            event_id = ts_response.json_response.get("event_id")

        assert event_id is not None
        state_ids, _ = await self.get_state_ids_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            room_id=room_id,
            event_id=event_id,
        )
        state_events = await self.get_events_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            events_list=state_ids,
        )
        event_base_list = []
        for event in state_events.values():
            if event.event_type == state_type_str:
                event_base_list.append(event)

        return event_base_list

    async def make_join_to_server(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        user_id: str,
    ) -> MakeJoinResponse:
        """

        Args:
            origin_server:
            destination_server:
            room_id:
            user_id:

        Returns: A MakeJoinResponse
        Raises: MatrixError with the data of why

        """
        response = await self.api.make_join(
            origin_server=origin_server,
            destination_server=destination_server,
            room_id=room_id,
            user_id=user_id,
        )
        if isinstance(response, MatrixError):
            raise response

        if response.http_code != 200:
            raise MatrixError(
                http_code=response.http_code,
                errcode=response.json_response.get("errcode"),
                error=response.json_response.get("error"),
            )

        return MakeJoinResponse(**response.__dict__)

    async def get_room_head(
        self, origin_server: str, destination_server: str, room_id: str | RoomID, user_id: str
    ) -> RoomHeadData:
        """
        Pre-calculate HEAD data on the room requested
        Args:
            origin_server:
            destination_server:
            room_id:
            user_id:

        Returns:

        """
        make_join = await self.make_join_to_server(origin_server, destination_server, room_id, user_id)
        if make_join.http_code != 200:
            raise MatrixError(
                http_code=make_join.http_code,
                errcode=make_join.json_response.get("errcode"),
                error=make_join.json_response.get("error"),
            )

        events_list = []
        for event_id in make_join.prev_events:
            event = await self.get_event(origin_server, destination_server, event_id)
            if isinstance(event, EventError):
                fed_handler_logger.debug(
                    "get_room_head: %s: %s: %s: error while retrieving event\n%r",
                    destination_server,
                    room_id,
                    event_id,
                    event,
                )
                continue
            events_list.append(event)

        room_head = RoomHeadData(make_join, events_list)
        return room_head

    async def get_last_event_id_in_room(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
    ) -> TimestampToEventResponse:
        """

        Args:
            origin_server:
            destination_server:
            room_id:

        Returns: A TimestampToEventResponse
        Raises: MatrixError with the data of why, TypeError when data is missing(like not in the room, forbidden, etc)

        """
        now = int(time.time() * 1000)
        response = await self.api.get_timestamp_to_event(origin_server, destination_server, room_id, now)
        if isinstance(response, MatrixError):
            raise response

        if response.http_code != 200:
            raise MatrixError(
                http_code=response.http_code,
                errcode=response.json_response.get("errcode"),
                error=response.json_response.get("error"),
            )

        return TimestampToEventResponse(response)

    async def get_events_from_backfill(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        start_event_id: str,
        limit: int = 1,
    ) -> List[EventBase]:
        """
        Retrieve a series of events from the backfill mechanism. This will have 3 types of
        return values(listed below)

        Args:
            origin_server: The server to make the request from(applies auth to request)
            destination_server: The server being asked
            room_id: The room the Event ID should be part of
            start_event_id: The actual Event ID to look up
            limit: Number of Events to pull

        Returns:
            * EventBase in question
            * Error from federation response in the EventError custom class

        """
        room_version = await self.discover_room_version(origin_server, destination_server, room_id)
        response = await self.api.get_backfill(
            origin_server, destination_server, room_id, start_event_id, limit=str(limit)
        )
        if response.http_code != 200:
            # If there was an error, put it into a format that is expected.
            return [
                EventError(
                    event_id=EventID(""),
                    data={
                        "error": f"{response.reason}",
                        "errcode": f"{response.http_code}",
                    },
                )
            ]

        pdus_list = response.json_response.get("pdus", [])

        return parse_list_response_into_list_of_event_bases(pdus_list, room_version)

    async def get_hosts_in_room_ordered(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        event_id_in_timeline: str,
    ) -> List[str]:
        # Should be a faithful recreation of what Synapse does.

        # SELECT
        #     /* Match the domain part of the MXID */
        #     substring(c.state_key FROM '@[^:]*:(.*)$') as server_domain
        # FROM current_state_events c
        # /* Get the depth of the event from the events table */
        # INNER JOIN events AS e USING (event_id)
        # WHERE
        #     /* Find any join state events in the room */
        #     c.type = 'm.room.member'
        #     AND c.membership = 'join'
        #     AND c.room_id = ?
        # /* Group all state events from the same domain into their own buckets (groups) */
        # GROUP BY server_domain
        # /* Sorted by lowest depth first */
        # ORDER BY min(e.depth) ASC;

        # (Given the toolbox at the time of writing) I think the best way to simulate
        # this will be to use get_state_ids_from_server(), which returns a tuple of the
        # current state ids and the auth chain ids. The state ids should have all the
        # data from the room up to that point already layered to be current. Pull those
        # events, then sort them based on above.
        # Update for 0.0.5: Taking Tom's suggestion, going to use the alternative,
        # get_state_from_server() instead. It will at the very least save some
        # processing steps.
        state_events, _ = await self.get_state_from_server(
            origin_server,
            destination_server,
            room_id,
            event_id_in_timeline,
        )
        fed_handler_logger.debug("get_hosts_in_room_ordered: got %d events from state", len(state_events))
        converted_state_events = []
        for state_event in state_events:
            # Won't be able to retrieve the room_version for this, and the event ids are not available or necessary
            converted_state_events.append(
                determine_what_kind_of_event(None, room_version=None, data_to_use=state_event)
            )

        filtered_room_member_events = cast(
            List[RoomMemberStateEvent],
            filter_events_based_on_type(converted_state_events, "m.room.member"),
        )
        joined_member_events = cast(
            List[RoomMemberStateEvent],
            filter_state_events_based_on_membership(filtered_room_member_events, "join"),
        )
        fed_handler_logger.debug("get_hosts_in_room_ordered: got %d joined member events", len(joined_member_events))
        joined_member_events.sort(key=lambda x: x.depth)
        hosts_ordered = []
        for member in joined_member_events:
            host = get_domain_from_id(member.state_key)
            if host not in hosts_ordered:
                hosts_ordered.extend([host])

        fed_handler_logger.debug("get_hosts_in_room_ordered: got %d hosts", len(hosts_ordered))
        return hosts_ordered

    async def get_event_auth(
        self, origin_server: str, destination_server: str, room_id: str, event_id: str, **kwargs
    ) -> List[EventBase]:
        response = await self.api.get_event_auth(
            origin_server,
            destination_server,
            room_id,
            event_id,
        )
        room_version = await self.discover_room_version(origin_server, destination_server, room_id)
        list_from_response = response.json_response.get("auth_chain", [])
        list_of_event_bases = parse_list_response_into_list_of_event_bases(list_from_response, room_version)
        return list_of_event_bases


def filter_events_based_on_type(events: List[EventBase], filter_by: str) -> List[EventBase]:
    events_to_return = []
    for event in events:
        if event.event_type == filter_by:
            events_to_return.append(event)
    return events_to_return


def filter_state_events_based_on_membership(
    events: List[RoomMemberStateEvent], filter_by: str
) -> List[RoomMemberStateEvent]:
    events_to_return = []
    for event in events:
        if event.membership == filter_by:
            events_to_return.append(event)
    return events_to_return


def parse_list_response_into_list_of_event_bases(
    list_from_response: List[Dict[str, Any]], room_version: Optional[str] = None
) -> List[EventBase]:
    """
    Parse a list returned from a federation request into a list of EventBase type
    objects. Best used when we don't have any event id's to add to the new EventBase.

    Returns: list of processed Event type objects, in the order they were received

    """
    list_of_event_bases = []
    for event_dict in list_from_response:
        list_of_event_bases.append(
            determine_what_kind_of_event(event_id=None, room_version=room_version, data_to_use=event_dict)
        )

    return list_of_event_bases
