"""
Provide command implementations for FederationBot.

This module contains command mixins that implement FederationBot's functionality.
Each command is implemented as a mixin class that inherits from the base command
class in `common.py`, which provides shared configuration and utilities.

The architecture uses Python's multiple inheritance to compose the final bot. For
example, the RoomWalkCommand mixin is imported into the main bot class like:

    class FederationBot(RoomWalkCommand):
        pass

This automatically adds all the room walking commands to the bot instance.

Command Implementation Guidelines:
- Each command mixin should inherit from FederationBotCommandBase
- Command methods must use a unique prefix for all public/private methods
  to prevent naming collisions when mixed in
- For example, RoomWalkCommand uses 'room_walk_command' for its public method
  and '_room_walk_' prefix for all private methods

This modular approach allows commands to be easily added or removed from the
bot by adjusting the inheritance chain, while ensuring each command's methods
remain isolated through consistent naming conventions.
"""

from .common import FederationBotCommandBase

__all__ = ["FederationBotCommandBase"]
