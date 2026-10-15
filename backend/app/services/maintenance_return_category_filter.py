"""领用及返还列表共用当前主数据库品类谓词，不参与返还规则判定。"""

from sqlalchemy import func, or_, select

from app.models.dimensions import DimPart


def category_match(part_id, *, category_major=None, category_minor=None, category_state=None):
    """Return a correlated SQL predicate, or None for the unchanged full view.

    Missing classification means either level is blank.  An unlinked row has
    no matching part identity; PN text is never used to guess a classification.
    Labels come from /parts/categories and retain their exact stored spelling.
    """
    for value, limit in ((category_major, 64), (category_minor, 128)):
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            raise ValueError("备件品类筛选无效")
    if category_state not in (None, "unclassified", "unlinked"):
        raise ValueError("备件品类筛选无效")
    major = category_major if category_major and category_major.strip() else None
    minor = category_minor if category_minor and category_minor.strip() else None
    if major is None and minor is None and category_state is None:
        return None
    linked = select(1).where(DimPart.id == part_id)
    if category_state == "unlinked":
        if major is not None or minor is not None:
            raise ValueError("未关联主数据筛选不能同时指定品类")
        return ~linked.exists()
    if major is not None:
        linked = linked.where(DimPart.category_major == major)
    if minor is not None:
        linked = linked.where(DimPart.category_minor == minor)
    if category_state == "unclassified":
        linked = linked.where(or_(
            func.nullif(func.btrim(DimPart.category_major), "").is_(None),
            func.nullif(func.btrim(DimPart.category_minor), "").is_(None),
        ))
    return linked.exists()
