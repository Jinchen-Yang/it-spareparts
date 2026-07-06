"""主数据回填 / 质量扫描 / 治理指标（整改 P2/P0，审核说明 §7）。

幂等原则（评审定稿）：
- 字典 seed 以归一键 upsert；
- 回填只填 NULL（part_alias.status / part_id、dim_part.brand_id 等），
  人工审核结果绝不被重扫刷回；
- 质量问题以部分唯一索引 (issue_type, entity_type, entity_id) WHERE open 防重，
  重扫自动 resolve 已修复问题，dismissed（人工确认非问题）不被触碰。
"""
import re
from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import config
from app.models.dimensions import DimPart, PartAlias
from app.models.inquiry import FPartInquiry
from app.models.inventory import Inventory
from app.models.master_data import (
    Brand,
    ProductCategory,
    ProductDataQualityIssue,
    ProductMatchCandidate,
)
from app.models.purchase import FPurchaseLine
from app.models.sales import FSalesLine
from app.services import spec_extract
from app.services.governance import _nonstd_clause

_TRAILING_JUNK = re.compile(r"[\s0-9]+$")


# ============================================================
# 品牌
# ============================================================

def normalize_brand(raw: str | None) -> str | None:
    """品牌归一：去首尾空白、去尾部数字（'华为（HUAWEI）2'→'华为（HUAWEI）'）。

    命中占位符黑名单（'待定11111'/'无' 等）返回 None——占位符不得洗成合法主数据。
    """
    if raw is None:
        return None
    s = _TRAILING_JUNK.sub("", raw.strip())
    if not s:
        return None
    for ph in config.BRAND_PLACEHOLDERS:
        if s == ph or s.startswith(ph):
            return None
    return s


def backfill_brands(db: Session) -> dict:
    """distinct 品牌文本 → 归一 seed brands → 回填 dim_part.brand_id（只填 NULL）。"""
    raw_brands = db.execute(
        select(DimPart.brand, func.count()).where(DimPart.brand.is_not(None))
        .group_by(DimPart.brand).order_by(func.count().desc())
    ).all()
    norm_map: dict[str, str] = {}      # raw -> normalized
    seeds: dict[str, str] = {}         # normalized -> display(首个=最高频写法)
    for raw, _cnt in raw_brands:
        norm = normalize_brand(raw)
        if norm is None:
            continue
        norm_map[raw] = norm
        seeds.setdefault(norm, raw)

    if seeds:
        stmt = pg_insert(Brand).values(
            [{"brand_name": disp, "normalized_name": norm} for norm, disp in seeds.items()]
        ).on_conflict_do_nothing(index_elements=[Brand.normalized_name])
        db.execute(stmt)
    brand_id = dict(db.execute(select(Brand.normalized_name, Brand.id)).all())

    updated = 0
    for raw, norm in norm_map.items():
        bid = brand_id.get(norm)
        if bid is None:
            continue
        res = db.execute(
            update(DimPart).where(DimPart.brand == raw, DimPart.brand_id.is_(None))
            .values(brand_id=bid)
        )
        updated += res.rowcount
    return {"brands": len(seeds), "parts_brand_filled": updated,
            "placeholder_brands": len(raw_brands) - len(norm_map)}


# ============================================================
# 品类
# ============================================================

