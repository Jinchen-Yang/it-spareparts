"""访问/安全审计的 JSONB 序列化回归：不能吞掉带日期筛选的审计行。"""

import json
import logging
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import select

from app import config, security
from app.models.system import SysAccessLog


@pytest.fixture(params=["access", "security"])
def write_log(request, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_ACCESS_LOG", True)
    if request.param == "access":
        return lambda detail: security.record_access_log(
            security.UserContext(user_id="audit-user", role="boss"),
            "search", "analytics", filters=detail,
        )
    return lambda detail: security.record_security_event(
        "audit-user", "boss", "login_success", resource="analytics", detail=detail,
    )


@pytest.fixture
def log_records(monkeypatch):
    # Alembic/TestClient 会改变 logger 状态；直接挂 handler，避免依赖 root 传播。
    logger = logging.getLogger("access")
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "level", logging.WARNING)
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


@pytest.fixture
def typed_detail():
    return {
        "date_from": date(2026, 10, 18),
        "date_to": None,
        "nested": [{
            "timestamp": datetime(2026, 10, 18, 9, 30, 45, 123456,
                                  tzinfo=timezone(timedelta(hours=8))),
            "naive_timestamp": datetime(2026, 10, 18, 9, 30),
            "time": time(9, 30, 45),
            "amount": Decimal("12345678901234567890.1234567890"),
            "id": UUID("12345678-1234-5678-1234-567812345678"),
            "tags": {"乙", "甲"},
            "mixed_set": frozenset({1, "1"}),
            "empty_set": set(),
            "tuple": (date(2026, 10, 19), None),
            "path": Path("synthetic/report.csv"),
        }, None],
        "plain": {"enabled": True, "count": 3, "ratio": 1.25, "label": "合成筛选"},
    }


EXPECTED_DETAIL = {
    "date_from": "2026-10-18",
    "date_to": None,
    "nested": [{
        "timestamp": "2026-10-18T09:30:45.123456+08:00",
        "naive_timestamp": "2026-10-18T09:30:00",
        "time": "09:30:45",
        "amount": "12345678901234567890.1234567890",
        "id": "12345678-1234-5678-1234-567812345678",
        "tags": ["乙", "甲"],
        "mixed_set": [1, "1"],
        "empty_set": [],
        "tuple": ["2026-10-19", None],
        "path": "synthetic/report.csv",
    }, None],
    "plain": {"enabled": True, "count": 3, "ratio": 1.25, "label": "合成筛选"},
}


def test_date_filters_persist_in_jsonb(db, write_log):
    write_log({"date_from": date(2026, 10, 18), "date_to": None})

    row = db.scalars(select(SysAccessLog)).one()
    assert row.detail == {"date_from": "2026-10-18", "date_to": None}


def test_nested_types_persist_without_mutation_and_can_be_serialized_again(
    db, write_log, typed_detail, log_records,
):
    original = deepcopy(typed_detail)
    write_log(typed_detail)
    row = db.scalars(select(SysAccessLog)).one()

    assert row.username == "audit-user"
    assert row.role == "boss"
    assert row.resource == "analytics"
    assert row.detail == EXPECTED_DETAIL
    assert typed_detail == original
    write_log(row.detail)
    rows = db.scalars(select(SysAccessLog).order_by(SysAccessLog.id)).all()
    assert len(rows) == 2
    assert rows[1].detail == EXPECTED_DETAIL
    assert log_records == []


@pytest.mark.parametrize("detail", [None, {}, {"values": [None, False, 0, "", [], {}]}])
def test_existing_json_details_are_preserved(db, write_log, detail):
    write_log(detail)
    assert db.scalars(select(SysAccessLog.detail)).one() == detail


def test_commit_receives_plain_json_without_a_custom_encoder(
    monkeypatch, write_log, typed_detail, log_records,
):
    """模拟驱动的严格 JSON 编码：旧实现会 TypeError、吞错且无法完成 commit。"""
    import app.db as db_mod

    with pytest.raises(TypeError, match="date.*not JSON serializable"):
        json.dumps(typed_detail)

    session = Mock()
    committed = []

    def strict_commit():
        row = session.add.call_args.args[0]
        committed.append(json.loads(json.dumps(row.detail)))

    session.commit.side_effect = strict_commit
    monkeypatch.setattr(db_mod, "SessionLocal", Mock(return_value=session))
    write_log(typed_detail)

    assert committed == [EXPECTED_DETAIL]
    session.commit.assert_called_once_with()
    session.close.assert_called_once_with()
    assert log_records == []


@pytest.mark.parametrize("failure", ["commit", "encode", "cycle"])
def test_unexpected_failure_is_best_effort_private_and_closes_session(
    monkeypatch, write_log, log_records, failure,
):
    import app.db as db_mod

    class BrokenText:
        def __str__(self):
            raise TypeError("SENSITIVE_DETAIL_SENTINEL")

    detail = {"private": "SENSITIVE_DETAIL_SENTINEL"}
    session = Mock()
    if failure == "commit":
        session.commit.side_effect = RuntimeError("SENSITIVE_DETAIL_SENTINEL")
        error_type = "RuntimeError"
    elif failure == "encode":
        detail["broken"] = BrokenText()
        error_type = "TypeError"
    else:
        detail["cycle"] = detail
        error_type = "ValueError"
    monkeypatch.setattr(db_mod, "SessionLocal", Mock(return_value=session))

    assert write_log(detail) is None
    session.close.assert_called_once_with()
    if failure == "commit":
        session.commit.assert_called_once_with()
    else:
        session.add.assert_not_called()
        session.commit.assert_not_called()
    assert len(log_records) == 1
    record = log_records[0]
    assert record.levelno == logging.WARNING
    assert record.getMessage().endswith(f"write failed ({error_type})")
    assert "SENSITIVE_DETAIL_SENTINEL" not in record.getMessage()
    assert record.exc_info is None


def test_disabled_logging_skips_serialization_and_connection(monkeypatch, write_log, log_records):
    import app.db as db_mod

    session_factory = Mock()
    monkeypatch.setattr(db_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(config, "ENABLE_ACCESS_LOG", False)
    detail = {}
    detail["cycle"] = detail
    assert write_log(detail) is None
    session_factory.assert_not_called()
    assert log_records == []


@pytest.mark.parametrize("path, action", [
    ("/api/purchases/analytics/summary", "analytics_summary"),
    ("/api/purchases/analytics/orders", "analytics_orders"),
    ("/api/sales/analytics/summary", "sales_analytics_summary"),
    ("/api/sales/analytics/orders", "sales_analytics_orders"),
])
def test_analytics_date_filters_record_audit_rows(db, monkeypatch, path, action):
    from tests.boss_board_helpers import client_for

    monkeypatch.setattr(config, "ENABLE_ACCESS_LOG", True)
    client = client_for(db, username="analytics-audit-boss", role="boss")
    response = client.get(path, params={"date_from": "2026-10-01", "date_to": "2026-10-18"})
    assert response.status_code == 200, response.text
    row = db.scalars(select(SysAccessLog).where(
        SysAccessLog.username == "analytics-audit-boss", SysAccessLog.action == action,
    )).one()
    assert row.detail["date_from"] == "2026-10-01"
    assert row.detail["date_to"] == "2026-10-18"
