"""维保项目成本 API（docs/维保出库成本核算-开发方案.md §5）。

鉴权双层：current_role 硬鉴权（缺/失效凭证 → 401，与 purchases/parts 一致，不依赖 RBAC 开关）
+ require_page('page_maintenance') 页面准入（admin/boss/purchaser 模板默认开）。
成本金额字段随 data_purchase_cost 脱敏（成本 7 键 + 聚合派生键已登记 FIELD_GROUPS）。
"""

import asyncio
import csv
import io
import logging
import os
import re
import tempfile
import threading
from contextlib import suppress
from datetime import date
from tempfile import SpooledTemporaryFile
from urllib.parse import quote

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from python_multipart.exceptions import FormParserError, MultipartParseError
from python_multipart.multipart import parse_options_header
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.api.maintenance_project_operations import _real_operator
from app.auth import current_identity, current_role, require_admin
from app.business_time import business_today
from app.db import SessionLocal, get_db
from app.security import (
    UserContext, apply_field_visibility, get_current_user_context, record_access_log,
    is_field_hidden, is_scoped_sales, require_action, require_page,
)
from app import config
from app.services import (
    maintenance_cost,
    maintenance_export,
    maintenance_roundtrip,
    maintenance_workbook_export,
    maintenance_workbook_renderer,
)

router = APIRouter(prefix="/maintenance", tags=["maintenance"])
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_CSV_SPOOL_MEMORY_BYTES = 5 * 1024 * 1024
_MAX_CSV_DATA_ROWS = 1_000_000
_MAX_CSV_CELL_CHARS = 32_767
_MAX_CSV_DYNAMIC_TEXT_BYTES = 64 * 1024 * 1024
_MAX_CSV_OUTPUT_BYTES = 512 * 1024 * 1024
_INVALID_CSV_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffe\uffff]")
_ROUNDTRIP_MULTIPART_OVERHEAD_BYTES = 64 * 1024
_ROUNDTRIP_PARSE_CHUNK_BYTES = 64 * 1024
_ROUNDTRIP_IMPORT_PARSE_LOCK = threading.Lock()
logger = logging.getLogger(__name__)


def _remove_roundtrip_temp(path: str | None) -> None:
    """Best-effort temp cleanup; cleanup failures must never replace the request result."""
    if path is None:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    except OSError:
        with suppress(Exception):
            logger.warning("无法删除维保回填临时文件 %s", path, exc_info=True)


async def _wait_for_roundtrip_task_terminal(
    task: asyncio.Task,
) -> asyncio.CancelledError | None:
    """Wait through repeated caller cancellation without ever cancelling ``task``."""
    cancellation = None
    current_task = asyncio.current_task()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if current_task is not None and current_task.cancelling():
                if cancellation is None:
                    cancellation = exc
                continue
            break
        except BaseException:
            break
    return cancellation


async def _close_roundtrip_form(form) -> asyncio.CancelledError | None:
    close_worker = asyncio.create_task(form.close())
    cancellation = await _wait_for_roundtrip_task_terminal(close_worker)
    try:
        close_worker.result()
    except BaseException as exc:
        if cancellation is not None:
            raise cancellation from exc
        raise
    return cancellation


def _validate_date_pair(date_from: date | None, date_to: date | None) -> None:
    if (date_from is None) != (date_to is None):
        raise HTTPException(
            status_code=422,
            detail="date_from 与 date_to 必须同时提供",
        )
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from 不能晚于 date_to",
        )


class _ClosingStreamingResponse(StreamingResponse):
    def __init__(self, content, resource, **kwargs):
        super().__init__(content, **kwargs)
        self._resource = resource

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with suppress(Exception):
                self._resource.close()


def _iter_download_chunks(resource):
    """按固定块读取二进制下载，避免文件迭代器按换行产生碎片或超大块。"""
    while chunk := resource.read(_DOWNLOAD_CHUNK_BYTES):
        yield chunk


def _release_db_before_stream(db: Session, resource) -> None:
    """流式响应结束前依赖不会 teardown；先归还连接，失败时同时关闭文件。"""
    try:
        db.rollback()
    except BaseException:
        resource.close()
        raise


@router.post("/recompute")
def recompute(db: Session = Depends(get_db),
              ident: dict = Depends(current_identity),
              _auth: str = Depends(require_admin),   # 全表重算(~1min 写库)：限管理员，与导入触发方口径一致
              ctx: UserContext = Depends(get_current_user_context)) -> dict:
    operator = _real_operator(db, ident)
    record_access_log(ctx, "recompute", "maintenance", {"operated_by": operator})
    try:
        return maintenance_cost.recompute(db)
    except maintenance_cost.MaintenanceCostRecomputeBusy as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=str(exc),
            headers={"Retry-After": "5"},
        ) from exc
    except maintenance_cost.WorkbookInvalidationConflictError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="维保数据归属在重算期间发生变化，本次重算已整体回滚，请重试",
            headers={"Retry-After": "5"},
        ) from exc


