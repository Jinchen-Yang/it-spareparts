"""临时设计探针（不提交）：验证审计锚点的两个前提。"""
from sqlalchemy import select

from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
)

from tests.test_maintenance_source_cancel_cascade import setup
from tests.test_maintenance_source_cancel_restore import confirm


def test_probe_audit_anchor_facts(db, tmp_path):
    client, project, _rows, parent, lines, exact, _unknown, _receipt, batch, _url, detail_url = setup(
        db, tmp_path)
    confirm(db, client, detail_url)
    db.expire_all()
    entry = next(iter((batch.report_json or {})["_source_cancel_confirmations"].values()))
    restoration = entry["restoration"]
    audits = list(db.scalars(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.project_id == project.project_id)))
    by_member = {(row.entity_type, row.entity_id): row for row in audits}

    print("AUDIT_COUNT", len(audits))
    print("OPERATED_AT_VALUES", {row.operated_at.isoformat() for row in audits})
    print("AUDIT_ACTIONS", sorted({(row.entity_type, row.action) for row in audits}))

    for row in restoration["demand_lines"]:
        audit = by_member.get(("maintenance_line", str(row["id"])))
        assert audit is not None, row["id"]
        same_before = audit.before_json == row["before"]
        same_after = audit.after_json == row["after"]
        diff_b = {k: (audit.before_json.get(k), row["before"].get(k))
                  for k in set(audit.before_json) | set(row["before"])
                  if audit.before_json.get(k) != row["before"].get(k)}
        diff_a = {k: (audit.after_json.get(k), row["after"].get(k))
                  for k in set(audit.after_json) | set(row["after"])
                  if audit.after_json.get(k) != row["after"].get(k)}
        print("DEMAND", row["id"], "before_eq=", same_before, "after_eq=", same_after,
              "diff_b=", diff_b, "diff_a=", diff_a)
    for row in restoration["site_lines"]:
        audit = by_member.get(("site_issue_line", row["issue_line_id"]))
        assert audit is not None, row["issue_line_id"]
        print("SITE_LINE", row["issue_line_id"],
              "before_eq=", audit.before_json == row["before"],
              "after_eq=", audit.after_json == row["after"],
              "diff_b=", {k: (audit.before_json.get(k), row["before"].get(k))
                          for k in set(audit.before_json) | set(row["before"])
                          if audit.before_json.get(k) != row["before"].get(k)},
              "diff_a=", {k: (audit.after_json.get(k), row["after"].get(k))
                          for k in set(audit.after_json) | set(row["after"])
                          if audit.after_json.get(k) != row["after"].get(k)})
    for row in restoration["site_issues"]:
        audit = by_member.get(("site_issue", row["issue_id"]))
        assert audit is not None, row["issue_id"]
        print("SITE_ISSUE", row["issue_id"], "voided=", row["voided"], "action=", audit.action,
              "before_eq=", audit.before_json == row["before"],
              "after_eq=", audit.after_json == row["after"])
    intent_id = db.execute(select(
        __import__("app.models.maintenance", fromlist=["MaintenanceDemandTombstone"]
                   ).MaintenanceDemandTombstone).where(
        __import__("app.models.maintenance", fromlist=["MaintenanceDemandTombstone"]
                   ).MaintenanceDemandTombstone.source_order_id == parent.raw_order_id)
    ).scalar_one().delete_intent_id
    intent = db.get(__import__("app.models.maintenance", fromlist=["MaintenanceDemandDeleteIntent"]
                               ).MaintenanceDemandDeleteIntent, intent_id)
    print("INTENT", "reason=", repr(intent.reason), "operated_by=", intent.operated_by,
          "created_at=", intent.created_at.isoformat())
    print("ENTRY", "reason=", repr(entry["reason"]), "operated_by=", entry.get("operated_by"))
    print("CONFIRMED_AT", entry["result"]["confirmed_at"])

    from app.models.maintenance_project import MaintenanceProjectAuditLog
    logs = list(db.scalars(select(MaintenanceProjectAuditLog).where(
        MaintenanceProjectAuditLog.project_id == project.project_id)))
    for log in logs:
        print("PROJ_LOG", log.entity_type, log.entity_id, log.action,
              "reason=", repr(log.reason), "operated_by=", log.operated_by,
              "operated_at=", log.operated_at.isoformat())
    assign_ids = {row["assignment_id"] for row in restoration["assignments"]}
    print("ASSIGN_IDS", assign_ids)
    print("SNAPSHOT_ASSIGN_KEYS", sorted(restoration["assignments"][0]))
