"""D-36 PR1：v1 整目录列举 /list 契约（keyset 翻页、墓碑一跳、截断、鉴权）。"""

import base64
import hashlib
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import event, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.resale_catalog_export import _encode_list_cursor
from app.config import get_settings
from app.db import engine, get_db
from app.main import app
from app.models.dimensions import DimPart
from app.models.master_data import ProductSpec

URL = "/api/integrations/resale/parts/list"
TOKEN = "synthetic-list-catalog-token-for-tests"
EXPECTED_KEYS = {
    "partId", "pnStd", "description", "brand", "categoryMajor", "categoryMinor",
    "unit", "needsReview", "isExcluded", "lockedFields", "updatedAt", "specs",
    "status", "mergedIntoId", "mergedIntoPnStd", "machineOrPart",
}


@pytest.fixture()
def client(db, monkeypatch):
    """已配置 catalog token 但未带 Authorization 头的裸客户端。"""
    monkeypatch.setattr(
        get_settings(), "resale_catalog_token_sha256",
        SecretStr(hashlib.sha256(TOKEN.encode()).hexdigest()),
    )
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as bare:
            yield bare
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


@pytest.fixture()
def catalog_client(client):
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    return client


def _parts(db, count=3, prefix="LIST", **columns):
    parts = [DimPart(pn_std=f"{prefix}-{i}", **columns) for i in range(count)]
    db.add_all(parts)
    db.flush()
    return parts


def _b64(plain: str) -> str:
    return base64.urlsafe_b64encode(plain.encode()).decode().rstrip("=")


def _walk(client, limit=3):
    pages = []
    cursor = None
    while True:
        body = {"limit": limit}
        if cursor is not None:
            body["cursor"] = cursor
        response = client.post(URL, json=body)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        payload = response.json()
        assert payload["schemaVersion"] == 1
        pages.append(payload)
        if not payload["hasMore"]:
            # 非空末页仍返回末行游标（续传水位）；仅空页为 null。
            if payload["items"]:
                assert payload["nextCursor"]
            else:
                assert payload["nextCursor"] is None
            return pages
        assert payload["nextCursor"]
        assert len(pages) < 1000, "翻页未收敛"
        cursor = payload["nextCursor"]


def test_disabled_without_configured_token(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "resale_catalog_token_sha256", SecretStr(""))
    response = client.post(URL, json={})
    assert response.status_code == 404
    assert response.json()["detail"] == "PN 只读导出尚未启用"


def test_rejects_missing_wrong_and_receipt_credentials(client, monkeypatch):
    receipt_token = "synthetic-receipt-token-for-list-cross-checks"
    monkeypatch.setattr(
        get_settings(), "resale_export_token_sha256",
        SecretStr(hashlib.sha256(receipt_token.encode()).hexdigest()),
    )
    missing = client.post(URL, json={})
    assert missing.status_code == 401
    assert missing.json()["detail"] == "PN 只读凭据无效"
    wrong = client.post(URL, json={}, headers={"Authorization": f"Bearer {'x' * 48}"})
    assert wrong.status_code == 401
    # 返件导出凭据必须换不到目录列举。
    cross = client.post(URL, json={}, headers={"Authorization": f"Bearer {receipt_token}"})
    assert cross.status_code == 401


def test_full_walk_returns_every_row_once(db, catalog_client):
    active = _parts(db, 5, prefix="WALK", description="w")
    active[1].needs_review = True
    active[2].is_excluded = True
    chain = _parts(db, 3, prefix="CHAIN")
    chain[0].status, chain[0].merged_into_id = "merged", chain[1].id
    chain[1].status, chain[1].merged_into_id = "merged", chain[2].id
    db.commit()

    pages = _walk(catalog_client, limit=3)
    assert len(pages) == 3
    ids = [row["partId"] for page in pages for row in page["items"]]
    assert len(ids) == len(set(ids)) == 8
    assert set(ids) == {p.id for p in active + chain}
    assert db.scalar(select(func.count()).select_from(DimPart)) == 8


