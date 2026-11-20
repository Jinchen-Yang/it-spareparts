"""把已核对的仓单导出独立投影为分析事实。只由离线命令调用。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from zipfile import BadZipFile
from xml.etree.ElementTree import ParseError
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import delete, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import config
from app.services.import_safety import validate_xlsx_zip


class WarehouseImportError(ValueError):
    pass


# 单据源、单头字段、明细字段。只选分析投影实际使用的源列。
_ID = "数据ID(不可修改)"
_HEADER = {"source_id": _ID, "source_modified_at": "修改时间(必填)", "status": "数据状态"}
_TEST = {"needs_test": "是否需测试", "tested": "测试完成", "tester": "测试人员",
         "tested_date": "测试完成日期", "whole_description": "整机描述", "whole_unit": "整机单位"}
_PART = {"source_id": "备件明细." + _ID, "pn": "备件明细.备件PN(必填)", "unit": "备件明细.单位"}
SHEETS = {
    "入库单": (
        "inbound",
        {**_HEADER, **_TEST, "number": "入库单号(必填)", "business_date": "入库日期(必填)",
         "kind": "入库类别(必填)", "center": "仓储中心(必填)", "operator": "拣货人员(必填)",
         "whole_pn": "整机PN(必填)", "whole_qty": "整机数量"},
        {**_PART, "description": "备件明细.备件描述", "quantity": "备件明细.入库数量",
         "warehouse": "备件明细.入库仓库名", "bin_name": "备件明细.库位名",
         "test_result": "备件明细.测试结果(必填)"},
    ),
    "返库单": (
        "return",
        {**_HEADER, **_TEST, "number": "返库单号(必填)", "business_date": "返库日期",
         "kind": "返库类别", "center": "仓储中心", "operator": "拣货人员",
         "whole_pn": "整机PN", "whole_qty": "整机返库数"},
        {**_PART, "description": "备件明细.产品描述", "quantity": "备件明细.返库数量",
         "category": "备件明细.产品大类", "brand": "备件明细.产品品牌",
         "warehouse": "备件明细.入库仓库", "bin_name": "备件明细.入库库位",
         "test_result": "备件明细.备件测试结果"},
    ),
    "直采直退": (
        "direct",
        {**_HEADER, "number": "退货单号(必填)", "business_date": "退货日期",
         "kind": "退货类型", "operator": "退货人员"},
        {"source_id": "退货明细." + _ID, "pn": "退货明细.产品名称", "description": "退货明细.产品描述",
         "quantity": "退货明细.退货数量", "unit": "退货明细.单位", "category": "退货明细.产品大类",
         "brand": "退货明细.品牌"},
    ),
}


@dataclass
class WarehouseRecord:
    head: dict
    lines: dict[str, dict] = field(default_factory=dict)


def _text(value):
    return None if value is None else str(value).strip() or None


def _value(key, value):
    if _text(value) is None:
        return None
    if key in {"whole_qty", "quantity"}:
        try:
            number = Decimal(str(value).strip())
            if (not number.is_finite() or abs(number) >= 10**14
                    or number.quantize(Decimal("0.000001")) != number):
                raise ValueError
        except (InvalidOperation, ValueError):
            raise WarehouseImportError("数量须为 Numeric(20,6) 可精确表示的数值") from None
        return number
    if key in {"source_modified_at", "business_date", "tested_date"}:
        try:
            if key == "source_modified_at":
                if not isinstance(value, datetime) and len(str(value).strip()) <= 10:
                    raise ValueError
                result = datetime.fromisoformat(str(value).strip())
                if result.tzinfo is not None:
                    raise ValueError
                return result
            if isinstance(value, datetime):
                return value.date()
            return date.fromisoformat(str(value).strip())
        except ValueError:
            raise WarehouseImportError("日期或本地时间无效") from None
    if key in {"needs_test", "tested"}:
        if isinstance(value, bool):
            return value
        if _text(value) in {"是", "否"}:
            return _text(value) == "是"
        raise WarehouseImportError("状态应为是、否或空白")
    return _text(value)


def _positions(source, labels, mapping):
    result = {}
    for key, label in mapping.items():
        hits = [index for index, name in enumerate(labels) if name == label]
        # 返库样本第一次同名库位为编码，紧邻“入库仓库”右侧的同名列为可读库位。
        if source == "return" and key == "bin_name" and len(hits) in {1, 2}:
            hits = [i for i in hits if i > 0 and labels[i - 1] == "备件明细.入库仓库"]
        if len(hits) != 1:
            raise WarehouseImportError(f"缺列或重复列「{label}」")
        result[key] = hits[0]
    return result


def _read_fields(values, columns, labels):
    record = {}
    for key, index in columns.items():
        try:
            record[key] = _value(key, values[index])
        except WarehouseImportError as exc:
            raise WarehouseImportError(f"{labels[index]}：{exc}") from None
    return record


def _parse_sheet(sheet):
    source, head_fields, line_fields = SHEETS[sheet.title]
    if (sheet.max_column or 0) > config.IMPORT_XLSX_MAX_COLUMNS:
        raise WarehouseImportError("工作表列数超限")
    # 流式读取真实的 XML 行，Excel 保存的 dimension 可能过小。
    sheet.reset_dimensions()
    rows = enumerate(sheet.iter_rows(values_only=True), 1)
    _, first = next(rows, (0, ()))
    technical = first if "SeqNo" in first and "ObjectId" in first else None
    labels = next(rows, (0, ()))[1] if technical else first
    heads = _positions(source, labels, head_fields)
    lines = _positions(source, labels, line_fields)
    if technical:
        matched = (len(technical) == len(labels)
                   and technical[heads["number"]] == "SeqNo"
                   and technical[heads["source_id"]] == "ObjectId"
                   and technical[heads["source_modified_at"]] == "ModifiedTime"
                   and str(technical[lines["source_id"]]).endswith(".ObjectId"))
        if not matched:
            raise WarehouseImportError("技术表头与中文字段身份不一致")
    prefix = line_fields["source_id"].split(".")[0] + "."
    detail_columns = {i for i, label in enumerate(labels) if str(label).startswith(prefix)}
    head_columns = [i for i, label in enumerate(labels) if label and i not in detail_columns]
    documents = {}
    numbers = set()
    line_ids = set()
    active = None
    for rownum, row in rows:
        try:
            if rownum > config.IMPORT_MAX_ROWS or len(row) > config.IMPORT_XLSX_MAX_COLUMNS:
                raise WarehouseImportError("工作表行列数超限")
            if not any(value is not None for value in row):
                active = None
                continue
            values = row + (None,) * max(0, len(labels) - len(row))
            has_head = any(_text(values[i]) is not None for i in head_columns)
            has_detail = any(_text(values[i]) is not None for i in detail_columns)
            if has_head:
                head = _read_fields(values, heads, labels)
                for key in ("number", "source_id", "business_date", "source_modified_at", "status"):
                    if head[key] is None:
                        raise WarehouseImportError(f"单头缺少「{head_fields[key]}」")
                if head["source_id"] in documents or head["number"] in numbers:
                    raise WarehouseImportError("重复单头源 ID 或单号")
                numbers.add(head["number"])
                active = WarehouseRecord({"source": source, **head})
                documents[head["source_id"]] = active
            elif active is None:
                raise WarehouseImportError("明细缺少所属单头")
            if has_detail:
                detail = _read_fields(values, lines, labels)
                ident = detail["source_id"]
                if ident is None or ident in line_ids:
                    raise WarehouseImportError("明细源 ID 缺失或重复")
                line_ids.add(ident)
                active.lines[ident] = detail
            elif not has_head:
                raise WarehouseImportError("无法识别数据行")
        except WarehouseImportError as exc:
            raise WarehouseImportError(f"第 {rownum} 行：{exc}") from None
    return list(documents.values())


def parse_warehouse_workbook(path: Path) -> dict[str, list[WarehouseRecord]]:
    """解析和校验全部已确认来源，独立于数据库；只有明确的辅助页可跳过。"""
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    with Path(path).open("rb") as file:
        data = file.read(limit + 1)
    validate_xlsx_zip(data, max_bytes=limit)
    try:
        book = load_workbook(BytesIO(data), read_only=True, data_only=True, keep_links=False)
    except (BadZipFile, KeyError, ParseError, ValueError, OSError) as exc:
        raise WarehouseImportError("无法读取 .xlsx 工作簿结构") from exc
    parsed = {}
    try:
        for sheet in book:
            if sheet.title == "重点保留" or (sheet.sheet_state != "visible" and sheet.title.startswith("Hidden_Options_")):
                continue
            if sheet.title not in SHEETS:
                raise WarehouseImportError(f"尚未支持的工作表「{sheet.title}」")
            try:
                parsed[SHEETS[sheet.title][0]] = _parse_sheet(sheet)
            except WarehouseImportError as exc:
                raise WarehouseImportError(f"{sheet.title}：{exc}") from None
        if not parsed:
            raise WarehouseImportError("工作簿未含支持的仓单")
        return parsed
    finally:
        book.close()


def _chunks(items, size=400):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _check_line_identity(db, doc, line, source, records):
    # 先检查全批次，防止同一批先删除 A 单旧行、再把相同源行 ID 移到 B 单。
    owners = {ident: record.head["source_id"] for record in records for ident in record.lines}
    for ids in _chunks(list(owners)):
        matches = db.execute(
            select(line.c.source_id, doc.c.source_id)
            .join(doc, line.c.document_id == doc.c.id)
            .where(doc.c.source == source, line.c.source_id.in_(ids))
        )
        if any(owners[line_id] != head_id for line_id, head_id in matches):
            raise WarehouseImportError(f"{source}：已有明细源 ID 不能移动到另一张单据")


def _apply_batch(db, doc, line, source, records, counts):
    owners = {record.head["number"]: record.head["source_id"] for record in records}
    stored = db.execute(select(doc).where(
        doc.c.source == source,
        or_(doc.c.number.in_(list(owners)), doc.c.source_id.in_(list(owners.values()))),
    )).mappings()
    existing = {}
    for row in stored:
        if row["number"] in owners and owners[row["number"]] != row["source_id"]:
            raise WarehouseImportError(f"{source}：同一业务单号已属于其他源单头 ID")
        existing[row["source_id"]] = row
    old_lines = {row["id"]: {} for row in existing.values()}
    if old_lines:
        for row in db.execute(select(line).where(line.c.document_id.in_(old_lines))).mappings():
            old_lines[row["document_id"]][row["source_id"]] = row

    changes = []
    for record in records:
        head = record.head
        old = existing.get(head["source_id"])
        if old is not None:
            version = head["source_modified_at"]
            if version < old["source_modified_at"]:
                raise WarehouseImportError(f"{source}：源修改时间早于现存单据版本")
            if version == old["source_modified_at"]:
                previous = old_lines[old["id"]]
                head_equal = all(old[k] == v for k, v in head.items())
                lines_equal = previous.keys() == record.lines.keys() and all(
                    all(previous[ident][k] == v for k, v in row.items()) for ident, row in record.lines.items()
                )
                if not head_equal or not lines_equal:
                    raise WarehouseImportError(f"{source}：同一源修改时间的单据或明细内容冲突")
                counts["unchanged"] += 1
                continue
        counts["updated" if old is not None else "inserted"] += 1
        changes.append(record)
    if not changes:
        return

    heads = insert(doc).values([r.head for r in changes])
    keys = changes[0].head.keys() - {"source", "source_id"}
    heads = heads.on_conflict_do_update(
        index_elements=["source", "source_id"], set_={k: heads.excluded[k] for k in keys},
    ).returning(doc.c.source_id, doc.c.id)
    parents = dict(db.execute(heads).all())

    discarded = []
    rows = []
    for r in changes:
        parent_id = parents[r.head["source_id"]]
        previous = old_lines.get(parent_id, {})
        discarded.extend(previous[ident]["id"] for ident in previous.keys() - r.lines.keys())
        rows.extend({"document_id": parent_id, **values} for values in r.lines.values())
    for ids in _chunks(discarded):
        db.execute(delete(line).where(line.c.id.in_(ids)))
    for chunk in _chunks(rows):
        details = insert(line).values(chunk)
        keys = chunk[0].keys() - {"document_id", "source_id"}
        db.execute(details.on_conflict_do_update(
            index_elements=["document_id", "source_id"], set_={k: details.excluded[k] for k in keys},
        ))


def apply_warehouse_workbook(db: Session, parsed: dict[str, list[WarehouseRecord]]) -> dict[str, int]:
    """整份已解析工作簿原子写入，提交由调用方负责；异常会回滚本次全部来源与分批写入。"""
    from app.models.warehouse_analytics import WarehouseAnalyticsDocument, WarehouseAnalyticsLine

    doc = WarehouseAnalyticsDocument.__table__
    line = WarehouseAnalyticsLine.__table__
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    with db.begin_nested():
        # 仓单投影只有这一条离线写入路径。同一事务锁串行化新单、行身份检查与版本比较。
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('warehouse_analytics_import', 0))"))
        for source, records in parsed.items():
            _check_line_identity(db, doc, line, source, records)
            for batch in _chunks(records):
                _apply_batch(db, doc, line, source, batch, counts)
    return counts
