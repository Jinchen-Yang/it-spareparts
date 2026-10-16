"""真实 PostgreSQL 摘要故障不应阻断只读主体数据。"""

import logging
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionLocal, engine, get_db
from app.main import app
from app.models.dimensions import DimPart
from app.services.maintenance_return_request_summary import ReturnSummaryError
from tests.boss_board_helpers import make_project
from tests.test_maintenance_boss_board_return_summary import _demand
from tests.test_maintenance_demand_safe_delete import _admin_client


@pytest.mark.parametrize("view", ["search", "detail", "board"])
def test_real_database_error_keeps_primary_payload_and_transaction_usable(
    db, view, monkeypatch, caplog,
):
    project = make_project(db, "真实数据库摘要故障")
    part = DimPart(pn_std=f"REAL-SUMMARY-ERROR-{uuid4()}")
    db.add(part)
    db.commit()
    order = _demand(db, project, part, 1, "5")
    source_id, project_id, part_id = order.raw_order_id, project.project_id, part.id
    client = _admin_client(db)

    def request():
        if view == "search":
            return client.post("/api/maintenance/demands/search", json={"q": source_id})
        if view == "detail":
            return client.get(f"/api/maintenance/demands/orders/{source_id}/lines")
        return client.get(f"/api/maintenance/boss-board/projects/{project_id}/orders")

    def summary_row(payload):
        if view == "search":
            return payload["items"][0]
        if view == "board":
            return payload["rows"][0]
        return payload

    normal = request()
    assert normal.status_code == 200, normal.text
    expected = normal.json()
    assert summary_row(expected).pop("return_summary")["demand_qty"] == "5.000"
    assert "return_summary_error" not in summary_row(expected)
    db.rollback()

    checked_transactions = []

    def checked_db():
        # 仍是真实 Session/数据库，只在响应完成后核验同一请求事务未被毒化。
        with SessionLocal() as request_db:
            pending = DimPart(pn_std="SUMMARY-FAILURE-PENDING-DRAFT")
            dirty = request_db.get(DimPart, part_id)
            dirty.category_major = "未提交的品类草稿"
            request_db.add(pending)
            yield request_db
            # Session.begin_nested() 会 flush；全事务 rollback 会清掉草稿。
            # 三种页面富化均须避免这两种副作用。
            assert pending in request_db.new and pending.id is None
            assert dirty in request_db.dirty
            assert dirty.category_major == "未提交的品类草稿"
            assert request_db.scalar(select(DimPart.category_major).where(
                DimPart.id == part_id,
            )) is None
            checked_transactions.append(request_db.scalar(select(1)))

    logger_name = {
        "search": "app.services.maintenance_demands",
        "detail": "app.api.maintenance_demands",
        "board": "app.services.maintenance_boss_board",
    }[view]
    monkeypatch.setattr(logging.getLogger(logger_name), "disabled", False)
    app.dependency_overrides[get_db] = checked_db
    # 仅 pytest 独占本地数据库。临时改名摘要专用日期列，让实际 SELECT 得到
    # 42703；表本身保留，Board 主体的既有实收事实仍可正常读取。
    # 不 mock 汇总、SQLAlchemy 或驱动；finally 恢复，不影响其他测试。
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE maintenance_rkd_return_line "
            "RENAME COLUMN occurred_at TO summary_error_test_occurred_at"
        ))
    try:
        with caplog.at_level(logging.ERROR, logger=logger_name):
            degraded = request()
        assert degraded.status_code == 200, degraded.text
        actual = degraded.json()
        row = summary_row(actual)
        assert row.pop("return_summary") is None
        assert row.pop("return_summary_error") == "summary_unavailable"
        assert actual == expected  # 包括列表分页、成本/联系人和删除/明细 digest。
        assert checked_transactions == [1]
        errors = [record.exc_info[1] for record in caplog.records
                  if record.name == logger_name and record.exc_info]
        assert len(errors) == 1
        assert isinstance(errors[0], ReturnSummaryError)
        assert isinstance(errors[0].__cause__, SQLAlchemyError)
        assert errors[0].__cause__.orig.sqlstate == "42703"
    finally:
        app.dependency_overrides.pop(get_db, None)
        with engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE maintenance_rkd_return_line "
                "RENAME COLUMN summary_error_test_occurred_at TO occurred_at"
            ))

    restored = request()
    assert restored.status_code == 200, restored.text
    restored_row = summary_row(restored.json())
    assert restored_row["return_summary"]["demand_qty"] == "5.000"
    assert "return_summary_error" not in restored_row
