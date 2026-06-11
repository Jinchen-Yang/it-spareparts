"""回填幂等性 / 品牌归一 / 质量扫描 / 候选生成与审核 / 指标（整改 P2/P0 回归）。"""
from datetime import date

import pytest
from sqlalchemy import func, select

from app.etl import loader
from app.models.dimensions import DimPart, PartAlias
from app.models.master_data import (
    Brand,
    ProductDataQualityIssue,
    ProductMatchCandidate,
)
from app.models.system import SysImportBatch
from app.services import master_data, match_candidates
from tests import factories as f


@pytest.fixture()
def seeded(db):
    b = SysImportBatch(filename="t.xlsx", file_type="sales", file_hash="h5")
    db.add(b)
    db.flush()
    so = {"S1": f.sales_head("S1", on=date(2026, 3, 1))}
    sl = [
        f.sales_line("S1", "SL1", "PN-HW1", brand="华为（HUAWEI）",
                     description="Huawei HDD 6TB 7.2K 3.5 SATA", category_major="3.5寸 SATA硬盘"),
        f.sales_line("S1", "SL2", "PN-HW2", brand="华为（HUAWEI）2",
                     description="华为硬盘 4T SAS"),
        f.sales_line("S1", "SL3", "PN-PH", brand="待定11111"),
        f.sales_line("S1", "SL4", "PN AMBIG", pn_raw="pn ambig", needs_review=True),
    ]
    loader.load(db, f.sales_result(so, sl), b.id, date(2026, 6, 1))
    db.commit()
    return b


def test_brand_normalization():
    assert master_data.normalize_brand("华为（HUAWEI）2") == "华为（HUAWEI）"
    assert master_data.normalize_brand("  联想（Lenovo） ") == "联想（Lenovo）"
    assert master_data.normalize_brand("待定11111") is None, "占位符不得洗成合法品牌"
    assert master_data.normalize_brand("无") is None
    assert master_data.normalize_brand(None) is None


def test_refresh_idempotent_and_correct(db, seeded):
    s1 = master_data.refresh(db)
    # 两个华为写法归到同一品牌
    hw = db.scalar(select(Brand).where(Brand.normalized_name == "华为（HUAWEI）"))
    assert hw is not None
    p1 = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-HW1"))
    p2 = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-HW2"))
    assert p1.brand_id == hw.id and p2.brand_id == hw.id
    # 占位符品牌不建档
    ph = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-PH"))
    assert ph.brand_id is None
    # 规格抽出（HDD 6TB SATA）
    assert s1["specs"]["parts_with_specs"] >= 2
    # 质量问题：缺品牌的 PN-PH 在 missing_brand 队列
    issue = db.scalar(select(ProductDataQualityIssue).where(
        ProductDataQualityIssue.part_id == ph.id,
        ProductDataQualityIssue.issue_type == "missing_brand",
        ProductDataQualityIssue.status == "open"))
    assert issue is not None
    assert ph.data_quality_score is not None

    # 幂等：重跑后行数不变
    counts1 = {t: db.scalar(select(func.count()).select_from(t)) for t in
               (Brand, ProductDataQualityIssue)}
    master_data.refresh(db)
    counts2 = {t: db.scalar(select(func.count()).select_from(t)) for t in
               (Brand, ProductDataQualityIssue)}
    assert counts1 == counts2, "refresh 必须幂等"


def test_refresh_does_not_reset_reviewed_alias(db, seeded):
    master_data.refresh(db)
    alias = db.scalar(select(PartAlias).where(PartAlias.pn_raw == "pn ambig"))
    assert alias.status == "pending"
    alias.status = "rejected"     # 模拟人工否决
    db.commit()
    master_data.refresh(db)
    db.expire_all()
    alias = db.scalar(select(PartAlias).where(PartAlias.pn_raw == "pn ambig"))
    assert alias.status == "rejected", "重扫不得把人工审核结果刷回"


def test_issue_dismissed_not_reopened(db, seeded):
    master_data.refresh(db)
    issue = db.scalar(select(ProductDataQualityIssue).where(
        ProductDataQualityIssue.issue_type == "missing_brand",
        ProductDataQualityIssue.status == "open").limit(1))
    master_data.resolve_issue(db, issue.id, "dismissed", "确认无需品牌", "t")
    master_data.refresh(db)
    db.expire_all()
    reopened = db.scalar(select(func.count()).select_from(ProductDataQualityIssue).where(
        ProductDataQualityIssue.entity_id == issue.entity_id,
        ProductDataQualityIssue.issue_type == "missing_brand",
        ProductDataQualityIssue.status == "open"))
    assert reopened == 0, "人工 dismissed 的问题不得重开"


