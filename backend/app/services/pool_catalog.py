"""PoolCatalog（互通PN池价格分析 §16）：人工池、成员与约束价的**唯一写入路径**。

职责边界（与只读的 pool.py / 未来的 pool_price_analysis 刻意分离）：
- 创建、编辑、归档、恢复人工池；增删成员；设置并版本化约束价；
- 并发冲突（乐观锁 version → 409）、成员唯一性、审计（sys_audit_log）。

并发与唯一性设计：
- 「一个有效 PN 只能属于一个有效池」：成员表是复合主键 (group_id, part_id)——归档池
  保留成员集合、其成员可再加入新有效池，因此唯一性不能靠 DB 单列约束，改由本模块保证：
  ① 涉及成员加入/恢复的写操作先按 part_id 排序取 pg_advisory_xact_lock（事务级，
     提交自动释放），串行化同一 PN 的并发加入；
  ② 池行 SELECT ... FOR UPDATE，持锁后校验（先池锁后 part 锁，全局同序防死锁）。
- 乐观锁：所有写操作携带 version，不匹配抛 PoolConflictError（API → 409），绝不静默覆盖。
- 当前约束价唯一：part_pool_price_policy 的部分唯一索引 (group_id) WHERE valid_to IS NULL
  兜底并发双写——第二笔 IntegrityError 收敛成 409。
- 池只归档不硬删除；所有写操作留审计（operated_by=真实用户名，勿记角色串）。
本模块只记录与配置，不做审批/拦截/自动重算（§1 产品边界）。
"""
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import config
from app.models.dimensions import DimPart
from app.models.inventory import PartPool, PartPoolMember, PartPoolPricePolicy
from app.models.system import SysAuditLog

# pg_advisory 锁命名空间（int4 双参版第一参）。918273645 是旧 rebuild 全局锁键，
# 已随 rebuild 退役；本命名空间按 part_id 细粒度加锁，互不冲突。
_PART_LOCK_NS = 918273646

_BASES = ("ex_tax", "inc_tax")
_POLICY_HISTORY_LIMIT = 20


class PoolCatalogError(Exception):
    """业务非法（空池名 / PN 不存在 / 不在本池等）→ API 400。"""


class PoolConflictError(Exception):
    """并发或唯一性冲突（版本不匹配 / PN 已属其他有效池 / 恢复冲突）→ API 409。"""


# ---------------------------------------------------------------- 内部工具

def _lock_parts(db: Session, part_ids) -> None:
    """按 part_id 升序取事务级 advisory 锁，串行化同一 PN 的并发池写入（全局同序防死锁）。"""
    for pid in sorted(set(part_ids)):
        db.execute(text("SELECT pg_advisory_xact_lock(:ns, :pid)"),
                   {"ns": _PART_LOCK_NS, "pid": pid})


def _pool_for_update(db: Session, group_id: int) -> PartPool | None:
    return db.scalar(select(PartPool).where(PartPool.group_id == group_id).with_for_update())


def _check_version(pool: PartPool, version: int) -> None:
    if pool.version != version:
        raise PoolConflictError(
            f"池「{pool.name}」已被他人修改（当前版本 {pool.version}），请刷新后重试")


def _require_active(pool: PartPool, action: str) -> None:
    if pool.status != "active":
        raise PoolCatalogError(f"池「{pool.name}」已归档，无法{action}；请先恢复")


def _active_pool_conflicts(db: Session, part_ids: list[int],
                           exclude_group_id: int | None = None) -> list[dict]:
    """这些 PN 中已属于其他**有效**池的冲突清单（明确提示现有池，§11）。"""
    if not part_ids:
        return []
    stmt = (
        select(PartPoolMember.part_id, PartPool.group_id, PartPool.name, DimPart.pn_std)
        .join(PartPool, PartPool.group_id == PartPoolMember.group_id)
        .join(DimPart, DimPart.id == PartPoolMember.part_id)
        .where(PartPoolMember.part_id.in_(part_ids), PartPool.status == "active")
    )
    if exclude_group_id is not None:
        stmt = stmt.where(PartPool.group_id != exclude_group_id)
    return [{"part_id": pid, "pn_std": pn, "group_id": gid, "pool_name": name}
            for pid, gid, name, pn in db.execute(stmt).all()]


