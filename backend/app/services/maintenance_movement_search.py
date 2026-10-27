"""可见项目内的流转事实搜索；页面与智能体共用，不读取金额列。"""

from datetime import UTC, datetime
from threading import BoundedSemaphore
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Date, DateTime, Text, and_, case, cast, false, func, literal, literal_column, or_, select, union_all
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.models.maintenance import FMaintenanceOrder, MaintenanceDemandTombstone
from app.models.maintenance_bad_return import MaintenanceBadReturn, MaintenanceBadReturnLine
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import MaintenanceSiteIssue, MaintenanceSiteIssueLine
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysUser
from app.security import FULL_SCOPE_ROLES, UserContext, is_field_hidden, page_allowed
from app.services import maintenance_project_assignments as assignments
from app.services.part_search_normalize import compact, normalize

Entity = Literal["all", "issue", "receipt", "demand_order", "bad_return"]
Status = Literal["confirmed", "corrected", "draft", "void", "active", "voided", "submitted", "in_transit", "warehouse_confirmed", "已生效"]
_STATUS_SETS = {
    "issue": {"confirmed", "corrected", "draft", "void"},
    "receipt": {"active", "voided"},
    "demand_order": {"已生效"},
    "bad_return": {"draft", "submitted", "in_transit", "warehouse_confirmed", "void"},
}
_SLOTS = BoundedSemaphore(4)
_FIELD_LABELS = {
    "pn": "PN", "serial_number": "SN", "serial_numbers": "逐件 SN",
    "source_serial_number": "导入源 SN", "remark": "现场备注", "note": "备注",
    "issue_no": "领用单号", "display_no": "领用展示号", "evidence_ref": "凭证号",
    "express_no": "快递单号", "express_no2": "快递单号（第二列）",
    "logistics_reference": "物流凭证",
}


class MovementSearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str = Field(min_length=1, max_length=256)
    field: Literal["auto", "pn", "sn", "evidence", "remark"] = "auto"
    entity: Entity = "all"
    project_ids: list[str] | None = Field(default=None, max_length=50)
    match_mode: Literal["literal", "exact"] = "literal"
    statuses: list[Status] | None = Field(default=None, min_length=1, max_length=12)
    page: int = Field(default=1, ge=1, le=1000000)
    page_size: int = Field(default=20, ge=1, le=100)

    @field_validator("q")
    @classmethod
    def clean_query(cls, value):
        if not normalize(value) or "\x00" in value:
            raise ValueError("搜索词不能为空或包含空字符")
        return value.strip()

    @field_validator("project_ids")
    @classmethod
    def clean_projects(cls, value):
        if value is not None and any(not v.strip() or len(v) > 36 for v in value):
            raise ValueError("项目筛选无效")
        return value

    @model_validator(mode="after")
    def validate_filters(self):
        if self.field == "remark" and self.match_mode == "literal" and len(normalize(self.q)) < 2:
            raise ValueError("备注包含搜索至少输入两个字符")
        if self.statuses and self.entity != "all" and not set(self.statuses) <= _STATUS_SETS[self.entity]:
            raise ValueError("状态不适用于所选类型")
        return self


def _json(**values):
    return func.jsonb_build_object(*[item for pair in values.items() for item in pair])


def _normalized(column):
    # PostgreSQL UTF8 的 normalize 与 Python 共用 NFKC；补齐 Python split 的空白集合。
    value = func.normalize(func.coalesce(column, ""), literal_column("NFKC"))
    value = func.translate(value, "\u001c\u001d\u001e\u001f\u0085", "     ")
    return func.btrim(func.regexp_replace(func.upper(value), r"\s+", " ", "g"))