def _add_dup_pair(db, batch, pn1, pn2, vol_pn=None):
    """构造 compact 相同的两个型号；vol_pn 给业务量（作为建议目标）。"""
    so = {"SD": f.sales_head("SD")}
    sl = [f.sales_line("SD", f"D-{pn1}", pn1), f.sales_line("SD", f"D-{pn2}", pn2)]
    if vol_pn:
        sl.append(f.sales_line("SD", f"D-extra-{vol_pn}", vol_pn))
    loader.load(db, f.sales_result(so, sl), batch.id, date(2026, 6, 1))
    db.commit()


def test_candidates_dup_group_and_review(db, seeded):
    # ST8000-NM 与 ST8000NM compact 相同（长度≥5 含字母）；后者业务量大 → 目标
    _add_dup_pair(db, seeded, "ST8000-NM", "ST8000NM", vol_pn="ST8000NM")
    stats = match_candidates.generate(db)
    assert stats["candidates_inserted"] >= 1
    cand = db.scalar(select(ProductMatchCandidate).where(
        ProductMatchCandidate.status == "pending",
        ProductMatchCandidate.raw_text == "ST8000-NM"))
    assert cand is not None
    tgt = db.get(DimPart, cand.candidate_part_id)
    assert tgt.pn_std == "ST8000NM", "组内建议目标 = 业务量最大者"

    # 幂等：重跑不重复入队
    n1 = db.scalar(select(func.count()).select_from(ProductMatchCandidate))
    match_candidates.generate(db)
    assert db.scalar(select(func.count()).select_from(ProductMatchCandidate)) == n1

    # 接受合并
    out = match_candidates.review(db, cand.id, "merge", None, "tester")
    assert out["merge"]["target_pn"] == "ST8000NM"
    db.expire_all()
    src = db.scalar(select(DimPart).where(DimPart.pn_std == "ST8000-NM"))
    assert src.status == "merged"
    cand = db.get(ProductMatchCandidate, cand.id)
    assert cand.status == "accepted"

    # 已审对不再重新生成
    match_candidates.generate(db)
    again = db.scalar(select(func.count()).select_from(ProductMatchCandidate).where(
        ProductMatchCandidate.raw_text == "ST8000-NM",
        ProductMatchCandidate.status == "pending"))
    assert again == 0


def test_dup_group_cjk_mismatch_warned(db, seeded):
    """compact 相同但中文不同（单模/多模光纤类）：降分 + 警示，防审核员被 1.0 误导。"""
    _add_dup_pair(db, seeded, "10米LC-LC单模光纤", "10米LC-LC多模光纤", vol_pn="10米LC-LC多模光纤")
    match_candidates.generate(db)
    cand = db.scalar(select(ProductMatchCandidate).where(
        ProductMatchCandidate.raw_text == "10米LC-LC单模光纤"))
    assert cand is not None
    assert float(cand.match_score) == 0.75
    assert "中文部分不同" in cand.match_reason


def test_junk_compact_not_candidate(db, seeded):
    _add_dup_pair(db, seeded, "3-米", "3米")    # compact='3'，垃圾碰撞
    match_candidates.generate(db)
    cand = db.scalar(select(ProductMatchCandidate).where(
        ProductMatchCandidate.raw_text.in_(["3-米", "3米"])))
    assert cand is None, "垃圾 compact 组不得进合并候选"


def test_confirm_independent_batch(db, seeded):
    ids = match_candidates.parts_without_candidates(db)
    assert ids, "PN AMBIG 待审且无候选"
    out = match_candidates.confirm_independent(db, ids, "tester")
    assert out["confirmed"] == len(ids)
    db.expire_all()
    left = db.scalar(select(func.count()).select_from(DimPart).where(
        DimPart.needs_review.is_(True)))
    assert left == 0
    confirmed = db.scalar(select(DimPart).where(DimPart.pn_std == "PN AMBIG"))
    assert confirmed.reviewed_at is not None, "确认后应有人工审核时间戳"


def test_metrics_shape(db, seeded):
    master_data.refresh(db)
    m = master_data.metrics(db)
    assert m["coverage"]["sales_lines"]["part_id_pct"] == 100.0
    assert m["coverage"]["sales_lines"]["raw_retained_pct"] == 100.0
    assert m["alias"]["total"] >= 4
    assert 0 <= m["completeness"]["all_pct"] <= 100
    assert "pending_candidates" in m["review_backlog"]
    assert "traceable_pct" in m["price_traceability"]
