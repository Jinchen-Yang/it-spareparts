"""D-0042：工作簿同号合单的只读计划与稳定身份迁移（调用方拥有事务）。"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timezone

from sqlalchemy import select

from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit, MaintenanceSiteIssue, MaintenanceSiteIssueLine,
)
from app.services.maintenance_expense_collection_workbook import WorkbookError
from app.services import maintenance_site_issue_merge_references as references

HEADER_FIELDS = {
    "issue_date": "领用日期", "site_location": "领用地址", "receiver": "领用人",
    "issued_by": "发放人", "normalized_status": "状态", "raw_status": "原始状态",
    "status_mapping_state": "状态映射", "status_mapping_version": "状态映射版本",
    "source": "来源", "import_batch_id": "来源批次", "display_no": "展示号",
    "created_by": "创建人", "confirmed_at": "确认时间", "corrected_at": "更正时间",
    "idempotency_key": "原创建幂等标识", "request_fingerprint": "原创建请求指纹",
}


def serial(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


def conflict(number, rows, message):
    where = f"第 {'、'.join(map(str, sorted(set(rows))))} 行" if rows else "本次上传"
    return WorkbookError("site_issue_merge_conflict",
                         f"06_领用返还 {where}领用单号 {number} 无法合并：{message}；请修改后重新校验",
                         details={"rows": [{"row_no": row, "issue_no": number} for row in sorted(set(rows))]})


def plan_merges(db, project_id, headers, flags, *, intents=(), issue_rows=None):
    """仅既有头参与合并；返回完整可比较计划，不改 ORM 或数据库。"""
    issues = {issue.issue_id: issue for issue in db.scalars(select(MaintenanceSiteIssue).where(
        MaintenanceSiteIssue.project_id == project_id,
    ))}
    requested = dict(headers)
    # D-02 通常忽略已作废行的编辑；本次若试图将它并入另一单则必须整本拒绝。
    for intent in intents:
        issue = issues.get(intent["issue_id"])
        if issue and issue.normalized_status == "void" and intent["field"] == "issue_no":
            requested[issue.issue_id] = (intent["value"], issue.issue_date)
    by_number = defaultdict(list)
    for issue in issues.values():
        by_number[requested.get(issue.issue_id, (issue.issue_no, issue.issue_date))[0]].append(issue)
    rows_by_issue = defaultdict(set)
    for issue_id, rows in (issue_rows or {}).items():
        rows_by_issue[issue_id].update(rows)
    for flag in flags:
        if flag.issue_id and flag.row_no is not None:
            rows_by_issue[flag.issue_id].add(flag.row_no)
    result = []
    for number, participants in sorted(by_number.items()):
        if len(participants) < 2:
            continue
        participants.sort(key=lambda issue: (issue.created_at, issue.issue_id))
        ids = [issue.issue_id for issue in participants]
        matching_intents = [intent for intent in intents if intent["issue_id"] in ids
            or (intent["issue_id"] is None and intent.get("target_issue_no") == number)]
        rows = sorted({row for issue_id in ids for row in rows_by_issue[issue_id]}
                      | {intent["row_no"] for intent in matching_intents})
        if any(issue.normalized_status == "void" for issue in participants):
            raise conflict(number, rows, "来源或目标领用单已作废，不能复活作废单")
        statuses = {issue.normalized_status for issue in participants}
        if "unknown" in statuses or (len(statuses) > 1 and not statuses <= {"confirmed", "corrected"}):
            raise conflict(number, rows, "领用单状态不兼容（" + "、".join(sorted(statuses)) + "）")
        survivor = next((issue for issue in participants if issue.issue_no == number), participants[0])
        if survivor.source == "site_issue_v2" and any(issue.source != "site_issue_v2" for issue in participants):
            raise conflict(number, rows, "原生领用单的发货身份流程不能接收其他来源的明细")
        if survivor.source == "site_issue_v2" and any(
            flag.is_create and flag.issue_no == number for flag in flags
        ):
            raise conflict(number, rows, "原生领用单不能通过工作簿合单新增无发货身份的明细")
        if survivor.source == "site_issue_v2" and db.scalar(select(MaintenanceSiteIssue.issue_id).where(
            MaintenanceSiteIssue.source == "site_issue_v2", MaintenanceSiteIssue.issue_no == number,
            MaintenanceSiteIssue.project_id != project_id,
        )):
            raise conflict(number, rows, "原生领用单号已被其他项目使用")
        lines = list(db.scalars(select(MaintenanceSiteIssueLine).where(
            MaintenanceSiteIssueLine.issue_id.in_(ids),
        ).order_by(MaintenanceSiteIssueLine.issue_id, MaintenanceSiteIssueLine.line_no,
                   MaintenanceSiteIssueLine.issue_line_id)))
        deliveries = [line.delivery_line_id for line in lines if line.delivery_line_id]
        if len(deliveries) != len(set(deliveries)):
            raise conflict(number, rows, "多张单引用了相同发货明细，无法保持返还义务身份")
        try:
            references.validate_references(db, ids)
        except WorkbookError as exc:
            raise conflict(number, rows, exc.message) from exc
        explicit = defaultdict(list)
        for intent in matching_intents:
            if intent["field"] in {"issue_date", "site_location"}:
                explicit[intent["field"]].append(intent)
        for flag in flags:
            if flag.issue_id in ids and not flag.is_void and flag.site_location_action != "keep":
                explicit["site_location"].append({"issue_id": flag.issue_id, "row_no": flag.row_no,
                    "value": flag.site_location if flag.site_location_action == "set" else None})
        final_header = {field: serial(getattr(survivor, field)) for field in HEADER_FIELDS}
        for field, choices in explicit.items():
            if len({json.dumps(choice["value"], sort_keys=True) for choice in choices}) > 1:
                raise conflict(number, rows, f"多行显式填写了不同{HEADER_FIELDS[field]}")
            final_header[field] = choices[0]["value"]
        conflicts = []
        for field, label in HEADER_FIELDS.items():
            survivor_value = serial(getattr(survivor, field))
            discarded = [{"issue_id": issue.issue_id, "issue_no": issue.issue_no,
                          "value": serial(getattr(issue, field))}
                         for issue in participants if serial(getattr(issue, field)) != final_header[field]]
            if discarded:
                conflicts.append({"field": field, "field_label": label, "survivor_value": survivor_value,
                                  "discarded": discarded, "final_value": final_header[field]})
        sources = [{"issue_id": issue.issue_id, "issue_no": issue.issue_no,
                    "line_count": sum(line.issue_id == issue.issue_id for line in lines),
                    "rows": sorted(rows_by_issue[issue.issue_id])} for issue in participants]
        snapshot = {
            "headers": [{column.key: serial(getattr(issue, column.key))
                         for column in MaintenanceSiteIssue.__table__.columns} for issue in participants],
            "lines": [{column.key: serial(getattr(line, column.key))
                       for column in MaintenanceSiteIssueLine.__table__.columns} for line in lines],
            "references": references.snapshot_references(db, ids),
        }
        result.append({"target_issue_no": number, "survivor_issue_id": survivor.issue_id,
                       "sources": sources, "total_line_count": len(lines), "rows": rows,
                       "header_conflicts": conflicts, "final_header": final_header,
                       "guard": digest(snapshot),
                       "message": f"领用单 {'、'.join(issue.issue_no for issue in participants)} 将合并为 {number}（共 {len(lines)} 行）"})
    return tuple(result)


def move_lines(db, merge, *, project_id, operated_by, reason):
    """头已按两阶段协议写成最终号/墓碑号；保留行 ID，追加并审计归属。"""
    survivor_id = merge["survivor_issue_id"]
    source_ids = [item["issue_id"] for item in merge["sources"]]
    survivor = db.get(MaintenanceSiteIssue, survivor_id)
    absorbed = [db.get(MaintenanceSiteIssue, issue_id) for issue_id in source_ids if issue_id != survivor_id]
    lines = list(db.scalars(select(MaintenanceSiteIssueLine).where(
        MaintenanceSiteIssueLine.issue_id.in_(source_ids),
    ).order_by(MaintenanceSiteIssueLine.issue_id, MaintenanceSiteIssueLine.line_no,
               MaintenanceSiteIssueLine.issue_line_id)))
    sequence = max((line.line_no for line in lines if line.issue_id == survivor_id), default=0)
    moved = []
    for line in lines:
        if line.issue_id == survivor_id:
            continue
        before = {"issue_id": line.issue_id, "line_no": line.line_no, "version": line.version}
        sequence += 1
        line.issue_id, line.line_no = survivor_id, sequence
        line.version += 1
        moved.append(line.issue_line_id)
        db.add(MaintenanceProjectOperationAudit(
            project_id=project_id, entity_type="site_issue_line", entity_id=line.issue_line_id,
            action="MERGE", operated_by=operated_by, reason=reason, before_json=before,
            after_json={"issue_id": survivor_id, "line_no": sequence, "version": line.version},
        ))
    now = datetime.now(timezone.utc)
    for issue in absorbed:
        issue.normalized_status = "void"
        issue.raw_status = "已作废"
        issue.status_mapping_state = "mapped"
        issue.voided_at = now
    survivor.version += 1
    db.flush()
    refs = references.move_references(db, project_id=project_id, survivor=survivor, absorbed=absorbed,
                                     operated_by=operated_by, reason=reason)
    after = {"survivor_issue_id": survivor_id, "target_issue_no": merge["target_issue_no"],
             "source_issue_ids": source_ids,
             "original_issue_nos": {item["issue_id"]: item["issue_no"] for item in merge["sources"]},
             "moved_line_ids": moved, "moved_line_count": len(moved),
             "header_conflicts": merge["header_conflicts"], "references": refs}
    for issue_id in source_ids:
        db.add(MaintenanceProjectOperationAudit(
            project_id=project_id, entity_type="site_issue", entity_id=issue_id,
            action="MERGE", operated_by=operated_by, reason=reason,
            before_json={"sources": merge["sources"]}, after_json=after,
        ))
    return moved
