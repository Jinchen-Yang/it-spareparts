"""D-34 通过网页、合法原件导入和只读 API 验证人工字段保护。"""

from datetime import date

import pytest
from sqlalchemy.orm import Session

from app.etl import loader
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine
from app.models.maintenance_project import MaintenanceProjectUserAssignment
from app.models.maintenance_project_operations import MaintenanceProjectWorkbookState
from app.models.system import SysUser
from app.services import maintenance_demand_manual as manual
from sqlalchemy import select
from tests import factories as f
from tests.test_demand_manual_api_guards import _admin_client, _client
from tests.test_demand_manual_edit import _batch, _make_project, _seed_line


def _read_line(client, order):
    response = client.get(f"/api/maintenance/demands/orders/{order.raw_order_id}/lines")
    assert response.status_code == 200, response.text
    return response.json()["items"][0]


def test_page_note_protects_only_note_and_source_upload_updates_quantity(db):
    order, line = _seed_line(db)
    _make_project(db, tag="FIELD-NOTE", source_order_id=order.raw_order_id)
    client = _admin_client(db)
    original = _read_line(client, order)
    response = client.patch(f"/api/maintenance/demands/lines/{line.raw_line_id}", json={
        "updates": {"line_note": "  人工核对备注  "}, "reason": "核对备注",
        "expected_digest": original["digest"],
    })
    assert response.status_code == 200, response.text
    assert response.json()["protected_fields"] == ["line_note"]
    assert response.json()["line_note"] == "人工核对备注"

    source = f.maintenance_line(order.raw_order_id, line.raw_line_id, "PN-E1", qty="7")
    source["line_note"] = "后续原件备注"
    report = loader.load(db, f.maintenance_result(
        {order.raw_order_id: f.maintenance_head(order.raw_order_id, on=date(2026, 3, 1))},
        [source],
    ), _batch(db), date(2026, 8, 2), mode="upsert")
    db.commit()
    current = _read_line(client, order)
    assert current["qty"] == "7.000"
    assert current["line_note"] == "人工核对备注"
    assert current["protected_fields"] == ["line_note"]
    assert report["protected_field_counts"] == {"line_note": 1}
    assert report["fact_rows_semantic_updated"] == 1


