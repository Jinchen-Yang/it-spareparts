"""D-0022 第 2 轮返工：回执恢复快照的完整 schema 校验层。

快照里的单头/需求行/领用行/领用单头/挂靠/墓碑 before、after 必须是与
ORM 表列一一对应的完整 dump：由表列内省生成 pydantic 模型，缺列、多列、
类型不符一律按「快照损坏」处理；再叠加语义不变量（成员 id 一致、is_active
转换方向、before 无作废痕迹、after.voided_at 与确认时间对齐、void 形状、
状态字段满足数据库 CHECK 约束、数量与确认结果对齐）。

清单 / 预览 / 执行三个入口都先整体走这一层，坏单得到可读的中文原因，
绝不按缺省成员执行，也不等写库才在约束上炸 500。
"""
import warnings
from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    create_model,
)
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum as SaEnum,
    Integer,
    Numeric,
    String,
    Text,
)

from app.models.maintenance import (
    FMaintenanceLine,
    FMaintenanceOrder,
    MaintenanceDemandTombstone,
)
from app.models.maintenance_project_operations import (
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.models.maintenance_source_assignment import (
    MaintenanceSourceOrderAssignment,
)
from app.services.maintenance_source_cancel_cascade import RESTORATION_SCHEMA

# 与库表 CHECK 约束（maintenance_project_operations.MaintenanceSiteIssue）同口径。
ISSUE_NORMALIZED_STATUSES = {"draft", "confirmed", "corrected", "void", "unknown"}
ISSUE_MAPPING_STATES = {"mapped", "unmapped"}


def _dump_annotation(column) -> Any:
    """把表列映射成快照 dump 值的类型：_entity/_json 已把 Decimal、日期时间字符串化。"""
    kind = column.type
    if isinstance(kind, Boolean):
        base: Any = StrictBool
    elif isinstance(kind, (Integer, BigInteger)):
        base = StrictInt
    elif isinstance(kind, (String, Text, SaEnum, DateTime, Date, Numeric)):
        base = StrictStr
    else:  # JSONB / ARRAY 等容器列：内容随业务演化，只要求能往返 JSON。
        base = Any
    return base | None if column.nullable else base


def _dump_model(orm_class):
    """按表列内省生成 dump 模型；extra=forbid 让多余字段也过不去。"""
    fields = {column.name: (_dump_annotation(column), ...)
              for column in orm_class.__table__.columns}
    return create_model(f"{orm_class.__name__}Dump",
                        __config__=ConfigDict(extra="forbid"), **fields)


OrderDump = _dump_model(FMaintenanceOrder)
DemandLineDump = _dump_model(FMaintenanceLine)
SiteIssueDump = _dump_model(MaintenanceSiteIssue)
SiteIssueLineDump = _dump_model(MaintenanceSiteIssueLine)
TombstoneDump = _dump_model(MaintenanceDemandTombstone)
AssignmentDump = _dump_model(MaintenanceSourceOrderAssignment)


class DemandLineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictInt
    raw_line_id: StrictStr | None
    before: DemandLineDump
    after: DemandLineDump


class SiteLineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_line_id: StrictStr
    issue_id: StrictStr
    before: SiteIssueLineDump
    after: SiteIssueLineDump


class SiteIssueEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_id: StrictStr
    voided: StrictBool
    before: SiteIssueDump
    after: SiteIssueDump


with warnings.catch_warnings():
    # 快照段名就叫 schema（与确认侧 RESTORATION_SCHEMA 常量对应）；
    # 遮蔽 BaseModel.schema 只是同名，不是调用旧类方法。
    warnings.simplefilter("ignore", UserWarning)

    class RestorationSnapshot(BaseModel):
        model_config = ConfigDict(extra="forbid")
        schema: Literal[RESTORATION_SCHEMA]
        order: OrderDump
        demand_lines: list[DemandLineEntry]
        site_lines: list[SiteLineEntry]
        site_issues: list[SiteIssueEntry]
        tombstone: TombstoneDump | None
        assignments: list[AssignmentDump]


# ---- 可读报错 -----------------------------------------------------------------

_CN_ERROR_KINDS = {
    "missing": "缺少字段",
    "extra_forbidden": "不允许的多余字段",
}


def _location(loc) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f"{'' if not out else '.'}{part}"
    return out


def _validation_problem(error: ValidationError) -> str:
    first = error.errors()[0]
    kind = first["type"].rsplit(".", 1)[0]
    detail = _CN_ERROR_KINDS.get(kind, f"取值不合法（{first['msg']}）")
    return f"回执恢复快照的 {_location(first['loc'])} {detail}"


# ---- 语义不变量（跨字段/跨段落，schema 表达不了的部分） ------------------------

def _valid_time(value):
    """快照时间戳必须是 null 或 ISO-8601 文本；恢复侧 _time 不允许再抛错。"""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False


def _time(value):
    return None if value is None else datetime.fromisoformat(str(value))


def issue_status_problem(label: str, dump: dict):
    """四个状态字段按库表约束校验（NOT NULL、枚举 CHECK、映射组合 CHECK）。"""
    raw = dump.get("raw_status")
    if not isinstance(raw, str) or not raw.strip():
        return f"回执恢复快照的{label} raw_status 不能为空"
    version = dump.get("status_mapping_version")
    if not isinstance(version, str) or not version.strip():
        return f"回执恢复快照的{label} status_mapping_version 不能为空"
    normalized = dump.get("normalized_status")
    if normalized not in ISSUE_NORMALIZED_STATUSES:
        return f"回执恢复快照的{label} normalized_status 取值 {normalized!r} 不在数据库约束内"
    state = dump.get("status_mapping_state")
    if state not in ISSUE_MAPPING_STATES:
        return f"回执恢复快照的{label} status_mapping_state 取值 {state!r} 不在数据库约束内"
    if (state == "mapped") != (normalized != "unknown"):
        return (f"回执恢复快照的{label} 映射组合不合法"
                f"（status_mapping_state={state!r} 与 normalized_status={normalized!r}）")
    return None


# before/after 语义对齐时参与比对的键：领用单头恢复时按 before 原样写回。
ISSUE_STATE_KEYS = ("normalized_status", "raw_status", "status_mapping_state",
                    "status_mapping_version", "voided_at")


def semantic_problem(snapshot: RestorationSnapshot, *, confirm_result, project_id):
    """schema 之后的跨字段校验；返回 None 才允许据此驱动一次精确恢复。"""
    order_id = snapshot.order.id
    confirmed_at = confirm_result.get("confirmed_at")
    confirm_time = (_time(confirmed_at)
                    if confirmed_at is not None and _valid_time(confirmed_at) else None)

    demand_ids = set()
    for index, row in enumerate(snapshot.demand_lines):
        where = f"demand_lines[{index}]"
        if row.id in demand_ids:
            return f"回执恢复快照的{where} 需求行重复"
        demand_ids.add(row.id)
        before, after = row.before.model_dump(), row.after.model_dump()
        # 确认只快照活跃行，随后统一置为作废；before/after 必须证明是这一代作废，
        # 否则「条数不变、成员换成别的行」会伪装成可恢复。
        if before.get("id") != row.id or after.get("id") != row.id:
            return f"回执恢复快照的{where} id 与快照不一致"
        if before.get("is_active") is not True or after.get("is_active") is not False:
            return f"回执恢复快照的{where} before/after 活跃状态不合法"
        if not _valid_time(before.get("voided_at")) or not _valid_time(after.get("voided_at")):
            return f"回执恢复快照的{where} 作废时间不是合法的 ISO-8601 文本"
        if any(before.get(key) is not None for key in ("voided_at", "voided_by", "void_reason")):
            return f"回执恢复快照的{where} before 带有作废痕迹"
        if before.get("order_id") != order_id or after.get("order_id") != order_id:
            return f"回执恢复快照的{where} 不属于来源单头"
        if row.raw_line_id != after.get("raw_line_id"):
            return f"回执恢复快照的{where} 与来源明细不一致"
        if confirm_time is None or _time(after["voided_at"]) != confirm_time:
            return f"回执恢复快照的{where} 作废时间与确认回执不一致"

    issue_ids = set()
    for index, row in enumerate(snapshot.site_issues):
        where = f"site_issues[{index}]"
        if row.issue_id in issue_ids:
            return f"回执恢复快照的{where} 领用单头重复"
        issue_ids.add(row.issue_id)
        before, after = row.before.model_dump(), row.after.model_dump()
        if before.get("issue_id") != row.issue_id or after.get("issue_id") != row.issue_id:
            return f"回执恢复快照的{where} id 与快照不一致"
        if before.get("project_id") != project_id or after.get("project_id") != project_id:
            return f"回执恢复快照的{where} 不属于本项目"
        # 状态字段先过库表约束（before/after 恢复时都会被读或写回）。
        for phase, dump in (("before", before), ("after", after)):
            problem = issue_status_problem(f"领用单头 {where} {phase}", dump)
            if problem is not None:
                return problem
        if row.voided:
            if before.get("normalized_status") == "void":
                return f"回执恢复快照的{where} before 已是作废状态"
            if after.get("normalized_status") != "void" or after.get("raw_status") != "void" \
                    or after.get("status_mapping_state") != "mapped" \
                    or after.get("status_mapping_version") != "site-issue-v2-workflow-v1":
                return f"回执恢复快照的{where} after 不是确认作废状态"
            if not _valid_time(before.get("voided_at")) or not _valid_time(after.get("voided_at")):
                return f"回执恢复快照的{where} 作废时间不是合法的 ISO-8601 文本"
            if confirm_time is None or _time(after["voided_at"]) != confirm_time:
                return f"回执恢复快照的{where} 作废时间与确认回执不一致"
        elif any(after.get(key) != before.get(key) for key in ISSUE_STATE_KEYS):
            return f"回执恢复快照的{where} 未作废领用单头 before/after 不一致"

    site_line_ids = set()
    for index, row in enumerate(snapshot.site_lines):
        where = f"site_lines[{index}]"
        if row.issue_line_id in site_line_ids:
            return f"回执恢复快照的{where} 领用行重复"
        site_line_ids.add(row.issue_line_id)
        before, after = row.before.model_dump(), row.after.model_dump()
        if row.issue_id not in issue_ids:
            return f"回执恢复快照的{where} 不属于任何记录的领用单头"
        if before.get("issue_line_id") != row.issue_line_id \
                or after.get("issue_line_id") != row.issue_line_id \
                or before.get("issue_id") != row.issue_id \
                or after.get("issue_id") != row.issue_id:
            return f"回执恢复快照的{where} id 与快照不一致"
        if before.get("is_active") is not True or after.get("is_active") is not False:
            return f"回执恢复快照的{where} before/after 活跃状态不合法"

    count_pairs = (
        ("需求行", len(snapshot.demand_lines), confirm_result.get("voided_lines")),
        ("领用行", len(snapshot.site_lines), confirm_result.get("voided_site_lines")),
        ("领用单头", sum(1 for row in snapshot.site_issues if row.voided),
         confirm_result.get("voided_site_issues")),
    )
    for label, actual, recorded in count_pairs:
        if not isinstance(recorded, int) or isinstance(recorded, bool):
            return "确认回执缺少作废数量，无法核对快照完整性"
        if actual != recorded:
            return f"回执恢复快照的{label}数量（{actual}）与确认回执（{recorded}）不一致"
    return None


def snapshot_problem(restoration, *, confirm_result, project_id):
    """整体校验一份回执恢复快照；None 表示结构、取值、一致性全部可信。"""
    if not isinstance(restoration, dict):
        return "回执恢复快照不是对象"
    if restoration.get("schema") != RESTORATION_SCHEMA:
        return "回执恢复快照版本不受支持"
    try:
        snapshot = RestorationSnapshot.model_validate(restoration)
    except ValidationError as error:
        return _validation_problem(error)
    return semantic_problem(snapshot, confirm_result=confirm_result, project_id=project_id)
