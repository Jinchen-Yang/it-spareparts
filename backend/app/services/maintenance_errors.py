"""Shared maintenance command errors, independent of workbook implementations.

The historical WorkbookError name and payload are retained for adapters that
already translate this exception. Importing the contract must not load Excel
parsing, rendering, or application services.
"""


class WorkbookError(ValueError):
    """回传给 API 层映射 422 的用户可读校验错误。"""

    def __init__(
        self, code: str, message: str, issues: list[dict] | None = None,
        *, details: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.issues = issues or []
        self.details = details or {}
