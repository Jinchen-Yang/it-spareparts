"""领用行批量应返/免返测试（ITSP-2，D-31 第 5 款）。

覆盖：免返/应返/恢复继承三态、逐单独立事务（部分成功）、发货关联行
逐单拒绝、跨项目行整单拒绝、参数校验。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.dimensions import DimPart
from app.services.maintenance_no_return_batch import (
    BatchNoReturnError,
    apply_batch_no_return,
)
from app.models.maintenance_project import MaintenanceProject
from app.services import maintenance_project_operations as operations
from app.services.maintenance_project_operations import (
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)

_PART_SEQ = {"n": 9701}


def _seed(db, *, project=None, project_id="bnr-p1", lines=2, delivery_first=False):
    if project is None:
        project = MaintenanceProject(
            project_id=project_id,
            project_code=f"SYNTH-{project_id[-8:]}",
            display_name="批量免返测试项目",
            lifecycle_status="ongoing",
        )
    _PART_SEQ["n"] += 1
    part = DimPart(id=_PART_SEQ["n"], pn_std=f"PNSYN-{_PART_SEQ['n']}",
                   description="测试件")
    db.add_all([project, part])
    db.flush()
    issue = MaintenanceSiteIssue(
        issue_id=str(uuid4()), project_id=project.project_id,
        issue_no=f"BNR-ISSUE-{uuid4().hex[:8]}", issue_date=date(2026, 10, 19),
        receiver="接收人", issued_by="发出人", site_location="现场",
        raw_status="confirmed", status_mapping_state="mapped",
        normalized_status="confirmed", status_mapping_version="contract-v1",
        source="page_manual", import_batch_id=None, version=1,
    )
    db.add(issue)
    db.flush()
    line_rows = []
    for i in range(1, lines + 1):
        row = MaintenanceSiteIssueLine(
            issue_line_id=str(uuid4()), issue_id=issue.issue_id, line_no=i,
            part_id=part.id, pn=part.pn_std, quantity=Decimal("2.000"),
            algorithm_version="contract-v1", is_active=True, version=1,
        )
        if delivery_first and i == 1:
            # 发货关联行：先种发货来源（delivery_line_id 有 FK）
            from datetime import date as _date
            from app.models.maintenance_project_operations import (
                MaintenanceSiteIssueDeliverySource,
            )
            db.add(MaintenanceSiteIssueDeliverySource(
                delivery_line_id=f"dl-{i}", adapter_key="warehouse_shipment_v1",
                project_id=project.project_id,
                source_order_id=f"WBDD-TEST-{i}", source_line_id=f"sl-{i}",
                delivery_no=f"DEL-{i}", delivery_date=_date(2026, 10, 1),
                part_id=part.id, pn=part.pn_std, delivered_quantity=Decimal("1.000"),
                mapping_state="ready", mapping_version="test-v1",
            ))
            db.flush()
            row.delivery_line_id = f"dl-{i}"
        line_rows.append(row)
    db.add_all(line_rows)
    operations.get_or_create_workbook_state(db, project_id=project.project_id)
    db.commit()
    return project, issue, line_rows, part


def _no_return_values(db, line_ids):
    rows = db.execute(select(MaintenanceSiteIssueLine).where(
        MaintenanceSiteIssueLine.issue_line_id.in_(line_ids))).scalars().all()
    return {row.issue_line_id: row.no_return for row in rows}


# ── 三态 ──


def test_batch_set_exempt_then_should_return_then_inherit(db):
    project, _, lines, _ = _seed(db)
    line_ids = [l.issue_line_id for l in lines]
    result = apply_batch_no_return(
        db, project_id=project.project_id, line_ids=line_ids,
        no_return=True, reason="客户确认不再返还", operated_by="张三")
    assert result["issue_ok"] == 1 and result["changed_lines"] == 2
    assert all(v is True for v in _no_return_values(db, line_ids).values())
    apply_batch_no_return(db, project_id=project.project_id, line_ids=line_ids,
                          no_return=False, reason="改回应返", operated_by="张三")
    assert all(v is False for v in _no_return_values(db, line_ids).values())
    apply_batch_no_return(db, project_id=project.project_id, line_ids=line_ids,
                          no_return=None, reason="恢复继承", operated_by="张三")
    assert all(v is None for v in _no_return_values(db, line_ids).values())


# ── 跨领用单与逐单独立事务 ──


def test_batch_spans_issues_per_issue_commit(db):
    project, _, l1, _ = _seed(db, project_id="bnr-p1")
    _, issue2, l2, _ = _seed(db, project=project, project_id="bnr-p1", lines=1)
    ids = [l.issue_line_id for l in l1] + [l2[0].issue_line_id]
    result = apply_batch_no_return(
        db, project_id=project.project_id, line_ids=ids,
        no_return=True, reason="批量免返", operated_by="李四")
    assert result["issue_total"] == 2 and result["issue_ok"] == 2
    assert result["changed_lines"] == 3
    assert all(r["ok"] for r in result["results"])


def test_delivery_backed_line_fails_per_issue_others_proceed(db):
    project, issue1, l1, _ = _seed(db, project_id="bnr-p1", delivery_first=True)
    _, issue2, l2, _ = _seed(db, project=project, project_id="bnr-p1", lines=1)
    ids = [l.issue_line_id for l in l1] + [l2[0].issue_line_id]
    result = apply_batch_no_return(
        db, project_id=project.project_id, line_ids=ids,
        no_return=True, reason="混合来源批量", operated_by="王五")
    by_issue = {r["issue_id"]: r for r in result["results"]}
    assert by_issue[issue1.issue_id]["ok"] is False
    assert "发货" in by_issue[issue1.issue_id]["error"]
    assert by_issue[issue2.issue_id]["ok"] is True
    values = _no_return_values(db, [l.issue_line_id for l in l1])
    assert all(v is None for v in values.values())   # 失败单回滚


def test_foreign_project_lines_rejected_whole_command(db):
    project, issue1, l1, _ = _seed(db, project_id="bnr-p1")
    _, issue2, l2, _ = _seed(db, project_id="bnr-p2", lines=1)
    foreign_id = l2[0].issue_line_id
    with pytest.raises(BatchNoReturnError, match="不属于当前项目"):
        apply_batch_no_return(
            db, project_id="bnr-p1",
            line_ids=[l1[0].issue_line_id, foreign_id],
            no_return=True, reason="混入他项目行", operated_by="赵六")


# ── 参数校验 ──


def test_invalid_no_return_value(db):
    project, _, lines, _ = _seed(db, lines=1)
    with pytest.raises(BatchNoReturnError, match="no_return 只允许"):
        apply_batch_no_return(db, project_id=project.project_id,
                              line_ids=[lines[0].issue_line_id],
                              no_return="yes", reason="r", operated_by="op")


def test_missing_reason_rejected(db):
    project, _, lines, _ = _seed(db, lines=1)
    with pytest.raises(BatchNoReturnError, match="原因"):
        apply_batch_no_return(db, project_id=project.project_id,
                              line_ids=[lines[0].issue_line_id],
                              no_return=True, reason="  ", operated_by="op")


def test_empty_and_missing_lines_rejected(db):
    project, _, lines, _ = _seed(db, lines=1)
    with pytest.raises(BatchNoReturnError, match="至少选择一条"):
        apply_batch_no_return(db, project_id=project.project_id,
                              line_ids=[], no_return=True,
                              reason="r", operated_by="op")
    with pytest.raises(BatchNoReturnError, match="不存在"):
        apply_batch_no_return(db, project_id=project.project_id,
                              line_ids=["nope-1"], no_return=True,
                              reason="r", operated_by="op")


def test_duplicate_line_ids_rejected(db):
    project, _, lines, _ = _seed(db, lines=1)
    lid = lines[0].issue_line_id
    with pytest.raises(BatchNoReturnError, match="重复"):
        apply_batch_no_return(db, project_id=project.project_id,
                              line_ids=[lid, lid], no_return=True,
                              reason="r", operated_by="op")
