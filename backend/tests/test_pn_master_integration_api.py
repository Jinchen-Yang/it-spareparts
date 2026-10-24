"""PN 主数据对外写接口（shop → itsp）：鉴权、幂等、审计、11+1 个端点契约。

覆盖 `.task/TASK.md` 验收清单：
- 未配置密钥 503 / 密钥错 401 / 请求头缺失或非法 400；
- 每个端点成功路径 + 建档重复与 baseline 过期的 409；
- 同一 X-Request-Id 重放只写一次库；
- 合并/改名/编辑后的审计操作人是 ``shop:<用户名>``；
- 停用非 shop 来源别名 403；
- /changes keyset 分页连续且包含 merged 墓碑；
- loader 重导不覆盖 manufacturer/summary；
- 迁移 upgrade → downgrade → upgrade 往返。
"""
import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select, text

from app.config import get_settings
from app.db import engine, get_db
from app.etl import loader
from app.main import app
from app.models.dimensions import DimPart, PartAlias
from app.models.master_data import IntegrationRequestLog, ProductSpec
from app.models.system import SysAuditLog
from app.services import master_recompute

BASE = "/api/integrations/pn-master"
TOKEN = "synthetic-pn-master-integration-token-for-tests"
_PREV_REVISION = "d8f6a2c4b9e7"
_THIS_REVISION = "b2f4a6c8d1e3"


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.setattr(
        get_settings(),
        "pn_master_integration_token_sha256",
        SecretStr(hashlib.sha256(TOKEN.encode()).hexdigest()),
    )
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as test_client:
            test_client.headers.update(
                {
                    "Authorization": f"Bearer {TOKEN}",
                    "X-Actor-System": "shop",
                    "X-Actor-User": "zhang%20san",
                }
            )
            yield test_client
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


def _rid() -> str:
    return str(uuid.uuid4())


def _write_headers(**overrides) -> dict:
    headers = {"X-Request-Id": _rid()}
    headers.update(overrides)
    return headers


def _seed_part(db, pn: str = "PN-INT-1", **columns) -> DimPart:
    part = DimPart(pn_std=pn, **columns)
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _seed_alias(db, part: DimPart, pn_raw: str, source: str = "shop") -> PartAlias:
    alias = PartAlias(
        pn_raw=pn_raw, pn_std=part.pn_std, part_id=part.id,
        source=source, needs_review=False, status="active",
    )
    db.add(alias)
    db.commit()
    db.refresh(alias)
    return alias


@pytest.fixture()
def no_recompute(monkeypatch):
    """合并/回滚会触发真实重算；非重算用例用空实现避免拖慢与耦合。"""
    monkeypatch.setattr(master_recompute.profit_svc, "recompute", lambda _db: {})
    monkeypatch.setattr(master_recompute.inventory_svc, "backfill_costs",
                        lambda _db: {})
    monkeypatch.setattr(master_recompute.maintenance_svc, "recompute", lambda _db: {})


# ---------------------------------------------------------------- 鉴权与请求头


def test_disabled_without_configured_token(client, monkeypatch, db):
    part = _seed_part(db, "PN-DISABLED")
    monkeypatch.setattr(
        get_settings(), "pn_master_integration_token_sha256", SecretStr("")
    )
    response = client.get(f"{BASE}/parts/{part.id}")
    assert response.status_code == 503
    assert response.json()["detail"] == "PN 主数据集成接口尚未启用"


def test_rejects_missing_and_wrong_credentials(client, db):
    part = _seed_part(db, "PN-AUTH")
    missing = client.get(f"{BASE}/parts/{part.id}", headers={"Authorization": ""})
    assert missing.status_code == 401
    wrong = client.get(
        f"{BASE}/parts/{part.id}", headers={"Authorization": f"Bearer {'x' * 48}"}
    )
    assert wrong.status_code == 401


