"""
Matrix-specific utility functions.
"""


def get_domain_from_id(string: str) -> str:
    """
    Extract domain portion from a Matrix ID.

    Example:
        "@user:example.com" -> "example.com"

    Returns:
        Domain portion of the Matrix ID
    """
    return string.split(":", 1)[1]
