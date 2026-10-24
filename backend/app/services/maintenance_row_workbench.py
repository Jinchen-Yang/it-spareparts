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


def _canonical_access(access, project_id=None):
    from app.authz.service import ResourceAccess
    from app.authz.context_registry import ENTRY_CONTRACTS
    if type(access) is not ResourceAccess or access.object_ref is None:
        raise ValueError("行级查询需要实际领域许可")
    if access.resource not in {"maintenance_row_workbench", "maintenance_global_rows", "maintenance_managed_rows"}:
        raise ValueError("不能借用其它模块的字段或范围许可")
    contract = ENTRY_CONTRACTS.get(access.entry_id, {}).get("branches", {}).get(access.context_branch, {})
    if access.action not in contract.get("requirements", {}).get(access.resource, ()):
        raise ValueError("行级许可与实际入口不匹配")
    if access.object_ref.kind == "project":
        if not project_id or access.object_ref.id != project_id:
            raise ValueError("行级许可不属于当前项目")
    elif access.object_ref.kind != "collection" or access.predicate is None:
        raise ValueError("行级集合必须使用实际 SQL 授权计划")
    return access


def can_view_amounts(db, ctx, project_id=None, *, authorization_access=None):
    if authorization_access is not None:
        return "data_purchase_cost" in _canonical_access(authorization_access, project_id).field_permissions
    if not config.ENABLE_RBAC or ctx.role == "admin":
        return True
    perms = ctx.permissions if ctx.permissions is not None else permissions.effective(ctx.role, None)
    return bool(perms.get("data_profit", False) or (
        project_id and assignments.is_project_workbook_editor(
            db, project_id=project_id, user_ctx=ctx,
        )
    ))


def visible_fields(db, ctx, project_id=None, *, authorization_access=None):
    if project_id and authorization_access is None:
        enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    allowed = can_view_amounts(db, ctx, project_id, authorization_access=authorization_access)
    return [field for field in FIELDS.values() if not field.amount or allowed]


def field_metadata(db, ctx, project_id=None, *, projection="workbook", direct_project=False, authorization_access=None):
    fields = visible_fields(db, ctx, project_id, authorization_access=authorization_access)
    stmt = query(db, ctx, project_id=project_id, projection=projection,
                 direct_project=direct_project, authorization_access=authorization_access).order_by(None)
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
          order_no=None, contract_no=None, projection="workbook", direct_project=False, authorization_access=None):
    if direct_project and not project_id:
        raise ValueError("direct project query requires project_id")
    if project_id and authorization_access is None:
        enforce_maintenance_project_access(db, project_id=project_id, ctx=ctx)
    # 单项目直达与全局目录有既有权限差异（canonical 销售），不可相互替换。
    if authorization_access is not None:
        access = _canonical_access(authorization_access, project_id)
        if access.object_ref.kind == "collection":
            from app.services.maintenance_authorization import project_condition
            from app.models.maintenance_project import MaintenanceProject
            allowed_projects = select(MaintenanceProject.project_id).where(project_condition(access))
        else:
            allowed_projects = None  # Exact project already passed its actual-object gate.
    else:
        allowed_projects = None if direct_project else resolve_visible_project_ids(db, ctx)
    stmt = master.assigned_lines_query(
        project_id=project_id, window=window, allowed_project_ids=allowed_projects,
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
        stmt, filters, can_filter_amounts=can_view_amounts(db, ctx, project_id, authorization_access=authorization_access),
        projection=projection,
    )


def project_page(db, ctx, *, project_id, filters, order_no, contract_no, page, page_size, authorization_access=None):
    stmt = query(db, ctx, project_id=project_id, filters=filters,
                 order_no=order_no, contract_no=contract_no, projection=project_projection(),
                 direct_project=True, authorization_access=authorization_access)
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


def _export_scalar(value):
    # 导出保留 Decimal：XLSX 写数值单元格，CSV 按数值原样输出、不进公式转义。
    if isinstance(value, date):
        return value.isoformat()
    return value


def read_rows(db, stmt, fields, *, projection="workbook", raw=False):
    selected = stmt.with_only_columns(
        FMaintenanceLine.id.label("line_id"),
        *(field_expression(field, projection=projection).label(field.field) for field in fields),
        maintain_column_froms=True,
    ).execution_options(yield_per=500)
    result = db.execute(selected)
    try:
        for row in result.mappings():
            convert = _export_scalar if raw else _scalar
            yield {key: convert(value) for key, value in row.items()}
    finally:
        result.close()


def check_export_size(db, stmt):
    # Excel 的工作表上限包含表头；统一预检，永不静默截断。
    if count(db, stmt) > MAX_EXPORT_ROWS:
        raise HTTPException(413, f"筛选集超过 {MAX_EXPORT_ROWS} 行，请缩小范围后导出")


def export_rows(db, stmt, fields, *, projection="workbook"):
    rows = read_rows(db, stmt, fields, projection=projection, raw=True)
    for index, row in enumerate(rows, 1):
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
            # 数值写数值单元格；文本一律显式写字符串（=1+1 仍为文本，不加撇号）。
            sheet.freeze_panes(1, 0)
            for row_index, values in enumerate(rows, 1):
                for column, value in enumerate(values):
                    value = excel_safe(value)
                    if value is None:
                        continue
                    if isinstance(value, Decimal):
                        if not value.is_finite():
                            raise HTTPException(422, "导出数值无效")
                        sheet.write_number(row_index, column, float(value))
                        continue
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        sheet.write_number(row_index, column, value)
                        continue
                    value = str(value)
                    text_bytes += len(value.encode("utf-8"))
                    if text_bytes > MAX_DYNAMIC_TEXT_BYTES:
                        raise HTTPException(413, "XLSX 动态文本超过 64 MiB 上限，请缩小筛选范围")
                    if len(value) > 32767:
                        raise HTTPException(413, "单元格超过 Excel 字符上限")
                    sheet.write_string(row_index, column, value)
        if output.tell() > MAX_WORKBOOK_BYTES:
            raise HTTPException(413, "XLSX 文件超过 256 MiB 上限，请缩小筛选范围")
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise
    finally:
        rows.close()