@router.get("/projects")
def projects(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    q: str | None = Query(None, max_length=128),
    lifecycle: str = Query("ongoing", pattern=r"^(ongoing|ended|missing|all)$"),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),   # 硬鉴权：缺/失效凭证 → 401，不依赖全局 RBAC 开关
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    _validate_date_pair(date_from, date_to)
    record_access_log(ctx, "projects", "maintenance")
    data = maintenance_cost.projects_aggregate(
        db, date_from, date_to, q, user_ctx=ctx,
        lifecycle=lifecycle, as_of=business_today(),
    )
    return apply_field_visibility(data, ctx)


@router.get("/lines")
def lines(
    project: str = Query(..., max_length=256),
    month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),   # 硬鉴权：缺/失效凭证 → 401
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    record_access_log(ctx, "lines", "maintenance", {"project": project})
    data = maintenance_cost.project_lines(
        db, project, month, date_from, date_to, page, page_size, user_ctx=ctx,
    )
    return apply_field_visibility(data, ctx)


_SOURCE_LABEL = maintenance_workbook_renderer.SOURCE_LABELS
_CONF_LABEL = maintenance_workbook_renderer.CONFIDENCE_LABELS


def _safe(v):
    """统一净化 CSV 动态文本：非法控制、公式注入、Excel 单元格长度。"""
    if not isinstance(v, str):
        return v
    value = _INVALID_CSV_CONTROLS.sub("", v)
    probe = value.lstrip()
    if probe[:1] in ("=", "+", "-", "@"):
        value = "'" + value
    if len(value) > _MAX_CSV_CELL_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"CSV 单元格超过 {_MAX_CSV_CELL_CHARS} 字符安全上限",
        )
    return value


_INVALID_DOWNLOAD_NAME = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')
_INVALID_ASCII_FALLBACK = re.compile(r"[^A-Za-z0-9._-]+")
_RFC5987_SAFE = "!#$&+-.^_`|~"


def _content_disposition(filename: str, ascii_fallback: str | None = None) -> str:
    """生成纯 ASCII 下载响应头，同时保留 RFC 5987 UTF-8 文件名。"""
    clean_name = _INVALID_DOWNLOAD_NAME.sub("_", filename).strip(" .") or "download"
    fallback_source = ascii_fallback or clean_name
    fallback = _INVALID_DOWNLOAD_NAME.sub("_", fallback_source)
    fallback = fallback.encode("ascii", "ignore").decode()
    fallback = _INVALID_ASCII_FALLBACK.sub("_", fallback).strip(" ._") or "download"
    encoded_name = quote(clean_name, safe=_RFC5987_SAFE)
    return (
        f'attachment; filename="{fallback}"; '
        f"filename*=UTF-8''{encoded_name}"
    )


def _require_workbook_export_permissions(ctx: UserContext) -> None:
    if is_scoped_sales(ctx):
        raise HTTPException(
            status_code=403,
            detail="受限销售账号不能导出项目成本工作簿",
        )
    visible = apply_field_visibility(
        {"unit_cost": 1, "gross_profit": 1},
        ctx,
    )
    if visible["unit_cost"] is None or visible["gross_profit"] is None:
        raise HTTPException(
            status_code=403,
            detail="无成本及利润查看权限，不能导出项目成本工作簿",
        )


def _require_full_contract_scope(ctx: UserContext) -> None:
    if is_scoped_sales(ctx):
        raise HTTPException(
            status_code=403,
            detail="受限销售账号不能查看合同级维保数据",
        )


def _require_roundtrip_customer_permission(ctx: UserContext) -> None:
    """可编辑客户字段的固定协议必须失败关闭，不能靠导出置空规避。"""
    if ctx.role == "admin":
        return
    if not ctx.permissions or ctx.permissions.get("data_customer") is not True:
        raise HTTPException(
            status_code=403,
            detail="无客户信息查看权限，不能导出或导入固定回填工作簿",
        )


def _require_roundtrip_permissions(ctx: UserContext) -> None:
    _require_workbook_export_permissions(ctx)
    _require_roundtrip_customer_permission(ctx)


