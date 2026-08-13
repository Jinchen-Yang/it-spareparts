"""Tritium project table import: preview diff and atomic, idempotent apply."""

import hashlib
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import xlrd
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.business_time import business_today
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_import import (
    MaintenanceProjectImportBatch,
    MaintenanceProjectSourceLink,
)


class ImportApplyConflict(ValueError):
    """Raised when applying would violate a uniqueness constraint."""


MAX_FILENAME_LENGTH = 256


def _hash_file(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _excel_serial_to_date(serial: float) -> date | None:
    """Convert Excel serial date number to Python date."""
    try:
        from datetime import timedelta
        excel_epoch = date(1899, 12, 30)
        return excel_epoch + timedelta(days=int(serial))
    except (ValueError, TypeError):
        return None


def _parse_chinese_month(text: str) -> str | None:
    """Parse '2026年10月' → '2026-10'."""
    import re
    m = re.match(r"(\d{4})年(\d{1,2})月", (text or "").strip())
    if not m:
        return None
    return f"{m.group(1)}-{int(m.group(2)):02d}"


def _safe_decimal(value) -> Decimal | None:
    """Strict decimal parse; thousands separators and out-of-range → None."""
    if isinstance(value, (int, float)):
        raw = str(value)
    elif isinstance(value, str):
        raw = value.replace(",", "").replace("¥", "").strip()
        if raw in {"", "/"}:
            return None
    else:
        return None
    try:
        d = Decimal(raw)
    except Exception:
        return None
    if d < 0 or d >= 1_000_000_000_000:
        return None
    return d


def _lifecycle(start: date | None, end: date | None, today: date) -> str:
    if end is None and start is None:
        return "missing"
    if end is not None and end < today:
        return "ended"
    return "ongoing"


def _parse_project_rows(ws) -> list[dict[str, Any]]:
    """Parse 维保项目清单 sheet rows (skip header row)."""
    rows = []
    for r in range(1, ws.nrows):
        xsd = str(ws.cell_value(r, 0)).strip()
        if not xsd or xsd == "/":
            continue
        start = _excel_serial_to_date(ws.cell_value(r, 5)) if ws.cell_value(r, 5) else None
        end = _excel_serial_to_date(ws.cell_value(r, 6)) if ws.cell_value(r, 6) else None
        amount_cell = ws.cell_value(r, 9)
        amount = _safe_decimal(amount_cell)
        rows.append({
            "source_id": xsd,
            "salesperson": str(ws.cell_value(r, 2)).strip() or None,
            "business_type": str(ws.cell_value(r, 3)).strip() or None,
            "project_name": str(ws.cell_value(r, 4)).strip(),
            "maintenance_start": start.isoformat() if start else None,
            "maintenance_end": end.isoformat() if end else None,
            "manager_name": str(ws.cell_value(r, 8)).strip() if ws.cell_value(r, 8) and str(ws.cell_value(r, 8)).strip() != "/" else None,
            "order_amount": float(amount) if amount is not None else None,
            "amount_unparsable": amount is None
                and amount_cell not in (None, "")
                and str(amount_cell).strip() not in {"", "/"},
            "collections": _parse_collections(ws, r),
        })
    return rows


def _parse_collections(ws, row: int) -> list[dict]:
    """Parse 7 pairs of collection time + amount (cols 17-30, 0-indexed 16-29)."""
    result = []
    for i in range(7):
        time_col = 16 + i
        amount_col = 23 + i
        time_text = str(ws.cell_value(row, time_col)).strip() if ws.cell_value(row, time_col) else ""
        amount = _safe_decimal(ws.cell_value(row, amount_col))
        if not time_text or time_text == "/" or amount is None:
            continue
        month = _parse_chinese_month(time_text)
        if month:
            result.append({"month": month, "amount": float(amount)})
    return result


def preview_import(
    db: Session,
    file_content: bytes,
    filename: str,
    operated_by: str,
) -> dict:
    """Parse file, compute diff, store preview batch.  Zero writes to projects.

    ``preview_json`` stores the FULL parsed row sets so apply can be atomic
    and idempotent; the HTTP response truncates the row lists for the UI.
    """
    file_hash = _hash_file(file_content)
    safe_filename = filename[:MAX_FILENAME_LENGTH]
    try:
        wb = xlrd.open_workbook(file_contents=file_content)
    except xlrd.XLRDError:
        return {
            "status": "error",
            "errors": ["文件无法解析，请确认是氚云导出的 .xls 文件"],
        }

    if "维保项目清单" not in wb.sheet_names():
        return {"status": "error", "errors": ["未找到'维保项目清单'工作表"]}

    ws = wb.sheet_by_name("维保项目清单")
    rows = _parse_project_rows(ws)

    errors = []
    seen = set()
    for row in rows:
        sid = row["source_id"]
        if sid in seen:
            errors.append(f"重复订单编号: {sid}")
        seen.add(sid)
        if not row["project_name"]:
            errors.append(f"项目名称为空: {sid}")
        if row.get("amount_unparsable"):
            errors.append(f"订单金额无法解析: {sid}")

    if errors:
        batch = MaintenanceProjectImportBatch(
            filename=safe_filename,
            file_hash=file_hash,
            status="error",
            preview_json={"errors": errors, "row_count": len(rows)},
            operated_by=operated_by,
        )
        db.add(batch)
        db.commit()
        return {
            "import_id": batch.id,
            "status": "error",
            "errors": errors,
            "row_count": len(rows),
        }

    existing = {
        link.source_id: link.project_id
        for link in db.scalars(select(MaintenanceProjectSourceLink)).all()
    }
    new = []
    updated = []
    for row in rows:
        if row["source_id"] in existing:
            updated.append({
                "source_id": row["source_id"],
                "project_id": existing[row["source_id"]],
                "project_name": row["project_name"],
            })
        else:
            new.append(row)

    preview = {
        "new_count": len(new),
        "updated_count": len(updated),
        "new_projects": new,
        "updated_projects": updated,
        "row_count": len(rows),
    }

    batch = MaintenanceProjectImportBatch(
        filename=safe_filename,
        file_hash=file_hash,
        status="preview",
        preview_json=preview,
        operated_by=operated_by,
    )
    db.add(batch)
    db.commit()

    return {
        "import_id": batch.id,
        "status": "preview",
        "new_count": len(new),
        "updated_count": len(updated),
        "row_count": len(rows),
        # UI preview truncates; full rows live in preview_json for apply.
        "new_projects": new[:50],
        "updated_projects": updated[:50],
    }


def apply_import(
    db: Session,
    import_id: int,
    operated_by: str,
) -> dict:
    """Atomically apply a previewed import batch; re-resolution makes it idempotent.

    Source links are re-queried at apply time: rows whose XSDD gained a link
    since preview are applied as updates rather than new inserts.
    """
    batch = db.get(MaintenanceProjectImportBatch, import_id)
    if batch is None:
        raise ValueError("导入批次不存在")
    if batch.status != "preview":
        raise ValueError("只能应用处于预览状态的批次")
    if batch.preview_json is None:
        raise ValueError("预览数据缺失")

    preview = batch.preview_json
    new_rows = preview.get("new_projects", [])
    updated_rows = preview.get("updated_projects", [])

    links = {
        link.source_id: link
        for link in db.scalars(select(MaintenanceProjectSourceLink)).all()
    }

    created = 0
    updated = 0
    today = business_today()
    for row in new_rows:
        existing_link = links.get(row["source_id"])
        if existing_link is not None:
            project = db.get(MaintenanceProject, existing_link.project_id)
            if project is not None:
                project.display_name = row["project_name"]
                existing_link.latest_batch_id = batch.id
                updated += 1
                continue
        project = MaintenanceProject(
            project_id=str(uuid.uuid4()),
            project_code=row["source_id"],
            display_name=row["project_name"],
            # 来源负责人原文只进入 project_manager_id（主档惯例：实名绑定
            # 由管理员在项目工作台显式指派，导入绝不自动绑定系统账号）。
            project_manager_id=row.get("manager_name"),
            lifecycle_status=_lifecycle(
                date.fromisoformat(row["maintenance_start"]) if row.get("maintenance_start") else None,
                date.fromisoformat(row["maintenance_end"]) if row.get("maintenance_end") else None,
                today,
            ),
        )
        db.add(project)
        db.flush()

        db.add(
            MaintenanceProjectSourceLink(
                source_id=row["source_id"],
                project_id=project.project_id,
                first_batch_id=batch.id,
                latest_batch_id=batch.id,
            )
        )
        created += 1

    for row in updated_rows:
        link = links.get(row["source_id"])
        if link is None:
            continue
        project = db.get(MaintenanceProject, link.project_id)
        if project is not None:
            project.display_name = row["project_name"]
            link.latest_batch_id = batch.id
            updated += 1

    batch.status = "applied"
    batch.applied_at = datetime.now(timezone.utc)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ImportApplyConflict(
            "导入冲突：部分订单编号已被其他批次创建，请重新上传后预览"
        ) from exc
    return {"created": created, "updated": updated}
