"""幂等入库：维度 upsert（字段优先级）+ 事实去重 upsert + 库存求和（§6.4/§7.5）。

调用方（pipeline）负责事务边界与 batch。本模块所有写操作复用传入 session。
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.etl import mapping
from app.etl.transform import TransformResult
from app.models.dimensions import DimCustomer, DimPart, DimSupplier, PartAlias
from app.models.inventory import Inventory
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder

_CHUNK = 1000  # 每批行数，控制单语句参数数 < PostgreSQL 65535 上限


def _chunks(seq: list, n: int = _CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


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
    """upsert dim_part；返回 pn_std -> id。

    字段优先级（§7.5）：描述/品牌/单位 fill-if-empty；品类仅销售可写（COALESCE 新值优先）；
    needs_review 取 OR。占位品类已在 cleaner 置空，故采购/库存天然不写品类。
    """
    if not part_attrs:
        return {}
    for chunk in _chunks(list(part_attrs.values())):
        stmt = pg_insert(DimPart).values(chunk)
        # fill-if-empty：保留已有值，空则用新值
        set_ = {
            "description": func.coalesce(DimPart.description, stmt.excluded.description),
            "brand": func.coalesce(DimPart.brand, stmt.excluded.brand),
            "machine_or_part": func.coalesce(DimPart.machine_or_part, stmt.excluded.machine_or_part),
            "unit": func.coalesce(DimPart.unit, stmt.excluded.unit),
            "pn_raw_sample": func.coalesce(DimPart.pn_raw_sample, stmt.excluded.pn_raw_sample),
            "needs_review": or_(DimPart.needs_review, stmt.excluded.needs_review),
        }
        if is_sales:
            # 销售可改写品类：新值非空优先
            set_["category_major"] = func.coalesce(stmt.excluded.category_major, DimPart.category_major)
            set_["category_minor"] = func.coalesce(stmt.excluded.category_minor, DimPart.category_minor)
        session.execute(stmt.on_conflict_do_update(index_elements=[DimPart.pn_std], set_=set_))
    out: dict[str, int] = {}
    for chunk in _chunks(list(part_attrs.keys())):
        for pn, pid in session.execute(
            select(DimPart.pn_std, DimPart.id).where(DimPart.pn_std.in_(chunk))
        ).all():
            out[pn] = pid
    return out


def _upsert_aliases(session: Session, rows: list[dict]) -> None:
    seen = {}
    for r in rows:
        if r.get("pn_raw"):
            seen[r["pn_raw"]] = {"pn_raw": r["pn_raw"], "pn_std": r["pn_std"],
                                 "source": "auto", "needs_review": bool(r.get("needs_review"))}
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


def _idempotent_insert(session: Session, model, rows: list[dict], conflict_col) -> int:
    """ON CONFLICT DO NOTHING，返回实际插入行数。"""
    if not rows:
        return 0
    inserted = 0
    for chunk in _chunks(rows):
        stmt = pg_insert(model).values(chunk).on_conflict_do_nothing(index_elements=[conflict_col])
        inserted += len(session.execute(stmt.returning(conflict_col)).all())
    return inserted


def load(session: Session, result: TransformResult, batch_id: int, snapshot_date: date) -> dict:
    if result.file_type == mapping.INVENTORY:
        return _load_inventory(session, result, batch_id, snapshot_date)
    return _load_orders(session, result, batch_id)


def _load_orders(session: Session, result: TransformResult, batch_id: int) -> dict:
    is_sales = result.file_type == mapping.SALES
    # 1) 维度 part + alias
    part_attrs = _merge_part_attrs(result.lines, is_sales)
    part_id = _upsert_parts(session, part_attrs, is_sales)
    _upsert_aliases(session, result.lines)

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
        } for o in orders.values() if o.get("supplier_name_raw")]
        sup_id = _upsert_named_dim(session, DimSupplier, sup_rows,
                                   ["name_normalized", "supplier_code", "supplier_type"])

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
            })
        order_rows.append(base)
    orders_inserted = _idempotent_insert(session, order_model, order_rows, order_model.raw_order_id)
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
            "line_no": ln["line_no"], "part_id": part_id.get(ln["pn_std"]),
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
    lines_inserted = _idempotent_insert(session, line_model, line_rows, line_model.raw_line_id)

    return {
        "source_rows_total": result.rows_total,
        "fact_rows_inserted": lines_inserted,
        "fact_rows_skipped": len(line_rows) - lines_inserted,
        "fact_rows_error": len(result.errors),
        "rows_inactive": result.rows_inactive,
        "orders_inserted": orders_inserted,
        "new_parts": len(part_attrs),
    }


def _load_inventory(session: Session, result: TransformResult, batch_id: int, snapshot_date: date) -> dict:
    # 1) part 维度
    part_attrs = _merge_part_attrs(result.inventory, is_sales=False)
    part_id = _upsert_parts(session, part_attrs, is_sales=False)
    _upsert_aliases(session, result.inventory)

    # 2) 同 (pn_std, warehouse) 求和（实测 15 组重复，§摸底）
    agg: dict[tuple, dict] = {}
    for r in result.inventory:
        key = (r["pn_std"], r["warehouse"])
        if key not in agg:
            agg[key] = {**r, "source_qty": Decimal("0")}
        agg[key]["source_qty"] += r["source_qty"]

    rows = [{
        "raw_inventory_id": r["raw_inventory_id"], "part_id": part_id.get(r["pn_std"]),
        "pn_std": r["pn_std"], "warehouse": r["warehouse"], "source_qty": r["source_qty"],
        "description": r["description"], "brand": r["brand"],
        "machine_or_part": r["machine_or_part"], "unit": r["unit"],
        "snapshot_date": snapshot_date, "import_batch_id": batch_id,
    } for r in agg.values()]

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

    return {
        "source_rows_total": result.rows_total,
        "fact_rows_inserted": len(rows),
        "fact_rows_skipped": 0,
        "fact_rows_error": len(result.errors),
        "rows_inactive": result.rows_inactive,
        "merged_pn_warehouse": len(result.inventory) - len(rows),
        "new_parts": len(part_attrs),
    }