def test_rejects_missing_actor_headers(client, db):
    part = _seed_part(db, "PN-HEADERS")
    saved_system = client.headers.pop("X-Actor-System")
    try:
        missing_system = client.get(f"{BASE}/parts/{part.id}")
    finally:
        client.headers["X-Actor-System"] = saved_system
    assert missing_system.status_code == 400

    saved_user = client.headers.pop("X-Actor-User")
    try:
        missing_user = client.get(f"{BASE}/parts/{part.id}")
    finally:
        client.headers["X-Actor-User"] = saved_user
    assert missing_user.status_code == 400


def test_rejects_non_shop_actor_and_bad_user_length(client, db):
    part = _seed_part(db, "PN-ACTOR")
    other = client.get(
        f"{BASE}/parts/{part.id}", headers={"X-Actor-System": "itsp"}
    )
    assert other.status_code == 400

    too_long = client.get(
        f"{BASE}/parts/{part.id}", headers={"X-Actor-User": "u" * 65}
    )
    assert too_long.status_code == 400
    blank = client.get(f"{BASE}/parts/{part.id}", headers={"X-Actor-User": "%20"})
    assert blank.status_code == 400


def test_write_requires_uuid_request_id(client, db):
    part = _seed_part(db, "PN-RID")
    missing = client.patch(
        f"{BASE}/parts/{part.id}",
        json={"baseline_version": part.updated_at.isoformat(), "manufacturer": "X"},
        headers={"X-Request-Id": ""},
    )
    assert missing.status_code == 400
    invalid = client.patch(
        f"{BASE}/parts/{part.id}",
        json={"baseline_version": part.updated_at.isoformat(), "manufacturer": "X"},
        headers={"X-Request-Id": "not-a-uuid"},
    )
    assert invalid.status_code == 400


def test_actor_user_64_chars_can_write_and_audit(client, db):
    user = "u" * 64
    response = client.post(
        f"{BASE}/parts",
        json={"pn_std": "PN-LONG-ACTOR"},
        headers=_write_headers(**{"X-Actor-User": user}),
    )
    assert response.status_code == 200, response.text
    audit = db.scalar(select(SysAuditLog).where(SysAuditLog.action == "create"))
    assert audit.operated_by == f"shop:{user}"


# ---------------------------------------------------------------- 读接口


