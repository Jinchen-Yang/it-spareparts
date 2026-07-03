"""测试数据工厂：构造 TransformResult 与 ORM 行。"""
from datetime import date
from decimal import Decimal

from app.etl import mapping
from app.etl.transform import TransformResult


def purchase_line(order_raw_id: str, line_raw_id: str, pn: str, qty="1", price="100",
                  needs_review=False, pn_raw: str | None = None, **kw) -> dict:
    return {
        "_order_raw_id": order_raw_id, "raw_line_id": line_raw_id, "line_no": 1,
        "pn_std": pn, "pn_raw": pn_raw or pn, "needs_review": needs_review,
        "description": kw.get("description"), "brand": kw.get("brand"),
        "machine_or_part": None, "unit": "个",
        "qty": Decimal(qty), "unit_price": Decimal(price),
        "line_amount": Decimal(qty) * Decimal(price),
        "recent_purchase_price": None, "anomaly_flags": [],
    }


def purchase_head(order_raw_id: str, order_no: str | None = None,
                  on: date | None = None, source_type="销售订单",
                  supplier="测试供应商", purchaser=None, source_channel="正规供应商",
                  is_tax_inclusive=None, tax_rate=None, amount_ex_tax=None,
                  tax_amount=None, amount_inc_tax=None, data_status="已生效",
                  linked_maintenance_order_no=None) -> dict:
    return {
        "raw_order_id": order_raw_id, "order_no": order_no or order_raw_id,
        "order_date": on or date(2026, 1, 1), "tax_rate": tax_rate,
        "amount_ex_tax": amount_ex_tax,
        "data_status": data_status, "purchaser": purchaser,
        "supplier_name_raw": supplier, "supplier_name_normalized": supplier,
        "supplier_code": None, "supplier_type": None,
        "supplier_source_channel": source_channel,
        "source_type": source_type, "source_type_raw": source_type,
        "linked_sales_order_no": None,
        "linked_maintenance_order_no": linked_maintenance_order_no,
        "is_tax_inclusive": is_tax_inclusive, "tax_amount": tax_amount,
        "amount_inc_tax": amount_inc_tax,
    }


def sales_line(order_raw_id: str, line_raw_id: str, pn: str, qty="1", price="200",
               needs_review=False, pn_raw: str | None = None, **kw) -> dict:
    return {
        "_order_raw_id": order_raw_id, "raw_line_id": line_raw_id, "line_no": 1,
        "pn_std": pn, "pn_raw": pn_raw or pn, "needs_review": needs_review,
        "description": kw.get("description"), "brand": kw.get("brand"),
        "machine_or_part": None, "unit": "个",
        "qty": Decimal(qty), "unit_price": Decimal(price),
        "line_amount": Decimal(qty) * Decimal(price),
        "recent_purchase_price": None, "anomaly_flags": [],
        "category_major": kw.get("category_major"), "category_minor": kw.get("category_minor"),
        "generic_product": None, "serial_numbers": None,
    }


def sales_head(order_raw_id: str, order_no: str | None = None,
               on: date | None = None, business_type="备件销售", data_status="已生效") -> dict:
    return {
        "raw_order_id": order_raw_id, "order_no": order_no or order_raw_id,
        "order_date": on or date(2026, 2, 1), "tax_rate": None, "amount_ex_tax": None,
        "data_status": data_status, "salesperson": "测试销售",
        "customer_name": "测试客户", "customer_type": None, "customer_source": None,
        "customer_city": None, "business_type": business_type, "warehouse": "总仓",
    }


def inventory_row(raw_id: str, pn: str, warehouse="总仓", qty="5",
                  needs_review=False, pn_raw: str | None = None, **kw) -> dict:
    return {
        "raw_inventory_id": raw_id, "pn_std": pn, "pn_raw": pn_raw or pn,
        "needs_review": needs_review, "warehouse": warehouse,
        "source_qty": Decimal(qty), "description": kw.get("description"),
        "brand": kw.get("brand"), "machine_or_part": None, "unit": "个",
        "generic_product": None, "data_status": "已生效",
    }


def maintenance_line(order_raw_id: str, line_raw_id: str, pn: str, qty="1",
                     return_qty=None, needs_review=False, pn_raw: str | None = None,
                     **kw) -> dict:
    return {
        "_order_raw_id": order_raw_id, "raw_line_id": line_raw_id, "line_no": 1,
        "pn_std": pn, "pn_raw": pn_raw or pn, "needs_review": needs_review,
        "description": kw.get("description"),
        "qty": Decimal(qty),
        "return_qty": Decimal(return_qty) if return_qty is not None else None,
        "serial_numbers": None, "anomaly_flags": [],
    }


def maintenance_head(order_raw_id: str, order_no: str | None = None,
                     on: date | None = None, project="测试维保项目",
                     sales_order=None, demand_type="报修供货",
                     business_type="备件维保", data_status="已生效") -> dict:
    return {
        "raw_order_id": order_raw_id, "order_no": order_no or order_raw_id,
        "order_date": on or date(2026, 3, 1),
        "linked_sales_order_no": sales_order,
        "project_raw": project, "project_std": project,
        "customer_name": "测试客户", "end_customer": None,
        "demand_type": demand_type, "business_type": business_type,
        "salesperson": "测试销售", "warehouse": "北京成品仓",
        "maint_start": None, "maint_end": None, "data_status": data_status,
    }


def purchase_result(orders: dict, lines: list) -> TransformResult:
    return TransformResult(file_type=mapping.PURCHASE, orders=orders, lines=lines,
                           rows_total=len(lines))


def maintenance_result(orders: dict, lines: list) -> TransformResult:
    return TransformResult(file_type=mapping.MAINTENANCE, orders=orders, lines=lines,
                           rows_total=len(lines))


def sales_result(orders: dict, lines: list) -> TransformResult:
    return TransformResult(file_type=mapping.SALES, orders=orders, lines=lines,
                           rows_total=len(lines))


def inventory_result(rows: list) -> TransformResult:
    return TransformResult(file_type=mapping.INVENTORY, inventory=rows, rows_total=len(rows))
