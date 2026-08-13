"""Tritium project table import: preview diff and atomic apply."""

import hashlib
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO
from typing import Any

import xlrd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_import import (
    MaintenanceProjectImportBatch,
    MaintenanceProjectSourceLink,
)


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


def _parse_project_rows(ws) -> list[dict[str, Any]]:
    """Parse 维保项目清单 sheet rows (skip header row)."""
    rows = []
    for r in range(1, ws.nrows):
        xsd = str(ws.cell_value(r, 0)).strip()
        if not xsd or xsd == "/":
            continue
        order_date_serial = ws.cell_value(r, 1)
        order_date = _excel_serial_to_date(order_date_serial) if isinstance(order_date_serial, (int, float)) else None
        rows.append({
            "source_id": xsd,
            "salesperson": str(ws.cell_value(r, 2)).strip() or None,
            "business_type": str(ws.cell_value(r, 3)).strip() or None,
            "project_name": str(ws.cell_value(r, 4)).strip(),
            "maintenance_start": _excel_serial_to_date(ws.cell_value(r, 5)) if ws.cell_value(r, 5) else None,
            "maintenance_end": _excel_serial_to_date(ws.cell_value(r, 6)) if ws.cell_value(r, 6) else None,
            "manager_name": str(ws.cell_value(r, 8)).strip() if ws.cell_value(r, 8) and str(ws.cell_value(r, 8)).strip() != "/" else None,
            "order_amount": _safe_decimal(ws.cell_value(r, 9)),
            "collections": _parse_collections(ws, r),
        })
    return rows


def _safe_decimal(value) -> Decimal | None:
    try:
        d = Decimal(str(value))
        if d < 0 or d >= 1_000_000_000_000:
            return None
        return d
    except Exception:
        return None


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
    """Parse file, compute diff, store preview batch.  Zero writes to projects."""
    file_hash = _hash_file(file_content)
    wb = xlrd.open_workbook(file_contents=file_content)

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
        if row["order_amount"] is not None and row["order_amount"] >= 1_000_000_000_000:
            errors.append(f"订单金额超出范围: {sid}")

    if errors:
        batch = MaintenanceProjectImportBatch(
            filename=filename,
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

    # Build diff: new projects, updated projects
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
        "new_projects": new[:50],  # Limit preview size
        "updated_projects": updated[:50],
        "row_count": len(rows),
    }

    batch = MaintenanceProjectImportBatch(
        filename=filename,
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
        **preview,
    }


def apply_import(
    db: Session,
    import_id: int,
    operated_by: str,
) -> dict:
    """Atomically apply a previewed import batch.

    Creates new MaintenanceProject rows for new XSDDs.  Updates
    display_name for existing ones.  Never touches project_manager_id.
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

    created = 0
    for row in new_rows:
        project = MaintenanceProject(
            project_id=str(uuid.uuid4()),
            project_code=row["source_id"],
            display_name=row["project_name"],
            project_manager_id=row.get("manager_name"),
            lifecycle_status="missing",
        )
        db.add(project)
        db.flush()

        link = MaintenanceProjectSourceLink(
            source_id=row["source_id"],
            project_id=project.project_id,
            first_batch_id=batch.id,
            latest_batch_id=batch.id,
        )
        db.add(link)
        created += 1

    for row in updated_rows:
        project = db.get(MaintenanceProject, row["project_id"])
        if project:
            project.display_name = row["project_name"]
            link = db.scalar(
                select(MaintenanceProjectSourceLink).where(
                    MaintenanceProjectSourceLink.source_id == row["source_id"]
                )
            )
            if link:
                link.latest_batch_id = batch.id
                link.source_version = batch.source_version

    batch.status = "applied"
    batch.applied_at = datetime.now(timezone.utc)

    db.commit()
    return {"created": created, "updated": len(updated_rows)}
