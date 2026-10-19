"""商城销售参考均价 GET：认证、PN 身份、窗口、时间衰减、税率、过滤与隐私。

口径见 docs/decisions/0020-商城参考价销售均价.md 与
docs/integrations/resale-catalog-v1.md 的参考价一节。
"""

import hashlib
import json
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings, get_settings
from app.db import engine, get_db
from app.main import app
from app.models.data_quality import FactDataQualityIssue
from app.models.dimensions import DimPart, PartAlias
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysImportBatch
from app.services import resale_price_reference as price_service
from app.services.resale_pn_lookup import canonical_part
from app.services.resale_price_reference import PriceReferenceUnavailable, _validated_amount

URL = "/api/integrations/resale/parts/price-reference"
RESOLVE_URL = "/api/integrations/resale/parts/resolve"
TOKEN = "synthetic-price-reference-catalog-token-for-tests"
TODAY = date(2026, 10, 19)
WINDOW_START = TODAY - timedelta(days=29)  # 默认 30 天，含今天
CONTRACT = (
    Path(__file__).parent / "fixtures" / "resale_price_reference_contract.json"
)


@pytest.fixture()
def catalog_client(db, monkeypatch):
    monkeypatch.setattr(
        get_settings(),
        "resale_catalog_token_sha256",
        SecretStr(hashlib.sha256(TOKEN.encode()).hexdigest()),
    )
    monkeypatch.setattr(price_service, "business_today", lambda: TODAY)
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            client.headers["Authorization"] = f"Bearer {TOKEN}"
            yield client
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


def _configure(monkeypatch, **values):
    settings = get_settings()
    for name, value in values.items():
        monkeypatch.setattr(
            settings, f"resale_price_reference_{name}", value
        )