def _predicates(column, group, query):
    value = _normalized(column)
    q = normalize(query.q)
    exact = value == q
    # 纯符号或含非 ASCII 字母数字的查询不能丢弃字符后扩大范围。
    compact_hit = false()
    if group == "pn" and compact(q) and q.isascii():
        compact_hit = func.regexp_replace(value, "[^A-Z0-9]", "", "g") == compact(q)
    contains = false()
    if query.match_mode == "literal" and len(q) >= 3:
        contains = value.contains(q, autoescape=True)
    elif query.match_mode == "literal" and group == "remark" and len(q) >= 2:
        contains = value.contains(q, autoescape=True)
    return exact, compact_hit, or_(exact, compact_hit, contains)


def _matches(column, group, query, *, array=False):
    if not array:
        return _predicates(column, group, query)
    element = func.jsonb_array_elements_text(column).table_valued("value").alias()
    predicates = _predicates(element.c.value, group, query)
    return tuple(select(1).select_from(element).where(p).correlate_except(element).exists() for p in predicates)


def _visible_projects(db, query, ctx):
    if not ctx.is_authenticated or not ctx.user_id:
        return []
    # 停用/已删除实名账号失败关闭；不接受客户端权限参数。
    if db.scalar(select(SysUser.id).where(SysUser.username == ctx.user_id, SysUser.is_active.is_(True))) is None:
        return []
    statement = select(MaintenanceProject.project_id).where(MaintenanceProject.is_active.is_(True))
    if ctx.role not in FULL_SCOPE_ROLES:
        statement = statement.where(assignments.accessible_project_condition(ctx))
    if query.project_ids is not None:
        statement = statement.where(MaintenanceProject.project_id.in_(query.project_ids))
    return [pid for pid in db.scalars(statement) if assignments.can_access_project(db, project_id=pid, user_ctx=ctx)]


def _valid_demand():
    return ~select(1).where(
        MaintenanceDemandTombstone.source_order_id == FMaintenanceOrder.raw_order_id,
        MaintenanceDemandTombstone.restored_at.is_(None),
    ).exists()


def _demand_ref(order_id, project_id):
    # 关联已移走、作废或失去归属时不输出旧单号。
    return select(_json(id=FMaintenanceOrder.raw_order_id, number=FMaintenanceOrder.order_no)).join(
        MaintenanceSourceOrderAssignment,
        MaintenanceSourceOrderAssignment.source_order_id == FMaintenanceOrder.raw_order_id,
    ).where(
        FMaintenanceOrder.raw_order_id == order_id,
        MaintenanceSourceOrderAssignment.project_id == project_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
        _valid_demand(),
    ).correlate_except(FMaintenanceOrder, MaintenanceSourceOrderAssignment).scalar_subquery()


def _branch(query, entity, statement, *, record_id, project_id, date, document, pn, serials, source_sn, quantity, demand_ref, fields, conditions):
    hits, exacts, compacts, labels = [], [], [], []
    raw_fields = {}
    for name, group, column, array in fields:
        raw_fields[name] = column
        # TASK 更正：坏件凭证搜索同时覆盖备注与行 PN 中留存的凭证片段。
        evidence_fallback = entity == "bad_return" and query.field == "evidence" and name in {"note", "pn"}
        if query.field not in ("auto", group) and not evidence_fallback:
            continue
        match_group = "evidence" if evidence_fallback else group
        if match_group == "remark" and len(normalize(query.q)) < 2:
            continue
        exact, compact_hit, hit = _matches(column, match_group, query, array=array)
        hits.append(hit)
        exacts.append(exact)
        compacts.append(compact_hit)
        labels.append(case((hit, name), else_=None))
    rank = case((or_(false(), *exacts), 0), (or_(false(), *compacts), 1), else_=2)
    payload = _json(
        entity_type=entity, record_id=record_id,
        project=_json(id=MaintenanceProject.project_id, code=MaintenanceProject.project_code, name=MaintenanceProject.display_name),
        document=document, pn=pn, serials=serials, source_serial_number=source_sn,
        quantity=cast(quantity, Text), demand_ref=demand_ref,
        matched_fields=func.jsonb_build_array(*labels), fields=_json(**raw_fields),
    )
    statuses = set(query.statuses or ()) & _STATUS_SETS[entity]
    if query.statuses and not statuses:
        conditions = [*conditions, false()]
    business_date = func.timezone("Asia/Shanghai", cast(date, DateTime())) if isinstance(date.type, Date) else date
    return statement.join(MaintenanceProject, MaintenanceProject.project_id == project_id).where(
        *conditions, or_(false(), *hits),
    ).with_only_columns(
        literal(entity).label("entity"), record_id.label("record_id"),
        cast(business_date, DateTime(timezone=True)).label("business_date"), rank.label("rank"), payload.label("payload"),
    )


