"""Stable domain failures; HTTP transport mapping belongs to the host."""


class CatalogError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 409,
                 details: dict | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        self.retryable = retryable
