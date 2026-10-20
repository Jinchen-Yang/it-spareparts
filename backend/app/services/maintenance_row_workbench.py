"""只读核对查询和导出；不产生任何可回传的身份/签名基线。"""

from datetime import date
from decimal import Decimal
from tempfile import SpooledTemporaryFile

from fastapi import HTTPException
from sqlalchemy import func, select
from xlsxwriter import Workbook

from app import config, permissions
from app.config import get_settings
from app.api.maintenance_project_scope import (
    enforce_maintenance_project_access, resolve_visible_project_ids,
)
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.services import maintenance_project_assignments as assignments
from app.services import maintenance_project_master_workbook as master
from app.services.maintenance_row_filter_registry import FIELDS, apply_row_filters, field_expression
from app.services.maintenance_expense_integrity import normalized_contract_no_sql, normalize_contract_no
from app.services.excel_text_safety import excel_safe


MAX_EXPORT_ROWS = 1_048_575
# 与既有逐单 XLSX 的资源门槛一致；触发时在响应文件前明确拒绝。
MAX_DYNAMIC_TEXT_BYTES = 64 * 1024 * 1024
MAX_WORKBOOK_BYTES = 256 * 1024 * 1024


def project_projection():
    return "workbook" if get_settings().maintenance_project_master_v2_enabled else "v1"


def can_view_amounts(db, ctx, project_id=None):
    if not config.ENABLE_RBAC or ctx.role == "admin":
        return True
    perms = ctx.permissions if ctx.permissions is not None else permissions.effective(ctx.role, None)
    return bool(perms.get("data_profit", False) or (
        project_id and assignments.is_project_workbook_editor(
            db, project_id=project_id, user_ctx=ctx,
        )
    ))


def visible_fields(db, ctx, project_id=None):
    if project_id:
        enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    allowed = can_view_amounts(db, ctx, project_id)
    return [field for field in FIELDS.values() if not field.amount or allowed]


def field_metadata(db, ctx, project_id=None, *, projection="workbook", direct_project=False):
    fields = visible_fields(db, ctx, project_id)
    stmt = query(db, ctx, project_id=project_id, projection=projection,
                 direct_project=direct_project).order_by(None)
    result = []
    for field in fields:
        item = field.metadata()
        if field.type == "enum":
            expr = field_expression(field, projection=projection)
            # 不从全库列举取值；新出现的源枚举也可选择，且不泄露外项目或成本信号。
            values = set(field.options)
            values.update(value for value in db.scalars(
                stmt.with_only_columns(expr, maintain_column_froms=True).distinct()
            ) if value not in (None, ""))
            item["options"] = [{"value": value, "label": value} for value in sorted(values)]
        result.append(item)
    return result


def query(db, ctx, *, project_id=None, window=None, filters=(), export_order=False,
          order_no=None, contract_no=None, projection="workbook", direct_project=False):
    if direct_project and not project_id:
        raise ValueError("direct project query requires project_id")
    if project_id:
        enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    # 单项目直达与全局目录有既有权限差异（canonical 销售），不可相互替换。
    stmt = master.assigned_lines_query(
        project_id=project_id, window=window,
        allowed_project_ids=None if direct_project else resolve_visible_project_ids(db, ctx),
        export_order=export_order,
    )
    if order_no is not None:
        stmt = stmt.where(FMaintenanceOrder.order_no == order_no)
    if contract_no is not None:
        stmt = stmt.where(
            normalized_contract_no_sql(FMaintenanceOrder.linked_sales_order_no)
            == normalize_contract_no(contract_no),
        )
    return apply_row_filters(
        stmt, filters, can_filter_amounts=can_view_amounts(db, ctx, project_id),
        projection=projection,
    )


def project_page(db, ctx, *, project_id, filters, order_no, contract_no, page, page_size):
    stmt = query(db, ctx, project_id=project_id, filters=filters,
                 order_no=order_no, contract_no=contract_no, projection=project_projection(),
                 direct_project=True)
    total = count(db, stmt)
    size = (page_size or 20) if page is not None else None
    if page is not None:
        stmt = stmt.offset((page - 1) * size).limit(size)
    return db.execute(stmt).all(), total, size


def redact_project_rows(rows, *, allowed):
    if allowed:
        return rows
    # 兼容旧 DTO；金额与所有派生成本信号一并置空，不留下可推断标志。
    names = {name for name, field in FIELDS.items() if field.amount} | {
        "unit_cost_ex_tax", "unit_cost_inc_tax", "cost_amount_inc_tax",
        "cost_source_label", "cost_quality", "change_reason",
    }
    return [{key: None if key in names else value for key, value in row.items()} for row in rows]


def count(db, stmt):
    return db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0


def _scalar(value):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def read_rows(db, stmt, fields, *, projection="workbook"):
    selected = stmt.with_only_columns(
        FMaintenanceLine.id.label("line_id"),
        *(field_expression(field, projection=projection).label(field.field) for field in fields),
        maintain_column_froms=True,
    ).execution_options(yield_per=500)
    result = db.execute(selected)
    try:
        for row in result.mappings():
            yield {key: _scalar(value) for key, value in row.items()}
    finally:
        result.close()


def check_export_size(db, stmt):
    # Excel 的工作表上限包含表头；统一预检，永不静默截断。
    if count(db, stmt) > MAX_EXPORT_ROWS:
        raise HTTPException(413, f"筛选集超过 {MAX_EXPORT_ROWS} 行，请缩小范围后导出")


def export_rows(db, stmt, fields, *, projection="workbook"):
    for index, row in enumerate(read_rows(db, stmt, fields, projection=projection), 1):
        if index > MAX_EXPORT_ROWS:
            raise HTTPException(413, f"筛选集超过 {MAX_EXPORT_ROWS} 行，请缩小范围后导出")
        yield [row[field.field] for field in fields]


def xlsx_file(db, stmt, fields, *, projection="workbook"):
    """复用现有 xlsxwriter，常量行内存、有界 spool；失败不返回半份文件。"""
    output = SpooledTemporaryFile(max_size=4 * 1024 * 1024, mode="w+b")
    rows = export_rows(db, stmt, fields, projection=projection)
    text_bytes = 0
    try:
        with Workbook(output, {
            "constant_memory": True, "strings_to_formulas": False, "strings_to_urls": False,
        }) as workbook:
            sheet = workbook.add_worksheet("筛选结果（不可回传）")
            sheet.write_row(0, 0, [field.label for field in fields])
            sheet.freeze_panes(1, 0)
            for row_index, values in enumerate(rows, 1):
                safe_values = []
                for value in values:
                    value = excel_safe(value)
                    if isinstance(value, str):
                        text_bytes += len(value.encode("utf-8"))
                    if text_bytes > MAX_DYNAMIC_TEXT_BYTES:
                        raise HTTPException(413, "XLSX 动态文本超过 64 MiB 上限，请缩小筛选范围")
                    if isinstance(value, str) and len(value) > 32767:
                        raise HTTPException(413, "单元格超过 Excel 字符上限")
                    safe_values.append(value)
                sheet.write_row(row_index, 0, safe_values)
        if output.tell() > MAX_WORKBOOK_BYTES:
            raise HTTPException(413, "XLSX 文件超过 256 MiB 上限，请缩小筛选范围")
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise
    finally:
        rows.close()
