"""Utility functions for commands."""

from enum import Enum


class CommandType(Enum):
    """The type of command."""

    avoid_excess = "avoid_excess"
    all = "all"
    count = "count"


def is_command_type(maybe_subcommand: str) -> str | None:
    """
    Check if the given string is a valid command type.

    Args:
        maybe_subcommand: The string to check.

    Returns:
        The command type if valid, otherwise None.
    """
    if maybe_subcommand in CommandType:
        return maybe_subcommand

    return None
