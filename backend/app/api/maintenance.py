"""维保项目成本 API（docs/维保出库成本核算-开发方案.md §5）。

鉴权双层：current_role 硬鉴权（缺/失效凭证 → 401，与 purchases/parts 一致，不依赖 RBAC 开关）
+ require_page('page_maintenance') 页面准入（admin/boss/purchaser 模板默认开）。
成本金额字段随 data_purchase_cost 脱敏（成本 7 键 + 聚合派生键已登记 FIELD_GROUPS）。
"""
import csv
import io
import re
from contextlib import suppress
from datetime import date
from tempfile import SpooledTemporaryFile
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.auth import current_role, require_admin
from app.business_time import business_today
from app.db import get_db
from app.security import (
    UserContext, apply_field_visibility, get_current_user_context, record_access_log,
    is_scoped_sales, require_page,
)
from app import config
from app.services import (
    maintenance_cost,
    maintenance_export,
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
              _auth: str = Depends(require_admin),   # 全表重算(~1min 写库)：限管理员，与导入触发方口径一致
              ctx: UserContext = Depends(get_current_user_context)) -> dict:
    record_access_log(ctx, "recompute", "maintenance")
    try:
        return maintenance_cost.recompute(db)
    except maintenance_cost.MaintenanceCostRecomputeBusy as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=str(exc),
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
    record_access_log(ctx, "export", "maintenance")
    data = maintenance_cost.projects_aggregate(
        db, date_from, date_to, q, user_ctx=ctx,
        lifecycle=lifecycle, as_of=business_today(),
    )
    data = apply_field_visibility(data, ctx)   # 导出同样过脱敏层（§8.5）
    header = ["项目", "期限状态", "维保终止日期",
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
                  "sales_history", "none",
              )),
              "月份数", "关联销售订单", "合同额(含税参考)", "合同被多项目共用"]
    rows = []
    for r in data["rows"]:
        bs = r["by_source"]
        source_counts = (
            [None] * 10
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
                     r["contract_amount"], "是" if r["contract_shared"] else ""])
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
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    record_access_log(ctx, "lines_export", "maintenance", {"project": project})
    line_count = maintenance_cost.project_line_count(
        db, project, month, date_from, date_to,
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
    record_access_log(ctx, "board", "maintenance")
    data = maintenance_cost.board(
        db, date_from, date_to, status, user_ctx=ctx, q_text=q,
        lifecycle=lifecycle, as_of=business_today(),
    )
    return apply_field_visibility(data, ctx)


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
    record_access_log(ctx, "export_workbook", "maintenance", {"contract": contract})
    try:
        output = maintenance_workbook_export.build_contract_workbook_file(db, contract)
    except maintenance_workbook_export.WorkbookExportRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
