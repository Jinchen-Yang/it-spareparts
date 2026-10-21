"""D-0022: exact reversal of one confirmed source cancellation (D-34).

Restore is bounded by the confirmation receipt's restoration snapshot: only
objects voided by that confirmation come back, any drift from the recorded
after-state refuses the whole command with zero writes, and receipts written
before this feature existed are explicitly non-restorable (never guessed).
"""
from datetime import UTC, datetime

from sqlalchemy import func, literal_column, select, text
from sqlalchemy.orm import Session

from app.models.maintenance import (
    FMaintenanceLine as L,
)
from app.models.maintenance import (
    FMaintenanceOrder as O,
)
from app.models.maintenance import (
    MaintenanceDemandTombstone as T,
)
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import MaintenanceSiteIssue as I
from app.models.maintenance_project_operations import (
    MaintenanceSiteIssueLine as IL,
)
from app.models.maintenance_project_operations import (
    MaintenanceProjectWorkbookState,
)
from app.models.maintenance_source_assignment import (
    MaintenanceSourceOrderAssignment as A,
)
from app.models.system import SysImportBatch
from app.services import maintenance_demands as demands
from app.services import maintenance_project_operations as operations
from app.services import maintenance_site_return_requirements as requirements
from app.services.maintenance_source_cancel_cascade import (
    RESTORATION_KEY,
    RESTORATION_SCHEMA,
    RESULTS_KEY,
)
from app.services.maintenance_wbdd_reconciliation import _digest, _entity
from app.services.maintenance_wbdd_sync import _audit

RESTORE_RESULTS_KEY = "_source_cancel_restorations"
# 并发/时间戳列只增不减：判断「已被恢复」时忽略，判断「仍在作废态」时全列比对。
_VOLATILE_COLUMNS = {"version", "updated_at"}


class RestorationConflict(Exception):
    """Drift from the receipt snapshot, or a receipt that cannot be restored."""

    def __init__(self, message, *, conflicts=None, unavailable=False):
        super().__init__(message)
        self.conflicts = conflicts or []
        self.unavailable = unavailable


def _time(value):
    if value is None:
        return None
    return datetime.fromisoformat(str(value))


def _confirmation_entries(db: Session, *, project_id, source_order_id=None):
    """All confirmation receipts of this project (optionally one source order)."""
    order_filter = "" if source_order_id is None else (
        "AND e.value -> 'result' ->> 'source_order_id' = :source_order_id")
    statement = text(
        f"SELECT b.id AS batch_id, e.key AS receipt_key, e.value AS entry "
        f"FROM sys_import_batch b, jsonb_each("
        f"COALESCE(b.report_json -> '{RESULTS_KEY}', '{{}}'::jsonb)) AS e "
        f"WHERE e.value ->> 'project_id' = :project_id {order_filter} "
        f"ORDER BY b.id DESC")
    rows = db.execute(statement, {"project_id": project_id,
                                  "source_order_id": source_order_id}).mappings().all()
    return [{"batch_id": row["batch_id"], "receipt_key": row["receipt_key"],
             "entry": row["entry"]} for row in rows
            if isinstance(row["entry"], dict) and isinstance(row["entry"].get("result"), dict)]


def _latest_confirmations(db: Session, *, project_id, source_order_id=None):
    """Newest receipt per source order: re-void cycles supersede older receipts."""
    latest = {}
    for receipt in _confirmation_entries(db, project_id=project_id,
                                         source_order_id=source_order_id):
        latest.setdefault(receipt["entry"]["result"]["source_order_id"], receipt)
    return list(latest.values())


def _restore_receipts(db: Session, batch_ids, *, replay_key=None,
                      confirmation_key=None, project_id=None):
    """Restore receipts live in the confirmation batch's report_json."""
    found = []
    for batch_id in sorted(set(batch_ids)):
        entries = (db.get(SysImportBatch, batch_id).report_json or {}).get(
            RESTORE_RESULTS_KEY) or {}
        for key, entry in (entries or {}).items():
            if not isinstance(entry, dict):
                continue
            if replay_key is not None and key != replay_key:
                continue
            if confirmation_key is not None and entry.get("confirmation_key") != confirmation_key:
                continue
            if project_id is not None and entry.get("project_id") != project_id:
                continue
            found.append({"batch_id": batch_id, "replay_key": key, "entry": entry})
    return found


