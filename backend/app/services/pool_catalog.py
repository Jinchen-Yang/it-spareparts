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

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app import config
from app.models.dimensions import DimPart
from app.models.inventory import PartPool, PartPoolMember, PartPoolPricePolicy
from app.models.system import SysAuditLog

# pg_advisory 锁命名空间（int4 双参版第一参）。918273645 是旧 rebuild 全局锁键，
# 已随 rebuild 退役；本命名空间按 part_id 细粒度加锁，互不冲突。
_PART_LOCK_NS = 918273646

_BASES = ("ex_tax", "inc_tax")
_POLICY_HISTORY_LIMIT = 20
_POLICY_MISSING = {"purchase", "sales", "either", "both"}
# 《互通PN池》核心规则第 5 条：每个有效池至少包含两个 PN。建池、调整成员、
# 恢复归档池和历史数据升级都必须守住这一全局不变量。
MIN_ACTIVE_POOL_MEMBERS = 2


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


def _distinct_part_ids(values: list[int] | None, *, label: str) -> list[int]:
    """校验成员请求不含重复 ID，并返回稳定排序结果。

    重复成员属于请求错误；静默 set 去重会让调用方误以为所有输入都被执行。
    """
    raw = list(values or [])
    seen: set[int] = set()
    duplicates: set[int] = set()
    for part_id in raw:
        if part_id in seen:
            duplicates.add(part_id)
        seen.add(part_id)
    if duplicates:
        raise PoolCatalogError(f"{label}包含重复 part_id: {sorted(duplicates)}")
    return sorted(raw)


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


def _policy_coverage(db: Session) -> dict[str, int]:
    """全局有效池约束价覆盖率（DEV-07）。

    只连接 ``valid_to IS NULL`` 的当前策略；历史策略与归档池均不进入分母。
    单条聚合 SQL 返回全部指标，查询次数不随池数增长。
    """
    policy = aliased(PartPoolPricePolicy)
    row = db.execute(
        select(
            func.count(PartPool.group_id).label("active_pool_count"),
            func.count(PartPool.group_id).filter(
                policy.purchase_ceiling_ex_tax.is_not(None)
            ).label("purchase_set_count"),
            func.count(PartPool.group_id).filter(
                policy.sales_floor_ex_tax.is_not(None)
            ).label("sales_set_count"),
            func.count(PartPool.group_id).filter(
                policy.purchase_ceiling_ex_tax.is_not(None),
                policy.sales_floor_ex_tax.is_not(None),
            ).label("both_set_count"),
        )
        .select_from(PartPool)
        .outerjoin(
            policy,
            and_(policy.group_id == PartPool.group_id, policy.valid_to.is_(None)),
        )
        .where(PartPool.status == "active")
    ).one()
    active = int(row.active_pool_count or 0)
    purchase_set = int(row.purchase_set_count or 0)
    sales_set = int(row.sales_set_count or 0)
    return {
        "active_pool_count": active,
        "purchase_set_count": purchase_set,
        "purchase_missing_count": active - purchase_set,
        "sales_set_count": sales_set,
        "sales_missing_count": active - sales_set,
        "both_set_count": int(row.both_set_count or 0),
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
               policy_missing: str | None = None,
               page: int = 1, page_size: int = 20) -> dict:
    """池清单（管理页）：搜索池名/描述/成员 PN/品牌，带当前约束价和全局覆盖率。"""
    if policy_missing is not None and policy_missing not in _POLICY_MISSING:
        raise PoolCatalogError("约束价缺失筛选必须是 purchase / sales / either / both")

    stmt = select(PartPool)
    if status in ("active", "archived"):
        stmt = stmt.where(PartPool.status == status)
    if policy_missing is not None:
        # 缺失筛选是“有效池约束补录”入口，归档池始终排除；当前策略只认 valid_to IS NULL。
        current = select(PartPoolPricePolicy.id).where(
            PartPoolPricePolicy.group_id == PartPool.group_id,
            PartPoolPricePolicy.valid_to.is_(None),
        )
        purchase_set = current.where(
            PartPoolPricePolicy.purchase_ceiling_ex_tax.is_not(None)
        ).exists()
        sales_set = current.where(
            PartPoolPricePolicy.sales_floor_ex_tax.is_not(None)
        ).exists()
        stmt = stmt.where(PartPool.status == "active")
        if policy_missing == "purchase":
            stmt = stmt.where(~purchase_set)
        elif policy_missing == "sales":
            stmt = stmt.where(~sales_set)
        elif policy_missing == "either":
            stmt = stmt.where(or_(~purchase_set, ~sales_set))
        else:  # both
            stmt = stmt.where(and_(~purchase_set, ~sales_set))
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
            "coverage": _policy_coverage(db),
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
    part_ids = _distinct_part_ids(member_part_ids, label="成员列表")
    if len(part_ids) < MIN_ACTIVE_POOL_MEMBERS:
        raise PoolCatalogError(
            f"有效池至少包含 {MIN_ACTIVE_POOL_MEMBERS} 个 PN（当前 {len(part_ids)} 个）")

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
    adds = _distinct_part_ids(add_part_ids, label="新增成员")
    removes = _distinct_part_ids(remove_part_ids, label="移除成员")
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

    # 有效池 ≥2 成员：先按增删差算出终态数量，任何删除发生前整体拒绝——
    # 绝不出现"删了一半才发现不足"的部分结果
    final_count = len(current_ids) - len(removes) + len(adds)
    if final_count < MIN_ACTIVE_POOL_MEMBERS:
        raise PoolCatalogError(
            f"有效池至少包含 {MIN_ACTIVE_POOL_MEMBERS} 个 PN：本次调整后仅剩 "
            f"{final_count} 个，未做任何改动")

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