def movement_statements(query, project_ids, *, include_issue=True):
    """构建可供 EXPLAIN 的事实查询；范围先于匹配和 UNION 分页。"""
    result = []
    if query.entity in ("all", "issue") and include_issue:
        h, l = MaintenanceSiteIssue, MaintenanceSiteIssueLine
        result.append(_branch(query, "issue", select(l).join(h, l.issue_id == h.issue_id),
            record_id=l.issue_line_id, project_id=h.project_id, date=h.issue_date,
            document=_json(id=h.issue_id, number=h.issue_no, display_number=h.display_no, date=h.issue_date, status=h.normalized_status, source=h.source),
            pn=l.pn, serials=func.jsonb_build_array(l.serial_number), source_sn=None, quantity=l.quantity,
            demand_ref=_demand_ref(l.source_order_id, h.project_id),
            fields=[("pn", "pn", l.pn, False), ("serial_number", "sn", l.serial_number, False), ("remark", "remark", l.remark, False), ("issue_no", "evidence", h.issue_no, False), ("display_no", "evidence", h.display_no, False)],
            conditions=[h.project_id.in_(project_ids), l.is_active.is_(True), h.source.in_(("site_issue_v2", "workbook", "legacy", "page_manual")), h.normalized_status.in_(query.statuses or ("confirmed", "corrected"))],
        ))
    if query.entity in ("all", "receipt"):
        r = MaintenanceRkdReturnLine
        source_sn_value = r.source_payload["source_metadata"]["sn"]
        source_sn = case((func.jsonb_typeof(source_sn_value) == "string", source_sn_value.astext), else_=None)
        result.append(_branch(query, "receipt", select(r), record_id=r.rkd_line_id, project_id=r.project_id, date=r.occurred_at,
            document=_json(id=r.rkd_line_id, number=r.head_no, display_number=None, date=r.occurred_at, status=r.line_status, source=r.source, source_ref=r.source_ref),
            pn=r.pn, serials=r.serial_numbers, source_sn=source_sn, quantity=r.qty,
            demand_ref=_demand_ref(r.source_order_id, r.project_id),
            fields=[("pn", "pn", r.pn, False), ("serial_numbers", "sn", r.serial_numbers, True), ("source_serial_number", "sn", source_sn, False), ("evidence_ref", "evidence", r.evidence_ref, False), ("note", "remark", r.note, False)],
            conditions=[r.project_id.in_(project_ids), r.line_status.in_(query.statuses or ("active",))],
        ))
    if query.entity in ("all", "demand_order"):
        o, a = FMaintenanceOrder, MaintenanceSourceOrderAssignment
        result.append(_branch(query, "demand_order", select(o).join(a, and_(a.source_order_id == o.raw_order_id, a.is_active.is_(True))),
            record_id=o.raw_order_id, project_id=a.project_id, date=o.order_date,
            document=_json(id=o.raw_order_id, number=o.order_no, display_number=None, date=o.order_date, status=o.data_status, source="wbdd"),
            pn=None, serials=cast(literal("[]"), JSONB), source_sn=None, quantity=None, demand_ref=_json(id=o.raw_order_id, number=o.order_no),
            fields=[("express_no", "evidence", o.express_no, False), ("express_no2", "evidence", o.express_no2, False)],
            conditions=[a.project_id.in_(project_ids), o.data_status == "已生效", _valid_demand()],
        ))
    if query.entity in ("all", "bad_return"):
        h, l = MaintenanceBadReturn, MaintenanceBadReturnLine
        pns = select(func.jsonb_agg(l.pn)).where(l.return_id == h.return_id).correlate(h).scalar_subquery()
        qty = select(func.sum(l.quantity)).where(l.return_id == h.return_id).correlate(h).scalar_subquery()
        result.append(_branch(query, "bad_return", select(h), record_id=h.return_id, project_id=h.project_id, date=h.created_at,
            document=_json(id=h.return_id, number=h.return_no, display_number=None, date=h.created_at, status=h.status, source="bad_return"),
            pn=pns, serials=cast(literal("[]"), JSONB), source_sn=None, quantity=qty, demand_ref=None,
            fields=[("pn", "pn", pns, True), ("logistics_reference", "evidence", h.logistics_reference, False), ("note", "remark", h.note, False)],
            conditions=[h.project_id.in_(project_ids), h.status.in_(query.statuses or ("submitted", "in_transit", "warehouse_confirmed"))],
        ))
    return result


