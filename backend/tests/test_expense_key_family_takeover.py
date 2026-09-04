"""报销幂等键形态冲突（2026-09-05 生产验收①）：旧视图复合键 × 新视图原生数据ID键。

批次 168（08-23，无「报销明细.数据ID」列的旧导出视图）把行落成复合键
`单号#序号@合同域hash`；客户 09-04 的大导出带原生 UUID。同一业务行（同项目、同
单号#序号）出现两把键，守卫当成真重复整批 422，且客户侧无解——只要那 11 条遗留
归因在，任何带数据ID的导出都永远传不进去。

规则（maintenance_expense_integrity.duplicate_identity_verdict）：
  takeover     既有遗留形态、来行原生 ⇒ 原生键接管：旧归因让位（删除+审计）、旧键事实行
               作废（审计）、随后 sync 建原生键归因
  keep_native  既有原生、来行遗留 ⇒ 原生键权威，来行作废跳过，不降级
  conflict     同形态不同键 ⇒ 真重复，仍整批拒绝（消息带冲突对）
两个方向都必须让预算看板（按事实行汇总）只计一次。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import config
from app.etl import loader, mapping
from app.etl.transform import TransformResult
from app.models.maintenance import FProjectExpense
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
from app.models.maintenance_project_operations import MaintenanceProjectExpenseAttribution
from app.models.system import SysAuditLog, SysImportBatch
from app.models.maintenance_project_operations import MaintenanceProjectOperationAudit
from app.services.maintenance_cost import _expense_by_contract
from app.services import maintenance_project_operations as ops
from app.services.maintenance_expense_integrity import (
    content_key_digest, duplicate_identity_verdict, raw_key_family,
)

CONTRACT = "XSDD-KEYFAM"
LEGACY_COMPOSITE = "BXD-20260328-0004#1@f02a8f6c"          # zcode 实锤冲突对：既有
NATIVE = "1ab68a49-ceb1-4728-b6d3-1db85d5ad465"             # zcode 实锤冲突对：本次
LEGACY_CONTENT = "EXP:0f3c9a1b2d4e5f60718293a4b5c6d7e8f9a0b1c2#0"


def _batch(db) -> int:
    b = SysImportBatch(filename=f"e-{uuid.uuid4()}.xlsx", file_type="expense",
                       file_hash=uuid.uuid4().hex, status="success")
    db.add(b); db.flush(); return b.id


def _expense(raw_id: str, *, bxd_no="BXD-20260328-0004", line_no=1, amount="100",
             contract=CONTRACT, reason="现场备件") -> dict:
    ex = Decimal(amount)
    return {
        "raw_line_id": raw_id, "bxd_no": bxd_no, "line_no": line_no,
        "data_status": config.MAINT_EXPENSE_ACTIVE_STATUS, "expense_date": date(2026, 3, 28),
        "person": "尤玉玲", "expense_type": "维保费用", "fee_category": "备件",
        "reason": reason, "linked_sales_order_no": contract,
        "amount": ex, "amount_ex_tax": ex,
        "amount_inc_tax": (ex * Decimal("1.13")).quantize(Decimal("0.01")),
        "tax_basis": "ex", "tax_rate_used": Decimal("0.13"),
    }


def _result(*lines: dict, anchored=True, anchor=CONTRACT) -> TransformResult:
    return TransformResult(file_type=mapping.EXPENSE, lines=list(lines), rows_total=len(lines),
                           expense_anchors=[anchor] if anchored else [])


def _add_contract(db, project, contract_no):
    c = MaintenanceProjectContract(
        project_contract_id=str(uuid.uuid4()), project_id=project.project_id,
        contract_id=f"KF-C-{uuid.uuid4().hex[:8]}", contract_no=contract_no,
        amount_inc_tax=Decimal("10000.00"), included_in_total=True,
        status_mapping_state="mapped", status_mapping_version="test",
        effective_from=date(2026, 1, 1), source="ledger", version=1)
    db.add(c); db.commit(); return c


def _seed_project(db) -> MaintenanceProject:
    project = MaintenanceProject(project_id=str(uuid.uuid4()), project_code=f"KF-{uuid.uuid4().hex[:8]}",
                                 display_name="键形态项目", lifecycle_status="ongoing")
    db.add(project); db.flush()
    db.add(MaintenanceProjectContract(
        project_contract_id=str(uuid.uuid4()), project_id=project.project_id,
        contract_id=f"KF-C-{uuid.uuid4().hex[:8]}", contract_no=CONTRACT,
        amount_inc_tax=Decimal("10000.00"), included_in_total=True,
        status_mapping_state="mapped", status_mapping_version="test",
        effective_from=date(2026, 1, 1), source="ledger", version=1))
    db.commit(); return project


def _load(db, *lines, mode="skip", anchored=True, anchor=CONTRACT):
    r = loader.load(db, _result(*lines, anchored=anchored, anchor=anchor), _batch(db),
                    date(2026, 3, 28), mode=mode, operated_by="keyfam-test",
                    audit_overwrites=(mode == "upsert"))
    db.commit(); db.expire_all(); return r


def _attr(db, raw_id):
    return db.get(MaintenanceProjectExpenseAttribution, f"bxd:{raw_id}")


def _raw(db, raw_id):
    return db.scalar(select(FProjectExpense).where(FProjectExpense.raw_line_id == raw_id))


def _audits(db, needle):
    return [a for a in db.scalars(select(SysAuditLog)) if needle in (a.reason or "")]


def _fact_audits(db, needle):
    return [a for a in db.scalars(select(MaintenanceProjectOperationAudit)) if needle in (a.reason or "")]


# ---------- 纯函数 ----------

def test_key_family_and_verdict():
    assert raw_key_family(NATIVE) == "native"
    assert raw_key_family(LEGACY_COMPOSITE) == "composite"
    assert raw_key_family(LEGACY_CONTENT) == "content"
    assert duplicate_identity_verdict(LEGACY_COMPOSITE, NATIVE) == "takeover"
    assert duplicate_identity_verdict(LEGACY_CONTENT, NATIVE) == "takeover"
    assert duplicate_identity_verdict(NATIVE, LEGACY_COMPOSITE) == "keep_native"
    assert duplicate_identity_verdict(NATIVE, str(uuid.uuid4())) == "conflict"
    assert duplicate_identity_verdict(LEGACY_COMPOSITE, "BXD-20260328-0004#1@deadbeef") == "conflict"


# ---------- ① zcode 实锤对：复合键归因 + 原生键重传 ⇒ 接管 ----------

@pytest.mark.parametrize("legacy", [LEGACY_COMPOSITE, LEGACY_CONTENT], ids=["composite", "content"])
def test_native_key_takes_over_legacy_attribution(db, legacy):
    project = _seed_project(db)
    first = _load(db, _expense(legacy, amount="100"))
    assert first["expense_attributions_synced"] == 1
    assert _attr(db, legacy).project_id == project.project_id

    second = _load(db, _expense(NATIVE, amount="120"))          # 新视图，同一业务行，金额已更正
    assert second["expense_attribution_legacy_takeovers"] == 1
    assert second["expense_attribution_duplicates_skipped"] == 0
    assert _attr(db, legacy) is None                             # 旧归因让位
    native_attr = _attr(db, NATIVE)
    assert native_attr.project_id == project.project_id and native_attr.amount_ex_tax == Decimal("120")
    assert _raw(db, legacy).data_status == "已作废"               # 旧键事实行作废
    assert _raw(db, NATIVE).data_status == config.MAINT_EXPENSE_ACTIVE_STATUS
    # 预算看板按事实行汇总：只计一次，且是新视图的金额
    assert _expense_by_contract(db)[CONTRACT] == Decimal("120")
    assert _fact_audits(db, "键形态升级接管·旧归因让位")          # 归因让位：项目事实审计
    assert _audits(db, "键形态升级接管·旧键行作废")               # 旧键事实行作废：导入覆盖审计


# ---------- ③ 反向：原生键已在，旧视图重复导出 ⇒ 跳过不降级 ----------

def test_legacy_reimport_after_native_is_skipped_not_downgraded(db):
    project = _seed_project(db)
    _load(db, _expense(NATIVE, amount="120"))
    native_version = _attr(db, NATIVE).version

    r = _load(db, _expense(LEGACY_COMPOSITE, amount="100"))
    assert r["expense_attribution_legacy_skipped"] == 1
    assert r["expense_attribution_legacy_takeovers"] == 0
    assert _attr(db, LEGACY_COMPOSITE) is None
    assert _attr(db, NATIVE).version == native_version and _attr(db, NATIVE).amount_ex_tax == Decimal("120")
    assert _raw(db, LEGACY_COMPOSITE).data_status == "已作废"     # 来行作废，看板不双计
    assert _expense_by_contract(db)[CONTRACT] == Decimal("120")
    assert _audits(db, "键形态降级跳过")
    assert project.project_id == _attr(db, NATIVE).project_id


# ---------- ④⑤ 同形态真重复：仍整批拒绝，消息带冲突对 ----------

@pytest.mark.parametrize("first,second", [
    (NATIVE, str(uuid.uuid4())),
    (LEGACY_COMPOSITE, "BXD-20260328-0004#1@deadbeef"),
], ids=["native×native", "composite×composite"])
def test_same_family_duplicate_still_rejects_whole_batch(db, first, second):
    _seed_project(db)
    _load(db, _expense(first, amount="100"))
    with pytest.raises(loader.ImportIntegrityError) as e:
        loader.load(db, _result(_expense(second, amount="100")), _batch(db), date(2026, 3, 28),
                    mode="skip", operated_by="keyfam-test")
    db.rollback()
    msg = str(e.value)
    assert "BXD-20260328-0004#1" in msg and first in msg and second in msg
    db.expire_all()
    assert _attr(db, first) is not None and _attr(db, second) is None
    assert _expense_by_contract(db)[CONTRACT] == Decimal("100")


# ---------- ⑥ 修复模式下的接管：旧键行同时是「不在本表」的作废候选 ----------

def test_takeover_in_upsert_mode_counts_once(db):
    _seed_project(db)
    _load(db, _expense(LEGACY_COMPOSITE, amount="100"))
    r = _load(db, _expense(NATIVE, amount="120"), mode="upsert")
    assert r["expense_attribution_legacy_takeovers"] == 1
    assert _attr(db, LEGACY_COMPOSITE) is None and _attr(db, NATIVE).amount_ex_tax == Decimal("120")
    assert _raw(db, LEGACY_COMPOSITE).data_status == "已作废"
    assert _expense_by_contract(db)[CONTRACT] == Decimal("120")


# ======== 对抗核验（2026-09-05）后补的前提与口径 ========

def test_keep_native_refuses_when_native_side_is_being_voided(db):
    """P1：原生行正被本批「缺行作废」，来行又是遗留形态——若照常 keep_native 把来行也作废，
    两把键都没了，整条业务行从所有读模型消失且回执不报错。必须退回整批拒绝。"""
    _seed_project(db)
    _load(db, _expense(NATIVE, amount="120"))
    with pytest.raises(loader.ImportIntegrityError, match="原生键行已作废或正被本批作废"):
        loader.load(db, _result(_expense(LEGACY_COMPOSITE, amount="100")), _batch(db),
                    date(2026, 3, 28), mode="upsert", operated_by="t", audit_overwrites=True)
    db.rollback(); db.expire_all()
    assert _raw(db, NATIVE).data_status == config.MAINT_EXPENSE_ACTIVE_STATUS
    assert _attr(db, NATIVE).normalized_status == "approved"
    assert _expense_by_contract(db)[CONTRACT] == Decimal("120")


def test_contract_edit_after_takeover_succeeds(db):
    """P1：接管留下的「已作废且无归因」旧键行不能让合同重算守卫把它当真重复——否则该项目
    之后所有合同修改/新建/归档全部 400，而 UI 里根本看不到那些行。"""
    project = _seed_project(db)
    _load(db, _expense(LEGACY_COMPOSITE, amount="100"))
    _load(db, _expense(NATIVE, amount="120"))
    contract = db.scalar(select(MaintenanceProjectContract).where(
        MaintenanceProjectContract.contract_no == CONTRACT))
    ops.update_contract(db, project_contract_id=contract.project_contract_id,
                        version=contract.version, updates={"contract_amount": Decimal("12345.00")},
                        reason="t", operated_by="t")
    db.commit(); db.expire_all()
    assert _attr(db, NATIVE).project_id == project.project_id
    assert _expense_by_contract(db)[CONTRACT] == Decimal("120")


def test_cross_contract_same_ref_is_a_conflict_not_a_takeover(db):
    """P2：复合键的 @合同域hash 正是为了让跨合同同名单号不互撞；同项目两个合同、同 单号#序号
    的两条不同报销明细不能被接管（会把另一个合同的真实费用作废）。"""
    project = _seed_project(db)
    _add_contract(db, project, "XSDD-KEYFAM-2")
    legacy2 = f"BXD-20260328-0004#1@{hashlib.sha1(CONTRACT.encode()).hexdigest()[:8]}"
    _load(db, _expense(legacy2, amount="100"))                       # 合同 1
    with pytest.raises(loader.ImportIntegrityError, match="不同合同域"):
        loader.load(db, _result(_expense(NATIVE, amount="120", contract="XSDD-KEYFAM-2"),
                                anchor="XSDD-KEYFAM-2"),
                    _batch(db), date(2026, 3, 28), mode="skip", operated_by="t")
    db.rollback(); db.expire_all()
    assert _raw(db, legacy2).data_status == config.MAINT_EXPENSE_ACTIVE_STATUS
    assert _expense_by_contract(db)[CONTRACT] == Decimal("100")


def test_manual_standalone_attribution_is_never_a_key_family(db):
    """P2：手工 create_expense 的独立归因（无事实行）不参与接管/跳过判定——否则一条手填
    归因就能作废导入事实行。两种来行形态都必须整批拒绝。"""
    project = _seed_project(db)
    ops.create_expense(db, project_id=project.project_id, expense_id="manual-abc",
                       project_contract_id=None, expense_ref="BXD-20260328-0004#1",
                       expense_date=date(2026, 3, 28), applicant="x", category=None,
                       expense_reason=None, amount_ex_tax=Decimal("50"), raw_status="已结束",
                       status_mapping_state="mapped", normalized_status="approved",
                       status_mapping_version="t", reason="t", operated_by="t")
    db.commit()
    for incoming in (LEGACY_COMPOSITE, NATIVE):
        with pytest.raises(loader.ImportIntegrityError, match="手工独立归因"):
            loader.load(db, _result(_expense(incoming, amount="100")), _batch(db),
                        date(2026, 3, 28), mode="skip", operated_by="t")
        db.rollback(); db.expire_all()
    assert db.get(MaintenanceProjectExpenseAttribution, "manual-abc") is not None


def test_identity_void_is_reported_separately_under_suppressed_upsert(db):
    """P2：D-09 抑制形态（多合同）下接管仍会作废旧键行——回执不能说「未作废任何旧行」，
    也不能把它计成「免于作废」。"""
    project = _seed_project(db)
    _add_contract(db, project, "XSDD-KEYFAM-2")
    _load(db, _expense(LEGACY_COMPOSITE, amount="100"))
    r = _load(db, _expense(NATIVE, amount="120"),
              _expense(str(uuid.uuid4()), bxd_no="BXD-OTHER", amount="7", contract="XSDD-KEYFAM-2"),
              mode="upsert")
    assert r["expense_void_suppressed_reason"] == "multi_contract"
    assert r["expense_rows_voided"] == 0
    assert r["expense_rows_voided_by_identity"] == 1
    assert r["expense_rows_void_protected"] == 0                      # 接管作废的行不算「被保留」
    assert _raw(db, LEGACY_COMPOSITE).data_status == "已作废"


def test_seqless_content_key_is_taken_over_only_on_exact_content_match(db):
    """P2：旧视图既无数据ID也无序号 ⇒ EXP 内容键、expense_ref 只有单号，与带序号原生行的 ref
    永不相等。只在内容摘要逐字节相同时接管；对不上就上报 unresolved，不猜。"""
    _seed_project(db)
    digest = content_key_digest(xsdd=CONTRACT, expense_date=date(2026, 3, 28),
                                amount=Decimal("100.00"), reason="现场备件", person="尤玉玲")
    legacy = f"EXP:{digest}#0"
    _load(db, _expense(legacy, line_no=None, amount="100"))
    assert _attr(db, legacy).expense_ref == "BXD-20260328-0004"

    r = _load(db, _expense(NATIVE, amount="100"))                     # 同内容，带序号
    assert r["expense_attribution_legacy_takeovers"] == 1 and r["expense_attribution_legacy_unresolved"] == 0
    assert _attr(db, legacy) is None and _raw(db, legacy).data_status == "已作废"
    assert _expense_by_contract(db)[CONTRACT] == Decimal("100")

    digest2 = content_key_digest(xsdd=CONTRACT, expense_date=date(2026, 3, 28),
                                 amount=Decimal("300.00"), reason="差旅", person="尤玉玲")
    legacy2 = f"EXP:{digest2}#0"
    _load(db, _expense(legacy2, bxd_no="BXD-X", line_no=None, amount="300", reason="差旅"))
    r2 = _load(db, _expense(str(uuid.uuid4()), bxd_no="BXD-X", line_no=1, amount="300", reason="差旅-已改"))
    assert r2["expense_attribution_legacy_takeovers"] == 0 and r2["expense_attribution_legacy_unresolved"] == 1
    assert _attr(db, legacy2) is not None                             # 不猜：旧归因原样保留


@pytest.mark.parametrize("native", [NATIVE, "fab68a49-" + NATIVE[9:]], ids=["排在旧键前", "排在旧键后"])
def test_receipt_does_not_depend_on_key_order(db, native):
    """P3：修复模式下旧键行也在 scope 里，处理顺序由 raw_line_id 字典序决定；同一对键
    不能因 UUID 首字符而被记进两个桶。"""
    _seed_project(db)
    _load(db, _expense(LEGACY_COMPOSITE, amount="100"))
    r = _load(db, _expense(native, amount="120"), mode="upsert")
    assert r["expense_attribution_legacy_takeovers"] == 1
    assert r["expense_attribution_legacy_skipped"] == 0
    assert r["expense_rows_voided_by_identity"] == 1
    assert _expense_by_contract(db)[CONTRACT] == Decimal("120")


def test_takeover_audit_records_full_attribution_and_diff(db):
    """P3：审计要能从自身重建被删归因（含 NOT NULL 列），并记录接管前后差异。"""
    _seed_project(db)
    _load(db, _expense(LEGACY_COMPOSITE, amount="100"))
    r = _load(db, _expense(NATIVE, amount="120"))
    assert r["expense_attribution_legacy_takeovers_amount_changed"] == 1
    (a,) = _fact_audits(db, "键形态升级接管·旧归因让位")
    for col in ("raw_status", "status_mapping_state", "status_mapping_version", "amount_ex_tax", "version"):
        assert col in a.before_json
    assert a.after_json["successor_raw_line_id"] == NATIVE
    assert a.after_json["diff"]["amount_ex_tax"] == {"legacy": "100.00", "successor": "120.00"}
    (v,) = _audits(db, "键形态升级接管·旧键行作废")
    assert v.after_json["raw_line_id"] == LEGACY_COMPOSITE and v.after_json["superseded_by"] == f"bxd:{NATIVE}"


def test_partial_export_takeover_warns_about_uncovered_siblings(db):
    """P2：同一报销单在旧键域下的其它明细行仍生效且不在本表（序号重排 + 部分导出）：
    接管后会多计，回执必须提示核对。"""
    _seed_project(db)
    scope = hashlib.sha1(CONTRACT.encode()).hexdigest()[:8]
    _load(db, _expense(f"BXD-20260328-0004#1@{scope}", line_no=1, amount="100"),
              _expense(f"BXD-20260328-0004#2@{scope}", line_no=2, amount="300"))
    r = _load(db, _expense(NATIVE, line_no=1, amount="100"))         # 只带回第 1 行
    assert r["expense_attribution_legacy_takeovers"] == 1
    assert r["expense_attribution_legacy_siblings_active"] == 1

