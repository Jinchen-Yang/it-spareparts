"""幂等入库：维度 upsert（字段优先级）+ 事实去重 upsert + 库存求和（§6.4/§7.5）。

调用方（pipeline）负责事务边界与 batch。本模块所有写操作复用传入 session。

主数据重定向（整改 P3）——导入时商品身份解析按三层确定，防止治理成果被导入冲掉：
1. 别名重定向：pn_raw 命中 status='active' 且指向其它型号的 part_alias
   （人工审核接受的别名/合并产生的映射）→ 直接用别名的 part_id，不再重复建档；
2. 合并重定向：pn_std 命中 status='merged' 的墓碑行 → 沿 merged_into_id 链取目标
   （合并服务做路径压缩，链长通常 ≤1，循环上限防御坏数据）；
3. 其余 → 照常按 pn_std upsert 建档。
明细行 pn_std/pn_raw 一律保留 cleaner 归一原文（不改写为目标 pn）——这是
追溯与合并回滚归属判定的前提；商品身份只体现在 part_id。
"""
import logging
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.etl import mapping
from app.etl.transform import SOFT_ERROR_TYPES, TransformResult
from app.models.dimensions import DimCustomer, DimPart, DimSupplier, PartAlias
from app.models.inventory import Inventory
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder, FProjectExpense
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysAuditLog

_log = logging.getLogger("loader")
_CHUNK = 1000  # 每批行数，控制单语句参数数 < PostgreSQL 65535 上限
_MERGE_CHAIN_LIMIT = 10
# 覆盖留痕（🥉/§3）：upsert/库存覆盖既有行时写 before/after，超量只记汇总防审计爆量。
_AUDIT_OVERWRITE_MAX = 2000
_AUDIT_IGNORE = {"import_batch_id"}   # 变化检测忽略（每次导入必变，非业务冲突）


def _jsonable(v):
    """JSONB 安全化：Decimal→str、date/datetime→isoformat，其余原样（list[str]/int/None 均可）。"""
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def _audit_overwrites(session: Session, entity_type: str, table: str,
                      entries: list[tuple], operated_by: str | None, batch_id: int) -> None:
    """对被覆盖的行写 SysAuditLog（before/after）。entries: [(entity_id, before_dict, after_dict)]。"""
    if not entries:
        return
    reason = f"导入覆盖（batch {batch_id} · {table}）"
    if len(entries) > _AUDIT_OVERWRITE_MAX:
        session.add(SysAuditLog(
            entity_type=entity_type, entity_id=batch_id, action="overwrite",
            before_json=None,
            after_json={"overwritten_rows": len(entries),
                        "note": f"超过 {_AUDIT_OVERWRITE_MAX} 行，仅记总数"},
            reason=reason, operated_by=operated_by))
        return
    for eid, before, after in entries:
        session.add(SysAuditLog(
            entity_type=entity_type, entity_id=eid, action="overwrite",
            before_json=before, after_json=after, reason=reason, operated_by=operated_by))


class MergeChainError(Exception):
    """merged_into_id 链超限/成环（数据损坏），拒绝整批导入。"""


