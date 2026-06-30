"""批量规范化（WP3）：整工作集跑 normalize → 采购按价值排序批量确认。

口径（研判定稿）：按近期销售额降序优先（高价值先清）；只产建议不自动覆盖；
人工锁定过的字段(locked_fields)一律不动；应用后字段进 locked_fields，氚云重导不覆盖。
"""
import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.sales import FSalesLine, FSalesOrder
from app.services import master_edit, standardize

_RECENT_DAYS = 540   # 约 18 个月，价值排序窗口


def _cutoff() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=_RECENT_DAYS)


def _proposed(part: DimPart) -> dict:
    """据当前字段算建议改动（排除已锁定字段与无变化）。返回 {field: new_value}。"""
    sug = standardize.standardize(part.pn_std, part.description, part.brand)
    locked = set(part.locked_fields or [])
    changes: dict = {}
    canon = sug.get("canonical_description")
    if canon and canon != (part.description or "") and "description" not in locked:
        changes["description"] = canon
    l1, l2 = sug.get("category_l1"), sug.get("category_l2")
    if l1 and "category_major" not in locked and (
            l1 != (part.category_major or "") or (l2 or "") != (part.category_minor or "")):
        changes["category_major"] = l1
        changes["category_minor"] = l2
    bn = sug.get("brand_norm")
    if bn and bn != (part.brand or "") and "brand" not in locked:
        changes["brand"] = bn
    return {"suggestion": sug, "changes": changes}


def _value_subq():
    return (select(FSalesLine.part_id, func.sum(FSalesLine.line_amount).label("amt"))
            .join(FSalesOrder, FSalesOrder.id == FSalesLine.order_id)
            .where(FSalesOrder.order_date >= _cutoff())
            .group_by(FSalesLine.part_id).subquery())


def preview(db: Session, page: int = 1, page_size: int = 20, only_changes: bool = True) -> dict:
    """按近期销售额降序列出备件 + 标准化建议。only_changes：只回会改动的（前向扫描翻页）。"""
    val = _value_subq()
    base = (select(DimPart, val.c.amt)
            .outerjoin(val, val.c.part_id == DimPart.id)
            .where(DimPart.status == "active", DimPart.machine_or_part == "备件")
            .order_by(val.c.amt.desc().nullslast(), DimPart.id))
    total_beijian = db.scalar(
        select(func.count()).select_from(DimPart)
        .where(DimPart.status == "active", DimPart.machine_or_part == "备件"))

    items: list[dict] = []
    scan_off = (page - 1) * page_size
    if not only_changes:
        rows = db.execute(base.offset(scan_off).limit(page_size)).all()
        for part, amt in rows:
            items.append(_row(part, amt))
        return {"total_beijian": total_beijian, "page": page, "page_size": page_size, "items": items}

    # only_changes：从 scan_off 起前向扫描，攒满 page_size 个有改动的（上限扫 50×page_size）
    seen = 0
    while len(items) < page_size and seen < 50 * page_size:
        rows = db.execute(base.offset(scan_off + seen).limit(page_size)).all()
        if not rows:
            break
        seen += len(rows)
        for part, amt in rows:
            r = _row(part, amt)
            if r["changes"]:
                items.append(r)
            if len(items) >= page_size:
                break
    return {"total_beijian": total_beijian, "page": page, "page_size": page_size,
            "scanned": seen, "items": items}


def _row(part: DimPart, amt) -> dict:
    p = _proposed(part)
    sug = p["suggestion"]
    ch = p["changes"]
    kinds = []                                   # 语义字段名（与 apply 的 fields / 前端对齐）
    if "description" in ch:
        kinds.append("description")
    if "category_major" in ch:
        kinds.append("category")
    if "brand" in ch:
        kinds.append("brand")
    return {
        "part_id": part.id, "pn_std": part.pn_std,
        "description": part.description, "brand": part.brand,
        "category_major": part.category_major, "category_minor": part.category_minor,
        "recent_sales_amount": float(amt) if amt is not None else None,
        "suggestion": {
            "canonical_description": sug.get("canonical_description"),
            "category_l1": sug.get("category_l1"), "category_l2": sug.get("category_l2"),
            "brand_norm": sug.get("brand_norm"),
            # 确定性引擎附带：对象类型 + 每字段证据 + 校验 + 审核状态（§16 展开行 / §17 门槛）
            "object_type": sug.get("object_type"),
            "structured_specs": sug.get("structured_specs"),
            "validation_errors": sug.get("validation_errors"),
            "review_status": sug.get("review_status"),
        },
        "changes": kinds,
        "review_status": sug.get("review_status"),       # 行级：AUTO_OK 才默认勾选 / 可应用
    }


def apply_batch(db: Session, part_ids: list[int], fields: list[str] | None,
                operated_by: str | None) -> dict:
    """对每个 part 重算建议并应用选定字段（服务端重算，不信客户端传值）。

    fields ⊆ {description, category, brand}；缺省全应用。应用走 master_edit.edit_part：
    改过的字段进 locked_fields + 落审计，氚云重导不覆盖。
    """
    want = set(fields or ["description", "category", "brand"])
    applied = skipped = 0
    for pid in part_ids:
        part = db.get(DimPart, pid)
        if part is None or part.status != "active":
            skipped += 1
            continue
        prop = _proposed(part)
        # §17 服务端门槛：仅 AUTO_OK（类型/分类确定、无校验错、无猜测字段）才批量写回；
        # REVIEW_REQUIRED 交单条人工，绝不批量写低置信结果。
        if prop["suggestion"].get("review_status") != standardize.AUTO_OK:
            skipped += 1
            continue
        changes = prop["changes"]
        updates: dict = {}
        if "description" in want and "description" in changes:
            updates["description"] = changes["description"]
        if "category" in want and "category_major" in changes:
            updates["category_major"] = changes["category_major"]
            updates["category_minor"] = changes["category_minor"]
        if "brand" in want and "brand" in changes:
            updates["brand"] = changes["brand"]
        if not updates:
            skipped += 1
            continue
        master_edit.edit_part(db, pn_std=part.pn_std, updates=updates, operated_by=operated_by)
        applied += 1
    return {"applied": applied, "skipped": skipped}
