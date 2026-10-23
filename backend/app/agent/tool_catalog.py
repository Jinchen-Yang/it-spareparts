"""Compose explicit tool families without changing runtime dispatch.

A declaration registers its schema and handler together. No filesystem scanning,
import side effects across unknown plugins, or implicit permission grants.
"""
from collections.abc import Callable
from copy import deepcopy
import inspect
import re

from sqlalchemy.orm import Session

from app.security import UserContext

ToolHandler = Callable[[Session, dict, UserContext], dict]


class ToolCatalog:
    def __init__(self):
        self._entries: dict[str, tuple[dict, ToolHandler]] = {}

    def register(self, schema: dict):
        """Bind a synchronous handler after lightweight declaration checks.

        This checks the local registration contract, not all of JSON Schema.
        Runtime argument and permission validation remains the handler's job.
        """
        spec = deepcopy(schema)
        if not isinstance(spec, dict) or spec.get("type") != "function":
            raise ValueError("tool must have type=function")
        function = spec.get("function")
        if not isinstance(function, dict):
            raise ValueError("tool requires a function object")
        name = function.get("name")
        # Match the current OpenAI-compatible provider's function-name contract.
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) is None:
            raise ValueError("tool name must be 1-64 ASCII letters, digits, underscores or dashes")
        if not isinstance(function.get("description"), str):
            raise ValueError(f"tool {name} requires a description")
        parameters = function.get("parameters")
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise ValueError(f"tool {name} requires object parameters")
        properties = parameters.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError(f"tool {name} requires object properties")
        required = parameters.get("required", [])
        if (
            not isinstance(required, list)
            or not all(isinstance(key, str) for key in required)
            or len(set(required)) != len(required)
            or any(key not in properties for key in required)
        ):
            raise ValueError(f"tool {name} required must list unique declared property names")

        def bind(handler: ToolHandler) -> ToolHandler:
            if name in self._entries:
                raise ValueError(f"duplicate tool name: {name}")
            if not callable(handler):
                raise TypeError(f"tool {name} requires a callable handler")
            if any(
                inspect.iscoroutinefunction(candidate) or inspect.isasyncgenfunction(candidate)
                for candidate in (handler, getattr(handler, "__call__", None))
            ):
                raise TypeError(f"tool {name} requires a synchronous handler")
            self._entries[name] = (spec, handler)
            return handler

        return bind

    def include(self, other: "ToolCatalog") -> None:
        """Include a known family atomically; duplicate names fail at startup."""
        duplicates = self._entries.keys() & other._entries.keys()
        if duplicates:
            raise ValueError(f"duplicate tool names: {', '.join(sorted(duplicates))}")
        self._entries.update({
            name: (deepcopy(spec), handler)
            for name, (spec, handler) in other._entries.items()
        })

    def export(self) -> tuple[list[dict], dict[str, ToolHandler]]:
        """Export startup snapshots; later registration is not hot-loaded.

        Compose all explicit families before exporting to the runtime adapter.
        """
        return (
            [deepcopy(spec) for spec, _ in self._entries.values()],
            {name: handler for name, (_, handler) in self._entries.items()},
        )
