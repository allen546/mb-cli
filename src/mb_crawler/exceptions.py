"""Exception types for mb-crawler."""


class CommandError(Exception):
    """Raised when a CLI/MCP command fails in a predictable way.

    Attributes:
        code: Machine-readable error code (e.g. ``"missing_credentials"``).
        message: Human-readable description.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)
