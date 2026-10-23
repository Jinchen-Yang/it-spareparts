"""Shared failures retain legacy catches/HTTP payloads without Excel imports."""

from decimal import Decimal
import subprocess
import sys
from uuid import uuid4

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select

from app.api import maintenance_project_master_workbook as workbook_api
from app.models.dimensions import DimPart
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
    MaintenanceSiteIssueDeliverySource,
    MaintenanceSiteIssueReturnEvent,
)
from app.services import maintenance_bad_returns as returns
from app.services import maintenance_expense_collection_workbook as legacy
from app.services import maintenance_project_operations as operations
from app.services import maintenance_site_issue_merge_references as merge
from app.services import maintenance_site_return_requirements as requirements
from app.services.maintenance_errors import WorkbookError
from tests.test_maintenance_site_issue_facts import _facts


def test_shared_error_retains_legacy_catch_and_http_payload():
    assert WorkbookError is legacy.WorkbookError
    issues = [{"entity_id": "synthetic", "field": "备注", "current": "新值"}]
    with pytest.raises(legacy.WorkbookError) as caught:
        raise WorkbookError("row_conflicts", "三路合并冲突", issues)
    assert str(caught.value) == "三路合并冲突"
    with pytest.raises(HTTPException) as response:
        workbook_api._fail(caught.value)
    assert response.value.status_code == 409
    assert response.value.detail == {
        "code": "row_conflicts", "message": "三路合并冲突",
        "issues": issues, "conflicts": issues,
    }
    error = WorkbookError("stale_site_replay", "旧式重放", details={
        "rows": [], "total_count": 0, "fix_hint": "重新下载", "private": "不公开",
    })
    with pytest.raises(HTTPException) as response:
        workbook_api._fail(error)
    assert response.value.detail == {
        "code": "stale_site_replay", "message": "旧式重放", "issues": [],
        "rows": [], "total_count": 0, "fix_hint": "重新下载",
    }


def test_fact_adapters_load_without_expense_workbook():
    subprocess.run([
        sys.executable, "-c",
        "import sys; "
        "import app.services.maintenance_site_return_requirements; "
        "import app.services.maintenance_site_issue_merge_references; "
        "assert 'app.services.maintenance_expense_collection_workbook' not in sys.modules",
    ], check=True, capture_output=True, text=True)


def test_native_projection_conflict_keeps_error_and_rolls_back(db):
    project, issue, lines, part = _facts(db, source="legacy")
    for line in lines:
        delivery = MaintenanceSiteIssueDeliverySource(
            delivery_line_id=str(uuid4()), adapter_key="synthetic_delivery_v1",
            project_id=project.project_id, source_order_id=f"source-{uuid4()}",
            source_line_id=f"line-{uuid4()}", delivery_no="CKD-ERROR-CONTRACT",
            delivery_date=issue.issue_date, part_id=part.id, pn=part.pn_std,
            delivered_quantity=Decimal("2"), mapping_state="ready",
            mapping_version="contract-v1",
        )
        db.add(delivery)
        db.flush()
        line.delivery_line_id = delivery.delivery_line_id
        line.no_return = False
    initial = MaintenanceSiteIssueReturnEvent(
        event_id=str(uuid4()), project_id=project.project_id, issue_id=issue.issue_id,
        issue_version=1, event_type="return_obligation_created",
        payload=operations._site_issue_return_payload(issue, lines),
    )
    replacement = DimPart(pn_std=f"REPLACEMENT-{uuid4().hex[:8]}")
    db.add_all([initial, replacement])
    db.flush()
    returns.consume_return_event(db, initial)
    db.commit()
    audit_count = db.scalar(select(func.count()).select_from(MaintenanceProjectOperationAudit))
    operations._lock_project_for_fact_write(db, project.project_id)
    db.refresh(issue, with_for_update=True)
    for line in lines:
        db.refresh(line, with_for_update=True)
    before = {line.issue_line_id: requirements.snapshot(line) for line in lines}
    lines[0].part_id, lines[0].pn = replacement.id, replacement.pn_std
    # The real legacy consumer rejects changing a stable delivery's PN.
    # Its domain conflict must still be caught by the old workbook entrypoint.
    with pytest.raises(legacy.WorkbookError) as caught:
        requirements.record_corrections(
            db, project_id=project.project_id, before=before,
            operated_by="tester", reason="验证旧义务冲突协议",
        )
    assert caught.value.code == "return_requirement_conflict"
    assert caught.value.message == "同一稳定发货明细的 PN 身份不能更改"
    assert isinstance(caught.value.__cause__, returns.BadReturnConflict)
    db.rollback()
    assert lines[0].part_id == part.id and lines[0].version == 1
    assert issue.version == 1
    assert db.scalar(select(func.count()).select_from(MaintenanceSiteIssueReturnEvent)) == 1
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectOperationAudit)) == audit_count


def test_merge_scope_conflict_retains_legacy_error_type(db):
    _, first, _, _ = _facts(db)
    _, foreign, _, _ = _facts(db)
    with pytest.raises(legacy.WorkbookError) as caught:
        merge.validate_references(db, {first.issue_id, foreign.issue_id})
    assert caught.value.code == "site_issue_merge_conflict"
    assert caught.value.message == "领用单合并失败：来源单不存在或不属于同一项目，请重新校验"
    assert not db.dirty and not db.new