def _conflict_message(conflicts: list[dict], verb: str = "加入") -> str:
    detail = "；".join(f"{c['pn_std']} 已属于有效池「{c['pool_name']}」(ID {c['group_id']})"
                      for c in conflicts[:5])
    more = f" 等 {len(conflicts)} 项" if len(conflicts) > 5 else ""
    return f"无法{verb}：{detail}{more}。一个有效 PN 只能属于一个有效池"


def _validate_parts(db: Session, part_ids: list[int]) -> dict[int, str]:
    """校验 PN 主数据存在且未合并墓碑；返回 part_id → pn_std。"""
    if not part_ids:
        return {}
    rows = db.execute(
        select(DimPart.id, DimPart.pn_std, DimPart.status).where(DimPart.id.in_(part_ids))
    ).all()
    found = {rid: (pn, st) for rid, pn, st in rows}
    missing = [pid for pid in part_ids if pid not in found]
    if missing:
        raise PoolCatalogError(f"型号不存在: part_id={sorted(missing)}")
    merged = [pn for pn, st in found.values() if st == "merged"]
    if merged:
        raise PoolCatalogError(f"型号已合并入他档，不能入池: {', '.join(sorted(merged))}")
    return {rid: pn for rid, (pn, st) in found.items()}


def _member_count(db: Session, group_id: int) -> int:
    return int(db.scalar(
        select(func.count()).select_from(PartPoolMember)
        .where(PartPoolMember.group_id == group_id)) or 0)


def _current_policy(db: Session, group_id: int) -> PartPoolPricePolicy | None:
    return db.scalar(
        select(PartPoolPricePolicy)
        .where(PartPoolPricePolicy.group_id == group_id,
               PartPoolPricePolicy.valid_to.is_(None)))


def _policy_dict(p: PartPoolPricePolicy | None) -> dict | None:
    if p is None:
        return None
    return {
        "purchase_ceiling_ex_tax": p.purchase_ceiling_ex_tax,
        "sales_floor_ex_tax": p.sales_floor_ex_tax,
        "purchase_input_value": p.purchase_input_value,
        "purchase_input_basis": p.purchase_input_basis,
        "sales_input_value": p.sales_input_value,
        "sales_input_basis": p.sales_input_basis,
        "valid_from": p.valid_from,
        "valid_to": p.valid_to,
        "changed_by": p.changed_by,
        "note": p.note,
    }


def _pool_dict(pool: PartPool, policy: PartPoolPricePolicy | None) -> dict:
    return {
        "group_id": pool.group_id,
        "name": pool.name,
        "description": pool.description,
        "status": pool.status,
        "source": pool.source,
        "version": pool.version,
        "member_count": pool.member_count,
        "created_by": pool.created_by,
        "updated_by": pool.updated_by,
        "created_at": pool.created_at,
        "updated_at": pool.updated_at,
        "purchase_ceiling_ex_tax": policy.purchase_ceiling_ex_tax if policy else None,
        "sales_floor_ex_tax": policy.sales_floor_ex_tax if policy else None,
    }


