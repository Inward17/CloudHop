class RepoError(Exception):
    """A problem to show the user, with the HTTP status to answer with."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class AuthError(RepoError):
    pass