def classify_backfill(db: Session) -> dict:
    """轻量分类引擎按（标准化）描述给型号打**干净品类**：category_major=一级名、
    category_minor=二级名（可空）、category_source='AUTO'。

    只动 status=active 且 category_source≠MANUAL 且 category_major 未锁的型号——尊重人工分类
    与锁定字段，绝不覆盖手工结果；分类不出（低置信/整机）的保持原值不动。幂等可重跑。
    产出即支撑「型号查询按品类筛选」：把 2 万多未分类型号打上标准品类。
    """
    from collections import defaultdict

    from app.services import classify as classify_svc

    rows = db.execute(
        select(DimPart.id, DimPart.pn_std, DimPart.description, DimPart.brand,
               DimPart.category_major, DimPart.category_minor)
        .where(DimPart.status == "active",
               or_(DimPart.category_source.is_(None), DimPart.category_source != "MANUAL"),
               ~DimPart.locked_fields.contains(["category_major"]))
    ).all()

    # 按 (一级,二级) 分组，每组一条 UPDATE ... WHERE id IN (...)（远快于逐行）
    groups: dict[tuple, list[int]] = defaultdict(list)
    for pid, pn, desc, brand, ma0, mi0 in rows:
        res = classify_svc.classify_part(desc, pn or "", brand or "")
        if not res:
            continue
        if res.get("whole_system"):
            # 整机(如带端口的交换机)误挂在某部件品类下 → 纠为「整机」，清掉错标签；
            # 原本未分类(NULL)的不动，避免把两万多待分类型号一次性冲成整机。
            if ma0 is not None and ma0 != "整机":
                groups[("整机", None)].append(pid)
            continue
        l1, l2 = res.get("category_l1"), res.get("category_l2")
        if not l1 or (l1 == ma0 and l2 == mi0):
            continue                                   # 分不出 / 已是该值 → 跳过
        groups[(l1, l2)].append(pid)

    updated = 0
    for (l1, l2), ids in groups.items():
        for i in range(0, len(ids), 1000):             # 分块控参数量
            updated += db.execute(
                update(DimPart).where(DimPart.id.in_(ids[i:i + 1000]))
                .values(category_major=l1, category_minor=l2, category_source="AUTO")
            ).rowcount
    db.commit()
    cat = backfill_categories(db)                       # 干净品类后补 product_categories/category_id
    return {"scanned": len(rows), "parts_reclassified": updated,
            "categories": cat["categories"]}


def backfill_categories(db: Session) -> dict:
    """distinct (大类,小类) → seed product_categories → 回填 category_id（只填 NULL）。"""
    pairs = db.execute(
        select(DimPart.category_major, DimPart.category_minor)
        .where(DimPart.category_major.is_not(None)).distinct()
    ).all()
    if pairs:
        stmt = pg_insert(ProductCategory).values(
            [{"category_major": ma, "category_minor": mi} for ma, mi in pairs]
        ).on_conflict_do_nothing(constraint="uq_category_major_minor")
        db.execute(stmt)
    cat_id = {(ma, mi): cid for ma, mi, cid in db.execute(
        select(ProductCategory.category_major, ProductCategory.category_minor, ProductCategory.id)
    ).all()}

    updated = 0
    for (ma, mi), cid in cat_id.items():
        cond = and_(DimPart.category_major == ma,
                    DimPart.category_minor == mi if mi is not None else DimPart.category_minor.is_(None),
                    DimPart.category_id.is_(None))
        updated += db.execute(update(DimPart).where(cond).values(category_id=cid)).rowcount
    return {"categories": len(cat_id), "parts_category_filled": updated}


# ============================================================
# 别名 / 询价 part_id 回填
# ============================================================

def backfill_aliases(db: Session) -> dict:
    """part_id 按 pn_std 关联回填（只填 NULL）；status 从 needs_review 派生（只填 NULL）。"""
    linked = db.execute(text("""
        UPDATE part_alias a SET part_id = p.id
        FROM dim_part p
        WHERE a.part_id IS NULL AND a.pn_std = p.pn_std
    """)).rowcount
    status_filled = db.execute(text("""
        UPDATE part_alias SET status = CASE WHEN needs_review THEN 'pending' ELSE 'active' END
        WHERE status IS NULL
    """)).rowcount
    return {"alias_part_linked": linked, "alias_status_filled": status_filled}


def backfill_inquiry(db: Session) -> dict:
    linked = db.execute(text("""
        UPDATE f_part_inquiry q SET part_id = p.id
        FROM dim_part p
        WHERE q.part_id IS NULL AND q.pn_std = p.pn_std
    """)).rowcount
    return {"inquiry_part_linked": linked}


# ============================================================
# 质量问题扫描（幂等：插新 + 自动关已修复，dismissed 不触碰）
# ============================================================

_ISSUE_NOTE_AUTO = "扫描确认问题已不存在，自动关闭"