def test_tombstone_reports_direct_parent_and_excluded_rows_still_listed(db, catalog_client):
    a, b, c = _parts(db, 3, prefix="CHAIN")
    a.status, a.merged_into_id = "merged", b.id
    b.status, b.merged_into_id = "merged", c.id
    excluded = DimPart(pn_std="EXCLUDED-1", is_excluded=True)
    db.add(excluded)
    db.commit()

    rows = {
        row["partId"]: row
        for page in _walk(catalog_client, limit=2)
        for row in page["items"]
    }
    # 一跳：A→B→C 时 A 指向直接父行 B，不解析到最终 C。
    assert rows[a.id]["status"] == "merged"
    assert rows[a.id]["mergedIntoId"] == b.id
    assert rows[a.id]["mergedIntoPnStd"] == b.pn_std
    assert rows[b.id]["mergedIntoPnStd"] == c.pn_std
    assert rows[c.id]["status"] == "active"
    assert rows[c.id]["mergedIntoId"] is None
    assert rows[c.id]["mergedIntoPnStd"] is None
    # 排除行仍进入目录（D-36：排除不产生商城业务影响）。
    assert rows[excluded.id]["isExcluded"] is True
    for row in rows.values():
        assert set(row) == EXPECTED_KEYS


def test_rows_sharing_one_timestamp_are_never_skipped(db, catalog_client):
    parts = _parts(db, 5, prefix="SAME-TS")
    db.commit()
    same = datetime(2026, 10, 1, 8, 0, 0, tzinfo=timezone.utc)
    db.execute(
        update(DimPart).where(DimPart.id.in_([p.id for p in parts])).values(updated_at=same)
    )
    db.commit()

    pages = _walk(catalog_client, limit=2)
    ids = [row["partId"] for page in pages for row in page["items"]]
    assert len(ids) == len(set(ids)) == 5
    # 游标落在同时间戳的较小 id 时，较大的 id 必须完整出现在下一页。
    sorted_ids = sorted(p.id for p in parts)
    response = catalog_client.post(
        URL, json={"limit": 10, "cursor": _encode_list_cursor(same, sorted_ids[2])}
    )
    assert response.status_code == 200, response.text
    assert [row["partId"] for row in response.json()["items"]] == sorted_ids[3:]


def test_cursor_roundtrip_keeps_microsecond_precision(db, catalog_client):
    first, second = _parts(db, 2, prefix="MICRO")
    db.commit()
    base = datetime(2026, 10, 1, 8, 0, 0, tzinfo=timezone.utc)
    db.execute(update(DimPart).where(DimPart.id == first.id).values(updated_at=base))
    db.execute(
        update(DimPart).where(DimPart.id == second.id)
        .values(updated_at=base + timedelta(microseconds=1))
    )
    db.commit()

    response = catalog_client.post(
        URL, json={"limit": 10, "cursor": _encode_list_cursor(base, first.id)}
    )
    assert response.status_code == 200, response.text
    ids = [row["partId"] for row in response.json()["items"]]
    assert second.id in ids
    assert first.id not in ids


def test_same_cursor_returns_identical_payload(db, catalog_client):
    _parts(db, 5, prefix="IDEM")
    db.commit()
    first = catalog_client.post(URL, json={"limit": 2})
    assert first.status_code == 200, first.text
    cursor = first.json()["nextCursor"]
    again = catalog_client.post(URL, json={"cursor": cursor, "limit": 2})
    twice = catalog_client.post(URL, json={"cursor": cursor, "limit": 2})
    assert again.status_code == twice.status_code == 200
    assert again.json() == twice.json()


