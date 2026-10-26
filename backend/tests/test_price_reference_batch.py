"""售前询价 M3 价格参考批量服务/接口（T10）。

对拍口径：batch 的销售/采购均价、样本数、订单数与单 PN ``build_price_reference``
逐值一致；窗口、税口径、零价/状态/计营收过滤全部沿用商城参考价。
"""

import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app import permissions
from app.auth import _make_token
from app.config import get_settings
from app.db import engine, get_db
from app.main import app
from app.models.dimensions import DimPart
from app.models.inquiry import FPartInquiry
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysAccessLog, SysImportBatch
from app.security import UserContext
from app.services import part_overview, price_reference
from app.services.resale_price_reference import build_price_reference

URL = "/api/parts/price-refs"
TODAY = date(2026, 10, 19)
WINDOW_START = TODAY - timedelta(days=30 - 1)


# ---------------------------------------------------------------- 夹具


def _batch(db, file_type="sales"):
    batch = SysImportBatch(
        filename=f"synthetic-price-batch-{file_type}.xlsx",
        file_type=file_type,
        file_hash=uuid.uuid4().hex + uuid.uuid4().hex,
    )
    db.add(batch)
    db.flush()
    return batch


def _part(db, pn, **values):
    part = DimPart(pn_std=pn, **values)
    db.add(part)
    db.flush()
    return part


def _sale(
    db,
    batch,
    part,
    key,
    *,
    order_date=TODAY,
    status="已生效",
    tax_rate=Decimal("0.13"),
    lines=((Decimal(1), Decimal("100.00"), True),),
):
    order = FSalesOrder(
        raw_order_id=key,
        order_no=f"SO-{key}",
        business_type="备件销售",
        data_status=status,
        order_date=order_date,
        tax_rate=tax_rate,
        import_batch_id=batch.id,
    )
    db.add(order)
    db.flush()
    for index, (qty, price, counts_revenue) in enumerate(lines):
        db.add(FSalesLine(
            raw_line_id=f"{key}-{index}",
            order_id=order.id,
            part_id=part.id,
            pn_std=part.pn_std,
            qty=qty,
            unit_price=price,
            counts_revenue=counts_revenue,
            import_batch_id=batch.id,
        ))
    db.flush()
    return order


def _purchase(
    db,
    batch,
    part,
    key,
    *,
    order_date=TODAY,
    status="已生效",
    tax_rate=Decimal("0.13"),
    is_tax_inclusive=True,
    lines=((Decimal(1), Decimal("113.00")),),
):
    order = FPurchaseOrder(
        raw_order_id=key,
        order_no=f"PO-{key}",
        data_status=status,
        order_date=order_date,
        tax_rate=tax_rate,
        is_tax_inclusive=is_tax_inclusive,
        import_batch_id=batch.id,
    )
    db.add(order)
    db.flush()
    for index, (qty, price) in enumerate(lines):
        db.add(FPurchaseLine(
            raw_line_id=f"{key}-{index}",
            order_id=order.id,
            part_id=part.id,
            pn_std=part.pn_std,
            qty=qty,
            unit_price=price,
            import_batch_id=batch.id,
        ))
    db.flush()
    return order


def _inquiry(db, batch, *, money, apply_time, part=None, pn=None):
    db.add(FPartInquiry(
        raw_row_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        apply_time=apply_time,
        pn_std=pn if pn is not None else (part.pn_std if part else None),
        part_id=part.id if part else None,
        money=money,
        import_batch_id=batch.id,
    ))
    db.flush()


def _ctx(role="admin", **overrides):
    perms = permissions.effective(role, None)
    perms.update(overrides)
    return UserContext(
        user_id=f"test-{role}",
        role=role,
        permissions=perms,
        is_authenticated=True,
    )


