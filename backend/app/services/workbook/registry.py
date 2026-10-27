"""工作簿业务适配注册表；权限和业务事务由显式回调提供。"""
from collections.abc import Callable
from dataclasses import dataclass

from app.services.workbook.schema import WorkbookError, WorkbookSchema


class NotFound(WorkbookError):
    status_code = 404


class Forbidden(WorkbookError):
    status_code = 403


class Conflict(WorkbookError):
    status_code = 409


class ApplyFailed(WorkbookError):
    status_code = 422


@dataclass(frozen=True)
class WorkbookSpec:
    schema: WorkbookSchema
    load_current: Callable
    apply_fn: Callable
    can_access: Callable


_specs: dict[str, WorkbookSpec] = {}


def register(name: str, spec: WorkbookSpec) -> None:
    if not name or len(name) > 64 or name != spec.schema.name:
        raise ValueError("注册名称必须与 schema 一致，且不超过 64 个字符")
    if name in _specs:
        raise ValueError(f"工作簿 schema 已注册: {name}")
    if not all(callable(fn) for fn in (spec.load_current, spec.apply_fn, spec.can_access)):
        raise ValueError("必须提供读取、应用和权限回调")
    _specs[name] = spec


def unregister(name: str) -> None:
    _specs.pop(name, None)


def get_spec(name: str) -> WorkbookSpec:
    try:
        return _specs[name]
    except KeyError:
        raise NotFound("未注册的工作簿模板") from None
