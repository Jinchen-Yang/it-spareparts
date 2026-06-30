"""备件主数据自治（WP1）：采购新建/编辑 PN + locked_fields 防重导覆盖 + 权限。"""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import permissions
from app.auth import _make_token
from app.etl import loader
from app.main import app
from app.models.dimensions import DimPart
from app.models.system import SysAuditLog, SysImportBatch
from app.services import master_edit, merge
from tests import factories as f

_client = TestClient(app)


def _hdr(role: str) -> dict:
    """造一个该角色的实名 token（sub 非 sys_user 中用户 → 跳过吊销校验）。"""
    tok, _ = _make_token(role, role, role, perms=permissions.effective(role, None))
    return {"Authorization": f"Bearer {tok}"}


# ---------------- 新建 ----------------

def test_create_part_basic(db):
    res = master_edit.create_part(
        db, pn_std="st8000nm000a", description="希捷 8TB 7.2K 3.5 SATA",
        brand="Seagate", category_major="3.5寸 SATA硬盘", operated_by="cui")
    assert res["created"] is True
    p = db.scalar(select(DimPart).where(DimPart.pn_std == "ST8000NM000A"))  # 大写归一
    assert p is not None
    assert p.master_source == "manual"
    assert p.machine_or_part == "备件"          # 缺省
    assert p.reviewed_at is not None            # 人工建档，不进待复核
    assert p.needs_review is False
    assert set(p.locked_fields) >= {"description", "brand", "category_major", "machine_or_part"}
    assert p.category_source == "MANUAL"
    log = db.scalar(select(SysAuditLog).where(SysAuditLog.entity_id == p.id,
                                              SysAuditLog.action == "create"))
    assert log is not None and log.operated_by == "cui"   # 实名审计（非角色）


def test_create_rejects_empty_pn(db):
    with pytest.raises(master_edit.MasterEditError):
        master_edit.create_part(db, pn_std="   ")


def test_create_hard_duplicate_rejected(db):
    master_edit.create_part(db, pn_std="PN-DUP", description="x")
    with pytest.raises(master_edit.MasterEditError):
        master_edit.create_part(db, pn_std="pn-dup", description="y")  # 同 pn_std（大写后）


def test_create_near_duplicate_blocks_then_force(db):
    master_edit.create_part(db, pn_std="ST8000NM000A", description="希捷 8TB")
    # 仅标点不同 → compact 相同 → 近似拦截
    res = master_edit.create_part(db, pn_std="ST-8000-NM000A", description="疑似重复")
    assert res["created"] is False and res["near_duplicates"]
    assert res["near_duplicates"][0]["pn_std"] == "ST8000NM000A"
    # force 跳过近似仍可强建
    res2 = master_edit.create_part(db, pn_std="ST-8000-NM000A", description="确认独立", force=True)
    assert res2["created"] is True


# ---------------- 编辑 ----------------

def test_edit_locks_fields_and_audits(db):
    master_edit.create_part(db, pn_std="PN-E1")
    res = master_edit.edit_part(db, pn_std="PN-E1",
                                updates={"description": "新描述", "category_major": "内存"},
                                operated_by="liu")
    assert set(res["updated"]) == {"description", "category_major"}
    p = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-E1"))
    assert p.description == "新描述" and p.category_major == "内存"
    assert {"description", "category_major"} <= set(p.locked_fields)
    assert p.category_source == "MANUAL"
    log = db.scalar(select(SysAuditLog).where(SysAuditLog.entity_id == p.id,
                                              SysAuditLog.action == "edit"))
    assert log is not None and log.operated_by == "liu"


def test_edit_missing_part_returns_none(db):
    assert master_edit.edit_part(db, pn_std="NOPE", updates={"description": "x"}) is None


def test_edit_merged_tombstone_rejected(db):
    master_edit.create_part(db, pn_std="PN-M1")    # 被并入的源
    master_edit.create_part(db, pn_std="PN-M2")    # 合并目标
    src = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-M1"))
    tgt = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-M2"))
    src.status = "merged"                            # 墓碑须同时有 merged_into_id（ck_part_merged_pair）
    src.merged_into_id = tgt.id
    db.commit()
    with pytest.raises(master_edit.MasterEditError):
        master_edit.edit_part(db, pn_std="PN-M1", updates={"description": "x"})


