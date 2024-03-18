class MalformedServerNameError(Exception):
    """
    The server name had a scheme when it should not have(like 'https://' or 'http://')
    """

    pass


class ServerUnavailable(Exception):
    """
    When the server was offline last time we checked, and we aren't trying them again
    for a while.
    """

    pass
