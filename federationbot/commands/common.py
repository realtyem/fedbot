"""
Common base classes and type definitions for FederationBot commands.

This module provides the base classes and type protocols that are used by the various
command mixins in FederationBot. It handles configuration management and provides
shared functionality for command handling.
"""

from __future__ import annotations

from time import time
import asyncio

from maubot.plugin_base import Plugin
from mautrix.api import Method
from mautrix.errors import MForbidden, MUnknown
from mautrix.types import EventType, Membership, RoomAlias as MautrixRoomAlias, RoomID
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from federationbot.constants import HTTP_STATUS_OK, NOT_IN_ROOM_ERROR, NOT_IN_ROOM_TRYING_FALLBACK
from federationbot.controllers import ReactionTaskController
from federationbot.errors import FedBotException
from federationbot.events import EventError
from federationbot.federation import FederationHandler
from federationbot.protocols import MessageEvent
from federationbot.responses import MatrixError
from federationbot.types import RoomConfigData
from federationbot.utils.matrix import get_domain_from_id, is_room_alias, is_room_id


class MaubotConfig(BaseProxyConfig):
    """Configuration for the Maubot plugin."""

    def do_update(self, helper: ConfigUpdateHelper) -> None:
        """Update the configuration."""
        helper.copy("whitelist")
        helper.copy("server_signing_keys")
        helper.copy("thread_pool_max_workers")