def _chunks(seq: list, n: int = _CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _alias_redirects(session: Session, rows: list[dict]) -> dict[str, int]:
    """pn_raw → part_id：已生效且指向「与本行归一结果不同型号」的别名。

    恒等别名（alias.pn_std == 行 pn_std）不构成重定向，照常走建档/enrich 路径。
    """
    raw_to_std = {r["pn_raw"]: r["pn_std"] for r in rows if r.get("pn_raw")}
    if not raw_to_std:
        return {}
    out: dict[str, int] = {}
    for chunk in _chunks(list(raw_to_std.keys())):
        for pn_raw, a_std, a_part in session.execute(
            select(PartAlias.pn_raw, PartAlias.pn_std, PartAlias.part_id).where(
                PartAlias.pn_raw.in_(chunk),
                PartAlias.status == "active",
                PartAlias.part_id.is_not(None),
            )
        ).all():
            if a_std != raw_to_std[pn_raw]:
                out[pn_raw] = a_part
    return out


def _resolve_merged(session: Session, ids: set[int]) -> dict[int, tuple[int, str]]:
    """id → (最终 part_id, 最终 pn_std)：沿 merged_into_id 链重定向到 active 行。"""
    out: dict[int, tuple[int, str]] = {}
    if not ids:
        return out
    info: dict[int, tuple[str, int | None, str]] = {}

    def _load(batch: set[int]) -> None:
        for chunk in _chunks(list(batch)):
            for pid, pn, status, into in session.execute(
                select(DimPart.id, DimPart.pn_std, DimPart.status, DimPart.merged_into_id)
                .where(DimPart.id.in_(chunk))
            ).all():
                info[pid] = (status, into, pn)

    _load(ids)
    for start in ids:
        cur = start
        for _ in range(_MERGE_CHAIN_LIMIT):
            status, into, pn = info[cur]
            if status != "merged" or into is None:
                out[start] = (cur, pn)
                break
            if into not in info:
                _load({into})
            cur = into
        else:
            raise MergeChainError(f"part {start} 的合并链超过 {_MERGE_CHAIN_LIMIT} 跳，疑似成环")
    return out


def _merge_part_attrs(rows: list[dict], is_sales: bool) -> dict[str, dict]:
    """同一 pn_std 聚合属性：描述/品牌/单位取首个非空，needs_review 取 OR，品类仅销售。"""
    out: dict[str, dict] = {}
    for r in rows:
        pn = r["pn_std"]
        a = out.setdefault(pn, {
            "pn_std": pn, "pn_raw_sample": r.get("pn_raw"),
            "description": None, "brand": None, "machine_or_part": None, "unit": None,
            "category_major": None, "category_minor": None, "needs_review": False,
        })
        for f in ("description", "brand", "machine_or_part", "unit"):
            if a[f] is None and r.get(f) is not None:
                a[f] = r[f]
        if is_sales:
            for f in ("category_major", "category_minor"):
                if a[f] is None and r.get(f) is not None:
                    a[f] = r[f]
        a["needs_review"] = a["needs_review"] or bool(r.get("needs_review"))
    return out


def _upsert_parts(session: Session, part_attrs: dict[str, dict], is_sales: bool) -> dict[str, int]:
    """upsert dim_part；返回 pn_std -> id（未做合并重定向的原始 id）。

    字段优先级（§7.5）：描述/品牌/单位 fill-if-empty；品类仅销售可写（COALESCE 新值优先）；
    needs_review 取 OR。占位品类已在 cleaner 置空，故采购/库存天然不写品类。
    merged 墓碑行不做属性 enrich（属性应落到合并目标上，由调用方重定向后处理）。
    """
    if not part_attrs:
        return {}

    def _respect_lock(field: str, normal):
        """采购人工维护过(locked_fields 含该字段)→ 一律保留人工值；否则走原优先级。
        "和氚云无 API、把服务器 PN 做成自治主数据"的地基：重导永不覆盖采购改过的字段。"""
        return case((DimPart.locked_fields.contains([field]), getattr(DimPart, field)),
                    else_=normal)

    for chunk in _chunks(list(part_attrs.values())):
        stmt = pg_insert(DimPart).values(chunk)
        # fill-if-empty：保留已有值，空则用新值（locked 字段则完全保留人工值）
        set_ = {
            "description": _respect_lock(
                "description", func.coalesce(DimPart.description, stmt.excluded.description)),
            "brand": _respect_lock(
                "brand", func.coalesce(DimPart.brand, stmt.excluded.brand)),
            "machine_or_part": _respect_lock(
                "machine_or_part",
                func.coalesce(DimPart.machine_or_part, stmt.excluded.machine_or_part)),
            "unit": _respect_lock("unit", func.coalesce(DimPart.unit, stmt.excluded.unit)),
            "pn_raw_sample": func.coalesce(DimPart.pn_raw_sample, stmt.excluded.pn_raw_sample),
            "needs_review": or_(DimPart.needs_review, stmt.excluded.needs_review),
        }
        if is_sales:
            # 销售可改写品类：新值非空优先（但采购锁定的品类不被覆盖）
            set_["category_major"] = _respect_lock(
                "category_major",
                func.coalesce(stmt.excluded.category_major, DimPart.category_major))
            set_["category_minor"] = _respect_lock(
                "category_minor",
                func.coalesce(stmt.excluded.category_minor, DimPart.category_minor))
        session.execute(stmt.on_conflict_do_update(
            index_elements=[DimPart.pn_std], set_=set_,
            where=(DimPart.status != "merged"),
        ))
    out: dict[str, int] = {}
    for chunk in _chunks(list(part_attrs.keys())):
        for pn, pid in session.execute(
            select(DimPart.pn_std, DimPart.id).where(DimPart.pn_std.in_(chunk))
        ).all():
            out[pn] = pid
    return out


def _resolve_line_parts(session: Session, rows: list[dict],
                        is_sales: bool) -> tuple[dict[str, tuple[int, str]], int]:
    """对一批明细/库存行做完整商品身份解析。

    返回 (pn_raw → (最终 part_id, 该 part 的 pn_std), 参与建档的型号数)。
    别名重定向的行不参与 dim_part 建档/enrich（防复活已治理的重复建档）。
    """
    redirect = _alias_redirects(session, rows)
    create_rows = [r for r in rows if r.get("pn_raw") not in redirect]
    part_attrs = _merge_part_attrs(create_rows, is_sales)
    pn_to_id = _upsert_parts(session, part_attrs, is_sales)

    resolved = _resolve_merged(session, set(pn_to_id.values()) | set(redirect.values()))
    out: dict[str, tuple[int, str]] = {}
    for r in rows:
        raw = r.get("pn_raw")
        pid = redirect.get(raw) if raw in redirect else pn_to_id.get(r["pn_std"])
        if pid is not None:
            out[raw] = resolved[pid]
    return out, len(part_attrs)


def _upsert_aliases(session: Session, rows: list[dict],
                    resolution: dict[str, tuple[int, str]]) -> None:
    """别名落库：pn_raw → (归属 part_id, 该 part 的 pn_std)。

    status 与 needs_review 由同一处派生（待审=pending），防双真相源；
    已有别名行不改写（人工审核结果优先，on conflict do nothing）。
    """
    seen = {}
    for r in rows:
        raw = r.get("pn_raw")
        if not raw or raw not in resolution:
            continue
        pid, canonical_pn = resolution[raw]
        pending = bool(r.get("needs_review"))
        seen[raw] = {"pn_raw": raw, "pn_std": canonical_pn, "part_id": pid,
                     "source": "auto", "needs_review": pending,
                     "status": "pending" if pending else "active"}
    if not seen:
        return
    for chunk in _chunks(list(seen.values())):
        stmt = pg_insert(PartAlias).values(chunk)
        session.execute(stmt.on_conflict_do_nothing(index_elements=[PartAlias.pn_raw]))


def _upsert_named_dim(session: Session, model, rows: list[dict], extra_cols: list[str]) -> dict[str, int]:
    """供应商/客户按 name_raw upsert，缺失属性 fill-if-empty。返回 name_raw -> id。"""
    dedup = {}
    for r in rows:
        if r.get("name_raw"):
            dedup[r["name_raw"]] = r
    if not dedup:
        return {}
    for chunk in _chunks(list(dedup.values())):
        stmt = pg_insert(model).values(chunk)
        set_ = {c: func.coalesce(getattr(model, c), getattr(stmt.excluded, c)) for c in extra_cols}
        session.execute(stmt.on_conflict_do_update(index_elements=[model.name_raw], set_=set_))
    out: dict[str, int] = {}
    for chunk in _chunks(list(dedup.keys())):
        for n, i in session.execute(select(model.name_raw, model.id).where(model.name_raw.in_(chunk))).all():
            out[n] = i
    return out


def _upsert_facts(session: Session, model, rows: list[dict], conflict_col,
                  update_cols: list[str] | None = None, audit: tuple | None = None) -> dict:
    """事实表幂等写入。

    - update_cols=None(默认 skip)：ON CONFLICT DO NOTHING；返回 {inserted, skipped}。
    - update_cols=列名列表(upsert 修复模式)：ON CONFLICT DO UPDATE 这些字段；
      预先点一下已存在的键以区分新增/更新；返回 {inserted, updated}。
    - audit=(operated_by, batch_id)（仅 upsert 有意义）：覆盖既有行时把 before/after 写 SysAuditLog，
      只记业务字段确有变化的行（忽略 import_batch_id），供「后到覆盖先到」回溯（🥉/§3）。
    """
    if not rows:
        return {"inserted": 0, "updated": 0, "skipped": 0}
    if update_cols is None:
        inserted = 0
        for chunk in _chunks(rows):
            stmt = pg_insert(model).values(chunk).on_conflict_do_nothing(index_elements=[conflict_col])
            inserted += len(session.execute(stmt.returning(conflict_col)).all())
        return {"inserted": inserted, "updated": 0, "skipped": len(rows) - inserted}
    # upsert 模式
    key_name = conflict_col.name
    keys = [r[key_name] for r in rows]
    before_by_key: dict = {}
    if audit is not None:   # 取既有行 id + 旧值（存原值，比较按数值/对象，避免 Decimal 标度误判）
        sel_cols = [conflict_col, model.id, *[getattr(model, c) for c in update_cols]]
        for chunk in _chunks(keys):
            for row in session.execute(select(*sel_cols).where(conflict_col.in_(chunk))).all():
                m = row._mapping
                before_by_key[m[key_name]] = (m["id"], {c: m[c] for c in update_cols})
        existing = len(before_by_key)
    else:
        existing = 0
        for chunk in _chunks(keys):
            existing += session.scalar(
                select(func.count()).select_from(model).where(conflict_col.in_(chunk))
            ) or 0
    for chunk in _chunks(rows):
        stmt = pg_insert(model).values(chunk)
        set_ = {c: getattr(stmt.excluded, c) for c in update_cols}
        session.execute(stmt.on_conflict_do_update(index_elements=[conflict_col], set_=set_))
    if audit is not None and before_by_key:
        op_by, b_id = audit
        entries = []
        for r in rows:
            prev = before_by_key.get(r[key_name])
            if prev is None:
                continue
            eid, before = prev          # before: 原值（Decimal/date/...）
            after = {c: r.get(c) for c in update_cols}
            if any(before.get(c) != after.get(c) for c in update_cols if c not in _AUDIT_IGNORE):
                entries.append((eid, {c: _jsonable(v) for c, v in before.items()},
                                {c: _jsonable(v) for c, v in after.items()}))
        _audit_overwrites(session, "import_overwrite", model.__tablename__, entries, op_by, b_id)
    return {"inserted": len(rows) - existing, "updated": existing, "skipped": 0}


# 可更新字段(upsert 修复模式)：排除主键 raw_*_id 与利润派生字段(recompute 专属)
_PURCHASE_ORDER_UPD = ["order_no", "order_date", "purchaser", "supplier_id",
                        "linked_sales_order_no", "linked_maintenance_order_no",
                        "source_type", "source_type_raw",
                        "amount_ex_tax", "tax_rate", "is_tax_inclusive", "tax_amount",
                        "amount_inc_tax", "data_status", "import_batch_id"]
_PURCHASE_LINE_UPD = ["order_id", "line_no", "part_id", "pn_std", "pn_raw", "description",
                       "brand", "machine_or_part", "unit", "qty", "unit_price", "line_amount",
                       "recent_purchase_price", "anomaly_flags", "import_batch_id"]
_SALES_ORDER_UPD = ["order_no", "order_date", "salesperson", "customer_id",
                     "business_type", "warehouse", "amount_ex_tax", "tax_rate",
                     "data_status", "import_batch_id"]
_SALES_LINE_UPD = ["order_id", "line_no", "part_id", "pn_std", "pn_raw", "description",
                    "brand", "category_major", "category_minor", "machine_or_part", "unit",
                    "qty", "unit_price", "line_amount", "generic_product", "serial_numbers",
                    "anomaly_flags", "import_batch_id"]
# 维保：成本回填字段(unit_cost/cost_amount/cost_source/cost_tax_basis/price_month/
# trace_months/linked_purchase_order_no)为 maintenance_cost.recompute 专属，一律排除
_MAINT_ORDER_UPD = ["order_no", "order_date", "linked_sales_order_no", "project_raw",
                     "project_std", "customer_id", "end_customer", "demand_type",
                     "business_type", "salesperson", "warehouse", "maint_start", "maint_end",
                     "data_status", "import_batch_id"]
_MAINT_LINE_UPD = ["order_id", "line_no", "part_id", "pn_std", "pn_raw", "description",
                    "qty", "return_qty", "serial_numbers", "anomaly_flags", "import_batch_id"]


def load(session: Session, result: TransformResult, batch_id: int, snapshot_date: date,
         mode: str = "skip", operated_by: str | None = None,
         audit_overwrites: bool = False) -> dict:
    if result.file_type == mapping.INVENTORY:
        return _load_inventory(session, result, batch_id, snapshot_date,
                               operated_by, audit_overwrites)
    if result.file_type == mapping.MAINTENANCE:
        return _load_maintenance(session, result, batch_id, mode, operated_by, audit_overwrites)
    if result.file_type == mapping.EXPENSE:
        return _load_expense(session, result, batch_id, mode, operated_by, audit_overwrites)
    return _load_orders(session, result, batch_id, mode, operated_by, audit_overwrites)


def _load_orders(session: Session, result: TransformResult, batch_id: int,
                 mode: str = "skip", operated_by: str | None = None,
                 audit_overwrites: bool = False) -> dict:
    upsert = (mode == "upsert")
    # 仅 upsert(覆盖)模式需留痕；skip 模式 ON CONFLICT DO NOTHING 不覆盖既有行
    audit = (operated_by, batch_id) if (upsert and audit_overwrites) else None
    is_sales = result.file_type == mapping.SALES
    # 1) 商品身份解析（别名/合并重定向 + 建档）+ alias
    resolution, new_parts = _resolve_line_parts(session, result.lines, is_sales)
    _upsert_aliases(session, result.lines, resolution)

    # 2) 供应商 / 客户
    orders = result.orders
    if is_sales:
        cust_rows = [{
            "name_raw": o["customer_name"], "name_normalized": o["customer_name"],
            "customer_type": o.get("customer_type"), "customer_source": o.get("customer_source"),
            "city": o.get("customer_city"),
        } for o in orders.values() if o.get("customer_name")]
        cust_id = _upsert_named_dim(session, DimCustomer, cust_rows,
                                    ["name_normalized", "customer_type", "customer_source", "city"])
    else:
        sup_rows = [{
            "name_raw": o["supplier_name_raw"], "name_normalized": o["supplier_name_normalized"],
            "supplier_code": o.get("supplier_code"), "supplier_type": o.get("supplier_type"),
            "source_channel": o.get("supplier_source_channel"),
        } for o in orders.values() if o.get("supplier_name_raw")]
        sup_id = _upsert_named_dim(session, DimSupplier, sup_rows,
                                   ["name_normalized", "supplier_code", "supplier_type",
                                    "source_channel"])

    # 3) 订单头
    order_model = FSalesOrder if is_sales else FPurchaseOrder
    order_rows = []
    for o in orders.values():
        base = {
            "raw_order_id": o["raw_order_id"], "order_no": o["order_no"],
            "order_date": o["order_date"], "amount_ex_tax": o["amount_ex_tax"],
            "tax_rate": o["tax_rate"], "data_status": o["data_status"],
            "import_batch_id": batch_id,
        }
        if is_sales:
            base.update({
                "salesperson": o.get("salesperson"),
                "customer_id": cust_id.get(o.get("customer_name")),
                "business_type": o.get("business_type"), "warehouse": o.get("warehouse"),
            })
        else:
            base.update({
                "purchaser": o.get("purchaser"),
                "supplier_id": sup_id.get(o.get("supplier_name_raw")),
                "source_type": o.get("source_type"), "source_type_raw": o.get("source_type_raw"),
                "linked_sales_order_no": o.get("linked_sales_order_no"),
                "linked_maintenance_order_no": o.get("linked_maintenance_order_no"),
                "is_tax_inclusive": o.get("is_tax_inclusive"),
                "tax_amount": o.get("tax_amount"),
                "amount_inc_tax": o.get("amount_inc_tax"),
            })
        order_rows.append(base)
    order_upd_cols = (_SALES_ORDER_UPD if is_sales else _PURCHASE_ORDER_UPD) if upsert else None
    order_stats = _upsert_facts(session, order_model, order_rows, order_model.raw_order_id,
                                order_upd_cols, audit=audit)
    # raw_order_id -> id（含已存在的）
    raw_ids = [o["raw_order_id"] for o in orders.values()]
    oid_map = dict(session.execute(
        select(order_model.raw_order_id, order_model.id).where(order_model.raw_order_id.in_(raw_ids))
    ).all())

    # 4) 明细行
    line_model = FSalesLine if is_sales else FPurchaseLine
    line_rows = []
    for ln in result.lines:
        base = {
            "raw_line_id": ln["raw_line_id"], "order_id": oid_map[ln["_order_raw_id"]],
            "line_no": ln["line_no"], "part_id": resolution[ln["pn_raw"]][0],
            "pn_std": ln["pn_std"], "pn_raw": ln["pn_raw"],
            "description": ln["description"], "brand": ln["brand"],
            "machine_or_part": ln["machine_or_part"], "unit": ln["unit"],
            "qty": ln["qty"], "unit_price": ln["unit_price"], "line_amount": ln["line_amount"],
            "anomaly_flags": ln["anomaly_flags"], "import_batch_id": batch_id,
        }
        if is_sales:
            base.update({
                "category_major": ln.get("category_major"), "category_minor": ln.get("category_minor"),
                "generic_product": ln.get("generic_product"), "serial_numbers": ln.get("serial_numbers"),
            })
        else:
            base["recent_purchase_price"] = ln["recent_purchase_price"]
        line_rows.append(base)
    line_upd_cols = (_SALES_LINE_UPD if is_sales else _PURCHASE_LINE_UPD) if upsert else None
    line_stats = _upsert_facts(session, line_model, line_rows, line_model.raw_line_id,
                               line_upd_cols, audit=audit)

    return {
        "source_rows_total": result.rows_total,
        "fact_rows_inserted": line_stats["inserted"],
        "fact_rows_updated": line_stats["updated"],
        "fact_rows_skipped": line_stats["skipped"],
        "fact_rows_error": sum(1 for e in result.errors if e.error_type not in SOFT_ERROR_TYPES),
        "rows_inactive": result.rows_inactive,
        "orders_inserted": order_stats["inserted"],
        "orders_updated": order_stats["updated"],
        "import_mode": mode,
        "new_parts": new_parts,
    }


def _load_maintenance(session: Session, result: TransformResult, batch_id: int,
                      mode: str = "skip", operated_by: str | None = None,
                      audit_overwrites: bool = False) -> dict:
    """维保出库（WBDD）入库：与订单路径同套路——商品身份解析 + 客户维度 + 头/行幂等 upsert。

    成本回填字段不在 upsert 白名单内（maintenance_cost.recompute 专属），重导不冲成本。
    """
    upsert = (mode == "upsert")
    audit = (operated_by, batch_id) if (upsert and audit_overwrites) else None
    # 1) 商品身份解析（别名/合并重定向 + 建档，非销售口径：不写品类）+ alias
    resolution, new_parts = _resolve_line_parts(session, result.lines, is_sales=False)
    _upsert_aliases(session, result.lines, resolution)

    # 2) 客户维度（维保客户名实测混入联系人后缀，v1 原样入库，治理留二期）
    orders = result.orders
    cust_rows = [{
        "name_raw": o["customer_name"], "name_normalized": o["customer_name"],
        "customer_type": None, "customer_source": None, "city": None,
    } for o in orders.values() if o.get("customer_name")]
    cust_id = _upsert_named_dim(session, DimCustomer, cust_rows,
                                ["name_normalized", "customer_type", "customer_source", "city"])

    # 3) 维保单头
    order_rows = [{
        "raw_order_id": o["raw_order_id"], "order_no": o["order_no"],
        "order_date": o["order_date"],
        "linked_sales_order_no": o.get("linked_sales_order_no"),
        "project_raw": o.get("project_raw"), "project_std": o.get("project_std"),
        "customer_id": cust_id.get(o.get("customer_name")),
        "end_customer": o.get("end_customer"),
        "demand_type": o.get("demand_type"), "business_type": o.get("business_type"),
        "salesperson": o.get("salesperson"), "warehouse": o.get("warehouse"),
        "maint_start": o.get("maint_start"), "maint_end": o.get("maint_end"),
        "data_status": o["data_status"], "import_batch_id": batch_id,
    } for o in orders.values()]
    order_stats = _upsert_facts(session, FMaintenanceOrder, order_rows,
                                FMaintenanceOrder.raw_order_id,
                                _MAINT_ORDER_UPD if upsert else None, audit=audit)
    raw_ids = [o["raw_order_id"] for o in orders.values()]
    oid_map = dict(session.execute(
        select(FMaintenanceOrder.raw_order_id, FMaintenanceOrder.id)
        .where(FMaintenanceOrder.raw_order_id.in_(raw_ids))
    ).all())

    # 4) 出库明细行
    line_rows = [{
        "raw_line_id": ln["raw_line_id"], "order_id": oid_map[ln["_order_raw_id"]],
        "line_no": ln["line_no"], "part_id": resolution[ln["pn_raw"]][0],
        "pn_std": ln["pn_std"], "pn_raw": ln["pn_raw"],
        "description": ln["description"],
        "qty": ln["qty"], "return_qty": ln["return_qty"],
        "serial_numbers": ln["serial_numbers"],
        "anomaly_flags": ln["anomaly_flags"], "import_batch_id": batch_id,
    } for ln in result.lines]
    line_stats = _upsert_facts(session, FMaintenanceLine, line_rows,
                               FMaintenanceLine.raw_line_id,
                               _MAINT_LINE_UPD if upsert else None, audit=audit)

    return {
        "source_rows_total": result.rows_total,
        "fact_rows_inserted": line_stats["inserted"],
        "fact_rows_updated": line_stats["updated"],
        "fact_rows_skipped": line_stats["skipped"],
        "fact_rows_error": sum(1 for e in result.errors if e.error_type not in SOFT_ERROR_TYPES),
        "rows_inactive": result.rows_inactive,
        "orders_inserted": order_stats["inserted"],
        "orders_updated": order_stats["updated"],
        "import_mode": mode,
        "new_parts": new_parts,
    }


# 可更新字段（upsert 修复模式）：排除幂等主键 raw_line_id
_EXPENSE_UPD = ["bxd_no", "line_no", "data_status", "expense_date", "person",
                "expense_type", "fee_category", "reason", "linked_sales_order_no",
                "amount", "import_batch_id"]


def _load_expense(session: Session, result: TransformResult, batch_id: int,
                  mode: str = "skip", operated_by: str | None = None,
                  audit_overwrites: bool = False) -> dict:
    """报销明细入库：单表平铺，按 raw_line_id 幂等（§16.3/§17.4）。

    不建商品/客户维度（费用行无 PN）；项目归集靠 linked_sales_order_no(XSDD) 查询时 join。
    模式语义（§17.4）——skip=增量（只进新行）；upsert=**以本表为准**：对本次文件出现的
    每个 XSDD，先删该合同全部报销行再插入（表单=该合同费用的全量真值；内容派生键下
    "改了一行金额"才不会留下旧行残影）。删除数进报告 expense_rows_replaced。
    """
    upsert = (mode == "upsert")
    audit = (operated_by, batch_id) if (upsert and audit_overwrites) else None
    replaced = 0
    if upsert and result.lines:
        contracts = {ln["linked_sales_order_no"] for ln in result.lines
                     if ln.get("linked_sales_order_no")}
        if contracts:
            # 替换前留痕：被删行的 before 快照进审计（金额数据的回溯能力，超量只记总数）
            if audit:
                olds = session.scalars(
                    select(FProjectExpense)
                    .where(FProjectExpense.linked_sales_order_no.in_(contracts))
                ).all()
                _audit_overwrites(session, "f_project_expense", "f_project_expense(替换删除)",
                                  [(o.id,
                                    {"raw_line_id": o.raw_line_id,
                                     **{c: _jsonable(getattr(o, c)) for c in _EXPENSE_UPD}},
                                    None) for o in olds],
                                  operated_by, batch_id)
            replaced = session.execute(
                delete(FProjectExpense)
                .where(FProjectExpense.linked_sales_order_no.in_(contracts))
            ).rowcount or 0
    rows = [{**ln, "import_batch_id": batch_id} for ln in result.lines]
    stats = _upsert_facts(session, FProjectExpense, rows, FProjectExpense.raw_line_id,
                          _EXPENSE_UPD if upsert else None, audit=audit)
    return {
        "source_rows_total": result.rows_total,
        "fact_rows_inserted": stats["inserted"],
        "fact_rows_updated": stats["updated"],
        "fact_rows_skipped": stats["skipped"],
        "fact_rows_error": sum(1 for e in result.errors if e.error_type not in SOFT_ERROR_TYPES),
        "rows_inactive": result.rows_inactive,
        "expense_rows_replaced": replaced,
        "import_mode": mode,
    }


def _load_inventory(session: Session, result: TransformResult, batch_id: int, snapshot_date: date,
                    operated_by: str | None = None, audit_overwrites: bool = False) -> dict:
    # 1) 商品身份解析（与订单路径同口径：别名/合并重定向）+ alias
    resolution, new_parts = _resolve_line_parts(session, result.inventory, is_sales=False)
    _upsert_aliases(session, result.inventory, resolution)

    # 2) 同 (pn_std, warehouse) 求和（实测 15 组重复，§摸底）。
    #    物理键是 pn_std+warehouse，每组只能落一个 part_id。身份选择规则（确定化，
    #    与行迭代顺序无关）：优先「未被重定向」成员（canonical==组 pn_std）；
    #    若全员重定向，取候选 part_id 最小者。若组内重定向到多个不同 part（极罕见：
    #    两个仅大小写不同的 pn_raw 各自被别名指向不同型号），告警——合并后的数量
    #    会整体记到选中型号，另一型号的库存可见性丢失。
    agg: dict[tuple, dict] = {}
    for r in result.inventory:
        key = (r["pn_std"], r["warehouse"])
        pid, canonical = resolution[r["pn_raw"]]
        is_identity = canonical == r["pn_std"]
        if key not in agg:
            agg[key] = {**r, "source_qty": Decimal("0"),
                        "_part_id": pid, "_identity": is_identity, "_targets": set()}
        g = agg[key]
        g["source_qty"] += r["source_qty"]
        g["_targets"].add(pid)
        # 身份优先级：identity 成员 > 非 identity；同级取 part_id 最小（确定化）
        better = (is_identity and not g["_identity"]) or \
                 (is_identity == g["_identity"] and pid < g["_part_id"])
        if better:
            g["_part_id"], g["_identity"] = pid, is_identity
    for key, g in agg.items():
        if len(g["_targets"]) > 1:
            _log.warning("库存组 %s 重定向到多个型号 %s，数量归到 part_id=%s",
                         key, sorted(g["_targets"]), g["_part_id"])

    rows = [{
        "raw_inventory_id": r["raw_inventory_id"], "part_id": r["_part_id"],
        "pn_std": r["pn_std"], "warehouse": r["warehouse"], "source_qty": r["source_qty"],
        "description": r["description"], "brand": r["brand"],
        "machine_or_part": r["machine_or_part"], "unit": r["unit"],
        "snapshot_date": snapshot_date, "import_batch_id": batch_id,
    } for r in agg.values()]

    # 覆盖留痕（🥉/§3）：库存快照恒覆盖 source_qty（latest-wins），先取既有量，量变则记 before/after。
    before_q: dict[tuple, tuple] = {}
    if audit_overwrites:
        wh_pairs = {(r["pn_std"], r["warehouse"]) for r in rows}
        pn_list = list({r["pn_std"] for r in rows})
        for chunk in _chunks(pn_list):
            for iid, pn, wh, sq, pid in session.execute(
                select(Inventory.id, Inventory.pn_std, Inventory.warehouse,
                       Inventory.source_qty, Inventory.part_id)
                .where(Inventory.pn_std.in_(chunk))
            ).all():
                if (pn, wh) in wh_pairs:
                    before_q[(pn, wh)] = (iid, sq, pid)

    # 3) upsert (pn_std,warehouse)：覆盖 source_qty/snapshot/批次，不动 manual_qty/is_qty_overridden/safety_stock
    for chunk in _chunks(rows):
        stmt = pg_insert(Inventory).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Inventory.pn_std, Inventory.warehouse],
            set_={
                "source_qty": stmt.excluded.source_qty,
                "snapshot_date": stmt.excluded.snapshot_date,
                "part_id": stmt.excluded.part_id,
                "description": stmt.excluded.description, "brand": stmt.excluded.brand,
                "machine_or_part": stmt.excluded.machine_or_part, "unit": stmt.excluded.unit,
                "import_batch_id": stmt.excluded.import_batch_id,
            },
        )
        session.execute(stmt)

    if audit_overwrites and before_q:
        entries = []
        for r in rows:
            prev = before_q.get((r["pn_std"], r["warehouse"]))
            if prev is None:
                continue
            iid, old_qty, old_pid = prev
            # 数量或商品身份(part_id，别名/合并改判后可变)任一变化都留痕（与订单行口径一致）
            if old_qty != r["source_qty"] or old_pid != r["part_id"]:
                entries.append((iid,
                    {"source_qty": _jsonable(old_qty), "part_id": old_pid},
                    {"source_qty": _jsonable(r["source_qty"]), "part_id": r["part_id"],
                     "snapshot_date": _jsonable(snapshot_date)}))
        _audit_overwrites(session, "inventory_overwrite", "inventory", entries, operated_by, batch_id)

    return {
        "source_rows_total": result.rows_total,
        "fact_rows_inserted": len(rows),
        "fact_rows_skipped": 0,
        "fact_rows_error": sum(1 for e in result.errors if e.error_type not in SOFT_ERROR_TYPES),
        "rows_inactive": result.rows_inactive,
        "rows_excluded_warehouse": result.rows_excluded_warehouse,
        "merged_pn_warehouse": len(result.inventory) - len(rows),
        "new_parts": new_parts,
    }