def _to_ex_tax(value: Decimal | None, basis: str) -> Decimal | None:
    """录入值 → 统一未税：含税÷(1+13%)（甲方统一税口径），未税原值入库。"""
    if value is None:
        return None
    if basis == "inc_tax":
        return (value / (1 + config.POOL_POLICY_VAT_RATE)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _audit(db: Session, group_id: int, action: str, before: dict | None,
           after: dict | None, note: str | None, operated_by: str | None) -> None:
    """审计留痕（不是审批流，§11）。Decimal/datetime→str 保证 JSONB 可序列化。"""
    def _j(d: dict | None) -> dict | None:
        if d is None:
            return None
        return {k: (v if v is None or isinstance(v, (bool, int, float, str, list, dict))
                    else str(v))
                for k, v in d.items()}
    db.add(SysAuditLog(entity_type="part_pool", entity_id=group_id, action=action,
                       before_json=_j(before), after_json=_j(after),
                       reason=note, operated_by=operated_by))


# ---------------------------------------------------------------- 读

def list_pools(db: Session, *, q: str | None = None, status: str = "active",
               page: int = 1, page_size: int = 20) -> dict:
    """池清单（管理页）：搜索池名/描述/成员 PN/品牌，带当前约束价。"""
    stmt = select(PartPool)
    if status in ("active", "archived"):
        stmt = stmt.where(PartPool.status == status)
    kw = (q or "").strip()
    if kw:
        like = f"%{kw}%"
        member_hit = (
            select(PartPoolMember.group_id)
            .join(DimPart, DimPart.id == PartPoolMember.part_id)
            .where(PartPoolMember.group_id == PartPool.group_id,
                   or_(DimPart.pn_std.ilike(like), DimPart.brand.ilike(like)))
        ).exists()
        stmt = stmt.where(or_(PartPool.name.ilike(like),
                              PartPool.description.ilike(like), member_hit))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    pools = db.execute(
        stmt.order_by(PartPool.updated_at.desc(), PartPool.group_id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()

    gids = [p.group_id for p in pools]
    policies: dict[int, PartPoolPricePolicy] = {}
    if gids:
        for pol in db.execute(
            select(PartPoolPricePolicy)
            .where(PartPoolPricePolicy.group_id.in_(gids),
                   PartPoolPricePolicy.valid_to.is_(None))
        ).scalars():
            policies[pol.group_id] = pol
    return {"total": total, "page": page, "page_size": page_size,
            "items": [_pool_dict(p, policies.get(p.group_id)) for p in pools]}


def get_pool(db: Session, group_id: int) -> dict | None:
    """池档案：基本信息 + 成员（带 PN/描述/品牌）+ 当前约束 + 约束变更历史。"""
    pool = db.scalar(select(PartPool).where(PartPool.group_id == group_id))
    if pool is None:
        return None
    members = [
        {"part_id": pid, "pn_std": pn, "description": desc, "brand": brand,
         "added_by": added_by, "note": note, "created_at": created_at}
        for pid, pn, desc, brand, added_by, note, created_at in db.execute(
            select(DimPart.id, DimPart.pn_std, DimPart.description, DimPart.brand,
                   PartPoolMember.added_by, PartPoolMember.note, PartPoolMember.created_at)
            .join(PartPoolMember, PartPoolMember.part_id == DimPart.id)
            .where(PartPoolMember.group_id == group_id)
            .order_by(DimPart.pn_std)
        ).all()
    ]
    history = [
        _policy_dict(p) for p in db.execute(
            select(PartPoolPricePolicy)
            .where(PartPoolPricePolicy.group_id == group_id)
            .order_by(PartPoolPricePolicy.valid_from.desc(), PartPoolPricePolicy.id.desc())
            .limit(_POLICY_HISTORY_LIMIT)
        ).scalars()
    ]
    current = next((h for h in history if h["valid_to"] is None), None)
    out = _pool_dict(pool, None)
    out.update({
        "purchase_ceiling_ex_tax": current["purchase_ceiling_ex_tax"] if current else None,
        "sales_floor_ex_tax": current["sales_floor_ex_tax"] if current else None,
        "members": members,
        "price_policy": current,
        "price_policy_history": history,
    })
    return out


# ---------------------------------------------------------------- 写

def create_pool(db: Session, *, name: str, description: str | None = None,
                member_part_ids: list[int] | None = None, note: str | None = None,
                operated_by: str | None = None) -> dict:
    """人工新建池。group_id 取自持久序列（单调递增、退役 ID 永不复用）。"""
    clean_name = (name or "").strip()
    if not clean_name:
        raise PoolCatalogError("池名称不能为空")
    if len(clean_name) > 128:
        raise PoolCatalogError("池名称过长（≤128 字符）")
    part_ids = sorted(set(member_part_ids or []))

    _lock_parts(db, part_ids)
    id_to_pn = _validate_parts(db, part_ids)
    conflicts = _active_pool_conflicts(db, part_ids)
    if conflicts:
        raise PoolConflictError(_conflict_message(conflicts))

    group_id = int(db.execute(text("SELECT nextval('part_pool_group_id_seq')")).scalar())
    pool = PartPool(group_id=group_id, name=clean_name, description=(description or None),
                    status="active", source="manual", version=1,
                    member_count=len(part_ids),
                    created_by=operated_by, updated_by=operated_by)
    db.add(pool)
    for pid in part_ids:
        db.add(PartPoolMember(group_id=group_id, part_id=pid, added_by=operated_by))
    db.flush()
    _audit(db, group_id, "create", None,
           {"name": clean_name, "description": description,
            "members": sorted(id_to_pn.values())}, note, operated_by)
    db.commit()
    return _pool_dict(pool, None)


def update_pool(db: Session, *, group_id: int, version: int, updates: dict,
                note: str | None = None, operated_by: str | None = None) -> dict | None:
    """改名称/说明（乐观锁）。updates ⊆ {name, description}，PATCH 语义只改显式传入项。"""
    allowed = {k: v for k, v in updates.items() if k in ("name", "description")}
    if not allowed:
        raise PoolCatalogError("没有可修改的字段")
    pool = _pool_for_update(db, group_id)
    if pool is None:
        return None
    _require_active(pool, "编辑")
    _check_version(pool, version)

    before = {"name": pool.name, "description": pool.description}
    if "name" in allowed:
        new_name = (allowed["name"] or "").strip()
        if not new_name:
            raise PoolCatalogError("池名称不能为空")
        if len(new_name) > 128:
            raise PoolCatalogError("池名称过长（≤128 字符）")
        pool.name = new_name
    if "description" in allowed:
        pool.description = (allowed["description"] or None)
    pool.version += 1
    pool.updated_by = operated_by
    _audit(db, group_id, "update", before,
           {"name": pool.name, "description": pool.description}, note, operated_by)
    db.commit()
    return _pool_dict(pool, _current_policy(db, group_id))


def update_members(db: Session, *, group_id: int, version: int,
                   add_part_ids: list[int] | None = None,
                   remove_part_ids: list[int] | None = None,
                   note: str | None = None, operated_by: str | None = None) -> dict | None:
    """一次事务增删成员（乐观锁 + 有效池唯一性）。"""
    adds = sorted(set(add_part_ids or []))
    removes = sorted(set(remove_part_ids or []))
    if not adds and not removes:
        raise PoolCatalogError("没有要增删的成员")
    both = set(adds) & set(removes)
    if both:
        raise PoolCatalogError(f"同一 PN 不能同时增删: part_id={sorted(both)}")

    pool = _pool_for_update(db, group_id)
    if pool is None:
        return None
    _require_active(pool, "调整成员")
    _check_version(pool, version)
    _lock_parts(db, adds)

    current_ids = set(db.scalars(
        select(PartPoolMember.part_id).where(PartPoolMember.group_id == group_id)).all())

    id_to_pn = _validate_parts(db, adds)
    already = [id_to_pn[pid] for pid in adds if pid in current_ids]
    if already:
        raise PoolCatalogError(f"已在本池，无需重复加入: {', '.join(already)}")
    conflicts = _active_pool_conflicts(db, adds, exclude_group_id=group_id)
    if conflicts:
        raise PoolConflictError(_conflict_message(conflicts))

    missing_remove = [pid for pid in removes if pid not in current_ids]
    if missing_remove:
        raise PoolCatalogError(f"不是本池成员，无法移除: part_id={sorted(missing_remove)}")

    removed_pns = []
    if removes:
        removed_pns = sorted(db.scalars(
            select(DimPart.pn_std).where(DimPart.id.in_(removes))).all())
        for m in db.execute(
            select(PartPoolMember).where(PartPoolMember.group_id == group_id,
                                         PartPoolMember.part_id.in_(removes))
        ).scalars():
            db.delete(m)
    for pid in adds:
        db.add(PartPoolMember(group_id=group_id, part_id=pid, added_by=operated_by))
    db.flush()

    pool.member_count = _member_count(db, group_id)
    pool.version += 1
    pool.updated_by = operated_by
    _audit(db, group_id, "members",
           {"member_count": len(current_ids)},
           {"added": sorted(id_to_pn.values()), "removed": removed_pns,
            "member_count": pool.member_count}, note, operated_by)
    db.commit()
    return _pool_dict(pool, _current_policy(db, group_id))


def set_price_policy(db: Session, *, group_id: int, version: int,
                     purchase_value: Decimal | None = None, purchase_basis: str = "ex_tax",
                     sales_value: Decimal | None = None, sales_basis: str = "ex_tax",
                     note: str | None = None, operated_by: str | None = None) -> dict | None:
    """设置采购最高价/销售最低价：关闭旧行 + 插入新行，不覆盖历史（§15.3）。

    值为 None = 该侧不设约束（unset，管理页明确显示"未设置"）；两侧都 None = 清空。
    含税录入 ÷1.13 统一成未税入库，原始录入值与口径保留。等于约束价不算越线（§13，
    越线判定属 Slice 2 分析侧，此处只管配置）。
    """
    for basis, side in ((purchase_basis, "采购"), (sales_basis, "销售")):
        if basis not in _BASES:
            raise PoolCatalogError(f"{side}录入口径必须是 ex_tax（未税）或 inc_tax（含税）")
    for value, side in ((purchase_value, "采购最高价"), (sales_value, "销售最低价")):
        if value is not None and value <= 0:
            raise PoolCatalogError(f"{side}必须大于 0")

    pool = _pool_for_update(db, group_id)
    if pool is None:
        return None
    _require_active(pool, "设置约束价")
    _check_version(pool, version)

    old = _current_policy(db, group_id)
    db.execute(
        update(PartPoolPricePolicy)
        .where(PartPoolPricePolicy.group_id == group_id,
               PartPoolPricePolicy.valid_to.is_(None))
        .values(valid_to=func.now())
    )
    new_policy = PartPoolPricePolicy(
        group_id=group_id,
        purchase_ceiling_ex_tax=_to_ex_tax(purchase_value, purchase_basis),
        sales_floor_ex_tax=_to_ex_tax(sales_value, sales_basis),
        purchase_input_value=purchase_value,
        purchase_input_basis=(purchase_basis if purchase_value is not None else None),
        sales_input_value=sales_value,
        sales_input_basis=(sales_basis if sales_value is not None else None),
        changed_by=operated_by, note=note,
    )
    db.add(new_policy)
    try:
        # 部分唯一索引 (group_id) WHERE valid_to IS NULL 兜底并发双写
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise PoolConflictError("约束价刚被他人修改，请刷新后重试") from exc

    pool.version += 1
    pool.updated_by = operated_by
    _audit(db, group_id, "set_policy", _policy_dict(old) if old else None,
           {"purchase_ceiling_ex_tax": new_policy.purchase_ceiling_ex_tax,
            "sales_floor_ex_tax": new_policy.sales_floor_ex_tax,
            "purchase_input_value": purchase_value, "purchase_input_basis": purchase_basis,
            "sales_input_value": sales_value, "sales_input_basis": sales_basis},
           note, operated_by)
    db.commit()
    out = _pool_dict(pool, new_policy)
    out["price_policy"] = _policy_dict(new_policy)
    return out


def archive_pool(db: Session, *, group_id: int, version: int, note: str | None = None,
                 operated_by: str | None = None) -> dict | None:
    """归档（不硬删除）：成员集合与约束历史原样保留，成员从此可加入其他有效池。"""
    pool = _pool_for_update(db, group_id)
    if pool is None:
        return None
    if pool.status == "archived":
        raise PoolCatalogError(f"池「{pool.name}」已是归档状态")
    _check_version(pool, version)
    pool.status = "archived"
    pool.version += 1
    pool.updated_by = operated_by
    _audit(db, group_id, "archive", {"status": "active"}, {"status": "archived"},
           note, operated_by)
    db.commit()
    return _pool_dict(pool, _current_policy(db, group_id))


def restore_pool(db: Session, *, group_id: int, version: int, note: str | None = None,
                 operated_by: str | None = None) -> dict | None:
    """恢复归档池。成员若已在归档期间加入其他有效池 → 409 冲突并列出占用池，
    先在成员维护里解决归属再恢复（不静默抢占）。"""
    pool = _pool_for_update(db, group_id)
    if pool is None:
        return None
    if pool.status == "active":
        raise PoolCatalogError(f"池「{pool.name}」已是有效状态")
    _check_version(pool, version)

    member_ids = sorted(db.scalars(
        select(PartPoolMember.part_id).where(PartPoolMember.group_id == group_id)).all())
    _lock_parts(db, member_ids)
    conflicts = _active_pool_conflicts(db, member_ids, exclude_group_id=group_id)
    if conflicts:
        raise PoolConflictError(_conflict_message(conflicts, verb="恢复"))

    pool.status = "active"
    pool.version += 1
    pool.updated_by = operated_by
    _audit(db, group_id, "restore", {"status": "archived"}, {"status": "active"},
           note, operated_by)
    db.commit()
    return _pool_dict(pool, _current_policy(db, group_id))
