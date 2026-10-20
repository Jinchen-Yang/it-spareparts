"""Read-only WBDD source reconciliation (D-27).

The archived source membership is immutable. Sequence numbers only bound each
day's order search; raw identities decide membership. No mutation happens here.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import re

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models.maintenance import FMaintenanceLine as L, FMaintenanceOrder as O
from app.models.maintenance import MaintenanceDemandTombstone, MaintenanceManualCostOverride
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import MaintenanceSiteIssue as I, MaintenanceSiteIssueLine as IL
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment as A
from app.models.system import SysImportBatch
from app.services import maintenance_wbdd_source_snapshot as source_snapshot
from app.services.maintenance_field_protection import is_manual_created

SNAPSHOT_KEY = "_wbdd_source_snapshot"
MAX_CANDIDATES = 1000
_ORDER_NO = re.compile(r"^WBDD-(\d{8})-(\d{4})$")
_MANUAL_ORDER_PREFIXES = ("page-manual-", "workbook-create-")


def _json(value):
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        # ORM values before/after expiry may differ only in NUMERIC scale.
        return format(value.normalize(), "f") if value else "0"
    return value


def _entity(entity):
    return {column.name: _json(getattr(entity, column.name))
            for column in entity.__table__.columns}


def _digest(value):
    return hashlib.sha256(json.dumps(_json(value), ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _time(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def is_manual_line(line):
    """An edited imported line keeps its raw identity; a created line does not."""
    return is_manual_created(line.raw_line_id)


def related_site_lines(db, *, orders, lines, include_order_references=False):
    """Exact source identity and current project, never PN/quantity guessing."""
    predicates = []
    if include_order_references:
        for order, assignment, _project in orders.values():
            if assignment is not None:
                predicates.append(and_(I.project_id == assignment.project_id,
                                       IL.source_order_id == order.raw_order_id))
    for line in lines:
        entry = orders.get(line.order_id)
        if entry is not None and entry[1] is not None:
            predicates.append(and_(I.project_id == entry[1].project_id,
                                   IL.source_line_id == line.raw_line_id))
    if not predicates:
        return []
    return list(db.execute(select(IL, I).join(I, I.issue_id == IL.issue_id)
                           .where(or_(*predicates)).order_by(IL.issue_line_id)))


def _base(batch, snapshot):
    uploaded_at = getattr(batch, "uploaded_at", None)
    return {"readiness": "ready", "batch_id": getattr(batch, "id", None),
            "uploaded_at": _json(uploaded_at or snapshot.get("captured_at")),
            "missing_count": 0, "missing_line_count": 0, "total_candidate_count": 0,
            "unmatched_site_line_count": 0,
            "missing_orders": [], "missing_lines": [], "truncated": False,
            "coverage": {"daily_windows": [], "source_order_count": 0,
                         "blocked_order_count": 0},
            "warnings": [], "candidate_digest": None}


def build_candidates(db: Session, batch, *, allowed_project_ids=None,
                     limit=MAX_CANDIDATES):
    """Build bounded candidates from a batch or a direct captured snapshot dict.

    The digest covers all current fields of returned candidates, overrides,
    assignments and related issues, so confirmation can recheck under locks.
    Missing legacy snapshots never fall back to the old date-window algorithm.
    """
    snapshot = (batch if isinstance(batch, dict) and "version" in batch else
                (getattr(batch, "report_json", None) or {}).get(SNAPSHOT_KEY))
    result = _base(batch, snapshot or {})
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
        result.update(readiness="snapshot_required", warnings=["历史上传没有原件身份快照，请重新上传原件后核对。"])
        return result
    captured_at = _time(snapshot.get("captured_at")) or _time(getattr(batch, "uploaded_at", None))
    if captured_at is None or not isinstance(snapshot.get("orders"), list):
        result.update(readiness="snapshot_required", warnings=["原件身份快照缺少可靠时点或成员，请重新上传。"])
        return result
    if snapshot.get("blocked_reasons"):
        result.update(readiness="blocked", warnings=["原件身份存在错误，不能据此判断缺项。"])
        result["coverage"]["blocked_reasons"] = list(snapshot["blocked_reasons"])
        return result
    if snapshot.get("mode") == "skip":
        result["warnings"].append("本次选择跳过已有记录，不提供同步作废候选；请使用更新模式重新上传。")
        result["candidate_digest"] = _digest({"snapshot": snapshot, "mode": "skip"})
        return result
    members = {row["raw_order_id"]: row for row in snapshot["orders"]}
    result["coverage"]["source_order_count"] = len(members)
    blocked = {key for key, row in members.items() if row.get("blocked_reasons")}
    result["coverage"]["blocked_order_count"] = len(blocked)
    if blocked:
        result["warnings"].append(f"{len(blocked)} 张单据存在身份或数据错误，已停止这些单据的缺行判断。")
    windows = source_snapshot.daily_order_windows(snapshot)
    result["coverage"]["daily_windows"] = windows
    result["warnings"].append("仅比较列出的每日单号区间和本文件出现单据的明细；局部导出也可能产生缺项，请核对后确认。")
    clauses = []
    for window in windows:
        day = window["date"].replace("-", "")
        clauses.append(O.order_no.between(f"WBDD-{day}-{window['from_sequence']:04d}",
                                         f"WBDD-{day}-{window['to_sequence']:04d}"))
    clauses.append(O.raw_order_id.in_(members))
    query = select(O, A, MaintenanceProject).outerjoin(
        A, and_(A.source_order_id == O.raw_order_id, A.is_active.is_(True))).outerjoin(
        MaintenanceProject, MaintenanceProject.project_id == A.project_id).where(
        or_(*clauses), O.data_status == "已生效", O.created_at <= captured_at,
        ~O.raw_order_id.startswith(_MANUAL_ORDER_PREFIXES[0]),
        ~O.raw_order_id.startswith(_MANUAL_ORDER_PREFIXES[1]),
        ~select(MaintenanceDemandTombstone.source_order_id).where(
            MaintenanceDemandTombstone.source_order_id == O.raw_order_id,
            MaintenanceDemandTombstone.restored_at.is_(None)).exists())
    if allowed_project_ids is not None:
        query = query.where(A.project_id.in_(allowed_project_ids))
    orders = {order.id: (order, assignment, project)
              for order, assignment, project in db.execute(query.order_by(O.order_no, O.raw_order_id))}
    if not orders:
        result["candidate_digest"] = _digest({"snapshot": snapshot, "scope": sorted(allowed_project_ids) if allowed_project_ids is not None else None,
                                              "orders": [], "lines": []})
        return result
    all_lines = list(db.scalars(select(L).where(L.order_id.in_(orders)).order_by(L.id)))
    batches = {row.id: row for row in db.scalars(select(SysImportBatch).where(
        SysImportBatch.id.in_({order.import_batch_id for order, _, _ in orders.values()}
                             | {line.import_batch_id for line in all_lines})))}
    snapshot_batch_id = getattr(batch, "id", None) or snapshot.get("batch_id")

    def before_snapshot(batch_id):
        source = batches.get(batch_id)
        return (source is not None and _time(source.uploaded_at) <= captured_at
                and (snapshot_batch_id is None or source.id <= snapshot_batch_id))

    per_order = {}
    for line in all_lines:
        per_order.setdefault(line.order_id, []).append(line)
    raw_present = set(snapshot.get("line_ids", ()))
    missing_orders, missing_lines, protected_orders = [], [], 0
    for order, assignment, project in orders.values():
        if not before_snapshot(order.import_batch_id):
            continue
        active = [line for line in per_order.get(order.id, ()) if line.is_active]
        if order.raw_order_id not in members:
            match = _ORDER_NO.fullmatch(order.order_no)
            if match is None:
                continue
            try:
                date.fromisoformat(f"{match[1][:4]}-{match[1][4:6]}-{match[1][6:]}")
            except ValueError:
                continue
            # A whole-order tombstone must not hide a later or manually created row.
            if any(is_manual_line(line) or not before_snapshot(line.import_batch_id) for line in active):
                protected_orders += 1
                continue
            missing_orders.append(order)
        elif order.raw_order_id not in blocked:
            present = set(members[order.raw_order_id].get("line_ids", ())) | raw_present
            missing_lines.extend(line for line in active if line.raw_line_id not in present
                                 and not is_manual_line(line) and before_snapshot(line.import_batch_id))
    if protected_orders:
        result["warnings"].append(f"{protected_orders} 张缺失单含人工新增或上传后新增明细，整单已保留。")
    result["missing_count"] = len(missing_orders)
    result["missing_line_count"] = len(missing_lines)
    result["total_candidate_count"] = len(missing_orders) + len(missing_lines)
    limit = max(1, min(int(limit), MAX_CANDIDATES))
    shown_orders = missing_orders[:limit]
    shown_lines = missing_lines[:max(0, limit - len(shown_orders))]
    result["truncated"] = result["total_candidate_count"] > limit
    shown_order_ids = {order.id for order in shown_orders} | {line.order_id for line in shown_lines}
    shown_context = {key: orders[key] for key in shown_order_ids}
    evidence_lines = [line for line in all_lines if line.order_id in {order.id for order in shown_orders}
                      or line.id in {item.id for item in shown_lines}]
    site_rows = related_site_lines(db, orders=shown_context, lines=evidence_lines,
                                   include_order_references=True)

    def assignment_fields(order):
        assignment, project = orders[order.id][1:]
        return {"assigned_project_id": assignment.project_id if assignment else None,
                "project_name": project.display_name if project else None,
                "assignment_id": assignment.assignment_id if assignment else None}

    def linked_count(order, line=None):
        assignment = orders[order.id][1]
        if assignment is None:
            return 0
        return sum(1 for sl, issue in site_rows
                   if issue.project_id == assignment.project_id and sl.is_active
                   and issue.normalized_status != "void"
                   and ((sl.source_line_id in
                         {entry.raw_line_id for entry in per_order.get(order.id, ()) if entry.is_active}) if line is None
                        else sl.source_line_id == line.raw_line_id))

    def unmatched_ids(order):
        assignment = orders[order.id][1]
        known_ids = {entry.raw_line_id for entry in per_order.get(order.id, ())}
        return {sl.issue_line_id for sl, issue in site_rows if assignment is not None
                and issue.project_id == assignment.project_id and sl.is_active
                and issue.normalized_status != "void" and sl.source_order_id == order.raw_order_id
                and sl.source_line_id not in known_ids}

    unmatched = set().union(*(unmatched_ids(orders[key][0]) for key in shown_order_ids))
    result["unmatched_site_line_count"] = len(unmatched)
    if unmatched:
        result["warnings"].append(f"无法精确关联，{len(unmatched)}条领用将保留；请另行核对这些领用记录。")

    result["missing_orders"] = [{"source_order_id": order.raw_order_id, "order_no": order.order_no,
        "order_date": _json(order.order_date), "line_count": sum(line.is_active for line in per_order.get(order.id, ())),
        "linked_site_line_count": linked_count(order), "unmatched_site_line_count": len(unmatched_ids(order)),
        **assignment_fields(order)} for order in shown_orders]
    result["missing_lines"] = [{"line_id": line.id, "raw_line_id": line.raw_line_id,
        "source_order_id": orders[line.order_id][0].raw_order_id,
        "order_no": orders[line.order_id][0].order_no, "pn": line.pn_std or line.pn_raw,
        "qty": _json(line.qty), "linked_site_line_count": linked_count(orders[line.order_id][0], line),
        "unmatched_site_line_count": len(unmatched_ids(orders[line.order_id][0])),
        **assignment_fields(orders[line.order_id][0])} for line in shown_lines]
    overrides = list(db.scalars(select(MaintenanceManualCostOverride).where(
        MaintenanceManualCostOverride.line_id.in_([line.id for line in evidence_lines])))) if evidence_lines else []
    result["candidate_digest"] = _digest({"snapshot": snapshot, "scope": sorted(allowed_project_ids) if allowed_project_ids is not None else None,
        "counts": [result["missing_count"], result["missing_line_count"]],
        "orders": [_entity(orders[key][0]) for key in sorted(shown_order_ids)],
        "assignments": [_entity(orders[key][1]) if orders[key][1] else None for key in sorted(shown_order_ids)],
        "lines": [_entity(line) for line in evidence_lines],
        "overrides": [_entity(row) for row in sorted(overrides, key=lambda row: row.id)],
        "site_lines": [{"line": _entity(line), "issue": _entity(issue)} for line, issue in site_rows]})
    return result
