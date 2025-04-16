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
from mautrix.types import EventType
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from federationbot import get_domain_from_id
from federationbot.constants import HTTP_STATUS_OK
from federationbot.controllers import ReactionTaskController
from federationbot.errors import FedBotException, MalformedRoomAliasError
from federationbot.federation import FederationHandler
from federationbot.protocols import MessageEvent


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
            message_id = await command_event.reply(
                f"{room_id_or_alias} does not seem to be a room alias or room id",
            )

            await self.reaction_task_controller.add_cleanup_control(message_id, command_event.room_id)

            return None, list_of_servers
        room_alias = RoomAlias(_room_alias)

        # look up the room alias. The server is extracted from the alias itself.
        try:
            (
                room_id,
                list_of_servers,
            ) = await self.federation_handler.resolve_room_alias(
                room_alias,
                origin_server or room_alias.origin_server,
            )

        except FedBotException as e:
            message_id = await command_event.reply(
                "Received an error while querying for room alias:\n\n"
                f"{e.summary_exception}: '{_room_alias}'\nTrying fallback method",
            )
            await self.reaction_task_controller.add_cleanup_control(message_id, command_event.room_id)
            room_alias_info = await self.client.resolve_room_alias(mautrix.types.RoomAlias(_room_alias))
            return str(room_alias_info.room_id), room_alias_info.servers

        return room_id, list_of_servers

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