def _search(db, query, ctx):
    unavailable = ["issue"] if is_field_hidden(ctx, "unit_cost") else []
    project_ids = _visible_projects(db, query, ctx)
    statements = movement_statements(query, project_ids, include_issue=not unavailable)
    items = []
    if project_ids and statements:
        combined = union_all(*statements).subquery()
        rows = db.execute(select(combined.c.payload, combined.c.rank).order_by(
            combined.c.rank, combined.c.business_date.desc().nulls_last(), combined.c.entity, combined.c.record_id,
        ).offset((query.page - 1) * query.page_size).limit(query.page_size + 1)).all()
        for payload, rank in rows:
            fields = payload.pop("fields")
            payload["matched_fields"] = [f for f in payload["matched_fields"] if f]
            payload["serials"] = [s for s in payload["serials"] if s]
            if isinstance(payload["pn"], list):
                payload["pn"] = " / ".join(dict.fromkeys(payload["pn"]))
            payload["match_kind"] = ("exact", "compact", "literal")[rank]
            excerpt = "；".join(f"{_FIELD_LABELS[name]}: {fields[name]}" for name in payload["matched_fields"])
            payload["excerpt"] = excerpt[:240]
            payload["truncated"] = len(excerpt) > 240
            payload["evidence"] = {key: fields[key] for key in ("express_no", "express_no2", "logistics_reference") if key in fields}
            entity = payload["entity_type"]
            payload["record_ref"] = {
                "project_id": payload["project"]["id"],
                "tab": "parts-orders" if entity == "demand_order" else "site",
                {"issue": "issue_line_id", "receipt": "receipt_id", "demand_order": "source_order_id", "bad_return": "return_id"}[entity]: payload["record_id"],
            }
            items.append(payload)
    return {
        "items": items[:query.page_size], "has_more": len(items) > query.page_size,
        "page": query.page, "page_size": query.page_size,
        "scope": "当前账号有权访问的有效维保项目", "unavailable_entities": unavailable,
        "as_of": datetime.now(UTC).isoformat(),
    }


def search_movements(db: Session, query: MovementSearchQuery, user_ctx: UserContext):
    if not page_allowed(user_ctx, "page_maintenance"):
        raise HTTPException(403, "无权访问维保页面")
    if not _SLOTS.acquire(blocking=False):
        raise HTTPException(429, "搜索繁忙，请稍后重试")
    try:
        # 用保存点恢复超时异常，不让调用方误将失败解释为没有命中。
        with db.begin_nested():
            previous = db.scalar(select(func.current_setting("statement_timeout")))
            db.execute(select(func.set_config("statement_timeout", "5000", True)))
            result = _search(db, query, user_ctx)
            db.execute(select(func.set_config("statement_timeout", previous, True)))
            return result
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "57014":
            raise HTTPException(503, "搜索超时，请缩小范围后重试") from exc
        raise
    finally:
        _SLOTS.release()