def test_description_truncated_to_4000_chars_null_preserved(db, catalog_client):
    long_part, null_part = _parts(db, 2, prefix="DESC")
    long_part.description = "x" * 4100
    null_part.description = None
    db.commit()

    payload = catalog_client.post(URL, json={"limit": 10}).json()
    rows = {row["pnStd"]: row for row in payload["items"]}
    assert rows[long_part.pn_std]["description"] == "x" * 4000
    assert rows[null_part.pn_std]["description"] is None
    db.expire_all()
    assert len(db.get(DimPart, long_part.id).description) == 4100, "库内原文不得被改写"


def test_max_page_of_500_truncated_rows_fits_4mb(db, catalog_client):
    db.add_all(DimPart(pn_std=f"BIG-{i:04d}", description="a" * 4100) for i in range(500))
    db.commit()
    response = catalog_client.post(URL, json={"limit": 500})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["hasMore"] is False
    # 末页非空仍返回末行游标（续传水位），空页才为 null。
    assert payload["nextCursor"]
    assert len(payload["items"]) == 500
    assert all(len(row["description"]) == 4000 for row in payload["items"])
    assert len(response.content) <= 4 * 1024 * 1024


def test_cursor_past_end_returns_empty_page(db, catalog_client):
    _parts(db, 2, prefix="END")
    db.commit()
    future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    response = catalog_client.post(
        URL, json={"cursor": _encode_list_cursor(future, 999999999)}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload == {
        "schemaVersion": 1, "items": [], "hasMore": False, "nextCursor": None,
    }


@pytest.mark.parametrize("cursor", [
    "!!!not-base64url!!!",                                   # 非法字符
    "ab+cd/ef",                                              # 标准 base64 字符不在 urlsafe 字母表
    _b64("2026-10-01T08:00:00.000000+00:00"),                # 无 | 分隔
    _b64("not-a-time|1"),                                    # 坏时间
    _b64("2026-10-01T08:00:00.000000|1"),                    # 裸时间戳（无时区）
    _b64("2026-10-01T08:00:00.000000+00:00|abc"),            # 非整数 id
    _b64("2026-10-01T08:00:00.000000+00:00|1|2"),            # id 段带分隔符
    _b64("2026-10-01T08:00:00.000000+00:00|0"),              # id 非正
    _b64("2026-10-01T08:00:00.000000+00:00|+1"),             # 带符号
    _b64("2026-10-01T08:00:00.000000+00:00|1_0"),            # 下划线分组
    _b64("2026-10-01T08:00:00.000000+00:00| 1"),             # 空白
    _b64("2026-10-01T08:00:00.000000+00:00|\uff11"),         # 全角数字
    _b64("2026-10-01T08:00:00.000000+00:00|\u0661"),         # 阿拉伯-印度数字
    _b64(f"2026-10-01T08:00:00.000000+00:00|{2**63}"),       # 超出 bigint
    "",
])
def test_invalid_cursors_rejected(cursor, catalog_client):
    response = catalog_client.post(URL, json={"cursor": cursor, "limit": 1})
    assert response.status_code == 422
    assert response.json()["detail"] == "游标无效"


@pytest.mark.parametrize("plain", [
    "0001-01-01T00:00:00+01:00|1",   # 换算 UTC 低于 datetime.min
    "9999-12-31T23:00:00-05:00|1",   # 换算 UTC 高于 datetime.max
])
def test_out_of_range_offset_cursor_is_422_not_500(plain, catalog_client):
    response = catalog_client.post(URL, json={"cursor": _b64(plain), "limit": 1})
    assert response.status_code == 422
    assert response.json()["detail"] == "游标无效"


def test_final_page_cursor_yields_empty_continuation(db, catalog_client):
    """商城追平末尾后可保存续传水位：末页非空 → nextCursor 非空；再用它请求得空页。"""
    _parts(db, 4, prefix="WATERMARK")
    db.commit()
    pages = _walk(catalog_client, limit=3)   # 3 + 1 两页
    final = pages[-1]
    assert final["hasMore"] is False
    assert len(final["items"]) == 1
    assert final["nextCursor"]

    response = catalog_client.post(
        URL, json={"cursor": final["nextCursor"], "limit": 3}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload == {
        "schemaVersion": 1, "items": [], "hasMore": False, "nextCursor": None,
    }
    # 同水位重复请求仍为空页（幂等）。
    repeat = catalog_client.post(URL, json={"cursor": final["nextCursor"], "limit": 3})
    assert repeat.json() == payload


@pytest.mark.parametrize("body", [
    {"limit": 0}, {"limit": 501}, {"limit": -1}, {"limit": 1.5},
    {"limit": 1, "extra": 1}, {"cursor": "x" * 513},
])
def test_request_bounds_rejected(body, catalog_client):
    assert catalog_client.post(URL, json=body).status_code == 422


def test_default_limit_is_200_and_bounds_accepted(db, catalog_client):
    db.add_all(DimPart(pn_std=f"DEF-{i:03d}") for i in range(205))
    db.commit()
    payload = catalog_client.post(URL, json={}).json()
    assert len(payload["items"]) == 200
    assert payload["hasMore"] is True
    assert payload["nextCursor"]
    single = catalog_client.post(URL, json={"limit": 1})
    assert single.status_code == 200
    assert len(single.json()["items"]) == 1
    assert single.json()["hasMore"] is True


def test_page_shape_has_one_cutover_check_and_two_data_queries(db, catalog_client):
    p1, p2, p3 = _parts(db, 3, prefix="SHAPE")
    for part, key in ((p1, "capacity"), (p1, "rpm"), (p2, "interface")):
        db.add(ProductSpec(part_id=part.id, spec_key=key, spec_value="v", source="manual"))
    db.commit()

    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = catalog_client.post(URL, json={"limit": 3})
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert response.status_code == 200, response.text
    # 每页恰好一条 dim_part 页查询 + 一条 product_specs IN 查询，禁止逐行 db.get。
    assert len(statements) == 3
    assert sum("sys_authorization_cutover" in statement for statement in statements) == 1
    assert sum(1 for s in statements if "from dim_part" in s.lower()) == 1
    assert sum(1 for s in statements if "product_specs" in s.lower()) == 1
    combined = " ".join(statements).lower()
    assert "part_alias" not in combined
    assert "f_sales" not in combined
    assert "f_purchase" not in combined
    rows = response.json()["items"]
    assert {row["pnStd"] for row in rows} == {p.pn_std for p in (p1, p2, p3)}
    by_id = {row["partId"]: row for row in rows}
    # specs 按 (part_id, spec_key) 排序输出。
    assert [spec["key"] for spec in by_id[p1.id]["specs"]] == ["capacity", "rpm"]
    assert [spec["key"] for spec in by_id[p2.id]["specs"]] == ["interface"]
    assert by_id[p3.id]["specs"] == []
    for row in rows:
        assert set(row) == EXPECTED_KEYS
        assert "salesRecordCount" not in row
        assert "excludeReason" not in row
        assert "dataQualityScore" not in row
        assert "reviewedAt" not in row


def test_thirty_thousand_rows_full_walk(db, catalog_client):
    total, chunk = 30_000, 1_000
    for start in range(0, total, chunk):
        db.execute(pg_insert(DimPart).values(
            [{"pn_std": f"BULK-{i:05d}"} for i in range(start, start + chunk)]
        ))
    db.commit()

    started = time.perf_counter()
    pages = _walk(catalog_client, limit=500)
    elapsed = time.perf_counter() - started
    ids = [row["partId"] for page in pages for row in page["items"]]
    assert len(pages) == 60
    assert len(ids) == len(set(ids)) == total
    assert db.scalar(select(func.count()).select_from(DimPart)) == total
    # 本地实测 ~7 s；预算放宽到 180 s，避免共享 CI 机器抖动误报（只防数量级退化）。
    assert elapsed < 180, f"30000 行全遍历耗时 {elapsed:.1f}s 超预算"
    print(f"\n[/list 规模] 30000 行 / {len(pages)} 页 / {elapsed:.2f}s")

