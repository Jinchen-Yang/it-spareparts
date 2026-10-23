"""Stable, non-cacheable errors shared by authorization adapters."""
from fastapi import HTTPException


def authz_error(status_code: int, code: str, message: str, *, conflicts=(), field_errors=()) -> HTTPException:
    return HTTPException(status_code, {
        "code": code, "message": message, "committed": False,
        "conflicts": list(conflicts), "field_errors": list(field_errors),
    }, headers={"Cache-Control": "no-store"})
