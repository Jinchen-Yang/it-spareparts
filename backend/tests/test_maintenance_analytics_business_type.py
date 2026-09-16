"""PN 分类须在聚合前按当前项目取交集，不能从 PN 或源单头猜分类。"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import config
from app.models.maintenance import (
    FMaintenanceOrder,
    MaintenanceDemandDeleteIntent,
    MaintenanceDemandTombstone,
)
from app.models.maintenance_doc_import import (
    MaintenanceDocHeadRow,
    MaintenanceDocImportBatch,
    MaintenanceRkdReturnLine,
)
from app.models.maintenance_project import MaintenanceProjectUserAssignment
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysUser
from app.services import maintenance_analytics as ana
from tests.boss_board_helpers import boss_client, client_for
from tests.test_maintenance_analytics import _line, _part, _project

URL = "/api/maintenance/analytics/pn-ranking"
ALL_CODES = "overall,spare,computing,refit,other,unlabeled"


def project(db, tag, business_type):
    p = _project(db, tag)
    p.business_type = business_type
    db.flush()
    return p


def line(db, proj, part, qty, cost=None, **kwargs):
    return _line(
        db,
        proj,
        part,
        order_no=f"WBDD-{uuid.uuid4().hex}",
        order_date=kwargs.pop("order_date", date(2026, 6, 1)),
        qty=Decimal(qty),
        cost_inc=Decimal(cost) if cost is not None else None,
        **kwargs,
    )


def ranking(db, business_type="all", **kwargs):
    return ana.pn_ranking(
        db,
        business_type=business_type,
        can_cost=kwargs.pop("can_cost", True),
        range_=kwargs.pop("range_", "all"),
        sort=kwargs.pop("sort", "cost_inc"),
        **kwargs,
    )


def bad_return(db, proj, part, qty, *, fallback=False, **kwargs):
    batch = MaintenanceDocImportBatch(
        batch_id=str(uuid.uuid4()),
        doc_type="rkd_inbound",
        file_hash=uuid.uuid4().hex,
        filename="analytics-rkd.xlsx",
        idempotency_key=str(uuid.uuid4()),
        uploaded_by="test",
        head_rows=1,
        line_rows=1,
        issue_rows=0,
        status="applied",
        applied_by="test",
        applied_at=datetime.now(timezone.utc),
    )
    db.add(batch)
    db.flush()
    head = MaintenanceDocHeadRow(
        row_id=str(uuid.uuid4()),
        batch_id=batch.batch_id,
        row_no=1,
        raw_json={},
        head_no=f"RKD-{uuid.uuid4().hex}",
        project_id=proj.project_id,
    )
    db.add(head)
    db.flush()
    r = MaintenanceRkdReturnLine(
        rkd_line_id=str(uuid.uuid4()),
        batch_id=batch.batch_id,
        head_row_id=head.row_id,
        project_id=proj.project_id,
        head_no=head.head_no,
        source_ref=str(uuid.uuid4()),
        part_id=None if fallback else part.id,
        pn=part.pn_std.lower(),
        qty=Decimal(qty),
        test_result="坏品",
        # Deliberately outside the demand window: retain the existing RKD time semantics.
        occurred_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    for key, value in kwargs.items():
        setattr(r, key, value)
    db.add(r)
    db.flush()
    return r


def test_classification_precedes_aggregation_shares_pagination_and_search(db):
    overall = project(db, "overall", " 整体维保 ")
    spare = project(db, "spare", "备件维保")
    shared = _part(db, "TYPE-SHARED", "Disk shared")
    second = _part(db, "TYPE-SECOND", "Disk second")
    excluded = _part(db, "TYPE-EXCLUDED")
    first = line(
        db, overall, shared, "10", "100", cost_ex=Decimal("90"), return_qty=Decimal("2")
    )
    line(db, overall, shared, "2")
    line(db, overall, second, "4", "300", cost_ex=Decimal("270"))
    line(db, spare, shared, "90", "9000")
    line(db, spare, excluded, "999", "99999")
    line(db, overall, shared, "800", "8000", order_date=date(2025, 1, 1))
    line(db, overall, shared, "700", "7000", active=False)
    # A conflicting source header must not override the currently assigned project.
    db.get(FMaintenanceOrder, first.order_id).business_type = "备件维保"
    db.commit()

    params = dict(
        range_="custom",
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        q="disk",
        page_size=1,
    )
    out = ranking(db, "overall", **params)
    assert out["total"] == out["summary"]["part_count"] == 2
    assert Decimal(out["summary"]["total_effective_qty"]) == 14
    assert out["summary"]["total_cost_inc"]["value"] == "400.00"
    assert out["summary"]["total_cost_ex"]["value"] == "360.00"
    assert out["rows"][0]["pn"] == "TYPE-SECOND"
    assert out["rows"][0]["cost_share_pct"] == 75
    row = ranking(db, "overall", page=2, **params)["rows"][0]
    assert row["rank"] == 2
    assert row["qty"] == 12 and row["return_qty"] == 2 and row["effective_qty"] == 10
    assert row["occurrences"] == row["order_count"] == 2
    assert row["project_count"] == row["missing_lines"] == 1
    assert row["cost_share_pct"] == 25 and row["monthly_avg_qty"] == 10
    assert row["first_date"] == row["last_date"] == "2026-06-01"
    params["q"] = "type-shared"
    searched = ranking(db, "overall", **params)
    assert searched["total"] == 1
    assert searched["rows"][0]["cost_share_pct"] == 100
    assert searched["summary"]["total_cost_inc"]["value"] == "100.00"
    assert ranking(db, "computing", **params)["total"] == 0


def test_six_categories_unassigned_multi_all_and_current_assignment(db):
    shared = _part(db, "TYPE-PARTITION")
    projects = {}
    for tag, label, qty in [
        ("overall", "整体维保", "1"),
        ("spare", "备件维保", "2"),
        ("computing", "算力运维", "4"),
        ("refit", "拆改配服务", "128"),
        ("other", "单次维修", "8"),
        ("null", None, "16"),
        ("blank", "   ", "32"),
    ]:
        projects[tag] = project(db, tag, label)
        line(db, projects[tag], shared, qty)
    unassigned = line(db, projects["null"], shared, "64")
    raw_id = db.get(FMaintenanceOrder, unassigned.order_id).raw_order_id
    assignment = db.scalar(
        select(MaintenanceSourceOrderAssignment).where(
            MaintenanceSourceOrderAssignment.source_order_id == raw_id
        )
    )
    assignment.is_active = False
    assignment.version += 1
    assignment.archived_by = "test"
    assignment.archived_at = datetime.now(timezone.utc)
    db.commit()

    for code, qty in [
        ("overall", 1),
        ("spare", 2),
        ("computing", 4),
        ("refit", 128),
        ("other", 8),
        ("unlabeled", 48),
    ]:
        assert ranking(db, code)["rows"][0]["qty"] == qty
    multi = ranking(db, "overall,spare")["rows"][0]
    assert multi["qty"] == 3 and multi["project_count"] == 2
    assert ranking(db, "overall,overall")["rows"][0]["qty"] == 1
    default = ana.pn_ranking(db, range_="all", can_cost=True)
    assert default == ranking(db) == ranking(db, ALL_CODES)
    assert default["rows"][0]["qty"] == 255
    assert default["rows"][0]["project_count"] == 7
    # 历史五档 URL（无 refit）是显式子集：不再等于 all，也不会把 refit 混进来；
    # 未归属行只进 all，具体分类（含旧五档）一律要求真实项目。
    legacy_five = ranking(db, "overall,spare,computing,other,unlabeled")
    assert legacy_five["rows"][0]["qty"] == 63
    assert legacy_five["rows"][0]["project_count"] == 6

    # Changing project type immediately reclassifies historical consumption.
    projects["overall"].business_type = "备件维保"
    db.commit()
    assert ranking(db, "overall")["total"] == 0
    assert ranking(db, "spare")["rows"][0]["qty"] == 3
    # Reassignment uses the active project, not historical assignment rows.
    db.add(
        MaintenanceSourceOrderAssignment(
            assignment_id=str(uuid.uuid4()),
            source_order_id=raw_id,
            project_id=projects["computing"].project_id,
            is_active=True,
            created_by="test",
        )
    )
    db.commit()
    assert ranking(db, "computing")["rows"][0]["qty"] == 68
    assert ranking(db, "unlabeled")["rows"][0]["qty"] == 48


@pytest.mark.parametrize("fallback", [False, True], ids=["part-id", "pn-fallback"])
def test_rkd_evidence_intersects_type_and_user_scope_before_pn_matching(db, fallback):
    own = project(db, "own", "整体维保")
    elsewhere = project(db, "elsewhere", "整体维保")
    spare = project(db, "spare", "备件维保")
    shared = _part(db, "TYPE-RKD")
    line(db, own, shared, "10", "100")
    line(db, elsewhere, shared, "20", "200")
    line(db, spare, shared, "30", "300")
    bad_return(db, own, shared, "2", fallback=fallback)
    bad_return(db, elsewhere, shared, "5", fallback=fallback)
    bad_return(db, spare, shared, "9", fallback=fallback)
    bad_return(db, own, shared, "100", fallback=fallback, test_result="成品")
    bad_return(
        db,
        own,
        shared,
        "100",
        fallback=fallback,
        source="manual",
        batch_id=None,
        head_row_id=None,
    )
    bad_return(
        db,
        own,
        shared,
        "100",
        fallback=fallback,
        line_status="voided",
        voided_at=datetime.now(timezone.utc),
        voided_by="test",
    )
    db.commit()

    scoped = dict(
        allowed_project_ids={own.project_id, spare.project_id},
        range_="custom",
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
    )
    out = ranking(db, "overall", **scoped)
    assert out["rows"][0]["bad_return_qty"] == 2
    assert out["rows"][0]["bad_return_rate_pct"] == 20
    assert Decimal(out["summary"]["total_bad_return_qty"]) == 2
    assert ranking(db, "overall")["rows"][0]["bad_return_qty"] == 7
    assert ranking(db, "overall,spare", **scoped)["rows"][0]["bad_return_qty"] == 11
    assert ranking(db, ALL_CODES, **scoped) == ranking(db, **scoped)
    assert ranking(db, "overall", allowed_project_ids=set())["total"] == 0
    assert ranking(db, "spare", allowed_project_ids={own.project_id})["total"] == 0
    own.business_type = "备件维保"
    db.commit()
    assert ranking(db, "overall", **scoped)["total"] == 0
    assert ranking(db, "spare", **scoped)["rows"][0]["bad_return_qty"] == 11


def test_api_validates_csv_preserves_permissions_and_row_scope(db):
    own = project(db, "api-own", "整体维保")
    other = project(db, "api-other", "整体维保")
    part = _part(db, "TYPE-API")
    line(db, own, part, "2", "100")
    line(db, other, part, "200", "10000")
    bad_return(db, own, part, "1")
    bad_return(db, other, part, "100")
    db.commit()
    client = client_for(
        db,
        username="analytics-scoped",
        overrides={
            "page_maintenance": True,
            "own_maintenance_projects_only": True,
            "data_purchase_cost": False,
        },
    )
    user = db.scalar(select(SysUser).where(SysUser.username == "analytics-scoped"))
    db.add(
        MaintenanceProjectUserAssignment(
            assignment_id=str(uuid.uuid4()),
            project_id=own.project_id,
            responsibility_type="primary_manager",
            user_id=user.id,
            version=1,
            assigned_by="test",
            assignment_reason="test",
        )
    )
    db.commit()
    params = {"range": "all", "business_type": "overall", "sort": "qty"}
    response = client.get(URL, params=params)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    row = response.json()["rows"][0]
    assert Decimal(row["qty"]) == 2 and Decimal(row["bad_return_qty"]) == 1
    assert row["project_count"] == 1
    assert row["cost_inc"]["state"] == row["cost_ex"]["state"] == "restricted"
    for sort in ("cost_inc", "cost_ex"):
        denied = client.get(URL, params={**params, "sort": sort})
        assert denied.status_code == 422
        assert denied.json()["detail"]["code"] == "sort_requires_cost_permission"
    assert (
        client.get(URL, params={**params, "business_type": "spare"}).json()["total"]
        == 0
    )

    full = boss_client(db)
    for value in ("all", ALL_CODES, "overall,spare", "overall,overall", "unlabeled"):
        assert (
            full.get(URL, params={**params, "business_type": value}).status_code == 200
        )
    for value in (
        "",
        "invalid",
        "整体维保",
        "all,overall",
        "overall,",
        "overall, spare",
    ):
        invalid = full.get(URL, params={**params, "business_type": value})
        assert invalid.status_code == 422, value
        assert invalid.json()["detail"][0]["loc"] == ["query", "business_type"]
    no_page = client_for(
        db, username="analytics-no-page", overrides={"page_maintenance": False}
    )
    assert no_page.get(URL, params=params).status_code == 403


@pytest.mark.parametrize("cutover", [False, True])
def test_concrete_filter_preserves_active_order_line_and_tombstone_rules(
    db, monkeypatch, cutover
):
    monkeypatch.setattr(config, "ACTIVE_STATUS_ONLY", True)
    monkeypatch.setattr(config.get_settings(), "maintenance_cutover_enabled", cutover)
    proj = project(db, "active-rules", "备件维保")
    part = _part(db, "TYPE-ACTIVE")
    line(db, proj, part, "1", "100")
    line(db, proj, part, "2", "200", active=False)
    draft = line(db, proj, part, "4", "400")
    db.get(FMaintenanceOrder, draft.order_id).data_status = "草稿"
    deleted = line(db, proj, part, "8", "800")
    intent = MaintenanceDemandDeleteIntent(
        intent_id=str(uuid.uuid4()),
        idempotency_key=str(uuid.uuid4()),
        request_digest="x" * 64,
        selection_digest="y" * 64,
        status="executed",
        reason="test",
        operated_by="test",
        header_count=1,
        line_count=1,
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc),
    )
    db.add(intent)
    db.flush()
    db.add(
        MaintenanceDemandTombstone(
            source_order_id=db.get(FMaintenanceOrder, deleted.order_id).raw_order_id,
            delete_intent_id=intent.intent_id,
            version_digest="z" * 64,
            deleted_by="test",
            delete_reason="test",
            deleted_at=datetime.now(timezone.utc),
            version=1,
        )
    )
    db.commit()
    result = ranking(db, "spare")
    assert result == ranking(db)
    row = result["rows"][0]
    assert row["qty"] == (1 if cutover else 9)
    assert row["occurrences"] == row["order_count"] == (1 if cutover else 2)
    assert row["cost_inc"]["value"] == ("100.00" if cutover else "900.00")
