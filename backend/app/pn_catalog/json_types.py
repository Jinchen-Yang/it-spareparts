"""Explicit recursive JSON schema shared by input/output and generated clients."""
from typing_extensions import TypeAliasType

# The built-in pydantic.JsonValue has an unconstrained serialization schema.
JsonValue = TypeAliasType(
    "JsonValue",
    dict[str, "JsonValue"] | list["JsonValue"] | str | int | float | bool | None,
)