def _restore_batch_for_replay(db: Session, replay_key):
    """Find a restore receipt by idempotency key across every batch.

    Mirrors the confirmation-side global receipt query: a key belongs to one
    request, not to the batch that happens to be retried, so cross-source-order
    reuse must be detected before any write.
    """
    return db.scalar(select(SysImportBatch).where(
        SysImportBatch.report_json.op("->")(
            literal_column(f"'{RESTORE_RESULTS_KEY}'")
        ).op("?")(replay_key)))


def _receipt_from_db(db: Session, *, project_id, source_order_id, batch_id, receipt_key):
    """Re-read the confirmation receipt after locks; never reuse a pre-lock copy."""
    for row in _confirmation_entries(db, project_id=project_id,
                                     source_order_id=source_order_id):
        if row["batch_id"] == batch_id and row["receipt_key"] == receipt_key:
            return row
    raise RestorationConflict(
        "确认回执已变化或无法读取，本次未恢复，请刷新预览后重新核对", unavailable=True)


def _entity_state(conflicts, *, kind, label, ident, current, after, before,
                  volatile=frozenset()):
    """Classify one entity: still voided / restored / drifted from the snapshot."""
    if current is None or after is None:
        conflicts.append({"kind": kind, "id": str(ident), "reason": f"{label}已不存在或快照缺失"})
        return "missing"
    if current == after:
        return "void"
    if before is not None and all(current.get(key) == before.get(key)
                                  for key in before if key not in volatile):
        return "restored"
    conflicts.append({"kind": kind, "id": str(ident),
                      "reason": f"{label}已被改动或重新导入，与确认作废时的快照不一致"})
    return "drift"


_BEFORE_KEYS = {
    "demand_line": ("is_active", "voided_at", "voided_by", "void_reason"),
    "site_line": ("is_active",),
    "site_issue": ("normalized_status", "raw_status", "status_mapping_state",
                   "status_mapping_version", "voided_at"),
}


def _valid_time(value):
    """Snapshot timestamps must be null or ISO-8601 text; _time must not raise later."""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        _time(value)
        return True
    except ValueError:
        return False


def _optional_text(value):
    return value is None or isinstance(value, str)


