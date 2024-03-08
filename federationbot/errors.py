class MalformedServerNameError(Exception):
    """
    The server name had a scheme when it should not have(like 'https://' or 'http://')
    """

    pass  # pylint: disable=unnecessary-pass
