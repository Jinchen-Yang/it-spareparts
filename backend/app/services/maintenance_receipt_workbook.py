"""D-0051 signed, project-scoped editing of existing return receipt facts."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import io
import json
import time
from typing import Callable

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill, Protection
from openpyxl.utils import get_column_letter
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.business_time import BUSINESS_TZ
from app.config import get_settings
from app.models.maintenance_project_operations import MaintenanceProjectOperationAudit
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.services import import_safety
from app.services import maintenance_return_receipts as receipts
from app.services.excel_text_safety import SafeTextWorkbook, has_illegal_excel_upload_text
from app.services.maintenance_project_workbook_v2 import _metadata_signature

PROTOCOL = "ITDATA_RETURN_RECEIPT_EDIT/1"
SHEET = "返还收货编辑"
BASE_SHEET = "_receipt_baseline"
META_SHEET = "_receipt_meta"
MAX_ROWS = 5000
MAX_BYTES = 20 * 1024 * 1024
ENTITY = "return_receipt_workbook"
HEADERS = ["记录身份（只读）", "版本（只读）", "PN（只读）", "来源（只读）",
           "需求单（只读）", "操作", "实收数量", "件况", "收到时间（北京时间）",
           "SN（每行一个）", "备注", "修改或作废原因"]
EDITABLE = {6: "qty", 7: "condition", 8: "occurred_at", 9: "serial_numbers", 10: "note"}


class ReceiptWorkbookError(receipts.ReturnReceiptValidation):
    pass


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _plan_hash(plan: dict) -> str:
    return _hash({"baseline": plan["baseline"], "operations": [
        {key: op[key] for key in ("receipt_id", "version", "action", "changes", "reason")}
        for op in plan["operations"]]})


def _confirmation(plan: dict, operator: str) -> str:
    key_id, key = get_settings().maintenance_manifest_signing_material()
    meta = {"protocol": PROTOCOL + "/preview", "project_id": plan["project_id"],
            "file_hash": plan["file_hash"], "plan_hash": _plan_hash(plan), "actor": operator, "key_id": key_id,
            "expires": str(int(time.time()) + 86400)}
    meta["metadata_hmac"] = _metadata_signature(meta, key)
    return _json(meta)


def _verify_confirmation(token: str, plan: dict, operator: str) -> None:
    try:
        meta = json.loads(token)
        key = get_settings().maintenance_manifest_verification_keys().get(meta.get("key_id", ""))
        if (key is None or meta.get("protocol") != PROTOCOL + "/preview"
                or meta.get("project_id") != plan["project_id"] or meta.get("file_hash") != plan["file_hash"]
                or meta.get("plan_hash") != _plan_hash(plan)
                or meta.get("actor") != operator or int(meta.get("expires", 0)) < time.time()
                or not hmac.compare_digest(meta.get("metadata_hmac", ""), _metadata_signature(meta, key))):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise receipts.ReturnReceiptConflict("确认已失效或文件/账号已改变，请重新预览") from None


def _display(row, order_no: str | None) -> list:
    occurred = row.occurred_at
    if occurred is not None:
        occurred = occurred.astimezone(BUSINESS_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return [row.rkd_line_id, row.version, row.pn, row.source,
            order_no or "",
            "保留", format(Decimal(row.qty), "f"), row.test_result or "", occurred or "",
            "\n".join(row.serial_numbers or []), row.note or "", ""]


def build(db: Session, *, project_id: str) -> bytes:
    receipts._get_project(db, project_id)
    rows = db.scalars(select(MaintenanceRkdReturnLine).where(
        MaintenanceRkdReturnLine.project_id == project_id,
        MaintenanceRkdReturnLine.line_status == "active",
    ).order_by(MaintenanceRkdReturnLine.rkd_line_id).limit(MAX_ROWS + 1)).all()
    if len(rows) > MAX_ROWS:
        raise ReceiptWorkbookError("有效收货超过5000行，请使用网页分批维护")
    source_ids = {row.source_order_id for row in rows if row.source_order_id}
    order_nos = dict(db.execute(select(FMaintenanceOrder.raw_order_id, FMaintenanceOrder.order_no)
                               .where(FMaintenanceOrder.raw_order_id.in_(source_ids))).all()) if source_ids else {}
    baseline = [_display(row, order_nos.get(row.source_order_id)) for row in rows]
    if any(has_illegal_excel_upload_text(value) for values in baseline for value in values):
        raise ReceiptWorkbookError("收货原值含Excel不可表示字符，请在网页维护该记录")
    if any(len(_json(row)) > 32767 for row in baseline):
        raise ReceiptWorkbookError("收货行文本超过Excel安全容量，请在网页维护该记录")
    wb = SafeTextWorkbook()
    ws = wb.active
    ws.title = SHEET
    ws.append(HEADERS)
    for values in baseline:
        ws.append(values)
    for row in ws.iter_rows(min_row=2):
        for cell in row[5:]:
            cell.protection = Protection(locked=False)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    ws.protection.sheet = True
    ws.protection.autoFilter = False
    ws.auto_filter.ref = ws.dimensions
    for col in range(1, len(HEADERS) + 1):
        ws.cell(1, col).font = Font(bold=True)
        ws.cell(1, col).fill = PatternFill("solid", fgColor="FFF2CC" if col >= 6 else "DCE6F1")
        ws.column_dimensions[get_column_letter(col)].width = 24 if col != 1 else 38
    ws.freeze_panes = "F2"
    instructions = wb.create_sheet("填写说明")
    for line in [
        "仅编辑现有收货：数量、件况、收到时间、SN和备注。填写修改原因，再在原入口预览上传。",
        "操作填保留或作废。保留行的黄色字段可改；作废必须填写原因。删行、新增行和修改只读列会被拒绝。",
        "收到时间按北京时间填写 YYYY-MM-DD HH:mm:ss；空白明确清空。SN每行一个，数量和SN须一致。",
        "PN、来源、需求关联和项目不可在本文件更改，请用网页修改。整机不能编辑SN且数量固定1台。",
        "全文件同一事务，任一行不通过则全部不保存。预览后数据变化会要求重新下载。",
        "这是独立收货编辑文件，不能上传到项目总表入口；项目总表的收货台账仍是只读快照。",
    ]:
        instructions.append([line])
    instructions.column_dimensions["A"].width = 115
    bs = wb.create_sheet(BASE_SHEET)
    for values in baseline:
        bs.append([_json(values)])
    bs.sheet_state = "veryHidden"
    key_id, key = get_settings().maintenance_manifest_signing_material()
    meta = {"protocol": PROTOCOL, "project_id": project_id,
            "baseline_hash": _hash(baseline), "key_id": key_id}
    meta["metadata_hmac"] = _metadata_signature(meta, key)
    ms = wb.create_sheet(META_SHEET)
    for k, v in meta.items():
        ms.append([k, v])
    ms.sheet_state = "veryHidden"
    # User text remains literal, including values beginning with '='.
    for sheet in wb:
        for cells in sheet:
            for cell in cells:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
    output = io.BytesIO()
    wb.save(output)
    return output.getvalue()


def _value(value):
    return "" if value is None else value


def _parsed_value(index: int, value, row_no: int):
    if index == 6:
        try:
            number = Decimal(str(value))
            if not number.is_finite() or number != number.to_integral_value() or not 0 < number < 10**11:
                raise ValueError()
            return int(number)
        except (ValueError, InvalidOperation):
            raise ReceiptWorkbookError(f"第{row_no}行：实收数量必须为正整数") from None
    if index == 8:
        if value == "":
            return None
        try:
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
            return parsed.replace(tzinfo=BUSINESS_TZ) if parsed.tzinfo is None else parsed
        except ValueError:
            raise ReceiptWorkbookError(f"第{row_no}行：收到时间格式无效") from None
    if not isinstance(value, str):
        raise ReceiptWorkbookError(f"第{row_no}行：文本字段格式无效")
    if has_illegal_excel_upload_text(value):
        raise ReceiptWorkbookError(f"第{row_no}行：含不支持的控制字符")
    if index == 9:
        return value.splitlines() if value else []
    if index == 7 and value not in ("", "成品", "坏品", "废品"):
        raise ReceiptWorkbookError(f"第{row_no}行：件况仅支持成品、坏品、废品或空白")
    if index == 10 and len(value) > 512:
        raise ReceiptWorkbookError(f"第{row_no}行：备注最多512字")
    return value or None


def parse(data: bytes, *, project_id: str) -> dict:
    try:
        import_safety.validate_xlsx_zip(data, max_bytes=MAX_BYTES)
        wb = load_workbook(io.BytesIO(data), data_only=False, read_only=True)
    except Exception as exc:
        raise ReceiptWorkbookError("无法读取安全的xlsx文件") from exc
    try:
        if not {SHEET, BASE_SHEET, META_SHEET}.issubset(wb.sheetnames):
            raise ReceiptWorkbookError("不是收货编辑模板，请在返还收货Excel编辑入口重新下载")
        ms, bs, ws = wb[META_SHEET], wb[BASE_SHEET], wb[SHEET]
        if (ms.max_row != 5 or ms.max_column != 2 or bs.max_row > MAX_ROWS
                or bs.max_column != 1 or ws.max_row > MAX_ROWS + 1 or ws.max_column != len(HEADERS)):
            raise ReceiptWorkbookError("模板结构或行数无效")
        meta = {str(r[0]): str(r[1]) for r in ms.iter_rows(values_only=True)}
        key = get_settings().maintenance_manifest_verification_keys().get(meta.get("key_id", ""))
        if (meta.get("protocol") != PROTOCOL or meta.get("project_id") != project_id
                or key is None or not hmac.compare_digest(meta.get("metadata_hmac", ""), _metadata_signature(meta, key))):
            raise ReceiptWorkbookError("模板签名或项目不匹配，请重新下载")
        baseline = [json.loads(r[0]) for r in bs.iter_rows(values_only=True) if r[0] is not None]
        if _hash(baseline) != meta["baseline_hash"]:
            raise ReceiptWorkbookError("模板基线已修改，请重新下载")
        all_rows = list(ws.iter_rows())
        if not all_rows or [c.value for c in all_rows[0]] != HEADERS:
            raise ReceiptWorkbookError("表头已改变，请重新下载模板")
        expected = {r[0]: r for r in baseline}
        seen = set()
        ops = []
        for row_no, cells in enumerate(all_rows[1:], 2):
            if all(c.value is None for c in cells):
                continue
            if any(c.data_type in {"f", "e"} for c in cells):
                raise ReceiptWorkbookError(f"第{row_no}行：编辑模板不接受公式，请粘贴明确值")
            values = [_value(c.value) for c in cells]
            base = expected.get(values[0])
            if base is None or values[0] in seen or values[:5] != base[:5]:
                raise ReceiptWorkbookError(f"第{row_no}行：身份、版本或只读字段已改变")
            seen.add(values[0])
            action = values[5]
            if action not in ("保留", "作废"):
                raise ReceiptWorkbookError(f"第{row_no}行：操作只能填写保留或作废")
            updates = {}
            for index, name in EDITABLE.items():
                if values[index] != base[index]:
                    updates[name] = _parsed_value(index, values[index], row_no)
            if action == "作废" and updates:
                raise ReceiptWorkbookError(f"第{row_no}行：作废行请保持其它字段不变")
            if updates or action == "作废":
                reason = values[11]
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
                    raise ReceiptWorkbookError(f"第{row_no}行：修改或作废须填写1至256字原因")
                changes = [{"field": name, "label": HEADERS[index], "before": base[index], "after": str(values[index])}
                           for index, name in EDITABLE.items() if name in updates]
                ops.append({"row_no": row_no, "receipt_id": values[0], "version": base[1],
                            "old_qty": base[6], "action": action, "updates": updates, "changes": changes,
                            "reason": reason.strip()})
        if seen != set(expected):
            raise ReceiptWorkbookError("不能删除收货行；需要作废时请在操作列明确填写作废")
        file_hash = hashlib.sha256(data).hexdigest()
        return {"file_hash": file_hash, "digest": _hash([PROTOCOL, project_id, file_hash]),
                "project_id": project_id, "baseline": expected, "operations": ops}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, ReceiptWorkbookError):
            raise
        raise ReceiptWorkbookError("模板结构无效，请重新下载") from exc
    finally:
        wb.close()


def _lock(db: Session, plan: dict, authorize: Callable[[], None]) -> None:
    receipts.lock_receipt_context(db)
    receipts.lock_receipt_projects(db, {plan["project_id"]})
    authorize()
    receipts._get_project(db, plan["project_id"], lock=True)
    ids = sorted(plan["baseline"])
    rows = db.scalars(select(MaintenanceRkdReturnLine).where(
        MaintenanceRkdReturnLine.rkd_line_id.in_(ids),
    ).order_by(MaintenanceRkdReturnLine.rkd_line_id).with_for_update()
        .execution_options(populate_existing=True)).all()
    if len(rows) != len(ids):
        raise receipts.ReturnReceiptConflict("收货记录已改变，请重新下载")
    for row in rows:
        base = plan["baseline"][row.rkd_line_id]
        if row.project_id != plan["project_id"] or row.version != base[1] or row.line_status != "active":
            raise receipts.ReturnReceiptConflict("收货归属或版本已改变，请重新下载")
    receipts.lock_receipt_demands(db, {r.source_order_id for r in rows if r.source_order_id})


def _execute(db: Session, plan: dict, operator: str) -> list[dict]:
    def order(op):
        if op["action"] == "作废":
            return (0, op["receipt_id"])
        return (1 if Decimal(str(op["updates"].get("qty", op["old_qty"]))) <= Decimal(op["old_qty"]) else 2, op["receipt_id"])
    results = []
    for op in sorted(plan["operations"], key=order):
        args = {"receipt_id": op["receipt_id"], "expected_version": op["version"],
                "reason": op["reason"], "operated_by": operator}
        if op["action"] == "作废":
            result = receipts.void_receipt(db, **args)
        else:
            result = receipts.update_receipt(db, updates=op["updates"], **args)
        results.append({"row_no": op["row_no"], "receipt_id": result["receipt_id"],
                        "version": result["version"], "action": op["action"]})
    return sorted(results, key=lambda row: row["row_no"])


def _replay(db: Session, plan: dict, operator: str):
    row = db.scalar(select(MaintenanceProjectOperationAudit).where(
        MaintenanceProjectOperationAudit.project_id == plan["project_id"],
        MaintenanceProjectOperationAudit.entity_type == ENTITY,
        MaintenanceProjectOperationAudit.entity_id == plan["file_hash"],
        MaintenanceProjectOperationAudit.action == "apply_completed",
        MaintenanceProjectOperationAudit.operated_by == operator,
    ).order_by(MaintenanceProjectOperationAudit.id).limit(1))
    if row is None:
        return None
    result = deepcopy(row.after_json)
    result["replayed"] = True
    return result


def preview(db: Session, *, data: bytes, project_id: str, operator: str, authorize: Callable[[], None]) -> dict:
    plan = parse(data, project_id=project_id)
    receipts.lock_receipt_context(db)
    receipts.lock_receipt_projects(db, {project_id})
    authorize()
    replay = _replay(db, plan, operator)
    if replay:
        return {**replay, "valid": True, "digest": _confirmation(plan, operator)}
    nested = db.begin_nested()
    try:
        _lock(db, plan, authorize)
        _execute(db, plan, operator)
    finally:
        nested.rollback()
    return {"valid": True, "digest": _confirmation(plan, operator), "replayed": False,
            "updates": sum(op["action"] != "作废" for op in plan["operations"]),
            "voids": sum(op["action"] == "作废" for op in plan["operations"]),
            "rows": [{"row_no": op["row_no"], "receipt_id": op["receipt_id"], "action": op["action"],
                      "fields": list(op["updates"]), "changes": op["changes"], "reason": op["reason"]}
                     for op in plan["operations"]]}


def apply(db: Session, *, data: bytes, project_id: str, operator: str, digest: str,
          authorize: Callable[[], None]) -> dict:
    plan = parse(data, project_id=project_id)
    _verify_confirmation(digest, plan, operator)
    # Serializes first receipts without a new table/unique index; held to commit.
    lock_key = int.from_bytes(hashlib.sha256(f"{ENTITY}:{project_id}:{operator}:{plan['file_hash']}".encode()).digest()[:8], "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    receipts.lock_receipt_context(db)
    receipts.lock_receipt_projects(db, {project_id})
    authorize()
    replay = _replay(db, plan, operator)
    if replay:
        return replay
    _lock(db, plan, authorize)
    rows = _execute(db, plan, operator)
    result = {"replayed": False, "project_id": project_id,
              "updates": sum(op["action"] != "作废" for op in plan["operations"]),
              "voids": sum(op["action"] == "作废" for op in plan["operations"]), "rows": rows}
    db.add(MaintenanceProjectOperationAudit(project_id=project_id, entity_type=ENTITY,
        entity_id=plan["file_hash"], action="apply_completed", before_json=None, after_json=result,
        reason="返还收货Excel编辑首次应用回执", operated_by=operator))
    db.flush()
    return result