def _snapshot_problem(restoration, confirm_result, project_id):
    """Return a reason when the embedded snapshot cannot drive an exact restore.

    Returns ``None`` only when every member recorded at confirmation time is
    present, typed, uniquely identified, owned by the receipt's source order and
    matches the counts recorded on the confirmation result. A structurally
    incomplete snapshot must never be mistaken for a smaller restore set.
    """
    if not isinstance(restoration, dict):
        return "回执恢复快照不是对象"
    if restoration.get("schema") != RESTORATION_SCHEMA:
        return "回执恢复快照版本不受支持"
    required = {"order": dict, "demand_lines": list, "site_lines": list,
                "site_issues": list, "assignments": list}
    for key, kind in required.items():
        if key not in restoration:
            return f"回执恢复快照缺少 {key}"
        if not isinstance(restoration[key], kind):
            return f"回执恢复快照的 {key} 类型不正确"
    if "tombstone" not in restoration:
        return "回执恢复快照缺少 tombstone"
    if restoration["tombstone"] is not None and not isinstance(restoration["tombstone"], dict):
        return "回执恢复快照的 tombstone 类型不正确"
    for row in restoration["assignments"]:
        if not isinstance(row, dict) or not isinstance(row.get("assignment_id"), str):
            return "回执恢复快照的挂靠成员缺少 assignment_id"

    order_id = restoration["order"].get("id")
    if not isinstance(order_id, int) or isinstance(order_id, bool):
        return "回执恢复快照的来源单头缺少 id"
    confirmed_at = confirm_result.get("confirmed_at")
    confirm_time = (_time(confirmed_at)
                    if confirmed_at is not None and _valid_time(confirmed_at) else None)

    demand_ids = set()
    for row in restoration["demand_lines"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), int):
            return "回执恢复快照的需求行缺少 id"
        if row["id"] in demand_ids:
            return "回执恢复快照的需求行重复"
        demand_ids.add(row["id"])
        before, after = row.get("before"), row.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            return "回执恢复快照的需求行缺少 before/after"
        if any(key not in before for key in _BEFORE_KEYS["demand_line"]):
            return "回执恢复快照的需求行 before 字段不完整"
        if not isinstance(before["is_active"], bool) \
                or not _valid_time(before["voided_at"]) \
                or not _optional_text(before["voided_by"]) \
                or not _optional_text(before["void_reason"]):
            return "回执恢复快照的需求行 before 取值不合法"
        # 确认只快照活跃行，随后统一置为作废；before/after 必须证明是这一代作废，
        # 否则「条数不变、成员换成别的行」会伪装成可恢复。
        if before.get("id") != row["id"] or after.get("id") != row["id"]:
            return "回执恢复快照的需求行 id 与快照不一致"
        if before["is_active"] is not True or after.get("is_active") is not False:
            return "回执恢复快照的需求行 before/after 活跃状态不合法"
        if any(before[key] is not None for key in ("voided_at", "voided_by", "void_reason")):
            return "回执恢复快照的需求行 before 带有作废痕迹"
        if before.get("order_id") != order_id or after.get("order_id") != order_id:
            return "回执恢复快照的需求行不属于来源单头"
        if row.get("raw_line_id") != after.get("raw_line_id"):
            return "回执恢复快照的需求行与来源明细不一致"
        if confirm_time is None or not _valid_time(after.get("voided_at")) \
                or _time(after["voided_at"]) != confirm_time:
            return "回执恢复快照的需求行作废时间与确认回执不一致"

    issue_ids = set()
    for row in restoration["site_issues"]:
        if not isinstance(row, dict) or not isinstance(row.get("issue_id"), str):
            return "回执恢复快照的领用单头缺少 issue_id"
        if not isinstance(row.get("voided"), bool):
            return "回执恢复快照的领用单头缺少 voided 标记"
        if row["issue_id"] in issue_ids:
            return "回执恢复快照的领用单头重复"
        issue_ids.add(row["issue_id"])
        before, after = row.get("before"), row.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            return "回执恢复快照的领用单头缺少 before/after"
        if any(key not in before for key in _BEFORE_KEYS["site_issue"]):
            return "回执恢复快照的领用单头 before 字段不完整"
        if not all(_optional_text(before[key]) for key in _BEFORE_KEYS["site_issue"]
                   if key != "voided_at") or not _valid_time(before["voided_at"]):
            return "回执恢复快照的领用单头 before 取值不合法"
        if before.get("issue_id") != row["issue_id"] or after.get("issue_id") != row["issue_id"]:
            return "回执恢复快照的领用单头 id 与快照不一致"
        if before.get("project_id") != project_id or after.get("project_id") != project_id:
            return "回执恢复快照的领用单头不属于本项目"
        if row["voided"]:
            if before.get("normalized_status") == "void":
                return "回执恢复快照的领用单头 before 已是作废状态"
            if after.get("normalized_status") != "void" or after.get("raw_status") != "void" \
                    or after.get("status_mapping_state") != "mapped" \
                    or after.get("status_mapping_version") != "site-issue-v2-workflow-v1":
                return "回执恢复快照的领用单头 after 不是确认作废状态"
            if confirm_time is None or not _valid_time(after.get("voided_at")) \
                    or _time(after["voided_at"]) != confirm_time:
                return "回执恢复快照的领用单头作废时间与确认回执不一致"
        elif any(after.get(key) != before.get(key) for key in _BEFORE_KEYS["site_issue"]):
            return "回执恢复快照的未作废领用单头 before/after 不一致"

    site_line_ids = set()
    for row in restoration["site_lines"]:
        if not isinstance(row, dict) or not isinstance(row.get("issue_line_id"), str):
            return "回执恢复快照的领用行缺少 issue_line_id"
        if row["issue_line_id"] in site_line_ids:
            return "回执恢复快照的领用行重复"
        site_line_ids.add(row["issue_line_id"])
        before, after = row.get("before"), row.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            return "回执恢复快照的领用行缺少 before/after"
        if any(key not in before for key in _BEFORE_KEYS["site_line"]):
            return "回执恢复快照的领用行 before 字段不完整"
        if not isinstance(before["is_active"], bool):
            return "回执恢复快照的领用行 before 取值不合法"
        if not isinstance(row.get("issue_id"), str) or row["issue_id"] not in issue_ids:
            return "回执恢复快照的领用行不属于任何记录的领用单头"
        if before.get("issue_line_id") != row["issue_line_id"] \
                or after.get("issue_line_id") != row["issue_line_id"] \
                or before.get("issue_id") != row["issue_id"] \
                or after.get("issue_id") != row["issue_id"]:
            return "回执恢复快照的领用行 id 与快照不一致"
        if before["is_active"] is not True or after.get("is_active") is not False:
            return "回执恢复快照的领用行 before/after 活跃状态不合法"

    count_pairs = (
        ("需求行", len(restoration["demand_lines"]), confirm_result.get("voided_lines")),
        ("领用行", len(restoration["site_lines"]), confirm_result.get("voided_site_lines")),
        ("领用单头", sum(1 for row in restoration["site_issues"] if row["voided"]),
         confirm_result.get("voided_site_issues")),
    )
    for label, actual, recorded in count_pairs:
        if not isinstance(recorded, int) or isinstance(recorded, bool):
            return "确认回执缺少作废数量，无法核对快照完整性"
        if actual != recorded:
            return f"回执恢复快照的{label}数量（{actual}）与确认回执（{recorded}）不一致"
    return None