_POLICY_OPS = ("set", "unset", "keep")


def _resolve_policy_side(op: str | None, value: Decimal | None, basis: str, side: str,
                         old: PartPoolPricePolicy | None,
                         ex_attr: str, in_attr: str, basis_attr: str) -> tuple:
    """单侧更新语义（复审阻塞 4）：返回 (op, ex_tax, input_value, input_basis)。

    - op 缺省按参数推导：给了值 = set，没给值 = keep（**绝不**把 None 当"清空"）；
    - 只有显式 op="unset" 才清空该侧；
    - keep 原样复制当前策略该侧三个字段（无当前策略则保持未设置）。
    """
    op = op or ("set" if value is not None else "keep")
    if op not in _POLICY_OPS:
        raise PoolCatalogError(f"{side}操作必须是 set / unset / keep")
    if op == "set":
        if value is None:
            raise PoolCatalogError(f"{side}为 set 时必须给出新值；清空请用显式 unset")
        if value <= 0:
            raise PoolCatalogError(f"{side}必须大于 0")
        if basis not in _BASES:
            raise PoolCatalogError(f"{side}录入口径必须是 ex_tax（未税）或 inc_tax（含税）")
        return op, _to_ex_tax(value, basis), value, basis
    if value is not None:
        raise PoolCatalogError(f"{side}同时给出了新值与 {op} 指令，请二选一")
    if op == "unset" or old is None:
        return op, None, None, None
    return op, getattr(old, ex_attr), getattr(old, in_attr), getattr(old, basis_attr)


def set_price_policy(db: Session, *, group_id: int, version: int,
                     purchase_op: str | None = None,
                     purchase_value: Decimal | None = None, purchase_basis: str = "ex_tax",
                     sales_op: str | None = None,
                     sales_value: Decimal | None = None, sales_basis: str = "ex_tax",
                     note: str | None = None, operated_by: str | None = None) -> dict | None:
    """设置采购最高价/销售最低价：关闭旧行 + 插入新行，不覆盖历史（§15.3）。

    单侧更新语义（复审阻塞 4）：每侧独立三态——set（给新值）/ unset（显式清空）/
    keep（缺省，保持当前值）。普通 None **不是**清空：脱敏成 null 的另一侧提交上来
    会按 keep 保留，杜绝"可写不可读"组合把看不见的一侧静默清空。
    含税录入 ÷1.13 统一成未税入库，原始录入值与口径保留。等于约束价不算越线（§13，
    越线判定属 Slice 2 分析侧，此处只管配置）。
    """
    pool = _pool_for_update(db, group_id)
    if pool is None:
        return None
    _require_active(pool, "设置约束价")
    _check_version(pool, version)

    old = _current_policy(db, group_id)
    p_op, p_ex, p_in, p_basis = _resolve_policy_side(
        purchase_op, purchase_value, purchase_basis, "采购最高价", old,
        "purchase_ceiling_ex_tax", "purchase_input_value", "purchase_input_basis")
    s_op, s_ex, s_in, s_basis = _resolve_policy_side(
        sales_op, sales_value, sales_basis, "销售最低价", old,
        "sales_floor_ex_tax", "sales_input_value", "sales_input_basis")
    if p_op == "keep" and s_op == "keep":
        raise PoolCatalogError("两侧都是 keep：没有要修改的约束价")

    db.execute(
        update(PartPoolPricePolicy)
        .where(PartPoolPricePolicy.group_id == group_id,
               PartPoolPricePolicy.valid_to.is_(None))
        .values(valid_to=func.now())
    )
    new_policy = PartPoolPricePolicy(
        group_id=group_id,
        purchase_ceiling_ex_tax=p_ex,
        sales_floor_ex_tax=s_ex,
        purchase_input_value=p_in,
        purchase_input_basis=p_basis,
        sales_input_value=s_in,
        sales_input_basis=s_basis,
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
    # 审计明确记录每侧发生了什么（set/unset/keep）——复核"谁清了哪一侧"不用比对猜
    _audit(db, group_id, "set_policy", _policy_dict(old) if old else None,
           {"purchase_op": p_op, "sales_op": s_op,
            "purchase_ceiling_ex_tax": p_ex, "sales_floor_ex_tax": s_ex,
            "purchase_input_value": p_in, "purchase_input_basis": p_basis,
            "sales_input_value": s_in, "sales_input_basis": s_basis},
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
    if len(member_ids) < MIN_ACTIVE_POOL_MEMBERS:
        raise PoolCatalogError(
            f"池「{pool.name}」仅有 {len(member_ids)} 个 PN，有效池至少包含 "
            f"{MIN_ACTIVE_POOL_MEMBERS} 个 PN；请先补齐成员再恢复"
        )
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
