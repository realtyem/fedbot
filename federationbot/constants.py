"""
Define constant values used throughout FederationBot.

These constants help maintain consistency and make configuration changes easier
by centralising common values.
"""

from typing import Final

# Standard HTTP success response code
HTTP_STATUS_OK: Final[int] = 200

# Maximum number of servers to attempt federation with in room-wide operations
MAX_NUMBER_OF_SERVERS_TO_ATTEMPT: Final[int] = 400

# Maximum number of concurrent federation requests to a single server
MAX_NUMBER_OF_CONCURRENT_TASKS: Final[int] = 10

# Maximum number of servers to make concurrent federation requests to
MAX_NUMBER_OF_SERVERS_FOR_CONCURRENT_REQUEST: Final[int] = 100

# Number of seconds to wait between progress message updates
SECONDS_BETWEEN_EDITS: Final[float] = 5.0

# Threshold in seconds below which backoff suggestions are ignored
SECONDS_BEFORE_IGNORE_BACKOFF: Final[float] = 1.0

# Multiplier applied to previous response time to calculate next backoff period
BACKOFF_MULTIPLIER: Final[float] = 0.5

# Column headers. Probably will remove these constants
SERVER_NAME: Final[str] = "Server Name"
SERVER_SOFTWARE: Final[str] = "Software"
SERVER_VERSION: Final[str] = "Version"
CODE: Final[str] = "Code"

# Error message displayed when bot cannot access a room it needs to operate on
NOT_IN_ROOM_ERROR: Final[str] = "Cannot process for a room I'm not in. Invite this bot to that room and try again."
NOT_IN_ROOM_TRYING_FALLBACK: Final[str] = "I do not seem to be in that room to access the data. Trying work-around."