def _evaluate(db: Session, *, project_id, receipt):
    """Classify the world against one confirmation receipt; never writes.

    Returns ``(info, restoration, corrupt)``. ``corrupt`` distinguishes a
    malformed snapshot (must be a hard 4xx on preview/execute) from a legacy
    receipt that predates restoration snapshots (listed as unavailable).
    """
    entry = receipt["entry"]
    confirm_result = entry["result"]
    source_order_id = confirm_result["source_order_id"]
    restoration = entry.get(RESTORATION_KEY)
    # 展示用单号：优先实况单头，历史确认（无快照）退回确认时的快照。
    order = db.scalar(select(O).where(O.raw_order_id == source_order_id))
    snapshot_order = restoration.get("order") if isinstance(restoration, dict) else None
    snapshot_order = snapshot_order if isinstance(snapshot_order, dict) else {}
    info = {
        "batch_id": receipt["batch_id"],
        "source_order_id": source_order_id,
        "order_no": order.order_no if order else snapshot_order.get("order_no"),
        "confirmed_at": confirm_result.get("confirmed_at"),
        "confirmed_by": entry.get("operated_by"),
        "confirm_reason": entry.get("reason"),
        "voided_lines": confirm_result.get("voided_lines", 0),
        "voided_site_lines": confirm_result.get("voided_site_lines", 0),
        "voided_site_issues": confirm_result.get("voided_site_issues", 0),
        "warnings": list(confirm_result.get("warnings") or []),
        "conflicts": [],
        "restored": False,
        "restore_receipt": None,
        "state": None,
        "expected_digest": None,
    }
    if restoration is None:
        info["state"] = "unavailable"
        info["conflicts"] = [{
            "kind": "receipt", "id": str(source_order_id),
            "reason": "回执缺少恢复前置快照（本功能上线前的历史确认），不可自动恢复；"
                      "需人工按实名更正流程处理并列入待决策项"}]
        return info, None, False
    problem = _snapshot_problem(restoration, confirm_result, project_id)
    if problem is not None:
        info["state"] = "unavailable"
        info["conflicts"] = [{
            "kind": "receipt", "id": str(source_order_id),
            "reason": f"{problem}，不可自动恢复；需人工按实名更正流程处理"}]
        return info, None, True

    conflicts = []
    _entity_state(conflicts, kind="order", label="来源单头", ident=source_order_id,
                  current=_entity(order) if order else None,
                  after=restoration.get("order"), before=None)
    project = db.get(MaintenanceProject, project_id)
    if project is None:
        conflicts.append({"kind": "project", "id": str(project_id), "reason": "项目不存在"})
    elif not project.is_active:
        conflicts.append({"kind": "project", "id": str(project_id), "reason": "项目已停用"})

    demand_states, site_states, issue_states = [], [], []
    for row in restoration.get("demand_lines") or []:
        line = db.get(L, row["id"])
        demand_states.append(_entity_state(
            conflicts, kind="demand_line", label="需求行", ident=row["id"],
            current=_entity(line) if line else None,
            after=row.get("after"), before=row.get("before")))
    for row in restoration.get("site_lines") or []:
        line = db.get(IL, row["issue_line_id"])
        site_states.append(_entity_state(
            conflicts, kind="site_issue_line", label="领用行", ident=row["issue_line_id"],
            current=_entity(line) if line else None,
            after=row.get("after"), before=row.get("before"), volatile=_VOLATILE_COLUMNS))
    issue_entities = {}
    for row in restoration.get("site_issues") or []:
        issue = db.get(I, row["issue_id"])
        issue_entities[row["issue_id"]] = issue
        issue_states.append(_entity_state(
            conflicts, kind="site_issue", label="领用单头", ident=row["issue_id"],
            current=_entity(issue) if issue else None,
            after=row.get("after"), before=row.get("before"),
            volatile=_VOLATILE_COLUMNS))

    tombstone = db.get(T, source_order_id)
    tomb_current = _entity(tombstone) if tombstone else None
    tomb_after = restoration.get("tombstone")
    if tomb_current is not None and tomb_current == tomb_after:
        tomb_state = "void"
    elif tombstone is not None and tombstone.restored_at is not None:
        tomb_state = "restored"
    else:
        tomb_state = "drift"
        conflicts.append({"kind": "tombstone", "id": str(source_order_id),
                          "reason": "墓碑状态与确认作废时不一致（可能已被再次作废换代）"})

    assignments = list(db.scalars(select(A).where(A.source_order_id == source_order_id)
                                  .order_by(A.assignment_id)))
    if any(row.is_active for row in assignments):
        conflicts.append({"kind": "assignment", "id": str(source_order_id),
                          "reason": "来源单已重新挂靠到项目（来源再现），不能自动恢复"})
    stored_assignments = {row.get("assignment_id"): row for row in restoration.get("assignments") or []}
    for row in assignments:
        stored = stored_assignments.get(row.assignment_id)
        if stored is None or _entity(row) != stored:
            conflicts.append({"kind": "assignment", "id": str(row.assignment_id),
                              "reason": "挂靠关系与确认作废时不一致"})

    # 展示清单：只描述快照中「本次确认作废」的对象。
    info["restorable_demands"] = [
        {"line_id": row["id"], "raw_line_id": row.get("raw_line_id"),
         "pn": (row.get("before") or {}).get("pn_std") or (row.get("before") or {}).get("pn_raw"),
         "qty": _display((row.get("before") or {}).get("qty"))}
        for row in restoration.get("demand_lines") or []]
    info["restorable_site_lines"] = [
        {"issue_line_id": row["issue_line_id"], "issue_id": row.get("issue_id"),
         "issue_no": _issue_display(db, issue_entities, row),
         "pn": (row.get("before") or {}).get("pn"),
         "quantity": _display((row.get("before") or {}).get("quantity"))}
        for row in restoration.get("site_lines") or []]
    info["restorable_site_issues"] = [
        {"issue_id": row["issue_id"], "issue_no": _issue_display(db, issue_entities, row),
         "voided_status": "void",
         "original_status": (row.get("before") or {}).get("normalized_status")}
        for row in restoration.get("site_issues") or [] if row.get("voided")]

    all_receipts = _confirmation_entries(db, project_id=project_id,
                                         source_order_id=source_order_id)
    matches = _restore_receipts(db, [item["batch_id"] for item in all_receipts],
                                confirmation_key=receipt["receipt_key"], project_id=project_id)
    entity_states = demand_states + site_states + issue_states
    if conflicts:
        info["state"] = "conflict"
    elif tomb_state == "void" and all(state == "void" for state in entity_states):
        info["state"] = "restorable"
    elif tomb_state == "restored" and all(state == "restored" for state in entity_states):
        # 单头单（无成员）也必须有确定状态：墓碑已恢复且没有行可核对时即 restored，
        # 否则恢复成功后再预览会被误判 conflict、新键重试返回 409。
        info["state"] = "restored"
        info["restored"] = True
        if matches:
            match = matches[0]
            info["restore_receipt"] = {
                "batch_id": match["batch_id"],
                "replay_key": match["replay_key"],
                "operated_by": match["entry"].get("operated_by"),
                "restored_at": (match["entry"].get("result") or {}).get("restored_at"),
                "result": match["entry"].get("result"),
            }
    else:
        info["state"] = "conflict"
        conflicts.append({"kind": "tombstone", "id": str(source_order_id),
                          "reason": "墓碑与行的恢复状态不一致（可能被部分人工恢复），不能自动恢复"})
    info["conflicts"] = conflicts
    if info["state"] in {"restorable", "restored"}:
        info["expected_digest"] = _digest({
            "receipt": {"batch_id": receipt["batch_id"],
                        "receipt_key": receipt["receipt_key"],
                        "confirmed_at": confirm_result.get("confirmed_at")},
            "state": info["state"],
            "current": {
                "order": _entity(order) if order else None,
                "demand_lines": [_entity(db.get(L, row["id"]))
                                 for row in restoration.get("demand_lines") or []],
                "site_lines": [_entity(db.get(IL, row["issue_line_id"]))
                               for row in restoration.get("site_lines") or []],
                "site_issues": [_entity(db.get(I, row["issue_id"]))
                                for row in restoration.get("site_issues") or []],
                "tombstone": tomb_current,
                "assignments": [_entity(row) for row in assignments],
            },
            # 摘要必须绑定完整恢复目标：before 内容被篡改时旧摘要立即失效。
            "restoration": restoration,
        })
    return info, restoration, False


