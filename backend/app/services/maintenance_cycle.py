"""坏件循环池状态机（D-0057 T2）。

业务边界（任务书 §6.1–6.3、§6.5）：
- 一行一件：单位表没有 qty 列；出站可售意图 ``qty`` 恒为 1。
- 循环动作**不写** ``inventory`` / ``stock_dynamic`` / 采购 / 销售 / ``maintenance_bad_salvage``，
  不触发氚云回写。
- 去电商（``to_shop``）与可售意图 outbox **同一事务**入队；worker 由 T4 另写，未启用时保持
  ``pending``。意图载荷只有 part_id / condition_bucket / qty=1 / delivery_id。
- 收货行（返还收货）互锁：本模块只提供读取/计数/撤回原语，异常仍由
  ``maintenance_return_receipts`` 抛出，锁顺序固定为「收货行 → 循环单位」。
- 字段裁剪：允许工位由实名账号的五页键决定；工人序列化与事件 payload 走白名单，
  非主管看不到供应商 / SN / 来源收货单 / 备注 / 项目 / 成本 / 价格。
- 待办（``work_item`` subject、领取入口）在 T3 接线；本模块的 ``_cleared_assignee``
  只保证「检测转维修清空负责人、委外回来重新派」的工位语义。
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Iterable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from app import permissions
from app.models.dimensions import DimPart
from app.models.maintenance_cycle import (
    CONDITION_BUCKETS,
    STATIONS,
    MaintenanceCycleEvent,
    MaintenanceCycleSellIntent,
    MaintenanceCycleUnit,
)
from app.models.system import SysUser
from app.models.workflow import WorkItem
from app.security import UserContext
from app.services.workflow import work_items
from app.services.workflow.registry import (
    Subject,
    SubjectSpec,
    get_spec as get_subject_spec,
    register,
    registered,
)

# ---------------------------------------------------------------------------
# 错误类型：API 层映射 400 / 404 / 409
# ---------------------------------------------------------------------------


class CycleError(RuntimeError):
    """坏件循环领域错误基类。"""


class CycleNotFound(CycleError):
    """单位不存在或当前账号不可见。"""


class CycleConflict(CycleError):
    """版本冲突 / 件数超限 / 已生成意图等稳定业务冲突。"""


class CycleInvalid(CycleError):
    """非法转移、缺桶、缺供应商、数量非法等入参/状态错误。"""


# ---------------------------------------------------------------------------
# 常量与页面/工位映射
# ---------------------------------------------------------------------------

MAX_INTAKE_QTY = 1000
MAX_SERIAL_LENGTH = 128
MAX_NOTE_LENGTH = 2000
MAX_VENDOR_LENGTH = 128

RECYCLE_PAGE = "page_cycle_recycle"
TEST_PAGE = "page_cycle_test"
REPAIR_PAGE = "page_cycle_repair"
MANAGE_PAGE = "page_cycle_manage"
WIP_PAGE = "page_cycle_wip"

# 任务书 §6.3：页键 → 可见工位
PAGE_STATIONS: dict[str, frozenset[str]] = {
    RECYCLE_PAGE: frozenset({"pool", "outsource", "ready"}),
    TEST_PAGE: frozenset({"testing"}),
    REPAIR_PAGE: frozenset({"repairing"}),
    MANAGE_PAGE: frozenset(STATIONS),
}
READ_PAGE_KEYS: tuple[str, ...] = tuple(PAGE_STATIONS)
# 检测/维修工位需要按「负责人 / 可领取公共池」二次收敛（回收岗与主管不按负责人过滤）。
ASSIGNEE_SCOPED_STATIONS = frozenset({"testing", "repairing"})

WRITE_PAGE_KEYS: dict[str, tuple[str, ...]] = {
    "handoff_from_receipt": (RECYCLE_PAGE, MANAGE_PAGE),
    "intake_external": (RECYCLE_PAGE, MANAGE_PAGE),
    "dispatch": (RECYCLE_PAGE, MANAGE_PAGE),
    "self_pass": (RECYCLE_PAGE, MANAGE_PAGE),
    "withdraw": (RECYCLE_PAGE, MANAGE_PAGE),
    "close": (RECYCLE_PAGE, MANAGE_PAGE),
    "outsource_return": (RECYCLE_PAGE, MANAGE_PAGE),
    "complete_test": (TEST_PAGE, MANAGE_PAGE),
    "complete_repair": (REPAIR_PAGE, MANAGE_PAGE),
    "claim": (TEST_PAGE, REPAIR_PAGE, MANAGE_PAGE),
    "mark_sold": (MANAGE_PAGE,),
    "read": READ_PAGE_KEYS,
    "wip": (WIP_PAGE, MANAGE_PAGE),
}

# 非主管事件 payload 白名单（任务书 §6.3 / P-06）
WORKER_EVENT_KEYS = frozenset({"result", "next", "to", "outcome", "condition_bucket"})

# 工人序列化白名单（任务书 §6.3 / P-05）
WORKER_UNIT_FIELDS = (
    "id", "unit_uid", "part_id", "pn", "station", "outcome", "condition_bucket",
    "intake_condition", "repair_round", "assignee_username", "version",
    "created_at", "updated_at", "closed_at",
)
# 仅主管可见字段（P-07；仍不含成本/价格/token）
MANAGER_UNIT_FIELDS = (
    "source_kind", "source_receipt_id", "serial_number", "vendor_name", "note",
    "created_by", "updated_by", "withdrawn_at",
)

_TS_FIELDS = {"created_at", "updated_at", "closed_at", "withdrawn_at"}


# ---------------------------------------------------------------------------
# 权限解析：五页键是「实名账号快照 ⊕ 覆盖」显式门（T1 已纳入 ACCOUNT_SCOPED）
# ---------------------------------------------------------------------------


def _graph(ctx: UserContext) -> dict:
    if isinstance(ctx.permissions, dict):
        return ctx.permissions
    return permissions.effective(ctx.role, None)


def allowed_page_keys(ctx: UserContext) -> set[str]:
    graph = _graph(ctx)
    return {
        key
        for key in PAGE_STATIONS
        if permissions.page_permission_allowed(
            role=ctx.role, permission_map=graph, page_key=key
        )
    }


def allowed_stations(ctx: UserContext) -> frozenset[str]:
    stations: set[str] = set()
    allowed = allowed_page_keys(ctx)
    for key in allowed:
        stations |= PAGE_STATIONS[key]
    return frozenset(stations)


def can_manage(ctx: UserContext) -> bool:
    return permissions.page_permission_allowed(
        role=ctx.role, permission_map=_graph(ctx), page_key=MANAGE_PAGE
    )


def can_read_units(ctx: UserContext) -> bool:
    return bool(allowed_stations(ctx))


def can_read_wip(ctx: UserContext) -> bool:
    graph = _graph(ctx)
    return any(
        permissions.page_permission_allowed(
            role=ctx.role, permission_map=graph, page_key=key
        )
        for key in (WIP_PAGE, MANAGE_PAGE)
    )


def require_any_page(ctx: UserContext, keys: Iterable[str]) -> None:
    graph = _graph(ctx)
    if any(
        permissions.page_permission_allowed(
            role=ctx.role, permission_map=graph, page_key=key
        )
        for key in keys
    ):
        return
    raise CycleForbidden("无权访问坏件循环页面")


class CycleForbidden(CycleError):
    """当前账号没有对应页键（API 层映射 403）。"""


# ---------------------------------------------------------------------------
# 序列化与可见性
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def serialize_unit(unit: MaintenanceCycleUnit, ctx: UserContext) -> dict:
    fields = WORKER_UNIT_FIELDS
    if can_manage(ctx):
        fields = WORKER_UNIT_FIELDS + MANAGER_UNIT_FIELDS
    payload: dict = {}
    for name in fields:
        value = getattr(unit, name)
        payload[name] = _iso(value) if name in _TS_FIELDS else value
    return payload


def serialize_units(units: Iterable[MaintenanceCycleUnit], ctx: UserContext) -> list[dict]:
    return [serialize_unit(unit, ctx) for unit in units]


def _username(ctx: UserContext) -> str | None:
    return ctx.user_id or None


def _claimable_item_exists_sql(subject_type: str):
    """列表 SQL 用的「该单位存在可领取活待办」关联子查询（F1）。"""
    return (
        select(WorkItem.id)
        .where(
            WorkItem.subject_type == subject_type,
            WorkItem.subject_id == MaintenanceCycleUnit.id,
            WorkItem.status == "pending",
            WorkItem.assignee_id.is_(None),
            WorkItem.claimed_by.is_(None),
            WorkItem.paused.is_(False),
        )
        .exists()
    )


def _has_claimable_item(db: Session, unit: MaintenanceCycleUnit) -> bool:
    subject_type = SUBJECT_BY_STATION.get(unit.station)
    if subject_type is None:
        return False
    return (
        db.scalar(
            select(func.count())
            .select_from(WorkItem)
            .where(
                WorkItem.subject_type == subject_type,
                WorkItem.subject_id == unit.id,
                WorkItem.status == "pending",
                WorkItem.assignee_id.is_(None),
                WorkItem.claimed_by.is_(None),
                WorkItem.paused.is_(False),
            )
        )
        or 0
    ) > 0


def can_view_unit(db: Session, unit: MaintenanceCycleUnit, ctx: UserContext) -> bool:
    """详情/列表统一可见谓词：工位允许集合 + 负责人收敛 + 撤回国主管可见。

    F1 修复：检测/维修工位负责人为空时，必须有「可领取的活待办」
    （pending、未指派、未领取、未暂停）才对他人工人可见；不能仅凭空负责人放行。
    """
    if unit.station not in allowed_stations(ctx):
        return False
    if unit.withdrawn_at is not None:
        return can_manage(ctx)
    if unit.station in ASSIGNEE_SCOPED_STATIONS and not can_manage(ctx):
        if unit.assignee_username is not None:
            return unit.assignee_username == _username(ctx)
        return _has_claimable_item(db, unit)
    return True


def _visible_unit(db: Session, unit_id: int, ctx: UserContext) -> MaintenanceCycleUnit:
    unit = db.get(MaintenanceCycleUnit, unit_id)
    if unit is None or not can_view_unit(db, unit, ctx):
        raise CycleNotFound("循环单位不存在")
    return unit


def _locked_unit(db: Session, unit_id: int) -> MaintenanceCycleUnit:
    unit = db.scalar(
        select(MaintenanceCycleUnit)
        .where(MaintenanceCycleUnit.id == unit_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if unit is None:
        raise CycleNotFound("循环单位不存在")
    return unit


# ---------------------------------------------------------------------------
# 通用状态机原语
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _check_version(unit: MaintenanceCycleUnit, expected_version: int) -> None:
    if unit.version != expected_version:
        raise CycleConflict("版本冲突：单位已被他人处理，请刷新后重试")


def _bump(unit: MaintenanceCycleUnit, actor: str) -> None:
    unit.version += 1
    unit.updated_by = actor
    unit.updated_at = _now()


def _event(
    db: Session,
    unit: MaintenanceCycleUnit,
    event_type: str,
    actor: str,
    payload: dict | None = None,
) -> MaintenanceCycleEvent:
    row = MaintenanceCycleEvent(
        unit_id=unit.id, event_type=event_type, payload=payload or {}, actor=actor
    )
    db.add(row)
    return row


def _clear_assignee(unit: MaintenanceCycleUnit) -> None:
    unit.assignee_username = None


def _validate_bucket(value: str | None, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise CycleInvalid("去电商必须选择好件/坏件桶（condition_bucket=good|bad）")
        return None
    if value not in CONDITION_BUCKETS:
        raise CycleInvalid("condition_bucket 仅支持 good|bad")
    return value


def _validate_qty(qty: object) -> int:
    if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= MAX_INTAKE_QTY:
        raise CycleInvalid(f"数量必须为 1–{MAX_INTAKE_QTY} 的整数")
    return qty


def _validate_serials(serials: object, *, qty: int) -> list[str]:
    if serials is None:
        return []
    if not isinstance(serials, list):
        raise CycleInvalid("SN 必须是字符串列表")
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in serials:
        if not isinstance(item, str):
            raise CycleInvalid("SN 必须是字符串")
        value = item.strip()
        if not value:
            raise CycleInvalid("SN 不能为空白")
        if len(value) > MAX_SERIAL_LENGTH:
            raise CycleInvalid(f"SN 长度不能超过 {MAX_SERIAL_LENGTH}")
        if value in seen:
            raise CycleInvalid(f"SN 重复：{value}")
        seen.add(value)
        cleaned.append(value)
    if cleaned and len(cleaned) != qty:
        raise CycleInvalid(
            f"带 SN 时数量必须等于 SN 个数（数量 {qty}，SN {len(cleaned)} 个）"
        )
    return cleaned


def _optional_text(value: object, name: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CycleInvalid(f"{name} 格式不合法")
    text = value.strip()
    if len(text) > limit:
        raise CycleInvalid(f"{name} 长度不能超过 {limit}")
    return text or None


def _resolve_active_part(db: Session, *, part_id: int | None, pn: str | None) -> DimPart:
    if part_id is not None:
        part = db.get(DimPart, part_id)
        if part is None or part.status != "active":
            raise CycleInvalid("所选备件不存在或不是 active 状态")
        return part
    if not pn or not pn.strip():
        raise CycleInvalid("必须提供 active part_id 或可解析的 pn")
    rows = list(
        db.scalars(
            select(DimPart)
            .where(DimPart.pn_std == pn.strip(), DimPart.status == "active")
            .order_by(DimPart.id)
            .limit(2)
        ).all()
    )
    if not rows:
        raise CycleInvalid("未找到 active 状态的备件身份")
    if len(rows) > 1:
        raise CycleInvalid("该 PN 对应多个 active 备件，请直接指定 part_id")
    return rows[0]


def _active_user_id(db: Session, username: str | None) -> str | None:
    if not username:
        return None
    user = db.scalar(
        select(SysUser).where(
            SysUser.username == username,
            SysUser.is_active.is_(True),
            SysUser.deleted_at.is_(None),
        )
    )
    if user is None:
        raise CycleInvalid("指派账号不存在或已停用")
    return user.username


def _create_intent(db: Session, unit: MaintenanceCycleUnit) -> MaintenanceCycleSellIntent:
    """去电商结案时同事务入队；worker 未启用时保持 pending（R1）。"""
    intent = MaintenanceCycleSellIntent(
        unit_id=unit.id,
        part_id=unit.part_id,
        qty=Decimal(1),
        condition_bucket=unit.condition_bucket,
        state="pending",
        attempts=0,
        available_at=_now(),
    )
    db.add(intent)
    db.flush()
    return intent


def _has_sell_intent(db: Session, unit_id: int) -> bool:
    return db.scalar(
        select(func.count())
        .select_from(MaintenanceCycleSellIntent)
        .where(MaintenanceCycleSellIntent.unit_id == unit_id)
    ) > 0


# ---------------------------------------------------------------------------
# 待办接线（T3）：只复用 work_item 引擎的 open_items/claim/complete/cancel_subject，
# 不复制领取 UPDATE；同一单位在检测/维修工位最多一张未结束待办。
# ---------------------------------------------------------------------------

SUBJECT_TYPES = ("cycle_test", "cycle_repair")
SUBJECT_BY_STATION = {"testing": "cycle_test", "repairing": "cycle_repair"}
POOL_KEY_BY_SUBJECT = {"cycle_test": TEST_PAGE, "cycle_repair": REPAIR_PAGE}
# 工人可见的待办事件 payload 字段（任务书 §6.4：pn/part_id/station/unit_uid）。
# 非主管事件 payload 由当前单位快照生成这四个业务字段；引擎外壳字段
# （version/assignee_id/from_user_id/to_user_id）与自由文本（reason）一律不返回。
# 主管视图保留完整 payload 供审计。N2 修复：此前误用循环事件白名单。
WORKER_SUBJECT_UNIT_FIELDS = ("pn", "part_id", "station", "unit_uid")
ACTIVE_ITEM_STATES = ("pending", "in_progress")


def _require_active_user(db: Session, username: str | None) -> SysUser | None:
    if not username:
        return None
    user = db.scalar(
        select(SysUser).where(
            SysUser.username == username,
            SysUser.is_active.is_(True),
            SysUser.deleted_at.is_(None),
        )
    )
    if user is None:
        raise CycleInvalid("指派账号不存在或已停用")
    return user


def _assert_visible(db: Session, unit: MaintenanceCycleUnit, ctx: UserContext) -> None:
    if not can_view_unit(db, unit, ctx):
        raise CycleNotFound("循环单位不存在")


def active_station_items(db: Session, unit_id: int) -> list[WorkItem]:
    return list(
        db.scalars(
            select(WorkItem)
            .where(
                WorkItem.subject_type.in_(SUBJECT_TYPES),
                WorkItem.subject_id == unit_id,
                WorkItem.status.in_(ACTIVE_ITEM_STATES),
            )
            .order_by(WorkItem.id)
        ).all()
    )


def _open_station_item(
    db: Session,
    unit: MaintenanceCycleUnit,
    station: str,
    assignee_user_id: int | None,
    ctx: UserContext,
) -> WorkItem:
    """派工进入检测/维修工位时开待办：有指派就指派，没有就进公共池。"""
    subject_type = SUBJECT_BY_STATION[station]
    if active_station_items(db, unit.id):
        raise CycleConflict("该单位已有未结束的检测/维修待办")
    items = work_items.open_items(
        db,
        Subject(subject_type, unit.id),
        [assignee_user_id] if assignee_user_id else [],
        subject_type,
        ctx,
    )
    return items[0]


def _finish_station_items(
    db: Session,
    unit: MaintenanceCycleUnit,
    ctx: UserContext,
    *,
    require_active: bool = False,
) -> None:
    """业务动作完成时结束对应待办：已领取/已指派 → complete；公共池 → 领取后 complete；
    主管可取消公共池待办。待办写入失败会抛出并让整个工位转移回滚。

    F1：完成检测/维修时必须存在活待办；不得用「没有待办」静默放行完成。
    """
    items = active_station_items(db, unit.id)
    if require_active and not items:
        raise CycleConflict("该单位没有可处理的检测/维修待办，请刷新或先派工")
    for item in items:
        pool_key = POOL_KEY_BY_SUBJECT[item.subject_type]
        pool_ok = permissions.page_permission_allowed(
            role=ctx.role, permission_map=_graph(ctx), page_key=pool_key
        )
        if item.claimed_by is not None or item.assignee_id is not None:
            work_items.complete(db, item.id, ctx, item.version)
        elif pool_ok:
            claimed = work_items.claim(db, item.id, ctx, item.version)
            # 引擎 complete 走同一 can_view_subject 谓词；领取后单位变为「负责人=本人」，
            # 保证「空负责人 + 可领取待办」条件消失后仍可见。各转移末尾会按业务清空/保留负责人。
            unit.assignee_username = _username(ctx)
            db.flush()
            work_items.complete(db, claimed.id, ctx, claimed.version)
        elif can_manage(ctx):
            work_items.cancel_subject(
                db,
                Subject(item.subject_type, unit.id),
                ctx,
                "业务动作已完成，待办取消",
                expected_versions={item.id: item.version},
            )
        else:
            raise CycleConflict("请先领取待办后再完成该动作")


def claim_unit(
    db: Session,
    *,
    unit_id: int,
    expected_version: int,
    item_version: int,
    actor: str,
    ctx: UserContext,
) -> dict:
    """公共池领取：同一事务调用 work_item 引擎 claim() 并写回单位负责人。"""
    unit = _locked_unit(db, unit_id)
    _check_version(unit, expected_version)
    if unit.withdrawn_at is not None:
        raise CycleConflict("单位已撤回，不能领取")
    if unit.station not in ("testing", "repairing"):
        raise CycleInvalid("只有检测/维修工位的单位可以领取")
    # W-03：第二个工人重复领取必须得到 409，而不是因不可见得到 404。
    if unit.assignee_username is not None:
        raise CycleConflict("该单位已被指派或领取")
    _assert_visible(db, unit, ctx)
    # F-08：API 门对 claim 放行 manage，但引擎 claim 只认 pool 权限；这里先做同一口径校验，
    # 让「只有 page_cycle_manage、无对应 pool 键」的主管得到一致的 403 而不是 API/引擎分叉。
    subject_type = SUBJECT_BY_STATION[unit.station]
    pool_key = POOL_KEY_BY_SUBJECT[subject_type]
    if not permissions.page_permission_allowed(
        role=ctx.role, permission_map=_graph(ctx), page_key=pool_key
    ):
        raise CycleForbidden("无权领取该工位待办（缺少对应的检测/维修页键）")
    items = active_station_items(db, unit.id)
    if not items:
        raise CycleConflict("该单位没有可领取的待办")
    item = items[0]
    if item.assignee_id is not None or item.claimed_by is not None:
        raise CycleConflict("待办已被领取")
    work_items.claim(db, item.id, ctx, item_version)
    unit.assignee_username = _username(ctx)
    _bump(unit, actor)
    _event(db, unit, "claim", actor, {})
    db.flush()
    return serialize_unit(unit, ctx)


def _subject_view(db: Session, unit_id: int, ctx: UserContext) -> bool:
    unit = db.get(MaintenanceCycleUnit, unit_id)
    return unit is not None and can_view_unit(db, unit, ctx)


def _subject_summarize(db: Session, unit_ids, ctx: UserContext) -> dict[int, dict]:
    ids = list(dict.fromkeys(unit_ids))
    if not ids:
        return {}
    result: dict[int, dict] = {}
    for unit in db.scalars(
        select(MaintenanceCycleUnit).where(MaintenanceCycleUnit.id.in_(ids))
    ).all():
        if can_view_unit(db, unit, ctx):
            result[unit.id] = {
                "pn": unit.pn,
                "part_id": unit.part_id,
                "station": unit.station,
                "unit_uid": str(unit.unit_uid),
            }
    return result


def _subject_filter_events(rows, ctx: UserContext):
    """非主管事件 payload 只暴露任务书 §6.4 的四个业务字段（N2）。

    由当前单位快照生成 ``pn/part_id/station/unit_uid``；引擎外壳字段
    （version/assignee_id/from_user_id/to_user_id）与自由文本（reason）不返回。
    主管保留完整引擎 payload 供审计。
    """
    if can_manage(ctx):
        return rows
    sanitized = []
    for row in rows:
        db = object_session(row)
        payload: dict = {}
        unit = db.get(MaintenanceCycleUnit, row.subject_id) if db is not None else None
        if unit is not None and can_view_unit(db, unit, ctx):
            payload = {
                "pn": unit.pn,
                "part_id": unit.part_id,
                "station": unit.station,
                "unit_uid": str(unit.unit_uid),
            }
        sanitized.append(
            SimpleNamespace(
                **{
                    column.name: getattr(row, column.name)
                    for column in row.__table__.columns
                    if column.name != "payload"
                },
                payload=payload,
                __table__=row.__table__,
            )
        )
    return sanitized


def _subject_filter_items(db: Session, rows, ctx: UserContext):
    """F2：待办列表与动作响应对非主管清空自由文本（last_reason）；主管保留审计。

    循环单位的敏感字段（供应商/SN/收货单/备注）不在 summary 四字段里；
    本钩子堵住通用 WorkItem 列 last_reason 这条自由文本泄露路径。
    """
    if can_manage(ctx):
        return rows
    sanitized = []
    for row in rows:
        item = dict(row)
        item["last_reason"] = None
        sanitized.append(item)
    return sanitized


def register_subjects() -> None:
    """把检测/维修两个主体注册进现有待办引擎（复用 open_items/claim/complete）。"""
    specs = registered()
    for subject_type, stage in (("cycle_test", "testing"), ("cycle_repair", "repairing")):
        if subject_type in specs:
            continue
        register(
            subject_type,
            SubjectSpec(
                pool_permission=POOL_KEY_BY_SUBJECT[subject_type],
                supervisor_permission=MANAGE_PAGE,
                can_view_subject=_subject_view,
                summarize=_subject_summarize,
                filter_events=_subject_filter_events,
                filter_items=_subject_filter_items,
                pool_key=subject_type,
            ),
        )


# ---------------------------------------------------------------------------
# 入池：返还收货交接 / 外部入池
# ---------------------------------------------------------------------------


def handoff_from_receipt(
    db: Session,
    *,
    receipt_id: str,
    qty: int,
    expected_receipt_version: int,
    serial_numbers: list[str] | None,
    actor: str,
) -> list[MaintenanceCycleUnit]:
    """从返还收货行交接入池：先锁收货行再建单位（锁顺序固定）。"""
    from app.services import maintenance_return_receipts as receipts

    receipts.lock_receipt_context(db)
    try:
        receipt = receipts.lock_receipt_row(db, receipt_id)
    except receipts.ReturnReceiptNotFound as exc:
        raise CycleNotFound("返还收货记录不存在") from exc
    except receipts.ReturnReceiptConflict as exc:
        raise CycleConflict(str(exc)) from exc
    except receipts.ReturnReceiptError as exc:
        raise CycleInvalid(str(exc)) from exc
    if receipt.line_status != "active":
        raise CycleConflict("返还收货记录已作废，不能入池")
    if receipt.version != expected_receipt_version:
        raise CycleConflict("收货记录版本冲突：已被他人修改，请刷新后重试")
    clean_qty = _validate_qty(qty)
    if receipt.part_id is None:
        raise CycleInvalid("返还收货记录缺少备件身份，不能入池")
    part = db.get(DimPart, receipt.part_id)
    if part is None or part.status != "active":
        raise CycleInvalid("返还收货的备件身份不是 active 状态，不能入池")

    consumed = db.scalar(
        select(func.count())
        .select_from(MaintenanceCycleUnit)
        .where(
            MaintenanceCycleUnit.source_receipt_id == receipt_id,
            MaintenanceCycleUnit.withdrawn_at.is_(None),
        )
    ) or 0
    if Decimal(consumed + clean_qty) > Decimal(receipt.qty):
        raise CycleConflict(
            f"入池件数超出收货数量：已入池 {consumed}，收货数量 {receipt.qty}，本次 {clean_qty}"
        )
    serials = _validate_serials(serial_numbers, qty=clean_qty)

    units: list[MaintenanceCycleUnit] = []
    try:
        for index in range(clean_qty):
            unit = MaintenanceCycleUnit(
                part_id=receipt.part_id,
                pn=receipt.pn,
                station="pool",
                source_kind="receipt",
                source_receipt_id=receipt.rkd_line_id,
                intake_condition=receipt.test_result,
                serial_number=serials[index] if serials else None,
                created_by=actor,
                updated_by=actor,
            )
            db.add(unit)
            units.append(unit)
        db.flush()
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == "23505":
            raise CycleConflict("同一收货行的 SN 已被其它单位占用，请刷新后重试") from exc
        raise
    for unit in units:
        _event(
            db,
            unit,
            "intake_receipt",
            actor,
            {"receipt_id": receipt.rkd_line_id, "intake_condition": receipt.test_result},
        )
    db.flush()
    return units


def intake_external(
    db: Session,
    *,
    part_id: int | None,
    pn: str | None,
    qty: int,
    intake_condition: str | None,
    note: str | None,
    serial_numbers: list[str] | None,
    actor: str,
) -> list[MaintenanceCycleUnit]:
    """外部入池：不挂收货行、不占收货件数；备注只进单位列与主管事件。"""
    clean_qty = _validate_qty(qty)
    part = _resolve_active_part(db, part_id=part_id, pn=pn)
    condition = _optional_text(intake_condition, "件况", 64)
    clean_note = _optional_text(note, "备注", MAX_NOTE_LENGTH)
    serials = _validate_serials(serial_numbers, qty=clean_qty)
    units: list[MaintenanceCycleUnit] = []
    for index in range(clean_qty):
        unit = MaintenanceCycleUnit(
            part_id=part.id,
            pn=part.pn_std,
            station="pool",
            source_kind="external",
            intake_condition=condition,
            note=clean_note,
            serial_number=serials[index] if serials else None,
            created_by=actor,
            updated_by=actor,
        )
        db.add(unit)
        units.append(unit)
    db.flush()
    for unit in units:
        _event(
            db,
            unit,
            "intake_external",
            actor,
            {"intake_condition": condition, "note": clean_note},
        )
    db.flush()
    return units


# ---------------------------------------------------------------------------
# 工位转移（全部 expected_version；成功 version+1 + 事件）
# ---------------------------------------------------------------------------


def dispatch(
    db: Session,
    *,
    unit_id: int,
    expected_version: int,
    to: str,
    assignee_username: str | None,
    vendor_name: str | None,
    actor: str,
    ctx: UserContext,
) -> dict:
    unit = _locked_unit(db, unit_id)
    _assert_visible(db, unit, ctx)
    _check_version(unit, expected_version)
    if unit.withdrawn_at is not None:
        raise CycleConflict("单位已撤回，不能派工")
    if unit.station not in ("pool", "ready"):
        raise CycleInvalid("只能从回收池或待处置派工")
    if to not in ("testing", "repairing", "outsource"):
        raise CycleInvalid("派工去向仅支持 testing/repairing/outsource")
    vendor: str | None = None
    assignee: str | None = None
    target_user: SysUser | None = None
    if to == "outsource":
        if vendor_name is None or not str(vendor_name).strip():
            raise CycleInvalid("委外必须填写供应商名称")
        vendor = _optional_text(vendor_name, "供应商名称", MAX_VENDOR_LENGTH)
    else:
        if vendor_name not in (None, ""):
            raise CycleInvalid("只有委外可以填写供应商名称")
        target_user = _require_active_user(db, assignee_username)
        assignee = target_user.username if target_user is not None else None
        # T3：进入检测/维修工位同一事务开待办；有指派则指派，否则进公共池。
        _open_station_item(
            db, unit, to, target_user.id if target_user is not None else None, ctx
        )
    unit.station = to
    unit.outcome = None
    # 从待处置再派回检测/维修时，清掉自测或上次通过留下的好坏标记。
    if to in ("testing", "repairing"):
        unit.condition_bucket = None
    unit.vendor_name = vendor
    unit.assignee_username = assignee
    _bump(unit, actor)
    _event(
        db,
        unit,
        "dispatch",
        actor,
        {"to": to, "assignee_username": assignee, "vendor_name": vendor},
    )
    db.flush()
    return serialize_unit(unit, ctx)


def self_pass(
    db: Session, *, unit_id: int, expected_version: int, actor: str, ctx: UserContext
) -> dict:
    unit = _locked_unit(db, unit_id)
    _assert_visible(db, unit, ctx)
    _check_version(unit, expected_version)
    if unit.withdrawn_at is not None:
        raise CycleConflict("单位已撤回，不能自测")
    if unit.station != "pool":
        raise CycleInvalid("自测通过只能从回收池发起")
    unit.station = "ready"
    unit.outcome = None
    unit.condition_bucket = "good"
    _clear_assignee(unit)
    _bump(unit, actor)
    _event(db, unit, "self_pass", actor, {"to": "ready", "condition_bucket": "good"})
    db.flush()
    return serialize_unit(unit, ctx)


def complete_test(
    db: Session,
    *,
    unit_id: int,
    expected_version: int,
    result: str,
    next_station: str | None,
    actor: str,
    ctx: UserContext,
) -> dict:
    unit = _locked_unit(db, unit_id)
    _assert_visible(db, unit, ctx)
    _check_version(unit, expected_version)
    if unit.station != "testing":
        raise CycleInvalid("只有检测中单位可以完成检测")
    if result not in ("good", "bad"):
        raise CycleInvalid("检测结果仅支持 good|bad")
    # T3：业务动作完成 → 结束检测待办；测坏送修 → 同事务打开未指派维修待办。
    # F1：完成检测必须有活待办，不能让「无待办」静默放行。
    _finish_station_items(db, unit, ctx, require_active=True)
    if result == "good":
        if next_station not in (None, "ready"):
            raise CycleInvalid("测好只能进入待处置（ready）")
        unit.station = "ready"
        unit.condition_bucket = "good"
        unit.outcome = None
        _clear_assignee(unit)
        payload = {"result": "good", "next": "ready"}
    else:
        if next_station not in ("repairing", "scrap"):
            raise CycleInvalid("测坏必须选择送修（repairing）或报废（scrap）")
        if next_station == "repairing":
            _open_station_item(db, unit, "repairing", None, ctx)
            unit.station = "repairing"
            unit.condition_bucket = None
            unit.outcome = None
            unit.repair_round += 1
            # §6.4 / R6：检测转维修清空检测人，维修待办未指派。
            _clear_assignee(unit)
            payload = {"result": "bad", "next": "repairing"}
        else:
            unit.station = "closed"
            unit.outcome = "scrap"
            unit.condition_bucket = None
            unit.closed_at = _now()
            _clear_assignee(unit)
            payload = {"result": "bad", "next": "scrap", "outcome": "scrap"}
    _bump(unit, actor)
    _event(db, unit, "complete_test", actor, payload)
    db.flush()
    return serialize_unit(unit, ctx)


def complete_repair(
    db: Session,
    *,
    unit_id: int,
    expected_version: int,
    result: str,
    next_station: str | None,
    actor: str,
    ctx: UserContext,
) -> dict:
    unit = _locked_unit(db, unit_id)
    _assert_visible(db, unit, ctx)
    _check_version(unit, expected_version)
    if unit.station != "repairing":
        raise CycleInvalid("只有维修中单位可以完成维修")
    if result not in ("fixed", "unfixed"):
        raise CycleInvalid("维修结果仅支持 fixed|unfixed")
    # T3：业务动作完成 → 结束维修待办；修好再测 → 同事务打开未指派检测待办。
    # F1：完成维修必须有活待办，不能让「无待办」静默放行。
    _finish_station_items(db, unit, ctx, require_active=True)
    if result == "fixed":
        if next_station not in ("testing", "ready"):
            raise CycleInvalid("修好后必须选择再测（testing）或不测（ready）")
        if next_station == "testing":
            _open_station_item(db, unit, "testing", None, ctx)
            unit.station = "testing"
            unit.condition_bucket = None
            payload = {"result": "fixed", "next": "testing"}
        else:
            unit.station = "ready"
            unit.condition_bucket = "good"
            payload = {"result": "fixed", "next": "ready"}
        unit.outcome = None
        _clear_assignee(unit)
    else:
        if next_station not in (None, "scrap"):
            raise CycleInvalid("修不好只能报废（scrap）")
        unit.station = "closed"
        unit.outcome = "scrap"
        unit.condition_bucket = None
        unit.closed_at = _now()
        _clear_assignee(unit)
        payload = {"result": "unfixed", "next": "scrap", "outcome": "scrap"}
    _bump(unit, actor)
    _event(db, unit, "complete_repair", actor, payload)
    db.flush()
    return serialize_unit(unit, ctx)


def outsource_return(
    db: Session,
    *,
    unit_id: int,
    expected_version: int,
    next_station: str,
    condition_bucket: str | None,
    actor: str,
    ctx: UserContext,
) -> dict:
    unit = _locked_unit(db, unit_id)
    _assert_visible(db, unit, ctx)
    _check_version(unit, expected_version)
    if unit.station != "outsource":
        raise CycleInvalid("只有委外中单位可以登记委外回来")
    if next_station not in ("testing", "repairing", "to_shop", "to_company", "scrap"):
        raise CycleInvalid("委外回来去向必须显式五选一")
    bucket = _validate_bucket(condition_bucket, required=next_station == "to_shop")
    # T3：委外本身没有工人待办；回到检测/维修工位时同事务开未指派待办。
    _finish_station_items(db, unit, ctx)
    if next_station in ("testing", "repairing"):
        _open_station_item(db, unit, next_station, None, ctx)
    _clear_assignee(unit)
    unit.condition_bucket = bucket
    if next_station in ("testing", "repairing"):
        unit.station = next_station
        unit.outcome = None
        payload = {"next": next_station}
    else:
        unit.station = "closed"
        unit.outcome = next_station
        unit.closed_at = _now()
        payload = {"next": next_station, "outcome": next_station,
                   "condition_bucket": bucket}
        if next_station == "to_shop":
            _create_intent(db, unit)
    _bump(unit, actor)
    _event(db, unit, "outsource_return", actor, payload)
    db.flush()
    return serialize_unit(unit, ctx)


def close(
    db: Session,
    *,
    unit_id: int,
    expected_version: int,
    outcome: str,
    condition_bucket: str | None,
    note: str | None,
    actor: str,
    ctx: UserContext,
) -> dict:
    unit = _locked_unit(db, unit_id)
    _assert_visible(db, unit, ctx)
    _check_version(unit, expected_version)
    if unit.withdrawn_at is not None:
        raise CycleConflict("单位已撤回，不能结案")
    if unit.station not in ("pool", "ready"):
        raise CycleInvalid("只能从回收池或待处置结案")
    if outcome not in ("to_shop", "to_company", "scrap"):
        raise CycleInvalid("结案去向仅支持 to_shop/to_company/scrap")
    bucket = _validate_bucket(condition_bucket, required=outcome == "to_shop")
    clean_note = _optional_text(note, "备注", MAX_NOTE_LENGTH)
    _finish_station_items(db, unit, ctx)
    unit.station = "closed"
    unit.outcome = outcome
    unit.condition_bucket = bucket
    unit.closed_at = _now()
    if clean_note is not None:
        unit.note = clean_note
    _clear_assignee(unit)
    if outcome == "to_shop":
        _create_intent(db, unit)
    _bump(unit, actor)
    _event(
        db,
        unit,
        "close",
        actor,
        {"outcome": outcome, "condition_bucket": bucket, "note": clean_note},
    )
    db.flush()
    return serialize_unit(unit, ctx)


def withdraw(
    db: Session, *, unit_id: int, expected_version: int, actor: str, ctx: UserContext
) -> dict:
    unit = _locked_unit(db, unit_id)
    _assert_visible(db, unit, ctx)
    _check_version(unit, expected_version)
    if unit.withdrawn_at is not None:
        raise CycleConflict("单位已撤回")
    if unit.station != "pool":
        raise CycleInvalid("只有仍在回收池的单位可以撤回")
    if _has_sell_intent(db, unit.id):
        raise CycleConflict("单位已生成可售意图，不能撤回")
    unit.withdrawn_at = _now()
    _bump(unit, actor)
    _event(db, unit, "withdraw", actor, {})
    db.flush()
    return serialize_unit(unit, ctx)


def mark_sold(
    db: Session, *, unit_id: int, expected_version: int, actor: str, ctx: UserContext
) -> dict:
    """人工主管兜底：closed + to_shop → sold；重复调用幂等返回当前状态（T9 商城回调另接）。

    F3：人工路径与其它写接口一致，先做 expected_version 乐观锁；stale sold → 409。
    （§8.5 基于 deliveryId/shopOrderLineId 的服务回调幂等不能替代人工 CAS。）
    """
    unit = _locked_unit(db, unit_id)
    _check_version(unit, expected_version)
    if unit.outcome == "sold":
        return serialize_unit(unit, ctx)
    if unit.station != "closed" or unit.outcome != "to_shop":
        raise CycleConflict("只有已结案去电商的单位可以标记售出")
    # F-04：人工售出同事务终止未投递意图（pending/blocked → failed），避免 worker 启动后
    # 仍把已售单位投递到商城；在途 delivering 返回 409（不抢在途 HTTP 的租约）。
    intent = db.scalar(
        select(MaintenanceCycleSellIntent)
        .where(MaintenanceCycleSellIntent.unit_id == unit.id)
        .with_for_update()
    )
    if intent is not None:
        if intent.state == "delivering":
            raise CycleConflict(
                "该单位的可售意图正在投递中，请先停止 worker 并等待回调后再标记售出"
            )
        if intent.state in ("pending", "blocked"):
            now = _now()
            intent.state = "failed"
            intent.receipt_status = None
            intent.block_code = None
            intent.last_error_code = "unit_sold_manually"
            intent.lease_token = None
            intent.lease_until = None
            intent.completed_at = now
            intent.updated_at = now
            db.flush()
    unit.outcome = "sold"
    _bump(unit, actor)
    _event(db, unit, "mark_sold", actor, {"outcome": "sold"})
    db.flush()
    return serialize_unit(unit, ctx)


# ---------------------------------------------------------------------------
# 读接口：列表 / 详情 / 事件 / 在制
# ---------------------------------------------------------------------------


def parse_station_filter(raw: str | None) -> frozenset[str] | None:
    if raw is None or not raw.strip():
        return None
    tokens = {token.strip() for token in raw.split(",") if token.strip()}
    unknown = tokens - set(STATIONS)
    if unknown:
        raise CycleInvalid(f"未知工位：{sorted(unknown)}")
    return frozenset(tokens)


def list_units(
    db: Session,
    *,
    ctx: UserContext,
    station: str | None,
    assignee: str | None,
    page: int,
    page_size: int,
) -> dict:
    allowed = allowed_stations(ctx)
    requested = parse_station_filter(station)
    effective = set(allowed) if requested is None else set(allowed) & set(requested)
    result = {"items": [], "total": 0, "page": page, "page_size": page_size}
    # R4：工位与允许集合求交为空时返回空列表，不查全表。
    if not effective:
        return result
    conditions = [
        MaintenanceCycleUnit.station.in_(sorted(effective)),
        MaintenanceCycleUnit.withdrawn_at.is_(None),
    ]
    if not can_manage(ctx):
        unscoped = sorted(effective - ASSIGNEE_SCOPED_STATIONS)
        scoped = sorted(effective & ASSIGNEE_SCOPED_STATIONS)
        clauses = []
        if unscoped:
            clauses.append(MaintenanceCycleUnit.station.in_(unscoped))
        # F1：检测/维修工位空负责人时，必须存在可领取活待办才对工人可见。
        for scoped_station in scoped:
            clauses.append(
                (MaintenanceCycleUnit.station == scoped_station)
                & or_(
                    MaintenanceCycleUnit.assignee_username == (_username(ctx) or ""),
                    and_(
                        MaintenanceCycleUnit.assignee_username.is_(None),
                        _claimable_item_exists_sql(
                            SUBJECT_BY_STATION[scoped_station]
                        ),
                    ),
                )
            )
        conditions.append(or_(*clauses))
    if assignee is not None and assignee.strip():
        raw = assignee.strip()
        target = _username(ctx) if raw == "me" else raw
        if not target:
            raise CycleInvalid("assignee 只能为 me 或账号名")
        conditions.append(MaintenanceCycleUnit.assignee_username == target)
    total = db.scalar(
        select(func.count()).select_from(MaintenanceCycleUnit).where(*conditions)
    ) or 0
    rows = list(
        db.scalars(
            select(MaintenanceCycleUnit)
            .where(*conditions)
            .order_by(MaintenanceCycleUnit.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    result["items"] = serialize_units(rows, ctx)
    result["total"] = total
    return result


def get_unit(db: Session, *, unit_id: int, ctx: UserContext) -> dict:
    unit = _visible_unit(db, unit_id, ctx)
    return serialize_unit(unit, ctx)


def filter_event_payload(payload: dict | None, *, manager: bool) -> dict:
    data = payload or {}
    if manager:
        return dict(data)
    return {key: value for key, value in data.items() if key in WORKER_EVENT_KEYS}


def list_events(db: Session, *, unit_id: int, ctx: UserContext) -> dict:
    unit = _visible_unit(db, unit_id, ctx)
    manager = can_manage(ctx)
    rows = list(
        db.scalars(
            select(MaintenanceCycleEvent)
            .where(MaintenanceCycleEvent.unit_id == unit.id)
            .order_by(MaintenanceCycleEvent.id)
        ).all()
    )
    items = [
        {
            "id": row.id,
            "event_type": row.event_type,
            "payload": filter_event_payload(row.payload, manager=manager),
            "actor": row.actor,
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]
    return {"unit_id": unit.id, "items": items, "total": len(items)}


def wip_counts(db: Session, *, part_ids: list[int]) -> dict:
    rows = db.execute(
        select(
            MaintenanceCycleUnit.part_id,
            MaintenanceCycleUnit.station,
            func.count(),
        )
        .where(
            MaintenanceCycleUnit.part_id.in_(part_ids),
            MaintenanceCycleUnit.withdrawn_at.is_(None),
            MaintenanceCycleUnit.station.in_(("testing", "repairing", "outsource", "ready")),
        )
        .group_by(MaintenanceCycleUnit.part_id, MaintenanceCycleUnit.station)
    ).all()
    buckets = {part_id: {"part_id": part_id, "testing": 0, "repairing": 0,
                         "outsource": 0, "ready": 0} for part_id in part_ids}
    for part_id, station, count in rows:
        buckets[part_id][station] = int(count)
    return {"items": [buckets[part_id] for part_id in part_ids]}


# ---------------------------------------------------------------------------
# 收货互锁原语（由 maintenance_return_receipts 在同一事务内调用）
# ---------------------------------------------------------------------------


def units_for_receipt(
    db: Session, receipt_id: str, *, lock: bool = False
) -> list[MaintenanceCycleUnit]:
    stmt = (
        select(MaintenanceCycleUnit)
        .where(MaintenanceCycleUnit.source_receipt_id == receipt_id)
        .order_by(MaintenanceCycleUnit.id)
    )
    if lock:
        stmt = stmt.with_for_update()
    return list(db.scalars(stmt.execution_options(populate_existing=True)).all())


def live_units(units: Iterable[MaintenanceCycleUnit]) -> list[MaintenanceCycleUnit]:
    return [unit for unit in units if unit.withdrawn_at is None]


def has_sell_intent(db: Session, unit_ids: list[int]) -> bool:
    if not unit_ids:
        return False
    return (
        db.scalar(
            select(func.count())
            .select_from(MaintenanceCycleSellIntent)
            .where(MaintenanceCycleSellIntent.unit_id.in_(unit_ids))
        )
        or 0
    ) > 0


def withdraw_units_for_void(
    db: Session, *, receipt_id: str, actor: str
) -> list[MaintenanceCycleUnit]:
    """作废收货行时同一事务撤回其全部未撤回单位（已由调用方锁住收货行）。"""
    units = units_for_receipt(db, receipt_id, lock=True)
    active = live_units(units)
    if not active:
        return []
    if any(unit.station != "pool" for unit in active):
        raise CycleConflict("存在已离开回收池的单位，不能作废收货行")
    if has_sell_intent(db, [unit.id for unit in active]):
        raise CycleConflict("存在已生成可售意图的单位，不能作废收货行")
    now = _now()
    for unit in active:
        unit.withdrawn_at = now
        unit.version += 1
        unit.updated_by = actor
        unit.updated_at = now
        _event(db, unit, "withdraw", actor, {"reason": "receipt_voided"})
    db.flush()
    return active