def _csv_stream(
    header: list,
    rows,
    filename: str,
    ascii_fallback: str | None = None,
    db: Session | None = None,
) -> StreamingResponse:
    """增量写入有界内存 spool；完整校验成功后才开始响应。"""
    output = SpooledTemporaryFile(max_size=_CSV_SPOOL_MEMORY_BYTES, mode="w+b")
    row_buffer = io.StringIO(newline="")
    writer = csv.writer(row_buffer)
    dynamic_text_bytes = 0

    def write_row(values) -> None:
        nonlocal dynamic_text_bytes
        safe_values = []
        for value in values:
            safe_value = _safe(value)
            safe_values.append(safe_value)
            if isinstance(safe_value, str):
                dynamic_text_bytes += len(safe_value.encode("utf-8"))
                if dynamic_text_bytes > _MAX_CSV_DYNAMIC_TEXT_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="CSV 动态文本超过 64 MiB 安全上限",
                    )
        writer.writerow(safe_values)
        row_bytes = row_buffer.getvalue().encode("utf-8")
        if output.tell() + len(row_bytes) > _MAX_CSV_OUTPUT_BYTES:
            raise HTTPException(
                status_code=413,
                detail="CSV 文件超过 512 MiB 安全上限",
            )
        output.write(row_bytes)
        row_buffer.seek(0)
        row_buffer.truncate(0)

    row_iterator = None
    try:
        output.write(b"\xef\xbb\xbf")  # BOM，Excel 正确识别 UTF-8
        write_row(header)
        row_iterator = iter(rows)
        for row_count, row in enumerate(row_iterator, 1):
            if row_count > _MAX_CSV_DATA_ROWS:
                raise HTTPException(
                    status_code=413,
                    detail=f"CSV 数据行超过 {_MAX_CSV_DATA_ROWS} 行上限",
                )
            write_row(row)
        output.seek(0)
        if db is not None:
            _release_db_before_stream(db, output)
    except BaseException:
        output.close()
        raise
    finally:
        close_rows = getattr(row_iterator, "close", None)
        if close_rows is not None:
            with suppress(Exception):
                close_rows()
        row_buffer.close()
    return _ClosingStreamingResponse(
        _iter_download_chunks(output),
        resource=output,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": _content_disposition(filename, ascii_fallback),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/export")
def export(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    q: str | None = Query(None, max_length=128),
    lifecycle: str = Query("ongoing", pattern=r"^(ongoing|ended|missing|all)$"),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),   # 硬鉴权：缺/失效凭证 → 401
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    _validate_date_pair(date_from, date_to)
    record_access_log(ctx, "export", "maintenance")
    data = maintenance_cost.projects_aggregate(
        db, date_from, date_to, q, user_ctx=ctx,
        lifecycle=lifecycle, as_of=business_today(),
    )
    contract_amount_restricted = is_field_hidden(ctx, "contract_amount")
    data = apply_field_visibility(data, ctx)   # 导出同样过脱敏层（§8.5）
    if not data["rows"]:
        raise HTTPException(
            status_code=422,
            detail="所选范围内没有可导出的项目数据",
        )
    header = ["项目", "期限状态", "维保终止日期",
              "维保订单数", "无明细订单数", "订单结构完整性",
              "出库行数", "出库数量",
              "实际采购参考-含税", "实际采购参考-不含税",
              "估算参考-含税", "估算参考-不含税",
              "实际参考行数", "估算参考行数", "缺失成本行数",
              "已知成本参考(混合原值)", "成本完整性",
              "已知成本参考-含税小计(兼容)", "已知成本参考-不含税小计(兼容)",
              "已知成本参考合计(兼容)", "覆盖率%",
              "备件成本-含税归一", "含税口径完整", "含税口径质量", "含税口径缺失行",
              "备件成本-未税归一", "未税口径完整", "未税口径质量", "未税口径缺失行",
              *(_SOURCE_LABEL[s] + "(行)" for s in (
                  "direct", "window", "month_avg", "trace_avg", "sales_ref",
                  "pool_purchase", "pool_sales", "purchase_history",
                  "sales_history", "manual", "none",
              )),
              "月份数", "关联销售订单", "合同额(含税参考)",
              "合同额证据状态", "合同被多项目共用"]
    rows = []
    for r in data["rows"]:
        bs = r["by_source"]
        source_counts = (
            [None] * 11
            if bs is None
            else [
                bs.get("direct", 0),
                bs.get("window", 0),
                bs.get("month_avg", 0),
                bs.get("trace_avg", 0),
                bs.get("sales_ref", 0),
                bs.get("pool_purchase", 0),
                bs.get("pool_sales", 0),
                bs.get("purchase_history", 0),
                bs.get("sales_history", 0),
                bs.get("manual", 0),
                bs.get("none", 0),
            ]
        )
        lifecycle_label = {"ongoing": "进行中", "ended": "已结束", "missing": "期限缺失"}
        quality_label = {
            "actual_only": "仅实际采购参考",
            "contains_estimate": "含估算参考",
            "incomplete": "成本不完整，需补数据",
        }
        rows.append([_safe(r["project"]), lifecycle_label[r["lifecycle_status"]], r["maint_end"],
                     r["order_count"], r["missing_detail_orders"],
                     "完整" if r["structure_complete"] else "不完整",
                     r["lines"], r["qty"],
                     r["actual_cost_inc"], r["actual_cost_ex"],
                     r["estimated_cost_inc"], r["estimated_cost_ex"],
                     r["actual_lines"], r["estimated_lines"], r["missing_cost_lines"],
                     r["known_cost_total"], quality_label.get(r["cost_quality"], r["cost_quality"]),
                     r["cost_inc"], r["cost_ex"], r["cost_total"], r["coverage_pct"],
                     r.get("parts_cost_inc_tax"), r.get("parts_cost_inc_tax_complete"),
                     quality_label.get(
                         r.get("parts_cost_inc_tax_quality"),
                         r.get("parts_cost_inc_tax_quality"),
                     ),
                     r.get("parts_cost_inc_tax_missing_lines"),
                     r.get("parts_cost_ex_tax"), r.get("parts_cost_ex_tax_complete"),
                     quality_label.get(
                         r.get("parts_cost_ex_tax_quality"),
                         r.get("parts_cost_ex_tax_quality"),
                     ),
                     r.get("parts_cost_ex_tax_missing_lines"),
                     *source_counts,
                     r["months"], _safe("、".join(r["sales_orders"])),
                     r["contract_amount_inc_tax"],
                     (
                         "未关联合同"
                         if not r["sales_orders"]
                         else "不完整"
                         if r["contract_incomplete"] is True
                         else "受限"
                         if (
                             r["contract_incomplete"] is None
                             or contract_amount_restricted
                         )
                         else "完整"
                     ),
                     "是" if r["contract_shared"] else ""])
    return _csv_stream(header, rows, "maintenance_projects.csv", db=db)