def _sync_issue_set(db: Session, issue_type: str, current: dict[int, str],
                    level: str = "warning") -> dict:
    """current: entity_id(part) -> 描述。插入缺失 open 行；resolve 不在集合内的 open 行。

    人工 dismissed（确认非问题）的实体不再重开，否则驳回失效。
    """
    dismissed = set(db.execute(
        select(ProductDataQualityIssue.entity_id).where(
            ProductDataQualityIssue.issue_type == issue_type,
            ProductDataQualityIssue.entity_type == "part",
            ProductDataQualityIssue.status == "dismissed")
    ).scalars().all())
    current = {pid: d for pid, d in current.items() if pid not in dismissed}
    rows = [{"issue_type": issue_type, "entity_type": "part", "entity_id": pid,
             "part_id": pid, "issue_level": level, "issue_description": desc}
            for pid, desc in current.items()]
    inserted = 0
    for i in range(0, len(rows), 1000):
        stmt = pg_insert(ProductDataQualityIssue).values(rows[i:i + 1000]).on_conflict_do_nothing(
            index_elements=["issue_type", "entity_type", "entity_id"],
            index_where=text("status = 'open'"),
        )
        inserted += len(db.execute(stmt.returning(ProductDataQualityIssue.id)).all())

    open_ids = set(db.execute(
        select(ProductDataQualityIssue.entity_id).where(
            ProductDataQualityIssue.issue_type == issue_type,
            ProductDataQualityIssue.entity_type == "part",
            ProductDataQualityIssue.status == "open")
    ).scalars().all())
    stale = open_ids - set(current.keys())
    closed = 0
    if stale:
        stale_list = list(stale)
        for i in range(0, len(stale_list), 1000):
            closed += db.execute(
                update(ProductDataQualityIssue).where(
                    ProductDataQualityIssue.issue_type == issue_type,
                    ProductDataQualityIssue.entity_type == "part",
                    ProductDataQualityIssue.status == "open",
                    ProductDataQualityIssue.entity_id.in_(stale_list[i:i + 1000]),
                ).values(status="resolved", resolved_by="scan",
                         resolved_at=datetime.now(timezone.utc),
                         resolution_note=_ISSUE_NOTE_AUTO)
            ).rowcount
    return {"open_new": inserted, "auto_resolved": closed, "open_total": len(current)}


def dup_groups(db: Session) -> dict[str, list[tuple[int, str]]]:
    """有效疑似重复组：pn_compact 相同的多个 active 型号。

    垃圾 compact（短于 DUP_COMPACT_MIN_LEN 或纯数字，如 '3'/'CPU'/'15M'）是偶然碰撞，
    不算重复组（按非标/待审治理）。
    """
    rows = db.execute(
        select(DimPart.pn_compact, DimPart.id, DimPart.pn_std).where(
            DimPart.status == "active",
            DimPart.pn_compact.is_not(None),
            func.length(DimPart.pn_compact) >= config.DUP_COMPACT_MIN_LEN,
            text("dim_part.pn_compact ~ '[A-Z]'"),
        ).order_by(DimPart.pn_compact, DimPart.id)
    ).all()
    groups: dict[str, list[tuple[int, str]]] = {}
    for compact, pid, pn in rows:
        groups.setdefault(compact, []).append((pid, pn))
    return {c: members for c, members in groups.items() if len(members) > 1}


