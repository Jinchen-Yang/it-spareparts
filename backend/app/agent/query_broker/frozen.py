"""Small recursively immutable containers for security-sensitive runtime values."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Any


class FrozenDict(Mapping[str, Any]):
    __slots__ = ("_data",)

    def __init__(self, source: Mapping[str, Any] | None = None):
        self._data = MappingProxyType({
            str(key): deep_freeze(value) for key, value in (source or {}).items()
        })

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return "FrozenDict(<redacted>)"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self.items()) == dict(other.items())
        return False


def deep_freeze(value: Any) -> Any:
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, Mapping):
        return FrozenDict(value)
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(deep_freeze(item) for item in value)
    return value


def deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [deep_thaw(item) for item in value]
    if isinstance(value, frozenset):
        return sorted(deep_thaw(item) for item in value)
    return value
