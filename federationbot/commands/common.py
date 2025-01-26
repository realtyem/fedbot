"""
Common base classes and type definitions for FederationBot commands.

This module provides the base classes and type protocols that are used by the various
command mixins in FederationBot. It handles configuration management and provides
shared functionality for command handling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from mautrix.types import EventID, RoomID, TextMessageEventContent
    from mautrix.util.config import ConfigUpdateHelper
    from typing_extensions import Protocol

    class MessageEvent(Protocol):
        """
        Type protocol for maubot's MessageEvent class.

        This protocol defines the expected interface for message events that commands
        will receive and interact with.
        """

        async def respond(
            self,
            content: str | TextMessageEventContent,
            edits: EventID | str | None = None,
            allow_html: bool = False,
        ) -> EventID | None:
            """Send a response message to the room where this event was received."""
            ...

        async def mark_read(self) -> None:
            """Mark this event as read."""
            ...

        async def reply(self, content: str | TextMessageEventContent, allow_html: bool = False) -> EventID | None:
            """Send a reply to this event."""
            ...

        room_id: RoomID
        event_id: EventID
        sender: str
        client: ClientSession


from time import time

from maubot.handlers import command
from maubot.plugin_base import Plugin
from mautrix.types import EventType
from mautrix.util.config import BaseProxyConfig

from federationbot.constants import HTTP_STATUS_OK
from federationbot.controllers import ReactionTaskController
from federationbot.errors import FedBotException, MalformedRoomAliasError
from federationbot.federation import FederationHandler


class MaubotConfig(BaseProxyConfig):
    """Configuration for the Maubot plugin."""

    @staticmethod
    def do_update(helper: ConfigUpdateHelper) -> None:
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

    async def pre_stop(self) -> None:
        """Stop the plugin and clean up resources."""
        self.client.remove_event_handler(EventType.REACTION, self.reaction_task_controller.react_control_handler)
        await self.reaction_task_controller.shutdown()
        # To stop any caching cleanup tasks
        await self.federation_handler.stop()

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
        room_id_or_alias: str | None,
        command_event: MessageEvent,
        origin_server: str,
    ) -> tuple[str | None, list[str] | None]:
        """
        Resolve a room ID or alias to a room ID and list of servers.

        If this is a room id, just return that and no list of server.
        If it was nothing, return the room id of the room the command was issued from.
        Depending on errors, let the user know there was a problem.

        Args:
            room_id_or_alias: The room ID or alias to resolve
            command_event: The event that triggered the command
            origin_server: The server the command was issued from

        Returns:
            A tuple containing the room ID and a list of servers to join through
        """
        list_of_servers = None
        if room_id_or_alias:
            # Sort out if the room id or alias passed in is valid and resolve the alias
            # to the room id if it is.
            if room_id_or_alias.startswith("#"):
                # look up the room alias. The server is extracted from the alias itself.
                try:
                    (
                        room_id,
                        list_of_servers,
                    ) = await self.federation_handler.resolve_room_alias(
                        origin_server=origin_server,
                        room_alias=room_id_or_alias,
                    )
                except MalformedRoomAliasError as e:
                    message_id = cast(
                        "EventID",
                        await command_event.reply(f"{e.summary_exception}: '{room_id_or_alias}'"),
                    )
                    await self.reaction_task_controller.add_cleanup_control(message_id, command_event.room_id)
                    return None, None
                except FedBotException as e:
                    message_id = cast(
                        "EventID",
                        await command_event.reply(
                            "Received an error while querying for room alias:\n\n"
                            f"{e.summary_exception}: '{room_id_or_alias}'",
                        ),
                    )
                    await self.reaction_task_controller.add_cleanup_control(message_id, command_event.room_id)
                    return None, None

            else:
                room_id = room_id_or_alias

        else:
            # When not supplied a room id, we assume they want the room the command was
            # issued from.
            room_id = str(command_event.room_id)

        return room_id, list_of_servers