def _batch(db):
    batch = SysImportBatch(
        filename="synthetic-price-reference.xlsx",
        file_type="sales",
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


def _alias(db, raw, part, status="active"):
    alias = PartAlias(
        pn_raw=raw, pn_std=part.pn_std, part_id=part.id, status=status
    )
    db.add(alias)
    db.flush()
    return alias


def _sale(
    db,
    batch,
    part,
    key,
    *,
    order_date=TODAY,
    status="已生效",
    business_type="备件销售",
    tax_rate=Decimal("0.13"),
    is_tax_inclusive=None,
    lines=((Decimal(1), Decimal("100.00"), True),),
):
    order = FSalesOrder(
        raw_order_id=key,
        order_no=f"PRIVATE-ORDER-{key}",
        salesperson="PRIVATE-PERSON",
        business_type=business_type,
        data_status=status,
        order_date=order_date,
        tax_rate=tax_rate,
        is_tax_inclusive=is_tax_inclusive,
        import_batch_id=batch.id,
    )
    db.add(order)
    db.flush()
    created = []
    for index, (qty, price, counts_revenue) in enumerate(lines):
        line = FSalesLine(
            raw_line_id=f"{key}-{index}",
            order_id=order.id,
            part_id=part.id,
            pn_std=part.pn_std,
            pn_raw="PRIVATE-RAW-PN",
            description="PRIVATE-DESCRIPTION",
            qty=qty,
            unit_price=price,
            counts_revenue=counts_revenue,
            import_batch_id=batch.id,
        )
        db.add(line)
        created.append(line)
    db.flush()
    return order, created


def _issue(db, line, status, rule_code="zero_price"):
    issue = FactDataQualityIssue(
        side="sales",
        line_id=line.id,
        part_id=line.part_id,
        rule_code=rule_code,
        rule_version="v1",
        evidence={},
        source_fingerprint=f"fingerprint-{line.id}-{rule_code}",
        status=status,
        detected_by="pytest",
    )
    db.add(issue)
    db.flush()
    return issue


def _get(client, pn="DEMO-PN"):
    return client.get(URL, params={"pn": pn})


def _amounts(payload):
    return {row["tax_basis"]: row["amount"] for row in payload["prices"]}


# ---------------------------------------------------------------- 契约夹具


def test_contract_fixture_matches_exactly(db, catalog_client):
    """同一份合成 JSON 锁定 itsp 响应与 shop DTO 契约（固定今天=2026-10-19）。"""
    part = _part(db, "DEMO-PN")
    batch = _batch(db)
    _sale(
        db, batch, part, "contract-today",
        order_date=date(2026, 10, 19),
        lines=((Decimal(1), Decimal("113.00"), True),),
    )
    _sale(
        db, batch, part, "contract-week-ago",
        order_date=date(2026, 10, 12),
        lines=((Decimal(1), Decimal("226.00"), True),),
    )
    db.commit()

    response = _get(catalog_client, "DEMO-PN")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    expected = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert response.json() == expected


# ---------------------------------------------------------------- 认证


def test_auth_and_no_store(db, catalog_client, monkeypatch):
    part = _part(db, "AUTH-PN")
    db.commit()
    response = _get(catalog_client, part.pn_std)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    empty = _get(catalog_client, "DOES-NOT-EXIST")
    assert empty.status_code == 200, empty.text
    assert empty.json()["prices"] == []
    assert empty.headers["cache-control"] == "no-store"

    monkeypatch.setattr(
        get_settings(), "resale_catalog_token_sha256", SecretStr("")
    )
    disabled = _get(catalog_client, part.pn_std)
    assert disabled.status_code == 404
    assert disabled.json()["detail"] == "PN 只读导出尚未启用"


def test_auth_rejects_missing_wrong_and_other_integration_credentials(
    db, monkeypatch
):
    monkeypatch.setattr(
        get_settings(),
        "resale_catalog_token_sha256",
        SecretStr(hashlib.sha256(TOKEN.encode()).hexdigest()),
    )
    receipt_token = "synthetic-receipt-token-for-price-reference"
    monkeypatch.setattr(
        get_settings(),
        "resale_export_token_sha256",
        SecretStr(hashlib.sha256(receipt_token.encode()).hexdigest()),
    )
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            missing = client.get(URL, params={"pn": "DEMO-PN"})
            assert missing.status_code == 401
            assert missing.json()["detail"] == "PN 只读凭据无效"
            wrong = client.get(
                URL,
                params={"pn": "DEMO-PN"},
                headers={"Authorization": f"Bearer {'x' * 48}"},
            )
            assert wrong.status_code == 401
            cross = client.get(
                URL,
                params={"pn": "DEMO-PN"},
                headers={"Authorization": f"Bearer {receipt_token}"},
            )
            assert cross.status_code == 401
            # 只接受原始凭据；把配置里的 SHA-256 摘要本身当 Bearer 必须拒绝。
            digest_as_bearer = client.get(
                URL,
                params={"pn": "DEMO-PN"},
                headers={
                    "Authorization": (
                        f"Bearer {hashlib.sha256(TOKEN.encode()).hexdigest()}"
                    )
                },
            )
            assert digest_as_bearer.status_code == 401
            malformed = client.get(
                URL,
                params={"pn": "DEMO-PN"},
                headers={"Authorization": TOKEN},
            )
            assert malformed.status_code == 401
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


# ---------------------------------------------------------------- PN 校验与回显


def test_pn_validation_and_echo(db, catalog_client):
    part = _part(db, "ECHO-1")
    batch = _batch(db)
    _sale(db, batch, part, "echo-sale")
    vpart = _part(db, "VCODE-1")
    _sale(db, batch, vpart, "vcode-sale")
    alias_part = _part(db, "ALIAS-TARGET")
    _alias(db, "ALIAS-RAW", alias_part)
    _sale(db, batch, alias_part, "alias-sale")
    db.commit()

    assert catalog_client.get(URL).status_code == 422
    assert catalog_client.get(URL, params={"pn": ""}).status_code == 422
    assert catalog_client.get(URL, params={"pn": "   "}).status_code == 422
    assert (
        catalog_client.get(URL, params={"pn": "X" * 257}).status_code == 422
    )
    one_char = _get(catalog_client, "X")
    assert one_char.status_code == 200, one_char.text
    assert one_char.json()["pn"] == "X"
    longest_pn = "Y" * 256
    longest = _get(catalog_client, longest_pn)
    assert longest.status_code == 200, longest.text
    assert longest.json()["pn"] == longest_pn

    spaced = _get(catalog_client, "  echo-1  ")
    assert spaced.status_code == 200, spaced.text
    assert spaced.json()["pn"] == "  echo-1  "
    assert spaced.json()["calculation"]["sample_count"] == 1

    vcoded = _get(catalog_client, "VCODE-1 V0230LD000000000")
    assert vcoded.status_code == 200, vcoded.text
    assert vcoded.json()["pn"] == "VCODE-1 V0230LD000000000"
    assert vcoded.json()["calculation"]["sample_count"] == 1

    alias = _get(catalog_client, "alias-raw")
    assert alias.status_code == 200, alias.text
    assert alias.json()["pn"] == "alias-raw"
    assert alias.json()["calculation"]["sample_count"] == 1

    unknown = _get(catalog_client, "UNKNOWN-PN")
    assert unknown.status_code == 200, unknown.text
    assert unknown.json()["prices"] == []
    assert unknown.json()["calculation"]["sample_count"] == 0


# ---------------------------------------------------------------- 别名与合并


def test_pending_alias_is_ambiguous(db, catalog_client):
    part = _part(db, "PENDING-TARGET")
    _alias(db, "PENDING-RAW", part, status="pending")
    db.commit()
    response = _get(catalog_client, "PENDING-RAW")
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == "PN 身份无法唯一确定"


def test_two_distinct_canonical_targets_are_ambiguous(db, catalog_client):
    first = _part(db, "DUP-PN")
    second = _part(db, "OTHER-PN")
    _alias(db, "DUP-PN", second)
    db.commit()
    response = _get(catalog_client, "DUP-PN")
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"


def test_tombstone_and_target_alias_converge_on_one_identity(db, catalog_client):
    target = _part(db, "DST-PN")
    source = _part(db, "SRC-PN", status="merged", merged_into_id=target.id)
    _alias(db, "SRC-PN", target)
    # 别名直接指向墓碑时，候选也必须继续沿合并链走到最终身份。
    _alias(db, "TOMBSTONE-ALIAS", source)
    batch = _batch(db)
    _sale(db, batch, target, "merged-sale")
    db.commit()

    response = _get(catalog_client, "SRC-PN")
    assert response.status_code == 200, response.text
    assert response.json()["calculation"]["sample_count"] == 1

    aliased = _get(catalog_client, "TOMBSTONE-ALIAS")
    assert aliased.status_code == 200, aliased.text
    assert aliased.json()["calculation"]["sample_count"] == 1
    assert aliased.json()["prices"] == response.json()["prices"]


def test_ten_hop_merge_chain_succeeds(db, catalog_client):
    nodes = [_part(db, "HOP-10")] + [
        _part(db, f"HOP-{index}") for index in range(9, -1, -1)
    ]
    for index in range(10):
        nodes[index].status = "merged"
        nodes[index].merged_into_id = nodes[index + 1].id
    batch = _batch(db)
    _sale(db, batch, nodes[-1], "hop-sale")
    db.commit()

    response = _get(catalog_client, "HOP-10")
    assert response.status_code == 200, response.text
    assert response.json()["calculation"]["sample_count"] == 1


def test_eleven_hop_merge_chain_is_ambiguous(db, catalog_client):
    nodes = [_part(db, "HOP-11")] + [
        _part(db, f"HOP-LONG-{index}") for index in range(10, -1, -1)
    ]
    for index in range(11):
        nodes[index].status = "merged"
        nodes[index].merged_into_id = nodes[index + 1].id
    db.commit()

    response = _get(catalog_client, "HOP-11")
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"


def test_cyclic_merge_chain_is_ambiguous(db, catalog_client):
    first = _part(db, "CYCLE-A")
    second = _part(db, "CYCLE-B")
    first.status, first.merged_into_id = "merged", second.id
    second.status, second.merged_into_id = "merged", first.id
    db.commit()

    response = _get(catalog_client, "CYCLE-A")
    assert response.status_code == 409


def test_broken_merge_chain_is_ambiguous_without_choosing_a_node():
    # 库约束要求 merged 必有目标且目标存在，因此“断链”只能在 helper 层构造；
    # HTTP 层同一 target is None 分支由环链与 11 跳用例覆盖（均断言 409）。
    broken = DimPart(
        id=1, pn_std="BROKEN", status="merged", merged_into_id=999999
    )

    class StubSession:
        def get(self, _model, _ident):
            return None

    assert canonical_part(StubSession(), broken) == (None, True)


def test_legacy_resolve_contract_is_unchanged(db, catalog_client):
    exact = _part(db, "RESOLVE-EXACT")
    target = _part(db, "RESOLVE-TARGET")
    source = _part(db, "RESOLVE-SOURCE", status="merged", merged_into_id=target.id)
    alias_part = _part(db, "RESOLVE-ALIAS")
    _alias(db, "RESOLVE-ALIAS-RAW", alias_part)
    pending_part = _part(db, "RESOLVE-PENDING")
    _alias(db, "RESOLVE-PENDING-RAW", pending_part, status="pending")
    dup_exact = _part(db, "RESOLVE-DUP")
    dup_alias = _part(db, "RESOLVE-DUP-ALIAS")
    _alias(db, "RESOLVE-DUP", dup_alias)
    db.commit()

    items = [
        {"sourceKey": "exact", "pnRaw": "resolve-exact"},
        {"sourceKey": "merged", "pnRaw": "RESOLVE-SOURCE"},
        {"sourceKey": "alias", "pnRaw": "resolve-alias-raw"},
        {"sourceKey": "pending", "pnRaw": "RESOLVE-PENDING-RAW"},
        {"sourceKey": "missing", "pnRaw": "RESOLVE-MISSING"},
        {"sourceKey": "multi", "pnRaw": "RESOLVE-DUP"},
    ]
    response = catalog_client.post(RESOLVE_URL, json={"items": items})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    rows = response.json()["items"]
    assert rows[0]["status"] == "exact"
    assert rows[0]["partId"] == exact.id
    assert rows[1]["status"] == "merged"
    assert rows[1]["partId"] == target.id
    assert rows[1]["redirectedFrom"] == source.id
    assert rows[2]["status"] == "alias"
    assert rows[2]["partId"] == alias_part.id
    assert rows[3]["status"] == "ambiguous"
    assert rows[4]["status"] == "not_found"
    assert rows[5]["status"] == "ambiguous"
    assert {row["partId"] for row in rows[5]["candidates"]} == {
        dup_exact.id,
        dup_alias.id,
    }


# ---------------------------------------------------------------- 加权与税率


def test_quantity_weighting_is_not_line_mean(db, catalog_client):
    part = _part(db, "QTY-PN")
    batch = _batch(db)
    _sale(
        db, batch, part, "qty-one",
        lines=((Decimal(1), Decimal("100.00"), True),),
    )
    _sale(
        db, batch, part, "qty-three",
        lines=((Decimal(3), Decimal("200.00"), True),),
    )
    db.commit()

    response = _get(catalog_client, part.pn_std)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert _amounts(payload) == {"inc_tax": "175.00", "ex_tax": "154.87"}
    assert payload["calculation"]["method"] == "time_decay_qty_weighted"
    assert payload["calculation"]["decay"] == "exponential_half_life"
    assert payload["calculation"]["sample_count"] == 2
    assert payload["calculation"]["order_count"] == 2


def test_mixed_tax_rates_convert_per_line_and_flag_nominal_side(
    db, catalog_client
):
    part = _part(db, "RATE-PN")
    batch = _batch(db)
    cases = [
        ("rate-13", Decimal("113.00"), Decimal("0.13"), True),
        ("rate-6", Decimal("106.00"), Decimal("0.06"), False),
        ("rate-06", Decimal("100.60"), Decimal("0.006"), None),
        ("rate-null", Decimal("113.00"), None, True),
        ("rate-zero", Decimal("113.00"), Decimal("0.00"), False),
    ]
    for key, price, rate, inclusive in cases:
        _sale(
            db, batch, part, key,
            tax_rate=rate,
            is_tax_inclusive=inclusive,
            lines=((Decimal(1), price, True),),
        )
    db.commit()

    response = _get(catalog_client, part.pn_std)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert _amounts(payload) == {"inc_tax": "109.12", "ex_tax": "100.00"}
    inc, ex = payload["prices"]
    assert inc["nominal_sample_count"] == 0
    # NULL 与 0 税率按 13% 推算的未税侧才是名义值。
    assert ex["nominal_sample_count"] == 2
    assert inc["sample_count"] == ex["sample_count"] == 5
    assert inc["order_count"] == ex["order_count"] == 5
    # is_tax_inclusive 不改变“销售 unit_price 恒含税”的解释。
    assert payload["calculation"]["sales_unit_price_basis"] == "inc_tax"


def test_nominal_sample_count_counts_lines_not_quantity(db, catalog_client):
    part = _part(db, "NOMINAL-QTY-PN")
    batch = _batch(db)
    _sale(
        db, batch, part, "nominal-null-qty3",
        tax_rate=None,
        lines=((Decimal(3), Decimal("113.00"), True),),
    )
    _sale(
        db, batch, part, "nominal-zero-qty2",
        tax_rate=Decimal("0"),
        lines=((Decimal(2), Decimal("113.00"), True),),
    )
    db.commit()

    payload = _get(catalog_client, part.pn_std).json()
    inc, ex = payload["prices"]
    assert inc["nominal_sample_count"] == 0
    # 名义样本是参与明细行数，不是数量合计。
    assert ex["nominal_sample_count"] == 2
    assert ex["sample_count"] == 2
    assert _amounts(payload) == {"inc_tax": "113.00", "ex_tax": "100.00"}


def test_half_up_rounding_uses_exact_decimal_weights(db, catalog_client):
    part = _part(db, "ROUND-PN")
    batch = _batch(db)
    _sale(
        db, batch, part, "round-today",
        order_date=TODAY,
        lines=((Decimal(1), Decimal("100.00"), True),),
    )
    _sale(
        db, batch, part, "round-week-a",
        order_date=TODAY - timedelta(days=7),
        lines=((Decimal(1), Decimal("100.02"), True),),
    )
    _sale(
        db, batch, part, "round-week-b",
        order_date=TODAY - timedelta(days=7),
        lines=((Decimal(1), Decimal("100.00"), True),),
    )
    db.commit()

    response = _get(catalog_client, part.pn_std)
    assert response.status_code == 200, response.text
    # (100 + 0.5*100.02 + 0.5*100) / 2 = 100.005 -> HALF_UP 100.01
    assert _amounts(response.json()) == {"inc_tax": "100.01", "ex_tax": "88.50"}


def test_fractional_quantities_are_weighted(db, catalog_client):
    part = _part(db, "FRACTION-PN")
    batch = _batch(db)
    _sale(
        db, batch, part, "fraction-a",
        lines=((Decimal("1.5"), Decimal("100.00"), True),),
    )
    _sale(
        db, batch, part, "fraction-b",
        lines=((Decimal("2.5"), Decimal("200.00"), True),),
    )
    db.commit()

    response = _get(catalog_client, part.pn_std)
    assert response.status_code == 200, response.text
    # (1.5*100 + 2.5*200) / 4 = 162.50
    assert _amounts(response.json())["inc_tax"] == "162.50"


def test_line_mean_override_and_decay(db, catalog_client, monkeypatch):
    _configure(monkeypatch, weighting="line_mean")
    part = _part(db, "LINE-MEAN-PN")
    batch = _batch(db)
    _sale(
        db, batch, part, "line-today",
        order_date=TODAY,
        lines=((Decimal(100), Decimal("100.00"), True),),
    )
    _sale(
        db, batch, part, "line-week",
        order_date=TODAY - timedelta(days=7),
        lines=((Decimal(1), Decimal("200.00"), True),),
    )
    db.commit()

    response = _get(catalog_client, part.pn_std)
    assert response.status_code == 200, response.text
    payload = response.json()
    # (1*100 + 0.5*200) / (1 + 0.5) = 133.33；数量不参与。
    assert payload["calculation"]["method"] == "time_decay_line_weighted"
    assert _amounts(payload)["inc_tax"] == "133.33"


# ---------------------------------------------------------------- 过滤与计数


def test_filtered_sample_counts_and_dates(db, catalog_client, monkeypatch):
    part = _part(db, "FILTER-PN")
    batch = _batch(db)
    included_date = TODAY - timedelta(days=2)
    _, lines = _sale(
        db, batch, part, "filter-same-order",
        order_date=included_date,
        lines=(
            (Decimal(1), Decimal("100.00"), True),
            (Decimal(1), Decimal("300.00"), True),
        ),
    )
    excluded = [
        ("filter-no-revenue", "已生效", Decimal(1), Decimal("999.00"), False, included_date),
        ("filter-draft", "草稿", Decimal(1), Decimal("999.00"), True, TODAY),
        ("filter-cancelled", "已取消", Decimal(1), Decimal("999.00"), True, TODAY),
        ("filter-pending", "进行中", Decimal(1), Decimal("999.00"), True, TODAY),
        ("filter-zero-price", "已生效", Decimal(1), Decimal("0.00"), True, TODAY),
        ("filter-negative-price", "已生效", Decimal(1), Decimal("-1.00"), True, TODAY),
        ("filter-null-price", "已生效", Decimal(1), None, True, TODAY),
        ("filter-zero-qty", "已生效", Decimal(0), Decimal("999.00"), True, TODAY),
        ("filter-negative-qty", "已生效", Decimal(-1), Decimal("999.00"), True, TODAY),
        ("filter-null-qty", "已生效", None, Decimal("999.00"), True, TODAY),
        ("filter-null-date", "已生效", Decimal(1), Decimal("999.00"), True, None),
        ("filter-future", "已生效", Decimal(1), Decimal("999.00"), True, TODAY + timedelta(days=1)),
        ("filter-too-old", "已生效", Decimal(1), Decimal("999.00"), True, WINDOW_START - timedelta(days=1)),
    ]
    for key, status, qty, price, counts, order_date in excluded:
        _sale(
            db, batch, part, key,
            status=status,
            order_date=order_date,
            lines=((qty, price, counts),),
        )
    db.commit()

    response = _get(catalog_client, part.pn_std)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert _amounts(payload) == {"inc_tax": "200.00", "ex_tax": "176.99"}
    assert payload["calculation"]["sample_count"] == 2
    assert payload["calculation"]["order_count"] == 1
    assert payload["prices"][0]["sample_count"] == 2
    assert payload["prices"][0]["order_count"] == 1
    # 被排除的草稿/未来日期不能抬高最近销售日。
    assert payload["calculation"]["latest_sale_date"] == included_date.isoformat()
    assert payload["calculation"]["oldest_sale_date"] == included_date.isoformat()
    assert payload["prices"][0]["latest_sale_date"] == included_date.isoformat()
    assert payload["calculation"]["positive_qty_only"] is True
    assert payload["calculation"]["counts_revenue_only"] is True
    assert payload["calculation"]["include_zero_prices"] is False

    _configure(monkeypatch, counts_revenue_only=False)
    widened = _get(catalog_client, part.pn_std)
    assert widened.status_code == 200, widened.text
    assert widened.json()["calculation"]["sample_count"] == 3
    assert widened.json()["calculation"]["counts_revenue_only"] is False

    # 非默认状态必须真正进入 SQL 过滤，而不只是回显到 calculation。
    _configure(
        monkeypatch,
        order_statuses=["已生效", "已取消", "草稿", "进行中"],
    )
    all_statuses = _get(catalog_client, part.pn_std)
    assert all_statuses.status_code == 200, all_statuses.text
    payload = all_statuses.json()
    assert payload["calculation"]["order_statuses"] == [
        "已生效", "已取消", "草稿", "进行中",
    ]
    assert payload["calculation"]["sample_count"] == 6
    assert payload["calculation"]["order_count"] == 5
    assert payload["calculation"]["latest_sale_date"] == TODAY.isoformat()


def test_zero_price_switch(db, catalog_client, monkeypatch):
    part = _part(db, "ZERO-PN")
    batch = _batch(db)
    _sale(
        db, batch, part, "zero-only",
        lines=((Decimal(1), Decimal("0.00"), True),),
    )
    db.commit()

    default = _get(catalog_client, part.pn_std)
    assert default.status_code == 200, default.text
    assert default.json()["prices"] == []
    assert default.json()["calculation"]["sample_count"] == 0
    assert default.json()["calculation"]["latest_sale_date"] is None

    _configure(monkeypatch, include_zero_prices=True)
    widened = _get(catalog_client, part.pn_std)
    assert widened.status_code == 200, widened.text
    assert _amounts(widened.json()) == {"inc_tax": "0.00", "ex_tax": "0.00"}
    assert widened.json()["calculation"]["sample_count"] == 1
    assert widened.json()["calculation"]["include_zero_prices"] is True

    # 零价必须进入加权分母：0 与 100 各一行，均价 50，而不是只计正价行。
    _sale(
        db, batch, part, "zero-with-positive",
        lines=((Decimal(1), Decimal("100.00"), True),),
    )
    db.commit()
    mixed = _get(catalog_client, part.pn_std)
    assert mixed.status_code == 200, mixed.text
    assert _amounts(mixed.json()) == {"inc_tax": "50.00", "ex_tax": "44.25"}
    assert mixed.json()["calculation"]["sample_count"] == 2

    _sale(
        db, batch, part, "negative-still-excluded",
        lines=((Decimal(1), Decimal("-1.00"), True),),
    )
    db.commit()
    still = _get(catalog_client, part.pn_std)
    assert still.status_code == 200, still.text
    assert still.json()["calculation"]["sample_count"] == 2
    assert _amounts(still.json()) == {"inc_tax": "50.00", "ex_tax": "44.25"}


# ---------------------------------------------------------------- 窗口与配置


def test_window_boundaries_and_dates(db, catalog_client):
    first = _part(db, "WINDOW-FIRST")
    last = _part(db, "WINDOW-LAST")
    batch = _batch(db)
    _sale(db, batch, first, "window-start", order_date=WINDOW_START)
    _sale(
        db, batch, last, "window-too-old",
        order_date=WINDOW_START - timedelta(days=1),
    )
    _sale(db, batch, last, "window-today", order_date=TODAY)
    _sale(db, batch, last, "window-future", order_date=TODAY + timedelta(days=1))
    _sale(
        db, batch, last, "window-null",
        order_date=None,
    )
    db.commit()

    start_payload = _get(catalog_client, first.pn_std).json()
    assert start_payload["calculation"]["window"] == {
        "start": "2026-09-20",
        "end": "2026-10-19",
    }
    assert start_payload["calculation"]["sample_count"] == 1
    assert start_payload["calculation"]["oldest_sale_date"] == "2026-09-20"
    assert start_payload["calculation"]["latest_sale_date"] == "2026-09-20"

    end_payload = _get(catalog_client, last.pn_std).json()
    assert end_payload["calculation"]["sample_count"] == 1
    assert end_payload["calculation"]["oldest_sale_date"] == "2026-10-19"
    assert end_payload["calculation"]["latest_sale_date"] == "2026-10-19"


def test_window_and_half_life_overrides(db, catalog_client, monkeypatch):
    part = _part(db, "OVERRIDE-PN")
    batch = _batch(db)
    _sale(
        db, batch, part, "override-today",
        order_date=TODAY,
        lines=((Decimal(1), Decimal("100.00"), True),),
    )
    _sale(
        db, batch, part, "override-yesterday",
        order_date=TODAY - timedelta(days=1),
        lines=((Decimal(1), Decimal("200.00"), True),),
    )
    db.commit()

    _configure(monkeypatch, window_days=1)
    one_day = _get(catalog_client, part.pn_std).json()
    assert one_day["calculation"]["window"] == {
        "start": "2026-10-19",
        "end": "2026-10-19",
    }
    assert one_day["calculation"]["sample_count"] == 1
    assert _amounts(one_day) == {
        "inc_tax": "100.00",
        "ex_tax": "88.50",
    }

    _configure(monkeypatch, window_days=30, half_life_days=Decimal("1"))
    decayed = _get(catalog_client, part.pn_std).json()
    # (100 + 0.5*200) / 1.5 = 133.33
    assert decayed["calculation"]["window_days"] == 30
    assert decayed["calculation"]["half_life_days"] == "1"
    assert _amounts(decayed)["inc_tax"] == "133.33"


def test_window_crosses_leap_day(db, catalog_client, monkeypatch):
    monkeypatch.setattr(
        price_service, "business_today", lambda: date(2028, 3, 1)
    )
    _configure(monkeypatch, window_days=2)
    part = _part(db, "LEAP-PN")
    batch = _batch(db)
    _sale(
        db, batch, part, "leap-day",
        order_date=date(2028, 2, 29),
        lines=((Decimal(1), Decimal("100.00"), True),),
    )
    db.commit()

    payload = _get(catalog_client, part.pn_std).json()
    assert payload["calculation"]["window"] == {
        "start": "2028-02-29",
        "end": "2028-03-01",
    }
    assert payload["calculation"]["sample_count"] == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("resale_price_reference_window_days", 0),
        ("resale_price_reference_window_days", 367),
        ("resale_price_reference_half_life_days", "0"),
        ("resale_price_reference_half_life_days", "-1"),
        ("resale_price_reference_half_life_days", "3651"),
        ("resale_price_reference_half_life_days", "NaN"),
        ("resale_price_reference_order_statuses", []),
        ("resale_price_reference_order_statuses", ["未知状态"]),
        ("resale_price_reference_weighting", "median"),
    ],
)
def test_settings_reject_invalid_values(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_settings_defaults_and_overrides(monkeypatch):
    defaults = Settings(_env_file=None)
    assert defaults.resale_price_reference_window_days == 30
    assert defaults.resale_price_reference_half_life_days == Decimal("7")
    assert defaults.resale_price_reference_order_statuses == ["已生效"]
    assert defaults.resale_price_reference_counts_revenue_only is True
    assert defaults.resale_price_reference_include_zero_prices is False
    assert defaults.resale_price_reference_weighting == "qty_weighted"
    assert (
        defaults.resale_price_reference_exclude_confirmed_source_error is True
    )

    deduped = Settings(
        _env_file=None,
        resale_price_reference_order_statuses=["已生效", "草稿", "已生效"],
    )
    assert deduped.resale_price_reference_order_statuses == ["已生效", "草稿"]

    monkeypatch.setenv("RESALE_PRICE_REFERENCE_WINDOW_DAYS", "7")
    monkeypatch.setenv(
        "RESALE_PRICE_REFERENCE_ORDER_STATUSES", '["已生效","草稿"]'
    )
    monkeypatch.setenv("RESALE_PRICE_REFERENCE_HALF_LIFE_DAYS", "3.5")
    monkeypatch.setenv("RESALE_PRICE_REFERENCE_WEIGHTING", "line_mean")
    configured = Settings(_env_file=None)
    assert configured.resale_price_reference_window_days == 7
    assert configured.resale_price_reference_order_statuses == ["已生效", "草稿"]
    assert configured.resale_price_reference_half_life_days == Decimal("3.5")
    assert configured.resale_price_reference_weighting == "line_mean"


def test_calculation_metadata_reflects_effective_settings(
    db, catalog_client, monkeypatch
):
    _configure(
        monkeypatch,
        window_days=7,
        half_life_days=Decimal("2.5"),
        weighting="line_mean",
        counts_revenue_only=False,
        include_zero_prices=True,
        exclude_confirmed_source_error=False,
        order_statuses=["已生效", "草稿"],
    )
    part = _part(db, "META-PN")
    batch = _batch(db)
    _sale(db, batch, part, "meta-sale", order_date=TODAY - timedelta(days=6))
    db.commit()

    payload = _get(catalog_client, part.pn_std).json()
    calculation = payload["calculation"]
    assert calculation["method"] == "time_decay_line_weighted"
    assert calculation["decay"] == "exponential_half_life"
    assert calculation["window_days"] == 7
    assert calculation["half_life_days"] == "2.5"
    assert calculation["date_basis"] == "order_date"
    assert calculation["timezone"] == "Asia/Shanghai"
    assert calculation["window"] == {
        "start": "2026-10-13",
        "end": "2026-10-19",
    }
    assert calculation["order_statuses"] == ["已生效", "草稿"]
    assert calculation["counts_revenue_only"] is False
    assert calculation["positive_qty_only"] is True
    assert calculation["include_zero_prices"] is True
    assert calculation["exclude_confirmed_source_error"] is False
    assert calculation["sales_unit_price_basis"] == "inc_tax"
    assert calculation["tax_policy"] == "order_rate_or_13pct"
    assert calculation["fallback_tax_rate"] == "0.13"
    assert calculation["currency"] == "CNY"


# ---------------------------------------------------------------- 源错误排除


def test_source_error_exclusion(db, catalog_client, monkeypatch):
    part = _part(db, "DQ-PN")
    batch = _batch(db)
    _, keep = _sale(
        db, batch, part, "dq-keep",
        lines=((Decimal(1), Decimal("100.00"), True),),
    )
    _, confirmed_error = _sale(
        db, batch, part, "dq-confirmed-error",
        lines=((Decimal(1), Decimal("200.00"), True),),
    )
    _, open_issue = _sale(
        db, batch, part, "dq-open",
        lines=((Decimal(1), Decimal("300.00"), True),),
    )
    _, valid = _sale(
        db, batch, part, "dq-valid",
        lines=((Decimal(1), Decimal("400.00"), True),),
    )
    _, changed = _sale(
        db, batch, part, "dq-changed",
        lines=((Decimal(1), Decimal("500.00"), True),),
    )
    _, multiple = _sale(
        db, batch, part, "dq-multiple",
        lines=((Decimal(1), Decimal("600.00"), True),),
    )
    _issue(db, confirmed_error[0], "confirmed_source_error")
    _issue(db, open_issue[0], "open")
    _issue(db, valid[0], "confirmed_valid")
    _issue(db, changed[0], "source_changed")
    _issue(db, multiple[0], "confirmed_source_error", rule_code="rule-a")
    _issue(db, multiple[0], "open", rule_code="rule-b")
    db.commit()

    default = _get(catalog_client, part.pn_std).json()
    assert default["calculation"]["sample_count"] == 4
    assert default["calculation"]["order_count"] == 4
    assert default["calculation"]["exclude_confirmed_source_error"] is True
    assert _amounts(default) == {"inc_tax": "325.00", "ex_tax": "287.61"}

    _configure(monkeypatch, exclude_confirmed_source_error=False)
    widened = _get(catalog_client, part.pn_std).json()
    # 多条 DQ 行不扩行：dq-multiple 仍只有一行。
    assert widened["calculation"]["sample_count"] == 6
    assert widened["calculation"]["order_count"] == 6
    assert widened["calculation"]["exclude_confirmed_source_error"] is False
    assert _amounts(widened) == {"inc_tax": "350.00", "ex_tax": "309.73"}


# ---------------------------------------------------------------- 空态、故障与隐私


def test_unknown_pn_and_no_sample_keep_full_calculation(db, catalog_client):
    part = _part(db, "EMPTY-PN")
    db.commit()

    for pn in (part.pn_std, "UNKNOWN-EMPTY"):
        response = _get(catalog_client, pn)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["pn"] == pn
        assert payload["prices"] == []
        assert payload["reference_only"] is True
        assert payload["calculation"]["sample_count"] == 0
        assert payload["calculation"]["order_count"] == 0
        assert payload["calculation"]["latest_sale_date"] is None
        assert payload["calculation"]["oldest_sale_date"] is None
        assert payload["calculation"]["window"] == {
            "start": "2026-09-20",
            "end": "2026-10-19",
        }
        assert response.headers["cache-control"] == "no-store"


def test_database_failure_maps_to_fixed_503(
    db, catalog_client, monkeypatch
):
    _part(db, "DBCRASH-PN")
    db.commit()

    def explode(*_args, **_kwargs):
        raise SQLAlchemyError("SELECT secret FROM private_table")

    monkeypatch.setattr(price_service, "_grouped_samples", explode)
    response = _get(catalog_client, "DBCRASH-PN")
    assert response.status_code == 503
    assert response.json()["detail"] == "参考价暂不可用"
    assert response.headers["cache-control"] == "no-store"
    assert "SELECT" not in response.text
    assert "private_table" not in response.text


@pytest.mark.parametrize("bad_rate", [Decimal("-1.0"), Decimal("1.0")])
def test_any_bad_tax_rate_fails_the_whole_request(
    db, catalog_client, bad_rate
):
    part = _part(db, "BAD-RATE-PN")
    batch = _batch(db)
    _sale(db, batch, part, "bad-rate-good")
    _sale(
        db, batch, part, "bad-rate-bad",
        tax_rate=bad_rate,
    )
    db.commit()

    response = _get(catalog_client, part.pn_std)
    assert response.status_code == 503
    assert response.json()["detail"] == "参考价暂不可用"
    assert response.headers["cache-control"] == "no-store"


def test_non_finite_and_oversized_amounts_are_rejected():
    for value in (
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("1e16"),
        Decimal("-1e16"),
    ):
        with pytest.raises(PriceReferenceUnavailable):
            _validated_amount(value)
    assert _validated_amount(Decimal("999999999999999.99")) == Decimal(
        "999999999999999.99"
    )


def test_zero_weight_denominator_is_503_not_fake_empty(
    db, catalog_client, monkeypatch
):
    part = _part(db, "UNDERFLOW-PN")
    batch = _batch(db)
    _sale(db, batch, part, "underflow-sale")
    db.commit()

    # 有合格样本时权重分母必须为正；数值下溢/配置异常不能伪装成 200 空结果。
    monkeypatch.setattr(
        price_service, "_decay_factor", lambda *_args: Decimal(0)
    )
    response = _get(catalog_client, part.pn_std)
    assert response.status_code == 503
    assert response.json()["detail"] == "参考价暂不可用"
    assert response.headers["cache-control"] == "no-store"


def test_response_key_whitelist_and_no_private_fields(db, catalog_client):
    part = _part(db, "PRIVACY-PN")
    batch = _batch(db)
    _sale(db, batch, part, "privacy-sale")
    db.commit()

    response = _get(catalog_client, part.pn_std)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"pn", "prices", "reference_only", "calculation"}
    assert set(payload["prices"][0]) == {
        "kind", "amount", "currency", "occurred_at", "source", "tax_basis",
        "sample_count", "order_count", "window", "latest_sale_date",
        "oldest_sale_date", "nominal_sample_count",
    }
    assert set(payload["calculation"]) == {
        "method", "decay", "window_days", "half_life_days", "date_basis",
        "timezone", "window", "order_statuses", "counts_revenue_only",
        "positive_qty_only", "include_zero_prices",
        "exclude_confirmed_source_error", "sales_unit_price_basis",
        "tax_policy", "fallback_tax_rate", "currency", "sample_count",
        "order_count", "latest_sale_date", "oldest_sale_date",
    }
    assert "PRIVATE-" not in response.text
    assert "raw_line" not in response.text

    # 空样本同样不泄露任何内部字段。
    empty = _get(catalog_client, "PRIVACY-UNKNOWN")
    assert set(empty.json()) == {"pn", "prices", "reference_only", "calculation"}
    assert "PRIVATE-" not in empty.text