# ---------------- 核心：防重导覆盖 ----------------

def _load_sales(db, pn: str, category: str):
    b = SysImportBatch(filename="s.xlsx", file_type="sales", file_hash=f"h-{pn}-{category}")
    db.add(b)
    db.flush()
    so = {"S": f.sales_head("S", on=date(2026, 3, 1))}
    sl = [f.sales_line("S", "L1", pn, category_major=category)]
    loader.load(db, f.sales_result(so, sl), b.id, date(2026, 6, 1))
    db.commit()


def test_locked_category_survives_reimport(db):
    """采购把品类锁成'内存'后，氚云销售重导带'硬盘'不得覆盖。"""
    master_edit.create_part(db, pn_std="RAM-LOCK", category_major="内存")
    _load_sales(db, "RAM-LOCK", "硬盘")
    p = db.scalar(select(DimPart).where(DimPart.pn_std == "RAM-LOCK"))
    assert p.category_major == "内存", "锁定字段被重导覆盖了——防覆盖失效"


def test_unlocked_category_still_overwritten(db):
    """对照：未锁定品类仍按原口径（销售导入新值优先）被写入。"""
    p = DimPart(pn_std="RAM-FREE", category_major="旧类", locked_fields=[])
    db.add(p)
    db.commit()
    _load_sales(db, "RAM-FREE", "硬盘")
    db.expire_all()
    p2 = db.scalar(select(DimPart).where(DimPart.pn_std == "RAM-FREE"))
    assert p2.category_major == "硬盘"


# ---------------- 权限 ----------------

def test_master_data_permission_template():
    assert permissions.template_for("purchaser")["page_master_data"] is True
    assert permissions.effective("admin", None)["page_master_data"] is True
    assert permissions.effective("sales", None)["page_master_data"] is False
    assert permissions.effective("readonly", None)["page_master_data"] is False


def test_master_endpoint_enforces_permission(db):
    """端点级安全边界：无凭证 401、销售 403、采购可建（后端实拦，不只是前端藏菜单）。"""
    assert _client.post("/api/parts/master", json={"pn_std": "NA"}).status_code == 401  # 硬鉴权
    r = _client.post("/api/parts/master", json={"pn_std": "API-X1"}, headers=_hdr("sales"))
    assert r.status_code == 403
    r2 = _client.post("/api/parts/master", json={"pn_std": "API-X1", "description": "d"},
                      headers=_hdr("purchaser"))
    assert r2.status_code == 200 and r2.json()["created"] is True
    r3 = _client.patch("/api/parts/master", json={"pn_std": "API-X1", "brand": "b"},
                       headers=_hdr("sales"))
    assert r3.status_code == 403


def test_create_does_not_pollute_search_miss_audit(db):
    """查重复用 resolver 但不得写 search_miss 审计、不得中途 commit（评审 medium 项）。"""
    master_edit.create_part(db, pn_std="BRANDNEW-9999", description="全新型号", operated_by="cui")
    misses = db.scalars(select(SysAuditLog).where(SysAuditLog.action == "search_miss")).all()
    assert misses == [], "新建查重污染了 search_miss 治理审计"


def test_merge_honors_locked_field(db):
    """采购把目标品类人工置空(locked-but-null)后，合并源的非空品类不得回填覆盖。"""
    master_edit.create_part(db, pn_std="MG-TGT")
    master_edit.create_part(db, pn_std="MG-SRC", category_major="内存")
    master_edit.edit_part(db, pn_std="MG-TGT", updates={"category_major": ""})  # 人工置空并锁定
    tgt = db.scalar(select(DimPart).where(DimPart.pn_std == "MG-TGT"))
    assert tgt.category_major is None and "category_major" in tgt.locked_fields
    merge.merge_parts(db, "MG-SRC", "MG-TGT", reason="同款", operated_by="cui")
    db.expire_all()
    tgt2 = db.scalar(select(DimPart).where(DimPart.pn_std == "MG-TGT"))
    assert tgt2.category_major is None, "合并回填覆盖了采购锁定的置空字段（跨路径锁失效）"