def _display(value):
    return None if value is None else str(value)


def _issue_display(db, issue_entities, row):
    issue = issue_entities.get(row.get("issue_id")) or db.get(I, row.get("issue_id"))
    if issue is not None:
        return issue.display_no_effective or issue.issue_no
    return (row.get("before") or {}).get("issue_no")


def _context(db, project_id):
    state = db.get(MaintenanceProjectWorkbookState, project_id)
    return {"revision": state.revision if state else None}


def list_restorations(db: Session, *, project_id):
    """Per-project confirmation receipts with their current restoration state."""
    items = []
    for receipt in _latest_confirmations(db, project_id=project_id):
        info, _restoration, _corrupt = _evaluate(db, project_id=project_id, receipt=receipt)
        info["context"] = _context(db, project_id)
        items.append(info)
    items.sort(key=lambda row: (row.get("confirmed_at") or ""), reverse=True)
    return {"items": items, "total": len(items)}


def restoration_preview(db: Session, *, project_id, source_order_id):
    """What a restore of the latest confirmation would and would not bring back."""
    receipts = _confirmation_entries(db, project_id=project_id,
                                     source_order_id=source_order_id)
    if not receipts:
        raise RestorationConflict(
            f"未找到来源单 {source_order_id} 在本项目的人工取消确认回执", unavailable=True)
    info, _restoration, corrupt = _evaluate(db, project_id=project_id, receipt=receipts[0])
    if corrupt:
        # 快照损坏不是「历史回执不可恢复」：必须明确 4xx，绝不能按缺省成员执行。
        raise RestorationConflict(info["conflicts"][0]["reason"],
                                  conflicts=info["conflicts"], unavailable=True)
    info["context"] = _context(db, project_id)
    return info