def scan_issues(db: Session) -> dict:
    """全量质量扫描：missing_brand/missing_category/missing_description/nonstd_pn/duplicate_suspected。"""
    active = DimPart.status == "active"
    out = {}

    def _ids(*conds) -> list[int]:
        return list(db.execute(select(DimPart.id).where(active, *conds)).scalars().all())

    out["missing_brand"] = _sync_issue_set(
        db, "missing_brand", {i: "缺品牌（无有效品牌或为占位符）" for i in _ids(DimPart.brand_id.is_(None))})
    out["missing_category"] = _sync_issue_set(
        db, "missing_category", {i: "缺品类" for i in _ids(DimPart.category_id.is_(None))})
    out["missing_description"] = _sync_issue_set(
        db, "missing_description", {i: "缺描述" for i in _ids(DimPart.description.is_(None))})
    out["nonstd_pn"] = _sync_issue_set(
        db, "nonstd_pn", {i: "非标型号（笼统/打包/纯中文），建议排除出利润统计"
                          for i in _ids(_nonstd_clause())})

    dups: dict[int, str] = {}
    for compact, members in dup_groups(db).items():
        sibling = "、".join(pn for _, pn in members)
        for pid, _pn in members:
            dups[pid] = f"疑似重复（compact={compact}）：{sibling}"
    out["duplicate_suspected"] = _sync_issue_set(db, "duplicate_suspected", dups)
    return out


def update_quality_scores(db: Session) -> int:
    """资料质量分：品牌25 + 品类25 + 描述20 + 无疑似重复15 + 已确认(无需复核)15。"""
    return db.execute(text("""
        UPDATE dim_part p SET data_quality_score =
            (CASE WHEN p.brand_id IS NOT NULL THEN 25 ELSE 0 END)
          + (CASE WHEN p.category_id IS NOT NULL THEN 25 ELSE 0 END)
          + (CASE WHEN p.description IS NOT NULL THEN 20 ELSE 0 END)
          + (CASE WHEN NOT EXISTS (SELECT 1 FROM product_data_quality_issues i
                                   WHERE i.part_id = p.id AND i.status = 'open'
                                     AND i.issue_type = 'duplicate_suspected') THEN 15 ELSE 0 END)
          + (CASE WHEN NOT p.needs_review THEN 15 ELSE 0 END)
        WHERE p.status = 'active'
    """)).rowcount


# ============================================================
# 治理指标（审核说明 §7，只读）
# ============================================================