class FederationBotCommandBase(Plugin):
    """
    Base class for all FederationBot commands.

    Provides shared functionality and state management for command mixins.
    """

    reaction_task_controller: ReactionTaskController
    cached_servers: dict[str, str]
    server_signing_keys: dict[str, str]
    federation_handler: FederationHandler
    command_conn_timeouts: dict[str, int]
    # experimental_resolver: ServerDiscoveryResolver

    @classmethod
    def get_config_class(cls) -> type[BaseProxyConfig] | None:
        """
        Get the config class for the plugin.

        Returns:
            The config class for the plugin.
        """
        return MaubotConfig

    async def start(self) -> None:
        """Start the plugin and initialize controllers and handlers."""
        await super().start()
        self.server_signing_keys = {}
        # Set the default, in case the config file got lost somehow
        max_workers: int = 10
        self.command_conn_timeouts = {}

        loop = asyncio.get_running_loop()
        loop.set_debug(True)
        if self.config:
            self.config.load_and_update()
            max_workers = self.config["thread_pool_max_workers"]
            for server, key_data in self.config["server_signing_keys"].items():
                self.server_signing_keys[server] = key_data
            for _command, _timeout in self.config["command_connection_timeouts"].items():
                self.command_conn_timeouts[_command] = _timeout

        self.reaction_task_controller = ReactionTaskController(self.client, max_workers)

        self.client.add_event_handler(EventType.REACTION, self.reaction_task_controller.react_control_handler, True)

        self.federation_handler = FederationHandler(
            bot_mxid=self.client.mxid,
            server_signing_keys=self.server_signing_keys,
            task_controller=self.reaction_task_controller,
        )

        # self.experimental_resolver = ServerDiscoveryResolver()

    async def pre_stop(self) -> None:
        """Stop the plugin and clean up resources."""
        self.client.remove_event_handler(EventType.REACTION, self.reaction_task_controller.react_control_handler)
        await self.reaction_task_controller.shutdown()
        # To stop any caching cleanup tasks
        await self.federation_handler.stop()
        # await self.experimental_resolver.http_client.close()

        loop = asyncio.get_running_loop()
        loop.set_debug(False)

    async def get_room_depth(
        self,
        origin_server: str,
        room_to_check: str,
        command_event: MessageEvent,
    ) -> int | None:
        """
        Get the current depth of a room from its latest event.

        Args:
            origin_server: Server to query
            room_to_check: Room ID to check
            command_event: Original command event for error responses

        Returns:
            int | None: Room depth if successful, None if failed

        Raises:
            FedBotException: If there is an error getting the room depth
        """
        now = int(time() * 1000)
        ts_response = await self.federation_handler.api.get_timestamp_to_event(
            origin_server=origin_server,
            destination_server=origin_server,
            room_id=room_to_check,
            utc_time_at_ms=now,
        )

        if ts_response.http_code != HTTP_STATUS_OK:
            await command_event.respond(
                "Something went wrong while getting last event in room("
                f"{ts_response.reason}"
                "). Please supply an event_id instead at the place in time of query",
            )
            return None

        event_id = ts_response.json_response.get("event_id")
        if not isinstance(event_id, str):
            msg = "Event ID is not a string"
            raise FedBotException(msg)

        event_result = await self.federation_handler.get_event_from_server(
            origin_server,
            origin_server,
            event_id,
        )
        event = event_result.get(event_id)
        if event is None:
            msg = "Event could not be retrieved from server"
            raise FedBotException(msg)

        return event.depth

    async def resolve_room_id_or_alias(
        self,
        room_id_or_alias: str,
        command_event: MessageEvent,
        origin_server: str | None = None,
    ) -> tuple[str | None, list[str]]:
        """
        Resolve a room ID or alias to a room ID and list of servers.

        If this is a room id, just return that and no list of server.
        Depending on errors, let the user know there was a problem.

        Allow that the room id of the room the command was issued from be used
        if none other was passed in.

        Args:
            room_id_or_alias: The room ID or alias to resolve
            command_event: The event that triggered the command
            origin_server: The server the command was issued from

        Returns:
            A tuple containing the room ID and a list of servers to join through if it was an alias
        """
        list_of_servers: list[str] = []

        # Sort out if the room id or alias passed in is valid and resolve the alias
        # to the room id if it is.
        _room_id = is_room_id(room_id_or_alias)
        if _room_id:
            return _room_id, list_of_servers

        _room_alias = is_room_alias(room_id_or_alias)
        if not _room_alias:
            await self.log_to_client(command_event, f"{room_id_or_alias} does not seem to be a room alias or room id")

            return None, list_of_servers

        try:
            room_alias_info = await self.client.resolve_room_alias(MautrixRoomAlias(_room_alias))
        except MUnknown as e2:
            await self.log_to_client(
                command_event,
                f"Received an error while querying for room alias:\n\n{e2.errcode}: {e2.message}",
            )
            return None, []

        return str(room_alias_info.room_id), room_alias_info.servers

    async def get_room_data(
        self,
        origin_server: str,
        destination_server: str,
        command_event: MessageEvent,
        room_id_or_alias: str | None = None,
        get_servers_in_room: bool = False,
        use_origin_room_as_fallback: bool = False,
    ) -> RoomConfigData | None:
        """
        Retrieve data about a room. If there was an alias, resolve it. Get the list of hosts in that room

        If no room id/alias was supplied, use the room the command came from

        Args:
            origin_server: Used to determine how/if to auth request
            destination_server: target server name. Can be the same as origin
            command_event: The command from maubot that originated this request
            room_id_or_alias:
            get_servers_in_room: if it is required to get a list of servers in the room currently
            use_origin_room_as_fallback: if nothing else, use the room id from the command_event

        Returns: RoomConfigData, or None if needs to error out(a message will have been supplied to the client window)

        """
        # check for a room_id
        # resolve any alias
        # if there was neither, use origin_event
        # get list of servers from room
        start_time = time()
        room_id = None
        if room_id_or_alias:
            if is_room_id(room_id_or_alias):
                room_id = room_id_or_alias
            elif is_room_alias(room_id_or_alias):
                # resolve alias, do we want the hosts?
                room_id, _ = await self.resolve_room_id_or_alias(room_id_or_alias, command_event, origin_server)
        if not room_id:
            if use_origin_room_as_fallback:
                if room_id_or_alias:
                    # Tried everything we could find to resolve the room, give up
                    return None
                room_id = command_event.room_id
            else:
                await command_event.respond(
                    "Was not able to resolve the room alias provided",
                )
                return None

        list_of_servers: list[str] | None = None
        # it may be we can side-step getting the room version if we already have the make_join response
        join_response = None
        newest_event_id = None
        timestamp = 0
        use_s2s_api = False
        if get_servers_in_room:
            # Try and use the client API to get the list of servers in the room, it is usually faster. However, if
            # the bot is not in whatever room it is, but the server is, then fallback to using federation state data
            list_of_servers = await self.get_current_hosts_in_room_client(command_event, room_id)
            if list_of_servers is None:
                use_s2s_api = True

        if use_s2s_api:
            # Let the audience know this might take a bit longer
            await command_event.respond(NOT_IN_ROOM_TRYING_FALLBACK)

            # To get the state, we need an event_id. We already know we are not in the room, so use make_join
            join_response = await self.federation_handler.make_join_to_server(
                origin_server, destination_server, room_id, self.client.mxid
            )
            # itemize the join response to figure out which event_id is last
            for prev_event in join_response.prev_events:
                event_response = await self.federation_handler.get_event_from_server(
                    origin_server, destination_server, prev_event
                )
                event = event_response.get(prev_event)
                if isinstance(event, EventError):
                    await command_event.respond(
                        "I'm sorry, the server is not disclosing details about this room",
                    )
                    return None
                assert event is not None, "Event was None in get_room_data"
                if timestamp < event.origin_server_ts:
                    timestamp = event.origin_server_ts
                    newest_event_id = prev_event
            if newest_event_id is None:
                await command_event.respond(
                    "I'm sorry, the server is not disclosing details about this room",
                )
                return None
            if get_servers_in_room:
                list_of_servers = await self.federation_handler.get_hosts_in_room_ordered(
                    origin_server, destination_server, room_id, newest_event_id
                )
        else:
            try:
                ts_response = await self.federation_handler.get_last_event_id_in_room(
                    origin_server, destination_server, room_id
                )
                newest_event_id = ts_response.event_id
                timestamp = ts_response.origin_server_ts
            except MatrixError as e:
                await command_event.respond(
                    f"I'm sorry, the server is not disclosing details about this room\n\n{e.errcode}: {e.error}",
                )
                return None

        if join_response:
            room_version = join_response.room_version
        else:
            # If it was that the client api was used above, try it here. If that bot is in the room, this will be faster
            try:
                create_event: dict = await self.client.api.request(
                    Method.GET, f"/_matrix/client/v3/rooms/{room_id}/state/m.room.create"
                )
                room_version = create_event.get("content", {}).get("room_version", "1")
            except Exception:
                # Well the bot wasn't in the room, try the federation handler instead
                room_version = await self.federation_handler.discover_room_version(
                    origin_server, destination_server, room_id
                )

        if use_origin_room_as_fallback and not newest_event_id:
            # So if we were not able to get the last event in the room, then we check the fallback switch to see if
            # should just use the data about the room the command was sent from instead. It's possible this will never
            # be used, as I think it all gets taken care of above
            self.log.warning(
                "HIT 'use_origin_room_as_fallback' and not 'newest_event_id' condition. Room ID: %s", room_id
            )
            newest_event_id = str(command_event.event_id)
            newest_event_as_mapping = await self.federation_handler.get_event_from_server(
                origin_server, destination_server, newest_event_id
            )
            newest_event = newest_event_as_mapping.get(newest_event_id)
            if newest_event:
                timestamp = newest_event.origin_server_ts
        end_time = time()

        assert newest_event_id is not None, "newest_event_id was not at an inconvenient time"
        return RoomConfigData(
            room_id,
            room_version,
            list_of_servers,
            timestamp,
            newest_event_id,
            int(1000 * (end_time - start_time)),
        )

    async def get_current_hosts_in_room_client(self, command_event: MessageEvent, room_id: str) -> list[str] | None:
        servers: set[str] = set()
        try:
            joined_members = await self.client.get_joined_members(RoomID(room_id))

        except MForbidden:
            await command_event.respond(NOT_IN_ROOM_ERROR)
            return None

        for member, status in joined_members.items():
            # In case of members not being JOINed, just filter them out
            if status.membership == Membership.JOIN:
                servers.add(get_domain_from_id(member))

        return list(servers)

    async def get_origin_server_and_assert_key_exists(
        self, command_event: MessageEvent, origin_server: str | None = None
    ) -> str | None:
        """
        Define and check that the origin server to sign any requests is one that we have keys for. When not found,
        send a message into the relevant room that originated the command. We can not force an exit from here, so
        make sure to guard for this being None from the calling function/command.

        Args:
            command_event: The original event that triggered the bot. Used to send the error message into the room
            origin_server: Optionally an origin server to use. If None, then use the bot's own server

        Returns: None if there is no key to use for signing requests, otherwise the server name that a key exists for

        """
        # The only way to request from a different server than what the bot is on is to
        # have the other server's signing keys. So just use the bot's server.
        if not origin_server:
            _origin_server = get_domain_from_id(self.client.mxid)
        else:
            _origin_server = origin_server
        if _origin_server not in self.server_signing_keys:
            await command_event.respond(
                "This bot does not seem to have the necessary clearance to make "
                f"requests on the behalf of it's server({origin_server}). Please add "
                "server signing keys to it's config first.",
            )
            return None
        return _origin_server

    async def log_to_client(
        self, command_event: MessageEvent, message: str, reply: bool = True, add_cleanup: bool = True
    ) -> None:
        """
        Send a message back to the client window, usually for errors or responses.

        Args:
            command_event: The origin event that triggered the bot, useful interface to interact with the room
            message: The data to send back
            reply: True if the response needs to be a reply to the original event, or just posted as an m.notice
            add_cleanup: If the message should be tagged for easy cleanup

        Returns: None
        """
        if reply:
            message_id = await command_event.reply(message)
        else:
            message_id = await command_event.respond(message, None)
        if add_cleanup:
            await self.reaction_task_controller.add_cleanup_control(message_id, command_event.room_id)