def test_page_clear_pn_restores_identity_group_and_requires_source_reupload(db):
    order, line = _seed_line(db)
    original_part_id = line.part_id
    _make_project(db, tag="FIELD-PN", source_order_id=order.raw_order_id)
    db.add(DimPart(pn_std="PN-REPLACED", status="active"))
    db.commit()
    client = _admin_client(db)
    original = _read_line(client, order)
    response = client.patch(f"/api/maintenance/demands/lines/{line.raw_line_id}", json={
        "updates": {"pn_std": "PN-REPLACED", "pn_raw": "PN-REPLACED"},
        "reason": "核对型号", "expected_digest": original["digest"],
    })
    assert response.status_code == 200, response.text
    assert response.json()["protected_fields"] == ["part_id", "pn_raw", "pn_std"]
    cleared = client.post(
        f"/api/maintenance/demands/lines/{line.raw_line_id}/clear-override", json={
            "field_name": "pn_std", "reason": "撤销人工覆盖",
            "expected_digest": response.json()["digest"],
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["protected_fields"] == []
    assert cleared.json()["pn_std"] == "PN-E1"
    assert cleared.json()["pn_raw"] == "PN-E1"
    assert cleared.json()["part_id"] == original_part_id
    assert cleared.json()["source_reupload_required"] is True
    assert "首次" in cleared.json()["message"]
    assert "重新上传" in cleared.json()["message"]


def test_legacy_review_lists_only_visible_source_rows_with_safe_count_and_paging(db):
    candidates = []
    for suffix in ("OWN", "OTHER", "VOID", "SOURCE"):
        order, line = _seed_line(db, order_raw=f"REVIEW-{suffix}",
                                line_raw=f"REVIEW-LINE-{suffix}", pn=f"REVIEW-PN-{suffix}")
        project = _make_project(db, tag=suffix, source_order_id=order.raw_order_id)
        if suffix != "SOURCE":
            line.edited_source = "workbook_manual"
        if suffix == "VOID":
            line.is_active = False
        db.commit()
        candidates.append((order, line, project))
    admin = _admin_client(db)
    created = admin.post("/api/maintenance/demands/lines", json={
        "order_date": "2026-03-01", "project_id": candidates[0][2].project_id,
        "pn_std": "REVIEW-PN-OWN", "qty": 1, "reason": "人工新增",
        "idempotency_key": "review-manual-new-001",
    })
    assert created.status_code == 201, created.text
    first = admin.get("/api/maintenance/demands/manual-protection-review?page_size=1")
    assert first.status_code == 200, first.text
    assert first.json()["total"] == 2
    assert len(first.json()["items"]) == 1
    second = admin.get("/api/maintenance/demands/manual-protection-review?page_size=1&page=2")
    assert second.status_code == 200, second.text
    assert {first.json()["items"][0]["raw_line_id"], second.json()["items"][0]["raw_line_id"]} == {
        "REVIEW-LINE-OWN", "REVIEW-LINE-OTHER",
    }
    assert "manual_override" not in first.json()["items"][0]
    assert not any("cost" in key for key in first.json()["items"][0])

    scoped = _client(db, username="field-review-own", role="sales", permissions={
        "page_maintenance": True, "action_maintenance_demand_manage": True,
        "own_maintenance_projects_only": True,
    })
    empty = scoped.get("/api/maintenance/demands/manual-protection-review")
    assert empty.status_code == 200, empty.text
    assert empty.json()["items"] == []
    assert empty.json()["total"] == 0
    user = db.scalar(select(SysUser).where(SysUser.username == "field-review-own"))
    db.add(MaintenanceProjectUserAssignment(
        assignment_id="field-review-viewer", project_id=candidates[0][2].project_id,
        user_id=user.id, responsibility_type="viewer", assigned_by="synthetic",
        assignment_reason="合成项目范围",
    ))
    db.commit()
    owned = scoped.get("/api/maintenance/demands/manual-protection-review")
    assert owned.status_code == 200, owned.text
    assert owned.json()["total"] == 1
    assert [item["raw_line_id"] for item in owned.json()["items"]] == ["REVIEW-LINE-OWN"]

    denied = _client(db, username="field-review-denied", role="sales", permissions={
        "page_maintenance": True, "action_maintenance_demand_manage": False,
    })
    assert denied.get("/api/maintenance/demands/manual-protection-review").status_code == 403


@pytest.mark.parametrize("command", ["patch", "clear"])
def test_stale_session_identity_cannot_overwrite_other_manual_fields(db, command):
    order, line = _seed_line(db)
    _make_project(db, tag="FIELD-CAS", source_order_id=order.raw_order_id)
    manual.patch_demand_line(db, raw_line_id=line.raw_line_id, updates={"qty": 3},
                             reason="初始人工量", operated_by="initial")
    db.commit()
    with Session(db.get_bind(), expire_on_commit=False) as first, Session(db.get_bind()) as second:
        # 强引用保留 Session 身份映射，模拟请求前已经读取过同一实体的调用者。
        held_line = first.scalar(select(FMaintenanceLine).where(
            FMaintenanceLine.raw_line_id == line.raw_line_id,
        ))
        original = manual.patch_demand_line(first, raw_line_id=line.raw_line_id,
                                           updates={}, reason="读取基线", operated_by="first")
        first.commit()
        changed = manual.patch_demand_line(second, raw_line_id=line.raw_line_id,
                                          updates={"line_note": "他人已确认"},
                                          reason="更新备注", operated_by="second")
        second.commit()
        assert held_line.line_note is None
        with pytest.raises(manual.DemandManualConflict):
            if command == "patch":
                manual.patch_demand_line(first, raw_line_id=line.raw_line_id,
                                         updates={"qty": 4}, reason="旧基线修改",
                                         operated_by="first", expected_digest=original["digest"])
            else:
                manual.clear_override(first, raw_line_id=line.raw_line_id, field="qty",
                                      reason="旧基线撤销", operated_by="first",
                                      expected_digest=original["digest"])
        first.rollback()
        after = manual.patch_demand_line(second, raw_line_id=line.raw_line_id,
                                        updates={}, reason="回读结果", operated_by="second")
        assert after["digest"] == changed["digest"]
        assert after["qty"] == "3.000"
        assert after["line_note"] == "他人已确认"
        assert after["protected_fields"] == ["line_note", "qty"]


def test_fresh_digest_with_cached_workbook_state_preserves_concurrent_revision(db):
    order, line = _seed_line(db)
    project = _make_project(db, tag="FIELD-STATE", source_order_id=order.raw_order_id)
    with Session(db.get_bind(), expire_on_commit=False) as first, Session(db.get_bind()) as second:
        manual.patch_demand_line(first, raw_line_id=line.raw_line_id, updates={},
                                 reason="读取基线", operated_by="first")
        cached_state = first.get(MaintenanceProjectWorkbookState, project.project_id)
        initial_revision = cached_state.revision
        first.commit()
        changed = manual.patch_demand_line(second, raw_line_id=line.raw_line_id,
                                          updates={"line_note": "他人备注"},
                                          reason="核对备注", operated_by="second")
        second.commit()
        assert cached_state.revision == initial_revision
        manual.patch_demand_line(first, raw_line_id=line.raw_line_id, updates={"qty": 4},
                                 reason="采用最新基线", operated_by="first",
                                 expected_digest=changed["digest"])
        first.commit()
        current_state = second.get(MaintenanceProjectWorkbookState, project.project_id)
        assert current_state.revision == initial_revision + 2
