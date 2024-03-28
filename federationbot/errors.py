class MalformedServerNameError(Exception):
    """
    The server name had a scheme when it should not have(like 'https://' or 'http://')
    """


class ServerUnreachable(Exception):
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


class MatrixError(Exception):
    """
    Generic Matrix-related error
    """

    http_code: int

    def __init__(self, http_code: int, reason: str) -> None:
        super().__init__(reason)
        self.http_code = http_code


class MatrixNotFoundError(MatrixError):
    """
    The homeserver returned a 404 M_NOT_FOUND
    """


class MatrixForbiddenError(MatrixError):
    """
    The homeserver returned a 403 M_FORBIDDEN
    """
