"""回款已完成桶（2026-09-04 客户反馈）：分类、三桶排除与权限防侧信道。

口径铁律：桶成员必须与卡片「回款/合同总额」同源（_card_contracts /
_card_collections），合同事实不完整或无回款快照的项目不得误入。
权限铁律：该桶由合同财务数据推得——无 data_profit 的账号请求该桶 422，
且三桶不得做排除（否则凭计数差就能反推哪些项目回款收满）。
"""
from datetime import date
from decimal import Decimal
import uuid

import pytest

from app.config import get_settings
from app.models.maintenance_project import MaintenanceProjectContract
from app.models.maintenance_project_operations import MaintenanceCollectionSnapshot
from tests.boss_board_helpers import boss_client, make_project


@pytest.fixture(autouse=True)
def _flag_on():
    settings = get_settings()
    original = settings.maintenance_boss_dashboard_enabled
    settings.maintenance_boss_dashboard_enabled = True
    try:
        yield
    finally:
        settings.maintenance_boss_dashboard_enabled = original


def _contract(db, project, *, amount="1000.00", included=True,
              mapping="mapped", contract_no=None) -> MaintenanceProjectContract:
    relation = MaintenanceProjectContract(
        project_contract_id=str(uuid.uuid4()),
        project_id=project.project_id,
        contract_id=f"c-{uuid.uuid4().hex[:8]}",
        contract_no=contract_no or f"XSDD-{uuid.uuid4().hex[:8].upper()}",
        contract_amount=None,
        amount_inc_tax=Decimal(amount) if amount is not None else None,
        contract_status="执行中",
        status_mapping_state=mapping,
        status_mapping_version="v1",
        included_in_total=included,
        effective_from=date(2020, 1, 1),
        source="synthetic_test",
        version=1,
    )
    db.add(relation)
    db.flush()
    return relation


def _collected(db, project, relation, amount):
    db.add(MaintenanceCollectionSnapshot(
        collection_id=str(uuid.uuid4()),
        project_id=project.project_id,
        project_contract_id=relation.project_contract_id,
        report_month=date(2026, 8, 1),
        cumulative_amount=Decimal(amount),
        status="confirmed",
        source="direct_api",
        version=1,
    ))
    db.flush()


def _seed_payment_world(db):
    """A 收满（ongoing）、B 未收满（ongoing）、C 收满（ended）、D 收满（missing，
    回款已完成优先级更高）、E 合同事实不完整、F 无回款快照、G 超额回款。"""
    pa = make_project(db, code="收满进行中A", lifecycle="ongoing")
    pb = make_project(db, code="未收满B", lifecycle="ongoing")
    pc = make_project(db, code="收满已结束C", lifecycle="ended")
    pd = make_project(db, code="收满期限缺失D", lifecycle="missing")
    pe = make_project(db, code="合同不完整E", lifecycle="ongoing")
    pf = make_project(db, code="无回款F", lifecycle="ongoing")
    pg = make_project(db, code="超额回款G", lifecycle="ongoing")

    _collected(db, pa, _contract(db, pa, amount="1000.00"), "1000.00")
    _collected(db, pb, _contract(db, pb, amount="1000.00"), "500.00")
    _collected(db, pc, _contract(db, pc, amount="100.00"), "100.00")
    _collected(db, pd, _contract(db, pd, amount="100.00"), "100.00")
    # 含税额缺失 → 合同事实不完整 → 不判定（即使回款已收满）
    _collected(db, pe, _contract(db, pe, amount=None), "100.00")
    _contract(db, pf, amount="1000.00")              # 没有任何回款快照
    _collected(db, pg, _contract(db, pg, amount="1000.00"), "1200.00")
    db.commit()
    return {p.project_code: p.project_id for p in (pa, pb, pc, pd, pe, pf, pg)}


def _codes(client, lifecycle):
    resp = client.get("/api/maintenance/boss-board/projects",
                      params={"page": 1, "page_size": 50, "lifecycle": lifecycle,
                              "sort": "name"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return (body["rows"], body["total"],
            {row["project_code"] for row in body["rows"]})


def test_payment_complete_bucket_membership_and_lifecycle_priority(db):
    _seed_payment_world(db)
    client = boss_client(db, username="pay-complete-boss")

    rows, total, codes = _codes(client, "payment_complete")
    assert total == 4
    assert codes == {"收满进行中A", "收满已结束C", "收满期限缺失D", "超额回款G"}
    # 桶内行的 lifecycle 字段说回款已完成；回款/合同额照常可见
    row_a = next(r for r in rows if r["project_code"] == "收满进行中A")
    assert row_a["lifecycle"] == "payment_complete"
    assert Decimal(str(row_a["collection_preview_inc_tax"]["value"])) == Decimal(
        "1000.00")

    # 收满项目从三个期限桶整体移出（含期限缺失）：不再需要盯
    _, total_ongoing, ongoing = _codes(client, "ongoing")
    assert ongoing == {"未收满B", "合同不完整E", "无回款F"}
    assert total_ongoing == 3
    _, _, ended = _codes(client, "ended")
    assert ended == set()
    _, _, missing = _codes(client, "missing")
    assert missing == set()

    # lifecycle=all 不计算该集合：行保持纯期限口径（导出/单项目取数不受影响）
    all_rows, _, _ = _codes(client, "all")
    row_all_a = next(r for r in all_rows if r["project_code"] == "收满进行中A")
    assert row_all_a["lifecycle"] == "ongoing"


def test_payment_complete_requires_contract_permission(db):
    _seed_payment_world(db)
    no_profit = boss_client(db, username="pay-complete-no-profit",
                            with_profit=False)

    resp = no_profit.get("/api/maintenance/boss-board/projects",
                         params={"page": 1, "page_size": 20,
                                 "lifecycle": "payment_complete", "sort": "name"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "cost_contract_permission_required"

    # 无合同权限的账号三桶不做排除：收满项目照旧留在原期限桶，
    # 不能让其凭「项目从进行中消失」反推回款收满名单。
    _, _, ongoing = _codes(no_profit, "ongoing")
    assert "收满进行中A" in ongoing


def test_export_accepts_payment_complete_lifecycle(db):
    _seed_payment_world(db)
    client = boss_client(db, username="pay-complete-export")

    resp = client.post("/api/maintenance/boss-board/projects/export",
                       json={"fields": ["project_name", "lifecycle"],
                             "lifecycle": "payment_complete", "sort": "name"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-export-row-count"] == "4"