def _already_restored(info):
    restore_receipt = info.get("restore_receipt") or {}
    prior = restore_receipt.get("result") or None
    return {
        "status": "already_restored",
        "source_order_id": info["source_order_id"],
        "restored_via": "cancel_restore" if prior else "other",
        "restored_by": restore_receipt.get("operated_by"),
        "restored_at": (prior or {}).get("restored_at"),
        "result": prior,
        "replayed": False,
        # 前端对两类回执统一渲染 warnings；此处缺失会让收据体契约分叉。
        "warnings": list((prior or {}).get("warnings") or []),
    }


def restore_cancellation(db: Session, *, project_id, source_order_id,
                         expected_digest, reason, operated_by, idempotency_key,
                         reauthorize, batch_id=None):
    """Reverse exactly one confirmed cancellation; the API owns commit/rollback.

    Lock order mirrors the confirmation: global advisory -> workbook state ->
    project -> source header -> assignments -> demand lines -> tombstone ->
    issue headers and all their lines -> batch row. Any drift refuses with
    zero writes; replay returns the historical receipt without re-writing.
    ``batch_id=None`` restores the latest confirmation receipt of the order.
    """
    from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY

    reason = str(reason or "").strip()
    if not reason or len(reason) > demands.MAX_REASON_LENGTH or not operated_by:
        raise ValueError("必须提供实名操作人和有效恢复原因（最多1000字）")
    replay_key = _digest([operated_by, idempotency_key])

    def _request_digest(target_batch_id):
        return _digest({"project_id": project_id, "batch_id": target_batch_id,
                        "source_order_id": source_order_id,
                        "expected_digest": expected_digest,
                        "reason": reason, "operated_by": operated_by})

    db.execute(select(func.pg_advisory_xact_lock(DATA_CHANGE_ADVISORY_LOCK_KEY)))
    db.expire_all()
    reauthorize()

    receipts = _confirmation_entries(db, project_id=project_id,
                                     source_order_id=source_order_id)
    if not receipts:
        raise RestorationConflict(
            f"未找到来源单 {source_order_id} 在本项目的人工取消确认回执", unavailable=True)
    replay_batch = _restore_batch_for_replay(db, replay_key)
    if replay_batch is not None:
        stored = ((replay_batch.report_json or {}).get(RESTORE_RESULTS_KEY) or {}).get(replay_key)
        stored_result = stored.get("result") if isinstance(stored, dict) else None
        prior_batch_id = stored_result.get("batch_id") if isinstance(stored_result, dict) else None
        if not isinstance(stored, dict) or not isinstance(stored_result, dict) \
                or not isinstance(prior_batch_id, int) or isinstance(prior_batch_id, bool) \
                or stored.get("request_digest") != _request_digest(prior_batch_id):
            raise RestorationConflict("同一幂等键已用于其他恢复请求，请刷新核对")
        # 幂等重放只在「仍处于该次恢复之后的状态」成立。恢复后来源单再次作废换代，
        # 盲目返回历史 result 会向前端谎报当前已恢复（与 void_fast 重放守卫同口径）。
        prior_key = stored.get("confirmation_key")
        if not isinstance(prior_key, str):
            raise RestorationConflict("恢复回执缺少确认回执引用，请刷新核对", unavailable=True)
        prior_receipt = _receipt_from_db(db, project_id=project_id,
                                         source_order_id=source_order_id,
                                         batch_id=prior_batch_id, receipt_key=prior_key)
        prior_info, _prior_restoration, _prior_corrupt = _evaluate(
            db, project_id=project_id, receipt=prior_receipt)
        if prior_info["state"] != "restored":
            raise RestorationConflict(
                "该恢复已被后续作废操作取代，本次重试不再返回历史结果，请刷新核对",
                conflicts=prior_info["conflicts"])
        return {**stored_result, "replayed": True}
    if batch_id is None:
        receipt = receipts[0]
    else:
        receipt = next((row for row in receipts if row["batch_id"] == batch_id), None)
        if receipt is None:
            raise RestorationConflict(
                "该批次中没有此来源单的取消确认回执，请按最新预览重试", unavailable=True)
    batch_id = receipt["batch_id"]
    request_digest = _request_digest(batch_id)

    receipt_key = receipt["receipt_key"]

    def rechecked():
        # 锁前拿到的 entry 可能已过期：每次都从库里重读回执再评估/算摘要。
        fresh = _receipt_from_db(db, project_id=project_id,
                                 source_order_id=source_order_id,
                                 batch_id=batch_id, receipt_key=receipt_key)
        info, restoration, corrupt = _evaluate(db, project_id=project_id, receipt=fresh)
        if info["state"] == "unavailable":
            raise RestorationConflict(
                "回执恢复快照缺失或损坏，不可自动恢复" if corrupt
                else "回执缺少恢复前置快照，不可自动恢复",
                conflicts=info["conflicts"], unavailable=True)
        if info["state"] == "restored":
            return fresh, info, restoration, _already_restored(info)
        if info["state"] != "restorable":
            raise RestorationConflict(
                "相关数据已变化，本次未恢复，请刷新预览后重新核对",
                conflicts=info["conflicts"])
        if not expected_digest or info["expected_digest"] != expected_digest:
            raise RestorationConflict(
                "预览摘要不一致，本次未恢复，请刷新预览后重新核对",
                conflicts=info["conflicts"])
        return fresh, info, restoration, None

    _receipt, _info, restoration, already = rechecked()
    if already is not None:
        return already

    states = operations.lock_workbook_states(db, project_ids=[project_id])
    db.scalar(select(MaintenanceProject).where(MaintenanceProject.project_id == project_id)
              .with_for_update().execution_options(populate_existing=True))
    db.scalar(select(O).where(O.raw_order_id == source_order_id)
              .with_for_update().execution_options(populate_existing=True))
    list(db.scalars(select(A).where(A.source_order_id == source_order_id)
                    .order_by(A.assignment_id).with_for_update()
                    .execution_options(populate_existing=True)))
    order_id = db.scalar(select(O.id).where(O.raw_order_id == source_order_id))
    list(db.scalars(select(L).where(L.order_id == order_id)
                    .order_by(L.id).with_for_update().execution_options(populate_existing=True)))
    db.scalar(select(T).where(T.source_order_id == source_order_id)
              .with_for_update().execution_options(populate_existing=True))
    issue_ids = {row.get("issue_id") for row in restoration.get("site_issues") or []}
    list(db.scalars(select(I).where(I.issue_id.in_(issue_ids)).order_by(I.issue_id)
                    .with_for_update().execution_options(populate_existing=True)))
    list(db.scalars(select(IL).where(IL.issue_id.in_(issue_ids)).order_by(IL.issue_line_id)
                    .with_for_update().execution_options(populate_existing=True)))
    db.scalar(select(SysImportBatch).where(SysImportBatch.id == batch_id)
              .with_for_update().execution_options(populate_existing=True))
    db.expire_all()
    reauthorize()
    receipt, info, restoration, already = rechecked()
    if already is not None:
        return already

    now = datetime.now(UTC)
    audit_reason = (f"{reason}（恢复取消确认回执 batch={batch_id} "
                    f"key={receipt['receipt_key'][:12]}）")
    for row in restoration["demand_lines"]:
        line = db.get(L, row["id"])
        before = row["before"]
        old = _entity(line)
        line.is_active = before["is_active"]
        line.voided_at = _time(before["voided_at"])
        line.voided_by = before["voided_by"]
        line.void_reason = before["void_reason"]
        _audit(db, project_id=project_id, entity_type="maintenance_line",
                    entity_id=line.id, action="RESTORE", before=old, after=_entity(line),
                    reason=audit_reason, operated_by=operated_by)
    requirement_before = {}
    for row in restoration["site_lines"]:
        line = db.get(IL, row["issue_line_id"])
        requirement_before[line.issue_line_id] = requirements.snapshot(line)
        line.is_active = True
    for row in restoration["site_issues"]:
        if not row["voided"]:
            continue
        issue = db.get(I, row["issue_id"])
        before = row["before"]
        issue.normalized_status = before["normalized_status"]
        issue.raw_status = before["raw_status"]
        issue.status_mapping_state = before["status_mapping_state"]
        issue.status_mapping_version = before["status_mapping_version"]
        issue.voided_at = _time(before["voided_at"])
    db.flush()
    # 反向 requirements 更正：新记录引用恢复人，不删原更正记录，版本只增。
    requirements.record_corrections(db, project_id=project_id, before=requirement_before,
                                    operated_by=operated_by, reason=audit_reason)
    db.flush()
    for row in restoration["site_lines"]:
        line = db.get(IL, row["issue_line_id"])
        _audit(db, project_id=project_id, entity_type="site_issue_line",
                    entity_id=line.issue_line_id, action="RESTORE",
                    before=row["after"], after=_entity(line),
                    reason=audit_reason, operated_by=operated_by)
    for row in restoration["site_issues"]:
        if not row["voided"]:
            continue
        issue = db.get(I, row["issue_id"])
        _audit(db, project_id=project_id, entity_type="site_issue",
                    entity_id=issue.issue_id, action="RESTORE",
                    before=row["after"], after=_entity(issue),
                    reason=audit_reason, operated_by=operated_by)

    tombstone = db.scalar(select(T).where(T.source_order_id == source_order_id)
                          .with_for_update())
    tombstone.restored_by = operated_by
    tombstone.restore_reason = reason
    tombstone.restored_at = now
    tombstone.version += 1
    result = {
        "batch_id": batch_id,
        "source_order_id": source_order_id,
        "status": "restored",
        "restored_at": now.isoformat(),
        "restored_lines": len(restoration.get("demand_lines") or []),
        "restored_site_lines": len(restoration.get("site_lines") or []),
        "restored_site_issues": sum(1 for row in restoration.get("site_issues") or []
                                    if row.get("voided")),
        "tombstone_version": tombstone.version,
        "replayed": False,
        "warnings": info["warnings"],
    }
    demands._event(db, event_type="restored",
                   # 事件键与回执来源单同作用域：不同来源单不能共用同一审计事件。
                   idempotency_key=f"cancel-restore:{source_order_id}:{replay_key}",
                   operated_by=operated_by, reason=reason, payload=result,
                   occurred_at=now, intent_id=tombstone.delete_intent_id,
                   source_order_id=source_order_id)
    operations.bump_locked_workbook_revision(db, state=states[project_id])
    batch = db.get(SysImportBatch, batch_id)
    saved = dict((batch.report_json or {}).get(RESTORE_RESULTS_KEY) or {})
    saved[replay_key] = {"request_digest": request_digest, "result": result,
                         "confirmation_key": receipt["receipt_key"],
                         "operated_by": operated_by, "reason": reason,
                         "project_id": project_id}
    batch.report_json = {**(batch.report_json or {}), RESTORE_RESULTS_KEY: saved}
    db.flush()
    return result