def test_get_part_returns_fields_specs_aliases_and_version(client, db):
    part = _seed_part(
        db, "PN-DETAIL", description="desc", brand="HPE",
        manufacturer="HP", summary="中文简介", category_major="硬盘",
    )
    db.add(ProductSpec(part_id=part.id, spec_key="capacity", spec_value="1TB",
                       spec_unit="GB", numeric_value=Decimal("1000"), source="manual"))
    db.commit()
    _seed_alias(db, part, "DETAIL-ALIAS")

    response = client.get(f"{BASE}/parts/{part.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["part_id"] == part.id
    assert body["status"] == "active" and body["merged_into_id"] is None
    assert body["version"] == part.updated_at.isoformat()
    assert body["fields"]["manufacturer"] == "HP"
    assert body["fields"]["summary"] == "中文简介"
    assert body["specs"]["capacity"]["value"] == "1TB"
    assert body["specs"]["capacity"]["numeric_value"] == "1000"
    assert [a["pn_raw"] for a in body["aliases"]] == ["DETAIL-ALIAS"]


def test_batch_get_returns_same_shape_and_missing(client, db):
    first = _seed_part(
        db, "BATCH-1", manufacturer="厂商甲", summary="批量简介",
        description="批量描述",
    )
    second = _seed_part(db, "BATCH-2")
    db.add(ProductSpec(part_id=first.id, spec_key="capacity", spec_value="1TB",
                       spec_unit="GB", numeric_value=Decimal("1000"),
                       source="manual"))
    db.add(PartAlias(pn_raw="BATCH-1-ALIAS", pn_std=first.pn_std,
                     part_id=first.id, source="manual", needs_review=False,
                     status="active"))
    db.commit()

    single = client.get(f"{BASE}/parts/{first.id}")
    assert single.status_code == 200
    response = client.post(
        f"{BASE}/parts/batch-get",
        json={"part_ids": [first.id, 999999, second.id, first.id]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["part_id"] for item in body["items"]] == [first.id, second.id]
    assert body["items"][0] == single.json()
    assert body["missing"] == [999999]
    assert client.post(
        f"{BASE}/parts/batch-get", json={"part_ids": []}
    ).status_code == 422
    assert client.post(
        f"{BASE}/parts/batch-get", json={"part_ids": [0]}
    ).status_code == 422


def test_lookup_resolves_exact_alias_and_miss(client, db):
    part = _seed_part(db, "SUPER-4089-RT")
    _seed_alias(db, part, "SUPER 4089 RT")

    exact = client.post(f"{BASE}/parts/lookup", json={"pn_raw": "super-4089-rt"})
    assert exact.status_code == 200
    assert exact.json()["status"] == "exact"
    assert exact.json()["exact"]["part_id"] == part.id

    alias = client.post(f"{BASE}/parts/lookup", json={"pn_raw": "SUPER 4089 RT"})
    assert alias.json()["status"] == "alias"
    assert alias.json()["exact"]["part_id"] == part.id

    miss = client.post(f"{BASE}/parts/lookup", json={"pn_raw": "NO-SUCH-PN"})
    assert miss.json()["status"] == "not_found"
    assert miss.json()["candidates"] == []


def test_search_returns_part_id(client, db):
    part = _seed_part(
        db, "SUPERMICRO-4089", description="超微准系统", brand="SuperMicro"
    )
    response = client.post(f"{BASE}/parts/search", json={"q": "4089", "limit": 5})
    assert response.status_code == 200
    assert part.id in [item["part_id"] for item in response.json()["items"]]


def test_changes_keyset_pages_include_merged_tombstone(client, db):
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    parts = [
        DimPart(pn_std=f"CHANGE-{index}", updated_at=base + timedelta(minutes=index))
        for index in range(3)
    ]
    db.add_all(parts)
    db.flush()
    source = DimPart(
        pn_std="CHANGE-TOMB", status="merged", merged_into_id=parts[0].id,
        updated_at=base + timedelta(minutes=3),
    )
    db.add(source)
    db.commit()

    seen: list[dict] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2}
        if cursor is not None:
            params["after"] = cursor
        response = client.get(f"{BASE}/changes", params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        pages += 1
        assert pages < 20, "分页未收敛"
        seen.extend(body["items"])
        if not body["has_more"]:
            break
        assert body["next_cursor"]
        cursor = body["next_cursor"]

    ids = [item["part_id"] for item in seen]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids)) == 4
    tomb = next(item for item in seen if item["part_id"] == source.id)
    assert tomb["status"] == "merged"
    assert tomb["merged_into_id"] == parts[0].id


# ---------------------------------------------------------------- 写接口


