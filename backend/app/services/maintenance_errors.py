"""Shared maintenance command errors, independent of workbook implementations.

The historical WorkbookError name and payload are retained for adapters that
already translate this exception. Importing the contract must not load Excel
parsing, rendering, or application services.
"""


class WorkbookError(ValueError):
    """User-readable validation error translated by the calling API adapter."""

    def __init__(
        self, code: str, message: str, issues: list[dict] | None = None,
        *, details: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.issues = issues or []
        self.details = details or {}