def metrics(db: Session) -> dict:
    def _pct(a, b):
        return round(100.0 * a / b, 2) if b else None

    parts_total = db.scalar(select(func.count()).select_from(DimPart).where(DimPart.status == "active"))
    parts_merged = db.scalar(select(func.count()).select_from(DimPart).where(DimPart.status == "merged"))
    parts_reviewed = db.scalar(select(func.count()).select_from(DimPart).where(
        DimPart.status == "active", DimPart.reviewed_at.is_not(None)))
    parts_needs_review = db.scalar(select(func.count()).select_from(DimPart).where(
        DimPart.status == "active", DimPart.needs_review.is_(True)))

    # 主数据覆盖率（§7.1）+ 原文保留率（§7.5）
    coverage = {}
    for name, model, raw_col in (
        ("purchase_lines", FPurchaseLine, FPurchaseLine.pn_raw),
        ("sales_lines", FSalesLine, FSalesLine.pn_raw),
        ("inventory", Inventory, Inventory.pn_std),
    ):
        total = db.scalar(select(func.count()).select_from(model))
        with_part = db.scalar(select(func.count()).select_from(model).where(model.part_id.is_not(None)))
        with_raw = db.scalar(select(func.count()).select_from(model).where(raw_col.is_not(None)))
        coverage[name] = {"total": total, "part_id_pct": _pct(with_part, total),
                          "raw_retained_pct": _pct(with_raw, total)}

    # 别名归一率（§7.2）
    a_total = db.scalar(select(func.count()).select_from(PartAlias))
    a_active = db.scalar(select(func.count()).select_from(PartAlias).where(PartAlias.status == "active"))
    a_pending = db.scalar(select(func.count()).select_from(PartAlias).where(PartAlias.status == "pending"))

    # 资料完整率（§7.4）：全量 + 高频（有销售记录的型号）
    complete_cond = and_(DimPart.brand_id.is_not(None), DimPart.category_id.is_not(None),
                         DimPart.description.is_not(None))
    complete = db.scalar(select(func.count()).select_from(DimPart).where(
        DimPart.status == "active", complete_cond))
    hf_sub = select(FSalesLine.part_id).distinct().scalar_subquery()
    hf_total = db.scalar(select(func.count()).select_from(DimPart).where(
        DimPart.status == "active", DimPart.id.in_(hf_sub)))
    hf_complete = db.scalar(select(func.count()).select_from(DimPart).where(
        DimPart.status == "active", DimPart.id.in_(hf_sub), complete_cond))

    # 疑似重复率（§7.3）
    dup_parts = db.scalar(select(func.count()).select_from(ProductDataQualityIssue).where(
        ProductDataQualityIssue.issue_type == "duplicate_suspected",
        ProductDataQualityIssue.status == "open"))

    # 审核积压（§7.7）
    c_pending = db.scalar(select(func.count()).select_from(ProductMatchCandidate).where(
        ProductMatchCandidate.status == "pending"))
    c_stale = db.scalar(select(func.count()).select_from(ProductMatchCandidate).where(
        ProductMatchCandidate.status == "pending",
        ProductMatchCandidate.created_at < text(f"now() - interval '{config.CANDIDATE_STALE_DAYS} days'")))

    # 价格可追溯率 / 低毛利识别率（§7.8/§7.9，已生效计营收行）
    sl_counts = db.scalar(select(func.count()).select_from(FSalesLine).where(
        FSalesLine.counts_revenue.is_(True)))
    sl_traceable = db.scalar(select(func.count()).select_from(FSalesLine).where(
        FSalesLine.counts_revenue.is_(True), FSalesLine.cost_source.is_not(None),
        FSalesLine.cost_source != "none"))
    sl_margin = db.scalar(select(func.count()).select_from(FSalesLine).where(
        FSalesLine.counts_revenue.is_(True), FSalesLine.gross_margin.is_not(None)))

    open_issues = dict(db.execute(
        select(ProductDataQualityIssue.issue_type, func.count())
        .where(ProductDataQualityIssue.status == "open")
        .group_by(ProductDataQualityIssue.issue_type)
    ).all())

    return {
        "parts": {"total_active": parts_total, "merged": parts_merged,
                  "reviewed": parts_reviewed, "needs_review": parts_needs_review,
                  "reviewed_pct": _pct(parts_reviewed, parts_total)},
        "coverage": coverage,
        "alias": {"total": a_total, "active": a_active, "pending": a_pending,
                  "normalized_pct": _pct(a_active, a_total)},
        "completeness": {
            "all_pct": _pct(complete, parts_total),
            "high_freq_pct": _pct(hf_complete, hf_total),
            "high_freq_total": hf_total,
        },
        "duplicates": {"open_suspected_parts": dup_parts,
                       "suspected_pct": _pct(dup_parts, parts_total)},
        "review_backlog": {"pending_candidates": c_pending,
                           "stale_candidates": c_stale,
                           "stale_days": config.CANDIDATE_STALE_DAYS},
        "price_traceability": {"sales_lines_counted": sl_counts,
                               "traceable_pct": _pct(sl_traceable, sl_counts),
                               "margin_computable_pct": _pct(sl_margin, sl_counts)},
        "open_issues": open_issues,
    }


# ============================================================
# 质量问题队列（P4）
# ============================================================

def list_issues(db: Session, issue_type: str | None, status: str,
                page: int, page_size: int) -> dict:
    stmt = select(ProductDataQualityIssue, DimPart.pn_std, DimPart.description).join(
        DimPart, ProductDataQualityIssue.part_id == DimPart.id, isouter=True
    ).where(ProductDataQualityIssue.status == status)
    if issue_type:
        stmt = stmt.where(ProductDataQualityIssue.issue_type == issue_type)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(stmt.order_by(ProductDataQualityIssue.id)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [{"id": i.id, "issue_type": i.issue_type, "issue_level": i.issue_level,
                       "pn_std": pn, "part_description": desc,
                       "description": i.issue_description, "status": i.status,
                       "detected_at": i.detected_at} for i, pn, desc in rows]}