def test_create_part_marked_shop_and_duplicate_returns_409(client, db):
    response = client.post(
        f"{BASE}/parts",
        json={
            "pn_std": "PN-CREATE",
            "description": "新建",
            "manufacturer": "Dell",
            "summary": "简介",
            "specs": {"interface": {"value": "SAS", "unit": None,
                                    "numeric_value": None}},
        },
        headers=_write_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] is True and body["part_id"]
    part = db.get(DimPart, body["part_id"])
    assert part.master_source == "shop"
    assert part.manufacturer == "Dell"
    assert part.locked_fields and "manufacturer" in part.locked_fields

    audit = db.scalar(select(SysAuditLog).where(SysAuditLog.action == "create"))
    assert audit.operated_by == "shop:zhang san"

    duplicate = client.post(
        f"{BASE}/parts", json={"pn_std": "PN-CREATE"}, headers=_write_headers()
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["part_id"] == part.id


def test_near_duplicate_returns_409_and_force_is_required(client, db):
    _seed_part(db, "NEAR-DUP")
    response = client.post(
        f"{BASE}/parts", json={"pn_std": "NEARDUP"}, headers=_write_headers()
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "near_duplicate"
    assert detail["near_duplicates"]
    assert db.scalar(select(DimPart).where(DimPart.pn_std == "NEARDUP")) is None

    forced = client.post(
        f"{BASE}/parts",
        json={"pn_std": "NEARDUP", "force": True},
        headers=_write_headers(),
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["created"] is True


def test_hard_duplicate_force_cannot_bypass(client, db):
    _seed_part(db, "HARD-DUP")
    response = client.post(
        f"{BASE}/parts",
        json={"pn_std": "HARD-DUP", "force": True},
        headers=_write_headers(),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "pn_exists"


def test_patch_uses_baseline_version_and_rejects_stale_one(client, db):
    part = _seed_part(db, "PN-PATCH")
    version = part.updated_at.isoformat()

    ok = client.patch(
        f"{BASE}/parts/{part.id}",
        json={"baseline_version": version, "manufacturer": "Lenovo",
              "summary": "说明"},
        headers=_write_headers(),
    )
    assert ok.status_code == 200, ok.text
    db.expire_all()
    part = db.get(DimPart, part.id)
    assert part.manufacturer == "Lenovo" and part.summary == "说明"
    audit = db.scalar(select(SysAuditLog).where(SysAuditLog.action == "edit"))
    assert audit.operated_by == "shop:zhang san"

    stale = client.patch(
        f"{BASE}/parts/{part.id}",
        json={"baseline_version": version, "manufacturer": "Other"},
        headers=_write_headers(),
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "version_conflict"
    assert stale.json()["detail"]["current_version"] == part.updated_at.isoformat()


def test_put_specs_replaces_only_manual_specs_and_requires_baseline(client, db):
    part = _seed_part(db, "PN-SPECS")
    db.add_all([
        ProductSpec(part_id=part.id, spec_key="capacity", spec_value="1TB",
                    spec_unit="GB", numeric_value=Decimal("1000"), source="manual"),
        ProductSpec(part_id=part.id, spec_key="interface", spec_value="SAS",
                    spec_unit=None, numeric_value=None, source="auto"),
    ])
    db.commit()
    db.refresh(part)
    version = part.updated_at.isoformat()

    response = client.put(
        f"{BASE}/parts/{part.id}/specs",
        json={"baseline_version": version,
              "specs": {"rpm": {"value": "7200", "unit": "RPM",
                                "numeric_value": "7200"}}},
        headers=_write_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "rpm" in body["specs"] and body["specs"]["rpm"]["source"] == "manual"
    assert "capacity" not in body["specs"], "未提交的手工规格应被整体替换删除"
    assert body["specs"]["interface"]["source"] == "auto", "auto 派生行不得被替换"

    legacy_versions = {
        row.spec_key for row in db.scalars(
            select(ProductSpec).where(ProductSpec.part_id == part.id)
        )
    }
    assert "capacity" not in legacy_versions

    stale = client.put(
        f"{BASE}/parts/{part.id}/specs",
        json={"baseline_version": version, "specs": {}},
        headers=_write_headers(),
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "version_conflict"


def test_merge_unmerge_roundtrip_audits_shop_operator(client, db, no_recompute):
    source = _seed_part(db, "PN-M-SRC", description="源描述")
    target = _seed_part(db, "PN-M-TGT")
    merged = client.post(
        f"{BASE}/parts/merge",
        json={"source_part_id": source.id, "target_part_id": target.id,
              "reason": "同款"},
        headers=_write_headers(),
    )
    assert merged.status_code == 200, merged.text
    merge_log_id = merged.json()["merge_log_id"]
    db.expire_all()
    source = db.get(DimPart, source.id)
    assert source.status == "merged" and source.merged_into_id == target.id
    audit = db.scalar(select(SysAuditLog).where(SysAuditLog.action == "merge"))
    assert audit.operated_by == "shop:zhang san"

    unmerged = client.post(
        f"{BASE}/parts/unmerge",
        json={"merge_log_id": merge_log_id},
        headers=_write_headers(),
    )
    assert unmerged.status_code == 200, unmerged.text
    db.expire_all()
    source = db.get(DimPart, source.id)
    assert source.status == "active" and source.merged_into_id is None


def test_merge_accepts_pn_strings(client, db, no_recompute):
    source = _seed_part(db, "PN-M-PN-SRC")
    target = _seed_part(db, "PN-M-PN-TGT")
    response = client.post(
        f"{BASE}/parts/merge",
        json={"source_pn": source.pn_std, "target_pn": target.pn_std},
        headers=_write_headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["source_part_id"] == source.id
    assert response.json()["target_part_id"] == target.id


def test_merge_runs_full_recompute_and_replay_does_not(client, db, monkeypatch):
    source = _seed_part(db, "PN-R2-M-SRC", description="源描述")
    target = _seed_part(db, "PN-R2-M-TGT")
    calls: list[str] = []

    def profit(_db):
        calls.append("profit")
        return {"sales_lines": 1}

    def inventory(_db):
        calls.append("inventory")
        return {"updated": 1}

    def maintenance(_db):
        calls.append("maintenance")
        return {"updated": 1}

    monkeypatch.setattr(master_recompute.profit_svc, "recompute", profit)
    monkeypatch.setattr(master_recompute.inventory_svc, "backfill_costs", inventory)
    monkeypatch.setattr(master_recompute.maintenance_svc, "recompute", maintenance)

    body = {"source_part_id": source.id, "target_part_id": target.id}
    request_id = _rid()
    merged = client.post(
        f"{BASE}/parts/merge", json=body, headers={"X-Request-Id": request_id}
    )
    assert merged.status_code == 200, merged.text
    assert calls == ["profit", "inventory", "maintenance"]
    assert merged.json()["recompute"] == {"sales_lines": 1}
    assert merged.json()["inventory_costs"] == {"updated": 1}
    assert merged.json()["maintenance_costs"] == {"updated": 1}
    db.expire_all()
    assert db.get(DimPart, source.id).status == "merged"

    replayed = client.post(
        f"{BASE}/parts/merge", json=body, headers={"X-Request-Id": request_id}
    )
    assert replayed.status_code == 200
    assert calls == ["profit", "inventory", "maintenance"], "重放不得再次重算"
    assert "recompute" not in replayed.json()
    assert "inventory_costs" not in replayed.json()


def test_merge_recompute_failure_keeps_commit_and_replay_skips(
    client, db, monkeypatch
):
    source = _seed_part(db, "PN-R2-F-SRC")
    target = _seed_part(db, "PN-R2-F-TGT")
    calls: list[str] = []

    def profit(_db):
        calls.append("profit")
        raise RuntimeError("recompute boom")

    monkeypatch.setattr(master_recompute.profit_svc, "recompute", profit)
    monkeypatch.setattr(master_recompute.inventory_svc, "backfill_costs",
                        lambda _db: calls.append("inventory") or {})
    monkeypatch.setattr(master_recompute.maintenance_svc, "recompute",
                        lambda _db: calls.append("maintenance") or {})

    body = {"source_part_id": source.id, "target_part_id": target.id}
    request_id = _rid()
    merged = client.post(
        f"{BASE}/parts/merge", json=body, headers={"X-Request-Id": request_id}
    )
    assert merged.status_code == 200, merged.text
    assert calls == ["profit"], "利润重算失败后不再继续后两项"
    assert merged.json()["recompute_failed"] == (
        "治理操作已完成，但后置重算失败，请联系管理员处理"
    )
    db.expire_all()
    assert db.get(DimPart, source.id).status == "merged", "重算失败不回滚合并"

    replayed = client.post(
        f"{BASE}/parts/merge", json=body, headers={"X-Request-Id": request_id}
    )
    assert replayed.status_code == 200
    assert calls == ["profit"], "重放不得再次重算"
    assert "recompute_failed" not in replayed.json()


def test_unmerge_runs_full_recompute(client, db, monkeypatch):
    monkeypatch.setattr(master_recompute.profit_svc, "recompute", lambda _db: {})
    monkeypatch.setattr(master_recompute.inventory_svc, "backfill_costs",
                        lambda _db: {})
    monkeypatch.setattr(master_recompute.maintenance_svc, "recompute",
                        lambda _db: {})
    source = _seed_part(db, "PN-R2-U-SRC")
    target = _seed_part(db, "PN-R2-U-TGT")
    merged = client.post(
        f"{BASE}/parts/merge",
        json={"source_part_id": source.id, "target_part_id": target.id},
        headers=_write_headers(),
    )
    assert merged.status_code == 200, merged.text

    calls: list[str] = []
    monkeypatch.setattr(master_recompute.profit_svc, "recompute",
                        lambda _db: calls.append("profit") or {})
    monkeypatch.setattr(master_recompute.inventory_svc, "backfill_costs",
                        lambda _db: calls.append("inventory") or {})
    monkeypatch.setattr(master_recompute.maintenance_svc, "recompute",
                        lambda _db: calls.append("maintenance") or {})
    unmerged = client.post(
        f"{BASE}/parts/unmerge",
        json={"merge_log_id": merged.json()["merge_log_id"]},
        headers=_write_headers(),
    )
    assert unmerged.status_code == 200, unmerged.text
    assert calls == ["profit", "inventory", "maintenance"]
    assert unmerged.json()["recompute"] == {}


def test_rename_part_audits_shop_operator(client, db):
    part = _seed_part(db, "PN-OLD-NAME")
    response = client.post(
        f"{BASE}/parts/{part.id}/rename",
        json={"new_pn_std": "PN-NEW-NAME", "reason": "厂商规范"},
        headers=_write_headers(),
    )
    assert response.status_code == 200, response.text
    db.expire_all()
    assert db.get(DimPart, part.id).pn_std == "PN-NEW-NAME"
    audit = db.scalar(select(SysAuditLog).where(SysAuditLog.action == "rename"))
    assert audit.operated_by == "shop:zhang san"


def test_alias_create_and_disable_shop_alias(client, db):
    part = _seed_part(db, "PN-ALIAS")
    created = client.post(
        f"{BASE}/aliases",
        json={"pn_raw": "PN ALIAS SHOP", "part_id": part.id},
        headers=_write_headers(),
    )
    assert created.status_code == 200, created.text
    alias_id = created.json()["alias_id"]
    alias = db.get(PartAlias, alias_id)
    assert alias.source == "shop" and alias.status == "active"
    assert alias.part_id == part.id and alias.pn_std == part.pn_std

    disabled = client.delete(f"{BASE}/aliases/{alias_id}", headers=_write_headers())
    assert disabled.status_code == 200, disabled.text
    db.expire_all()
    alias = db.get(PartAlias, alias_id)
    assert alias.status == "rejected"
    actions = db.scalars(
        select(SysAuditLog.action).where(SysAuditLog.entity_type == "part_alias")
    ).all()
    assert set(actions) == {"alias_create", "alias_disable"}


def test_alias_create_can_resolve_part_by_pn_std(client, db):
    part = _seed_part(db, "PN-ALIAS-BY-PN")
    response = client.post(
        f"{BASE}/aliases",
        json={"pn_raw": "ALIAS BY PN", "pn_std": "PN-ALIAS-BY-PN"},
        headers=_write_headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["part_id"] == part.id


def test_disable_non_shop_alias_is_forbidden(client, db):
    part = _seed_part(db, "PN-MANUAL-ALIAS")
    alias = _seed_alias(db, part, "MANUAL-ALIAS", source="manual")
    response = client.delete(f"{BASE}/aliases/{alias.id}", headers=_write_headers())
    assert response.status_code == 403


def test_replayed_request_returns_first_response_and_writes_once(client, db):
    part = _seed_part(db, "PN-IDEMPOTENT")
    request_id = _rid()
    first = client.post(
        f"{BASE}/aliases",
        json={"pn_raw": "IDEMPOTENT-ALIAS", "part_id": part.id},
        headers={"X-Request-Id": request_id},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        f"{BASE}/aliases",
        json={"pn_raw": "IDEMPOTENT-ALIAS", "part_id": part.id},
        headers={"X-Request-Id": request_id},
    )
    assert second.status_code == first.status_code
    assert second.json() == first.json()
    assert db.scalar(
        select(func.count()).select_from(PartAlias).where(
            PartAlias.pn_raw == "IDEMPOTENT-ALIAS"
        )
    ) == 1
    assert db.scalar(
        select(func.count()).select_from(SysAuditLog).where(
            SysAuditLog.action == "alias_create"
        )
    ) == 1
    assert db.scalar(
        select(func.count()).select_from(IntegrationRequestLog)
    ) == 1


def test_request_id_reused_with_different_body_returns_422(client, db):
    part = _seed_part(db, "PN-R2-FP")
    request_id = _rid()
    first = client.post(
        f"{BASE}/aliases",
        json={"pn_raw": "FP-ALIAS-A", "part_id": part.id},
        headers={"X-Request-Id": request_id},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        f"{BASE}/aliases",
        json={"pn_raw": "FP-ALIAS-B", "part_id": part.id},
        headers={"X-Request-Id": request_id},
    )
    assert second.status_code == 422
    assert second.json()["detail"]["code"] == "request_id_reused"
    assert db.scalar(select(PartAlias).where(PartAlias.pn_raw == "FP-ALIAS-B")) is None


def test_request_id_reused_with_different_path_returns_422(client, db):
    part = _seed_part(db, "PN-R2-FP-PATH")
    request_id = _rid()
    first = client.post(
        f"{BASE}/aliases",
        json={"pn_raw": "FP-PATH-ALIAS", "part_id": part.id},
        headers={"X-Request-Id": request_id},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        f"{BASE}/parts/{part.id}/rename",
        json={"new_pn_std": "PN-R2-FP-RENAMED"},
        headers={"X-Request-Id": request_id},
    )
    assert second.status_code == 422
    assert second.json()["detail"]["code"] == "request_id_reused"


def test_loader_reimport_does_not_overwrite_manufacturer_or_summary(client, db):
    part = _seed_part(
        db, "PN-LOADER", manufacturer="原始厂商", summary="原始简介",
        description="旧描述",
    )
    loader._upsert_parts(
        db,
        {
            "PN-LOADER": {
                "pn_std": "PN-LOADER", "pn_raw_sample": "PN-LOADER",
                "description": "导入描述", "brand": "导入品牌",
                "machine_or_part": None, "unit": None, "needs_review": False,
                "category_major": None, "category_minor": None,
            }
        },
        is_sales=False,
    )
    db.commit()
    db.expire_all()
    part = db.get(DimPart, part.id)
    assert part.description == "旧描述", "description 由 fill-if-empty 保留"
    assert part.manufacturer == "原始厂商"
    assert part.summary == "原始简介"


# ---------------------------------------------------------------- 迁移往返


def _alembic_cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "alembic")
    )
    return cfg


def _column_exists(table: str, column: str) -> bool:
    with engine.connect() as conn:
        return bool(
            conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns"
                    " WHERE table_name = :table AND column_name = :column"
                ),
                {"table": table, "column": column},
            ).scalar()
        )


def _audit_width() -> int:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT character_maximum_length FROM information_schema.columns"
                " WHERE table_name = 'sys_audit_log' AND column_name = 'operated_by'"
            )
        ).scalar_one()


def test_migration_upgrade_downgrade_upgrade_roundtrip(db):
    db.rollback()  # 让出夹具会话，迁移走独立连接
    cfg = _alembic_cfg()
    assert _column_exists("dim_part", "manufacturer")
    assert _column_exists("dim_part", "summary")
    assert _column_exists("integration_request_log", "request_fingerprint")
    assert _audit_width() == 96

    alembic_command.downgrade(cfg, _PREV_REVISION)
    assert not _column_exists("dim_part", "manufacturer")
    assert not _column_exists("dim_part", "summary")
    assert not _column_exists("integration_request_log", "request_id")
    assert _audit_width() == 64

    alembic_command.upgrade(cfg, "head")
    assert _column_exists("dim_part", "manufacturer")
    assert _column_exists("dim_part", "summary")
    assert _column_exists("integration_request_log", "request_id")
    assert _column_exists("integration_request_log", "request_fingerprint")
    assert _audit_width() == 96