def _headers(role="admin", **overrides):
    perms = permissions.effective(role, None)
    perms.update(overrides)
    token, _ = _make_token(
        role, f"test-{role}", "测试", perms=perms, authn="sys_user"
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def client(db):
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


def _seed_two_parts(db):
    sales_batch = _batch(db, "sales")
    purchase_batch = _batch(db, "purchase")
    first = _part(db, "BATCH-PN-A")
    second = _part(db, "BATCH-PN-B")
    _sale(
        db, sales_batch, first, "a-today",
        lines=((Decimal(1), Decimal("113.00"), True),),
    )
    _sale(
        db, sales_batch, first, "a-week-ago", order_date=date(2026, 10, 12),
        tax_rate=Decimal(0),
        lines=((Decimal(2), Decimal("226.00"), True),),
    )
    # 零价、非已生效、不计营收都不应进入参考价（单 PN / 批量同一过滤）。
    _sale(
        db, sales_batch, first, "a-zero", order_date=date(2026, 10, 5),
        lines=((Decimal(1), Decimal(0), True),),
    )
    _sale(
        db, sales_batch, first, "a-cancelled", order_date=date(2026, 10, 4),
        status="已取消", lines=((Decimal(1), Decimal("999.00"), True),),
    )
    _sale(
        db, sales_batch, first, "a-not-revenue", order_date=date(2026, 10, 3),
        lines=((Decimal(1), Decimal("888.00"), False),),
    )
    _purchase(
        db, purchase_batch, first, "a-purchase-recent",
        order_date=date(2026, 10, 18), tax_rate=Decimal(0),
        is_tax_inclusive=False,
        lines=((Decimal(2), Decimal("100.00")), (Decimal(1), Decimal("200.00"))),
    )
    _purchase(
        db, purchase_batch, first, "a-purchase-older",
        order_date=date(2026, 10, 11), tax_rate=None,
        lines=((Decimal(1), Decimal("113.00")),),
    )
    _purchase(
        db, purchase_batch, first, "a-purchase-zero",
        order_date=date(2026, 10, 9),
        lines=((Decimal(1), Decimal(0)),),
    )
    _sale(
        db, sales_batch, second, "b-sale",
        lines=((Decimal(3), Decimal("339.00"), True),),
    )
    _purchase(
        db, purchase_batch, second, "b-purchase",
        lines=((Decimal(2), Decimal("226.00")),),
    )
    db.commit()
    return first, second


# ---------------------------------------------------------------- 服务对拍


def test_batch_matches_single_pn_shop_reference(db):
    first, second = _seed_two_parts(db)
    ctx = _ctx()
    settings = get_settings()

    result = price_reference.batch(
        db, [first.id, second.id], ctx, today=TODAY
    )

    for part in (first, second):
        single = build_price_reference(db, part.pn_std, settings, today=TODAY)
        by_basis = {
            (entry.kind, entry.tax_basis): entry for entry in single.prices
        }
        item = result[part.id]
        assert item["window"] == {
            "start": WINDOW_START, "end": TODAY, "days": 30
        }
        assert item["method"] == "time_decay_qty_weighted"
        assert item["purchase_restricted"] is False
        for kind in ("sale", "purchase"):
            inc = by_basis.get((kind, "inc_tax"))
            ex = by_basis.get((kind, "ex_tax"))
            if inc is None:
                assert item[kind] is None
                continue
            assert item[kind] == {
                "avg_inc_tax": inc.amount,
                "avg_ex_tax": ex.amount,
                "sample_count": inc.sample_count,
                "order_count": inc.order_count,
                "latest_date": inc.latest_sale_date,
                "oldest_date": inc.oldest_sale_date,
            }


def test_batch_inquiry_ref_matches_single_part_overview(db):
    first, _second = _seed_two_parts(db)
    batch = _batch(db, "inquiry")
    _inquiry(
        db, batch, part=first, money=Decimal("100.00"),
        apply_time=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    _inquiry(
        db, batch, part=first, money=Decimal("300.00"),
        apply_time=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    # part_id 未回填的历史行按 pn_std 兜底，与单 part 版一致。
    _inquiry(
        db, batch, pn=first.pn_std, money=Decimal("200.00"),
        apply_time=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )
    db.commit()

    result = price_reference.batch(db, [first.id], _ctx(), today=TODAY)
    expected = part_overview._inquiry_ref(db, first.id, first.pn_std)
    assert result[first.id]["inquiry_ref"] == expected


# ---------------------------------------------------------------- 窗口/空样本


def test_window_includes_first_and_last_day_only(db):
    part = _part(db, "WINDOW-PN")
    batch = _batch(db)
    for key, day in (
        ("before", WINDOW_START - timedelta(days=1)),
        ("first", WINDOW_START),
        ("last", TODAY),
        ("after", TODAY + timedelta(days=1)),
    ):
        _sale(
            db, batch, part, f"window-{key}", order_date=day,
            lines=((Decimal(1), Decimal("113.00"), True),),
        )
    db.commit()

    item = price_reference.batch(db, [part.id], _ctx(), today=TODAY)[part.id]
    assert item["sale"]["sample_count"] == 2
    assert item["sale"]["order_count"] == 2
    assert item["sale"]["oldest_date"] == WINDOW_START
    assert item["sale"]["latest_date"] == TODAY
    assert item["window"] == {"start": WINDOW_START, "end": TODAY, "days": 30}


def test_empty_samples_return_null_sale(db):
    part = _part(db, "EMPTY-PN")
    db.commit()

    item = price_reference.batch(db, [part.id], _ctx(), today=TODAY)[part.id]
    assert item["sale"] is None
    assert item["purchase"] is None
    assert item["purchase_restricted"] is False
    assert item["inquiry_ref"] == {
        "min_money": None, "max_money": None, "last_money": None, "count": 0,
    }


# ---------------------------------------------------------------- 权限与查询预算


def test_no_purchase_cost_does_not_query_purchase_tables(db):
    part, _second = _seed_two_parts(db)
    statements = []

    def capture(_conn, _cursor, statement, _params, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        item = price_reference.batch(
            db, [part.id], _ctx("sales", data_purchase_cost=False), today=TODAY
        )[part.id]
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert item["purchase_restricted"] is True
    assert item["purchase"] is None
    assert item["sale"] is not None
    lowered = "\n".join(statements).lower()
    assert "f_purchase_line" not in lowered
    assert "f_purchase_order" not in lowered


def test_batch_uses_four_queries_for_all_parts(db):
    first, second = _seed_two_parts(db)
    part_ids = [first.id, second.id]
    statements = []

    def capture(_conn, _cursor, statement, _params, _context, _executemany):
        if statement.lstrip().lower().startswith("select"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = price_reference.batch(
            db, part_ids, _ctx(), today=TODAY
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert set(result) == {first.id, second.id}
    assert len(statements) == 4
    assert sum("from f_sales_line" in sql.lower() for sql in statements) == 1
    assert sum("from f_purchase_line" in sql.lower() for sql in statements) == 1
    assert sum("from f_part_inquiry" in sql.lower() for sql in statements) == 1


# ---------------------------------------------------------------- 接口


def test_endpoint_requires_login_and_validates_bounds(client):
    assert client.post(URL, json={"part_ids": [1]}).status_code == 401

    headers = _headers()
    assert client.post(URL, json={"part_ids": []}, headers=headers).status_code == 422
    assert client.post(
        URL, json={"part_ids": list(range(1, 202))}, headers=headers
    ).status_code == 422
    assert client.post(
        URL, json={"part_ids": [1], "window_days": 0}, headers=headers
    ).status_code == 422
    assert client.post(
        URL, json={"part_ids": [1], "window_days": 367}, headers=headers
    ).status_code == 422


def test_endpoint_dedupes_excludes_merged_and_audits_count(client, db):
    target = _part(db, "API-ACTIVE")
    second = _part(db, "API-ACTIVE-2")
    merged = _part(db, "API-MERGED", status="merged", merged_into_id=target.id)
    db.commit()

    response = client.post(
        URL,
        json={"part_ids": [target.id, second.id, target.id, merged.id, 999999]},
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body["items"]) == {str(target.id), str(second.id)}

    db.expire_all()
    audit = db.scalar(
        select(SysAccessLog).where(SysAccessLog.action == "price_refs")
    )
    assert audit is not None
    assert audit.resource == "part"
    assert audit.detail == {"count": 4}


def test_endpoint_accepts_200_unique_ids(client, db):
    parts = [_part(db, f"API-MAX-{index}") for index in range(200)]
    db.commit()

    response = client.post(
        URL, json={"part_ids": [part.id for part in parts]}, headers=_headers()
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 200


# ---------------------------------------------------------------- 性能


@pytest.mark.skipif(
    os.environ.get("RUN_PRICE_REF_PERF") != "1",
    reason="性能测试不在默认 pytest 中跑；设置 RUN_PRICE_REF_PERF=1 开启",
)
def test_batch_performance_50_parts_20_samples(db):
    sales_batch = _batch(db, "sales")
    purchase_batch = _batch(db, "purchase")
    parts = [_part(db, f"PERF-PN-{index:03d}") for index in range(50)]
    for part_index, part in enumerate(parts):
        for index in range(20):
            day = TODAY - timedelta(days=index)
            _sale(
                db, sales_batch, part, f"perf-s-{part_index}-{index}",
                order_date=day,
                lines=((Decimal(1), Decimal(113 + index), True),),
            )
            _purchase(
                db, purchase_batch, part, f"perf-p-{part_index}-{index}",
                order_date=day,
                lines=((Decimal(1), Decimal(100 + index)),),
            )
    db.commit()

    ctx = _ctx()
    part_ids = [part.id for part in parts]
    timings = []
    for _attempt in range(3):
        started = time.perf_counter()
        result = price_reference.batch(db, part_ids, ctx, today=TODAY)
        timings.append((time.perf_counter() - started) * 1000)
    assert len(result) == 50
    assert all(item["sale"]["sample_count"] == 20 for item in result.values())
    assert all(item["purchase"]["sample_count"] == 20 for item in result.values())
    best = min(timings)
    print(
        "batch 50 parts x 20 samples: "
        f"runs={['%.1f' % value for value in timings]} ms, best={best:.1f} ms"
    )
    assert best <= 300