def resolve_issue(db: Session, issue_id: int, action: str, note: str | None,
                  operated_by: str | None) -> dict | None:
    from app.models.system import SysAuditLog  # 局部导入避免环

    if action not in ("resolved", "dismissed"):
        raise ValueError(f"action 非法: {action}（resolved | dismissed）")
    issue = db.get(ProductDataQualityIssue, issue_id)
    if issue is None:
        return None
    issue.status = action
    issue.resolved_by = operated_by
    issue.resolved_at = datetime.now(timezone.utc)
    issue.resolution_note = note
    db.add(SysAuditLog(entity_type="quality_issue", entity_id=issue_id, action=action,
                       after_json={"issue_type": issue.issue_type, "note": note},
                       operated_by=operated_by))
    db.commit()
    return {"issue_id": issue_id, "status": action}


# ============================================================
# 别名审核（P4）
# ============================================================

class AliasError(Exception):
    """别名审核动作非法。"""


def list_aliases(db: Session, status: str, page: int, page_size: int) -> dict:
    stmt = select(PartAlias).where(PartAlias.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(stmt.order_by(PartAlias.id)
                      .offset((page - 1) * page_size).limit(page_size)).scalars().all()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [{"id": a.id, "pn_raw": a.pn_raw, "pn_std": a.pn_std,
                       "part_id": a.part_id, "source": a.source, "status": a.status,
                       "created_at": a.created_at} for a in rows]}


def review_alias(db: Session, alias_id: int, action: str, target_pn: str | None,
                 operated_by: str | None) -> dict:
    """approve=确认映射；reject=否决；reassign=改指 target_pn 并重指既有事实行。

    身份别名（pn_raw==pn_std，即"这个型号其实是另一个型号"）的改指等价于合并，
    必须走合并流程，此处拒绝。
    """
    from app.models.system import SysAuditLog  # 局部导入避免环

    a = db.get(PartAlias, alias_id)
    if a is None:
        raise AliasError(f"别名不存在: {alias_id}")
    before = {"pn_std": a.pn_std, "part_id": a.part_id, "status": a.status}
    repointed = {}

    if action == "approve":
        a.status, a.needs_review = "active", False
    elif action == "reject":
        a.status, a.needs_review = "rejected", False
    elif action == "reassign":
        if not target_pn:
            raise AliasError("reassign 需要 target_pn")
        if a.pn_raw == a.pn_std:
            raise AliasError("身份别名改指等价于合并型号，请使用合并功能")
        target = db.scalar(select(DimPart).where(DimPart.pn_std == target_pn))
        if target is None or target.status != "active":
            raise AliasError(f"目标型号不存在或不可用: {target_pn}")
        a.part_id, a.pn_std = target.id, target.pn_std
        a.status, a.needs_review, a.source = "active", False, "manual"
        # 既有事实行按 pn_raw 重指（库存不存 pn_raw，新映射在下次导入生效）
        for name, model in (("purchase_lines", FPurchaseLine), ("sales_lines", FSalesLine)):
            repointed[name] = db.execute(
                update(model).where(model.pn_raw == a.pn_raw, model.part_id != target.id)
                .values(part_id=target.id)
            ).rowcount
    else:
        raise AliasError(f"action 非法: {action}（approve | reject | reassign）")

    db.add(SysAuditLog(entity_type="part_alias", entity_id=alias_id,
                       action=f"alias_{action}", before_json=before,
                       after_json={"pn_std": a.pn_std, "part_id": a.part_id,
                                   "status": a.status, **repointed},
                       operated_by=operated_by))
    db.commit()
    return {"alias_id": alias_id, "action": action, "repointed": repointed}


# ============================================================
# 一键刷新（导入后/治理操作后调用，全程幂等）
# ============================================================

def refresh(db: Session) -> dict:
    """字典回填 → 别名/询价关联 → 规格抽取 → 质量扫描 → 质量分。单事务提交。"""
    stats = {}
    stats["brands"] = backfill_brands(db)
    stats["categories"] = backfill_categories(db)
    stats["aliases"] = backfill_aliases(db)
    stats["inquiry"] = backfill_inquiry(db)
    stats["specs"] = spec_extract.backfill_specs(db)
    stats["issues"] = scan_issues(db)
    stats["quality_scored"] = update_quality_scores(db)
    db.commit()
    return stats