def test_query_count_is_constant_and_get_is_read_only(db, catalog_client):
    part = _part(db, "QUERY-COUNT-PN")
    batch = _batch(db)
    _sale(db, batch, part, "query-count-one")
    db.commit()

    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip())

    def select_count(recorded):
        return sum(
            1
            for statement in recorded
            if statement.upper().startswith(("SELECT", "WITH"))
        )

    event.listen(engine, "before_cursor_execute", record)
    try:
        statements.clear()
        assert _get(catalog_client, part.pn_std).status_code == 200
        first_request = list(statements)

        for index in range(40):
            _sale(
                db, batch, part, f"query-count-more-{index}",
                # 跨多个 (order_date, rate) 分组，逐组查询的实现会暴露增长。
                order_date=TODAY - timedelta(days=index % 10),
                tax_rate=(
                    Decimal("0.13") if index % 2 == 0 else Decimal("0.06")
                ),
                lines=((Decimal(1), Decimal(index + 1), True),),
            )
        db.commit()
        statements.clear()
        assert _get(catalog_client, part.pn_std).status_code == 200
        second_request = list(statements)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    first_count = select_count(first_request)
    second_count = select_count(second_request)
    assert first_count > 0
    assert first_count == second_count
    assert second_count <= 5
    # GET 只读：请求期间除 SELECT/WITH 外不得出现任何业务写入语句。
    for statement in first_request + second_request:
        assert statement.upper().startswith(("SELECT", "WITH")), statement