@router.get("/orders/export")
def orders_export(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    if is_scoped_sales(ctx):
        raise HTTPException(status_code=403, detail="受限销售账号不能导出逐单维保数据")
    if (date_from is None) != (date_to is None):
        raise HTTPException(status_code=422, detail="date_from 与 date_to 必须同时提供")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from 不能晚于 date_to")
    audit_scope = (
        {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}
        if date_from is not None and date_to is not None
        else {"scope": "all"}
    )
    record_access_log(ctx, "orders_export", "maintenance", audit_scope)
    try:
        output = maintenance_export.build_workbook(db, ctx, date_from, date_to)
    except maintenance_export.ExcelExportEmpty as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except maintenance_export.ExcelExportBusy as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "5"},
        ) from exc
    except maintenance_export.ExcelExportTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except (maintenance_export.ExcelCellTooLong, maintenance_export.ExcelRowLimitExceeded) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _release_db_before_stream(db, output)
    scope = f"{date_from.isoformat()}_{date_to.isoformat()}" if date_from and date_to else "all"
    return _ClosingStreamingResponse(
        _iter_download_chunks(output),
        resource=output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": _content_disposition(
                f"maintenance_orders_{scope}.xlsx",
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/as-of")
def maintenance_as_of(
    response: Response,
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict[str, str]:
    record_access_log(ctx, "as_of", "maintenance")
    response.headers["Cache-Control"] = "no-store"
    return {"as_of": business_today().isoformat()}


@router.get("/export-workbooks")
def export_workbooks(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    """按命中维保订单的时间范围，一次导出全部合同项目工作簿。"""
    _require_workbook_export_permissions(ctx)
    audit_scope = (
        {"scope": "all"}
        if date_from is None and date_to is None
        else {
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
        }
    )
    record_access_log(ctx, "export_workbooks", "maintenance", audit_scope)
    try:
        output = maintenance_workbook_export.build_contract_workbooks_zip(
            db, date_from=date_from, date_to=date_to,
        )
    except maintenance_workbook_export.WorkbookExportBusy as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "5"},
        ) from exc
    except maintenance_workbook_export.WorkbookExportRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    _release_db_before_stream(db, output)
    scope = f"{date_from.isoformat()}_{date_to.isoformat()}" if date_from and date_to else "all"
    return _ClosingStreamingResponse(
        _iter_download_chunks(output),
        resource=output,
        media_type="application/zip",
        headers={
            "Content-Disposition": _content_disposition(
                f"maintenance_project_workbooks_{scope}.zip",
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/lines/export")
def lines_export(
    project: str = Query(..., max_length=256),
    month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),   # 硬鉴权：缺/失效凭证 → 401
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    """单项目 SKU 明细导出（财务逐行复核入账用）——全量、含成本来源/税口径。"""
    if (date_from is None) != (date_to is None):
        raise HTTPException(status_code=422, detail="date_from 与 date_to 必须同时提供")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from 不能晚于 date_to")
    record_access_log(ctx, "lines_export", "maintenance", {"project": project})
    if not maintenance_cost.project_exists(db, project, user_ctx=ctx):
        raise HTTPException(status_code=404, detail=f"项目不存在：{project}")
    line_count = maintenance_cost.project_line_count(
        db, project, month, date_from, date_to, user_ctx=ctx,
    )
    if line_count == 0:
        raise HTTPException(
            status_code=422,
            detail="项目存在，但所选范围内没有可导出的明细",
        )
    if line_count > _MAX_CSV_DATA_ROWS:
        raise HTTPException(
            status_code=413,
            detail=f"CSV 数据行超过 {_MAX_CSV_DATA_ROWS} 行上限",
        )
    header = ["日期", "维保单号", "需求类型", "业务类型", "出库仓库", "PN", "描述",
              "数量", "退货", "单价", "金额",
              "含税单位成本", "未税单位成本", "含税成本金额", "未税成本金额",
              "成本事实层级", "成本来源", "置信度", "含税口径", "取价月",
              "追溯月数", "距采购天数", "关联采购单",
              "参考侧", "参考池ID", "参考池版本", "参考样本数",
              "参考起始日", "参考截止日", "最近样本日", "异常标记"]

    def rows():
        source_rows = maintenance_cost.iter_project_lines(
            db,
            project,
            month,
            date_from,
            date_to,
            user_ctx=ctx,
        )
        try:
            for raw_row in source_rows:
                r = apply_field_visibility(raw_row, ctx)
                yield [
                    r["order_date"], r["order_no"], r["demand_type"], r["business_type"],
                    r["warehouse"], r["pn_std"], r["description"],
                    r["qty"], r["return_qty"], r["unit_cost"], r["cost_amount"],
                    r.get("unit_cost_inc_tax"), r.get("unit_cost_ex_tax"),
                    r.get("cost_amount_inc_tax"), r.get("cost_amount_ex_tax"),
                    {"actual": "实际采购参考", "estimated": "估算参考",
                     "missing": "成本缺失"}.get(r["cost_tier"], r["cost_tier"]),
                    _SOURCE_LABEL.get(r["cost_source"], r["cost_source"]),
                    _CONF_LABEL.get(r["confidence"], r["confidence"] or ""),
                    r["cost_tax_basis"], r["price_month"], r["trace_months"],
                    r["price_distance_days"], r["linked_purchase_order_no"],
                    r.get("reference_side"), r.get("reference_pool_group_id"),
                    r.get("reference_pool_version"), r.get("reference_sample_count"),
                    r.get("reference_from_date"), r.get("reference_to_date"),
                    r.get("reference_latest_date"),
                    "、".join(r["anomaly_flags"] or []),
                ]
        finally:
            close_source = getattr(source_rows, "close", None)
            if close_source is not None:
                with suppress(Exception):
                    close_source()
    return _csv_stream(
        header,
        rows(),
        f"maintenance_lines_{project[:40]}.csv",
        ascii_fallback="maintenance_lines.csv",
        db=db,
    )


@router.get("/board")
def board(
    status: str | None = Query(
        None,
        pattern=(
            r"^(incomplete_cost|expense_data_unavailable|"
            r"red|yellow|green|no_budget)$"
        ),
    ),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    q: str | None = Query(None, max_length=128),
    lifecycle: str = Query("ongoing", pattern=r"^(ongoing|ended|missing|all)$"),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),   # 硬鉴权：缺/失效凭证 → 401
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """合同预算消耗参考：成本或费用数据不完整时不计算红黄绿。"""
    _validate_date_pair(date_from, date_to)
    _require_full_contract_scope(ctx)
    record_access_log(ctx, "board", "maintenance")
    data = maintenance_cost.board(
        db, date_from, date_to, status, user_ctx=ctx, q_text=q,
        lifecycle=lifecycle, as_of=business_today(),
    )
    return apply_field_visibility(data, ctx)


def _revenue_evidence_status(row: dict, basis: str) -> str:
    if row.get(f"revenue_{basis}") is not None:
        return "available"
    status = row.get(f"parts_profit_status_{basis}")
    if status in {
        "missing_revenue",
        "missing_tax_rate",
        "invalid_tax_rate",
        "ambiguous_revenue",
    }:
        return status
    return "restricted" if status is None else str(status)


def _expense_evidence_status(row: dict) -> str:
    status = row.get("expense_evidence_status")
    return "restricted" if status is None else str(status)


@router.get("/board/export")
def board_export(
    status: str | None = Query(
        None,
        pattern=(
            r"^(incomplete_cost|expense_data_unavailable|"
            r"red|yellow|green|no_budget)$"
        ),
    ),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    q: str | None = Query(None, max_length=128),
    lifecycle: str = Query("ongoing", pattern=r"^(ongoing|ended|missing|all)$"),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    """合同级详细盈亏 CSV；与看板复用同一计算、范围与脱敏口径。"""
    _validate_date_pair(date_from, date_to)
    _require_full_contract_scope(ctx)
    record_access_log(
        ctx,
        "board_export",
        "maintenance",
        {
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "status": status,
            "lifecycle": lifecycle,
        },
    )
    data = maintenance_cost.board(
        db,
        date_from,
        date_to,
        status,
        user_ctx=ctx,
        q_text=q,
        lifecycle=lifecycle,
        as_of=business_today(),
    )
    data = apply_field_visibility(data, ctx)
    if not data["rows"]:
        raise HTTPException(
            status_code=422,
            detail="所选范围内没有可导出的合同详细盈亏数据",
        )
    header = [
        "合同",
        "关联项目",
        "order_count",
        "missing_detail_orders",
        "revenue_inc",
        "revenue_ex",
        "expense_inc",
        "expense_ex",
        "parts_cost_inc_tax",
        "parts_cost_ex_tax",
        "parts_gross_profit_inc",
        "parts_gross_profit_ex",
        "parts_gross_margin_inc",
        "parts_gross_margin_ex",
        "contribution_profit_inc",
        "contribution_profit_ex",
        "contribution_margin_inc",
        "contribution_margin_ex",
        "parts_profit_status_inc",
        "parts_profit_status_ex",
        "contribution_status_inc",
        "contribution_status_ex",
        "成本证据状态",
        "成本证据状态-含税",
        "成本证据状态-未税",
        "收入证据状态-含税",
        "收入证据状态-未税",
        "费用证据状态",
    ]
    rows = [
        [
            row.get("contract"),
            "、".join(
                str(project.get("project") or "")
                for project in row.get("projects", [])
                if project.get("project")
            ),
            row.get("order_count"),
            row.get("missing_detail_orders"),
            *(
                row.get(field)
                for field in (
                    "revenue_inc",
                    "revenue_ex",
                    "expense_inc",
                    "expense_ex",
                    "parts_cost_inc_tax",
                    "parts_cost_ex_tax",
                    "parts_gross_profit_inc",
                    "parts_gross_profit_ex",
                    "parts_gross_margin_inc",
                    "parts_gross_margin_ex",
                    "contribution_profit_inc",
                    "contribution_profit_ex",
                    "contribution_margin_inc",
                    "contribution_margin_ex",
                    "parts_profit_status_inc",
                    "parts_profit_status_ex",
                    "contribution_status_inc",
                    "contribution_status_ex",
                )
            ),
            row.get("cost_quality"),
            row.get("parts_cost_inc_tax_quality"),
            row.get("parts_cost_ex_tax_quality"),
            _revenue_evidence_status(row, "inc"),
            _revenue_evidence_status(row, "ex"),
            _expense_evidence_status(row),
        ]
        for row in data["rows"]
    ]
    return _csv_stream(
        header,
        rows,
        "maintenance_contract_profit.csv",
        db=db,
    )


# 兼容既有内部测试与调用；真实实现只保留在 service renderer。
def _build_workbook(contract: str, data: dict):
    return maintenance_workbook_renderer.render_contract_workbook(
        contract,
        data,
        maintenance_workbook_export.safe_xlsx_text,
    )


@router.get("/export-workbook")
def export_workbook(
    contract: str = Query(..., max_length=64),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),   # 硬鉴权：缺/失效凭证 → 401
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    """§16.4 工作簿形态回填导出：与财务现用「项目预算工作簿」同构（四个 Sheet）。

    xlsx 无法走字段级脱敏，因此必须同时具备成本与利润查看权限。
    「产品成本」按财务习惯填单据级总成本于每张 WBDD 首行；行级取价明细作附加列（增强不破坏）。
    """
    _require_workbook_export_permissions(ctx)
    if (date_from is None) != (date_to is None):
        raise HTTPException(status_code=422, detail="date_from 与 date_to 必须同时提供")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from 不能晚于 date_to")
    record_access_log(ctx, "export_workbook", "maintenance", {"contract": contract})
    try:
        output = maintenance_workbook_export.build_contract_workbook_file(
            db,
            contract,
            date_from=date_from,
            date_to=date_to,
        )
    except maintenance_workbook_export.WorkbookExportNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except maintenance_workbook_export.WorkbookExportRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    _release_db_before_stream(db, output)
    return _ClosingStreamingResponse(
        _iter_download_chunks(output),
        resource=output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": _content_disposition(
                f"project_workbook_{contract[:40]}.xlsx",
                ascii_fallback="project_workbook.xlsx",
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/roundtrip-template")
def roundtrip_template(
    contract: str | None = Query(None, max_length=64),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    blank: bool = Query(False),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    """导出固定协议的维保项目可编辑工作簿。"""
    _require_roundtrip_permissions(ctx)
    record_access_log(
        ctx,
        "roundtrip_template",
        "maintenance",
        {
            "contract": contract,
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "blank": blank,
        },
    )
    try:
        output = maintenance_roundtrip.build_roundtrip_template(
            db,
            contract=contract,
            date_from=date_from,
            date_to=date_to,
            exported_by=ctx.user_id,
            blank=blank,
        )
    except maintenance_roundtrip.RoundtripWorkbookError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=str(exc),
            headers={"Retry-After": "5"} if exc.status_code == 429 else None,
        ) from exc
    _release_db_before_stream(db, output)
    return _ClosingStreamingResponse(
        _iter_download_chunks(output),
        resource=output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": _content_disposition(
                "maintenance_roundtrip_template.xlsx",
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/roundtrip-templates")
def roundtrip_templates(
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> StreamingResponse:
    """按合同拆分导出可独立校验的固定回填工作簿 ZIP。"""
    _require_roundtrip_permissions(ctx)
    record_access_log(
        ctx,
        "roundtrip_templates",
        "maintenance",
        {
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
        },
    )
    try:
        output = maintenance_roundtrip.build_roundtrip_template_bundle(
            db,
            date_from=date_from,
            date_to=date_to,
            exported_by=ctx.user_id,
        )
    except maintenance_roundtrip.RoundtripWorkbookError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=str(exc),
            headers={"Retry-After": "5"} if exc.status_code == 429 else None,
        ) from exc
    _release_db_before_stream(db, output)
    return _ClosingStreamingResponse(
        _iter_download_chunks(output),
        resource=output,
        media_type="application/zip",
        headers={
            "Content-Disposition": _content_disposition(
                "维保项目批量回填模板.zip",
                ascii_fallback="maintenance_roundtrip_templates.zip",
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _save_roundtrip_upload(file: UploadFile) -> tuple[str, str]:
    original_name = (
        (file.filename or "maintenance_roundtrip.xlsx")
        .replace("\\", "/")
        .rsplit("/", 1)[-1]
    )
    if not original_name.lower().endswith(".xlsx"):
        raise HTTPException(
            status_code=400,
            detail="维保回填只支持系统导出的 .xlsx 工作簿",
        )
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    size = 0
    try:
        with os.fdopen(fd, "wb") as output:
            while chunk := file.file.read(_DOWNLOAD_CHUNK_BYTES):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"工作簿超过 {config.MAX_UPLOAD_MB}MB 上传上限",
                    )
                output.write(chunk)
    except BaseException:
        _remove_roundtrip_temp(path)
        raise
    return path, original_name[:256]


async def _parse_and_save_roundtrip_upload(request: Request) -> tuple[str, str]:
    """鉴权后才解析 multipart，并同时限制总请求体、文件数和文件字节数。"""
    content_type = request.headers.get("content-type", "")
    media_type = content_type.partition(";")[0].strip().lower()
    if media_type != "multipart/form-data":
        raise HTTPException(
            status_code=415,
            detail="维保回填只接受 multipart/form-data",
        )
    try:
        _parsed_media_type, content_type_params = parse_options_header(content_type)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=400, detail="multipart Content-Type 格式无效") from exc
    boundary = content_type_params.get(b"boundary")
    if not boundary:
        raise HTTPException(status_code=400, detail="multipart boundary 缺失或为空")
    file_limit = config.MAX_UPLOAD_MB * 1024 * 1024
    body_limit = file_limit + _ROUNDTRIP_MULTIPART_OVERHEAD_BYTES
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Content-Length 格式无效") from exc
        if declared_length < 0:
            raise HTTPException(status_code=400, detail="Content-Length 格式无效")
        if declared_length > body_limit:
            raise HTTPException(
                status_code=413,
                detail=f"工作簿超过 {config.MAX_UPLOAD_MB}MB 上传上限",
            )

    consumed = 0
    frame_body = b""
    frame_offset = 0
    frame_more_body = False

    async def limited_receive():
        nonlocal consumed, frame_body, frame_offset, frame_more_body
        if frame_offset < len(frame_body):
            chunk_end = min(
                frame_offset + _ROUNDTRIP_PARSE_CHUNK_BYTES,
                len(frame_body),
            )
            chunk = frame_body[frame_offset:chunk_end]
            frame_offset = chunk_end
            has_pending = frame_offset < len(frame_body)
            await anyio.lowlevel.checkpoint()
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": has_pending or frame_more_body,
            }
        message = await request.receive()
        if message["type"] == "http.request":
            body = message.get("body", b"")
            consumed += len(body)
            if consumed > body_limit:
                raise HTTPException(
                    status_code=413,
                    detail=f"工作簿超过 {config.MAX_UPLOAD_MB}MB 上传上限",
                )
            frame_body = body
            frame_offset = min(_ROUNDTRIP_PARSE_CHUNK_BYTES, len(frame_body))
            frame_more_body = bool(message.get("more_body"))
            chunk = frame_body[:frame_offset]
            has_pending = frame_offset < len(frame_body)
            await anyio.lowlevel.checkpoint()
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": has_pending or frame_more_body,
            }
        return message

    limited_request = Request(request.scope, limited_receive)
    parser = None
    form = None
    owned_path = None
    try:
        try:
            parser = MultiPartParser(
                limited_request.headers,
                limited_request.stream(),
                max_files=1,
                max_fields=0,
                # 1024B 会在表单解析阶段拒绝正常体积的 Excel（#267 修复 4）。
                # 总量已有 content-length + limited_receive 双重 413 限制，
                # 这里放宽到文件上限只是取消对文件 part 的误伤。
                max_part_size=file_limit,
            )
            form = await parser.parse()
        except HTTPException:
            raise
        except (
            MultiPartException,
            FormParserError,
            MultipartParseError,
            UnicodeError,
        ) as exc:
            raise HTTPException(
                status_code=400,
                detail="multipart 请求格式无效",
            ) from exc
        items = form.multi_items()
        if (
            len(items) != 1
            or items[0][0] != "file"
            or not isinstance(items[0][1], UploadFile)
        ):
            raise HTTPException(
                status_code=422,
                detail="必须且只能上传一个名为 file 的 .xlsx 工作簿",
            )
        save_worker = asyncio.create_task(
            anyio.to_thread.run_sync(
                _save_roundtrip_upload,
                items[0][1],
                abandon_on_cancel=False,
            )
        )
        save_cancellation = await _wait_for_roundtrip_task_terminal(save_worker)
        try:
            owned_path, original_name = save_worker.result()
        except BaseException as exc:
            if save_cancellation is not None:
                raise save_cancellation from exc
            raise
        if save_cancellation is not None:
            raise save_cancellation

        form_to_close = form
        form = None
        close_cancellation = await _close_roundtrip_form(form_to_close)
        if close_cancellation is not None:
            raise close_cancellation

        result = (owned_path, original_name)
        owned_path = None
        return result
    except BaseException:
        _remove_roundtrip_temp(owned_path)
        raise
    finally:
        if form is not None:
            try:
                await _close_roundtrip_form(form)
            except BaseException:
                with suppress(Exception):
                    logger.warning(
                        "关闭维保回填 multipart 临时文件失败",
                        exc_info=True,
                    )
        elif parser is not None:
            for partial_file in parser._files_to_close_on_error:
                with suppress(Exception):
                    partial_file.close()


def _import_roundtrip_in_worker(
    path: str,
    original_name: str,
    operated_by: str,
    ctx: UserContext,
) -> dict:
    """在线程内完成审计、事务、回滚和关闭，避免跨线程使用 Session。"""
    db = SessionLocal()
    try:
        record_access_log(
            ctx,
            "roundtrip_import",
            "maintenance",
            {"filename": original_name},
        )
        result = maintenance_roundtrip.import_roundtrip_workbook(
            db,
            path,
            filename=original_name,
            operated_by=operated_by,
        )
    except BaseException:
        try:
            db.rollback()
        except BaseException:
            with suppress(Exception):
                logger.warning(
                    "维保回填失败后的数据库回滚也失败",
                    exc_info=True,
                )
        try:
            db.close()
        except BaseException:
            with suppress(Exception):
                logger.warning(
                    "维保回填失败后的数据库会话关闭也失败",
                    exc_info=True,
                )
        raise
    else:
        try:
            db.close()
        except BaseException:
            with suppress(Exception):
                logger.warning(
                    "维保回填成功后的数据库会话关闭失败",
                    exc_info=True,
                )
        return result


async def _wait_for_roundtrip_import_worker(
    path: str,
    original_name: str,
    operated_by: str,
    ctx: UserContext,
) -> dict:
    """普通请求取消时仍等待已启动 worker 收尾，防止提前删文件或释放进程锁。"""
    worker = asyncio.create_task(
        anyio.to_thread.run_sync(
            _import_roundtrip_in_worker,
            path,
            original_name,
            operated_by,
            ctx,
            abandon_on_cancel=False,
        )
    )
    cancellation = await _wait_for_roundtrip_task_terminal(worker)
    try:
        result = worker.result()
    except BaseException as exc:
        if cancellation is not None:
            raise cancellation from exc
        raise
    if cancellation is not None:
        raise cancellation
    return result


@router.post("/roundtrip-import")
async def roundtrip_import(
    request: Request,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(require_action(
        "action_maintenance_roundtrip_apply",
        require_data="data_profit",
    )),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    """校验并原子应用系统导出的固定协议工作簿。

    路由签名不声明 ``UploadFile``，避免 FastAPI 在依赖执行前消费 multipart。身份、页面、
    显式写动作和数据可见依赖全部通过后，才进入限并发、限总字节的解析路径。
    """
    _require_roundtrip_permissions(ctx)
    operator = _real_operator(db, ident)
    if not _ROUNDTRIP_IMPORT_PARSE_LOCK.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="已有维保回填文件正在解析，请稍后重试",
            headers={"Retry-After": "5"},
        )
    path = None
    try:
        path, original_name = await _parse_and_save_roundtrip_upload(request)
        try:
            return await _wait_for_roundtrip_import_worker(
                path,
                original_name,
                operator,
                ctx,
            )
        except maintenance_roundtrip.RoundtripWorkbookError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except maintenance_cost.MaintenanceCostRecomputeBusy as exc:
            raise HTTPException(
                status_code=409,
                detail=str(exc),
                headers={"Retry-After": "5"},
            ) from exc
    finally:
        try:
            _remove_roundtrip_temp(path)
        finally:
            _ROUNDTRIP_IMPORT_PARSE_LOCK.release()
