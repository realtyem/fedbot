"""
Custom exceptions for Matrix federation bot error handling.

This module defines the exception hierarchy used throughout the federation bot:

Federation Errors:
- ServerDiscoveryError: Problems finding/connecting to Matrix servers
- WellKnownError: Issues with .well-known federation discovery
- WellKnownSchemeError: Invalid server name formatting
- ServerSSLException: SSL/TLS connection failures
- ServerUnreachable: Server offline or unreachable

Message/Event Errors:
- MalformedRoomAliasError: Invalid room alias format
- MessageAlreadyHasReactions: Duplicate reaction handling
- MessageNotWatched: Missing reaction tracking
- EventKeyMissing: Required event field not found

Bot Operation Errors:
- PluginTimeout: Operation timeout
- BotConnectionError: General connection issues
- ReferenceKeyAlreadyExists/NotFound: Task tracking errors

All federation-specific errors inherit from FedBotException to allow consistent
error handling and reporting in the bot's federation inspection commands.
"""

from __future__ import annotations


# General or legacy exceptions
class FedBotException(Exception):
    """Base exception for federation-specific errors."""

    summary_exception: str
    long_exception: str

    def __init__(self, summary_exception: str, long_exception: str | None = None) -> None:
        """
        Initialize federation bot exception.

        Args:
            summary_exception: Brief error description
            long_exception: Optional detailed error message
        """
        super().__init__(summary_exception, long_exception)
        self.summary_exception = summary_exception
        self.long_exception = long_exception or ""


class PluginTimeout(FedBotException):
    """Specialized timeout for bot operations."""


class BotConnectionError(FedBotException):
    """Base error for connection failures."""


class ServerSSLException(BotConnectionError):
    """SSL/TLS connection error with Matrix server."""


class MalformedServerNameError(Exception):
    """Server name contains invalid scheme prefix, e.g. 'https://' or 'http://'."""


class MalformedRoomAliasError(FedBotException):
    """Room alias missing required '#' prefix or ':' domain separator."""


class ServerUnreachable(FedBotException):
    """Server was offline last time we checked, and temporarily blocked from retries."""


# Errors during the discovery process
class ServerDiscoveryError(Exception):
    """Error during Matrix server discovery process."""

    server_name: str
    reason: str | None

    def __init__(self, server_name: str, reason: str | None = None) -> None:
        self.server_name = server_name
        self.reason = reason


class ServerDiscoveryDNSError(ServerDiscoveryError):
    """Error during DNS query"""


class WellKnownServerError(ServerDiscoveryError):
    """Connection error from Matrix server to client."""


class WellKnownServerTimeout(ServerDiscoveryError):
    """Connection Timeout waiting for Matrix server response."""


class WellKnownClientError(ServerDiscoveryError):
    """Connection error from client to Matrix server."""


class WellKnownError(ServerDiscoveryError):
    """Unknown error during .well-known federation discovery."""


class WellKnownSchemeError(WellKnownError):
    """Invalid server name format (contains scheme)."""


class WellKnownParsingError(WellKnownError):
    """Error occurred while parsing the well-known response."""


# Internal Fedbot exceptions
class MessageAlreadyHasReactions(Exception):
    """The Message given already has Reactions attached."""


class MessageNotWatched(Exception):
    """The Message given is not being watched."""


class ReferenceKeyAlreadyExists(Exception):
    """The Reference Key given already exists."""


class ReferenceKeyNotFound(Exception):
    """The Reference Key was not found."""


class EventKeyMissing(Exception):
    """The key needed from an Event was missing."""
