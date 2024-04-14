from typing import Optional


class FedBotException(Exception):
    """
    Base class of all Exceptions in FedBot. Not Matrix related errors

    Attributes:
        summary_exception: A simple short explanation, usually whatever raised the original exception
    """

    summary_exception: str
    long_exception: Optional[str]

    def __init__(
        self, summary_exception: str, long_exception: Optional[str] = None
    ) -> None:
        super().__init__(summary_exception, long_exception)
        self.summary_exception = summary_exception
        self.long_exception = long_exception


class PluginTimeout(FedBotException):
    """
    A more specific type of asyncio.Timeout
    """


class BotConnectionError(FedBotException):
    """
    An error occurred while connecting
    """


class ServerSSLException(BotConnectionError):
    """
    The server has some kind of SSL error
    """


class MalformedServerNameError(Exception):
    """
    The server name had a scheme when it should not have(like 'https://' or 'http://')
    """


class ServerUnreachable(FedBotException):
    """
    When the server was offline last time we checked, and we aren't trying them again
    for a while.
    """


class ServerDiscoveryError(Exception):
    """
    Error occurred during server discovery process
    """


class WellKnownError(ServerDiscoveryError):
    """
    Error occurred during the well-known phase of server discovery
    """


class WellKnownHasSchemeError(WellKnownError):
    """
    The Host found in well-known has a scheme when it should not
    """


class WellKnownParsingError(WellKnownError):
    """
    Error occurred while parsing the well-known response
    """


class MessageAlreadyHasReactions(Exception):
    """
    The Message given already has Reactions attached
    """


class MessageNotWatched(Exception):
    """
    The Message given is not being watched
    """


class ReferenceKeyAlreadyExists(Exception):
    """
    The Reference Key given already exists
    """


class ReferenceKeyNotFound(Exception):
    """
    The Reference Key was not found
    """


class EventKeyMissing(Exception):
    """
    The key needed from an Event was missing
    """
