"""车道 B1：项目经理回款计划 .xls 专用 parser（Task 3 Step 3.2）。

机器合同：``.ai/contracts/maintenance-collections/project-manager-xls-v1.yaml``。
规则摘要：
- 只接受 ``.xls`` + OLE/BIFF magic；不信任 MIME。
- ``xlrd.open_workbook(on_demand=True, ragged_rows=True, formatting_info=False)``，
  每个 Sheet 先做资源预算，用后 ``unload_sheet``；第二张费用 Sheet 计入预算
  但不产生计划事实（只投影第 1 张）。
- 精确 64 列有序表头；header_signature（sha256(json compact array)）漂移整个
  文件失败关闭，不按相似列名猜测。
- 订单号只 trim 首尾空白，不改变大小写/内部空白/标点；``YYYY年M月`` 输出月初
  ``YYYY-MM`` 且 precision=month。
- 金额一律 ``Decimal(str(value))``（BIFF numeric 为 float，读后立即转 Decimal 字符串）；
  零/负/超上限/精度越界拒绝，绝不静默 round。
- 孤儿日期/金额、重复订单、序号断档、未知/合计行、多余列都是 blocker；
  计划合计与订单金额不一致是 warning。
- 颜色/格式（formatting_info=False）不参与状态；公式缓存只作为需人工确认的
  观察值读取，不宣称验证公式。
- 错误信息只给 sheet/row/field code 与原因，不回显业务值；row_key 为派生稳定键，
  绝不等于原始订单号。
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from dataclasses import dataclass, field
from decimal import Decimal

import xlrd

CONTRACT_VERSION = "project-manager-xls-v1"

# 64 列有序表头（与 YAML ordered_headers 逐字一致；由测试锁定签名）。
ORDERED_HEADERS: tuple[str, ...] = (
    "订单编号",
    "订单日期",
    "销售人员",
    "业务类型",
    "项目名称",
    "维保起始日期",
    "维保终止日期",
    "CMO",
    "项目经理",
    "订单金额",
    "已收尾款",
    "待收尾款",
    "验收材料",
    "验收材料是否完成及上传附件",
    "巡检时间",
    "巡检是否完成及上传附件",
) + tuple(
    label
    for _slot in range(1, 25)
    for label in (f"回款时间{_slot}", "回款金额")
)

# 合同冻结的 header_signature（json_array_utf8_ensure_ascii_false_compact + sha256）。
CONTRACT_HEADER_SIGNATURE = "eee2d1f5f67644d18ae3c2dadada6f9f2422a8545bc316bff9e01998e3b9c13e"

# resource_budget（机器合同，Step 3.2：预算从合同读取或由同一模块常量与合同测试锁定）。
MAX_FILE_SIZE_BYTES = 8 * 1024 * 1024
MAX_SHEET_COUNT = 8
MAX_ROWS_PER_SHEET = 2001
MAX_COLUMNS_PER_SHEET = 128
MAX_TOTAL_PHYSICAL_CELLS = 250_000
MAX_STRING_CHARS = 2048
MAX_FACT_ROWS = 2000
MAX_PLAN_NODES = 48_000

# sheet_selector：只读第一张，表头在第 1 行（1-based），数据从第 2 行开始。
FACT_SHEET_INDEX = 0
HEADER_ROW_ONE_BASED = 1
DATA_START_ROW_ONE_BASED = 2
EXPECTED_COLUMN_COUNT = 64

# field_contract：必填文本位（1-based）与 24 组付款槽位。
REQUIRED_TEXT_POSITIONS_ONE_BASED = (1, 5)
PAYMENT_SLOT_COUNT = 24
FIRST_DATE_POSITION_ONE_BASED = 17
FIRST_AMOUNT_POSITION_ONE_BASED = 18

# row_contract / plan_amount / plan_month。
ORDER_NO_MAX_CHARS = 128
MONTH_RE = re.compile(r"^(20[0-9]{2})年(1[0-2]|[1-9])月$")
MIN_MONTH_YEAR = 2000
MAX_MONTH_YEAR = 2099
AMOUNT_TEXT_RE = re.compile(r"^[0-9]+(?:\.[0-9]{1,2})?$")
MAX_AMOUNT_SCALE = 2
MIN_AMOUNT_EXCLUSIVE = Decimal("0")
MAX_AMOUNT_EXCLUSIVE = Decimal("1000000000000")

# OLE2 compound document 魔数与 raw BIFF8 BOF（version 0x0600）。
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_BIFF8_BOF_RECORD = b"\x09\x08"

_EMPTY = xlrd.XL_CELL_EMPTY
_TEXT = xlrd.XL_CELL_TEXT
_NUMBER = xlrd.XL_CELL_NUMBER


class CollectionPlanContractError(Exception):
    """合同/资源预算级失败关闭（API 层映射为 HTTP 422 invalid_request）。

    ``code`` 供错误体使用；``message`` 只含合同规则描述，绝不含业务值。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        sheet: str | None = None,
        row: int | None = None,
        field: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.sheet = sheet
        self.row = row
        self.field = field


@dataclass(frozen=True)
class ParsedPlanNode:
    """单个计划节点：月份精度固定 month，金额为十进制定点字符串。"""

    sequence: int
    planned_month: str  # YYYY-MM（月初）
    planned_amount: str  # 十进制定点字符串
    date_precision: str = "month"


@dataclass(frozen=True)
class ParsedPlanOrder:
    """一个外部订单的规范化计划（evidence-only 字段不参与绑定）。"""

    row_key: str
    external_order_no: str
    source_project_name: str
    order_amount: str | None  # validation-only（订单金额），仅用于合计警告
    plan_total: str
    nodes: tuple[ParsedPlanNode, ...]
    warning_codes: tuple[str, ...]
    blocker_codes: tuple[str, ...]


@dataclass(frozen=True)
class ParsedCollectionPlan:
    """parser 输出：JSON 可序列化、确定性；Decimal 一律为字符串。"""

    contract_version: str
    header_sha256: str
    semantic_hash: str
    requires_human_preview_confirmation: bool = True
    rows: tuple[ParsedPlanOrder, ...] = ()
    issues: tuple[dict, ...] = ()
    resource_metrics: dict = field(default_factory=dict)

    def plan_rows(self) -> list[dict]:
        """JSON 安全的规范化计划行（Decimal 以字符串进入 plan_json）。"""
        return [
            {
                "row_key": row.row_key,
                "external_order_no": row.external_order_no,
                "source_project_name": row.source_project_name,
                "order_amount": row.order_amount,
                "plan_total": row.plan_total,
                "nodes": [
                    {
                        "sequence": node.sequence,
                        "planned_month": node.planned_month,
                        "planned_amount": node.planned_amount,
                        "date_precision": node.date_precision,
                    }
                    for node in row.nodes
                ],
            }
            for row in self.rows
        ]


# ---------- 入口 ----------

def parse_project_manager_collection_xls(
    content: bytes, *, filename: str
) -> ParsedCollectionPlan:
    """解析项目经理回款计划 .xls（只读，零领域事实写入）。

    失败关闭（CollectionPlanContractError）：错误扩展名、非 BIFF、超文件/
    Sheet/行/列/物理单元格/字符串预算、表头签名漂移。行级数据质量问题以
    blocker/warning issue 返回，不抛出。
    """
    if not str(filename or "").lower().endswith(".xls"):
        raise CollectionPlanContractError(
            "unsupported_extension", "只接受 .xls 扩展名的工作簿"
        )
    if not content:
        raise CollectionPlanContractError("not_biff", "工作簿内容为空")
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise CollectionPlanContractError(
            "file_too_large", "工作簿超过文件大小安全上限"
        )
    if not _is_biff_magic(content):
        raise CollectionPlanContractError(
            "not_biff", "文件不是受支持的 OLE/BIFF 工作簿"
        )
    try:
        book = xlrd.open_workbook(
            file_contents=content,
            on_demand=True,
            ragged_rows=True,
            formatting_info=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise CollectionPlanContractError(
            "not_biff", "工作簿无法解析，可能是损坏的 BIFF 文件"
        ) from exc
    try:
        physical_cells = _budget_sheets(book)
        return _parse_fact_sheet(book, physical_cells=physical_cells)
    finally:
        try:
            book.release_resources()
        except Exception:  # noqa: BLE001
            pass


# ---------- magic / 预算 ----------

def _is_biff_magic(content: bytes) -> bool:
    if content.startswith(_OLE2_MAGIC):
        return True
    if content.startswith(_BIFF8_BOF_RECORD) and len(content) >= 6:
        length = struct.unpack("<H", content[2:4])[0]
        return length >= 4 and content[4:6] == b"\x00\x06"
    return False


def _budget_sheets(book) -> int:
    """逐 Sheet 资源预算（用后 unload_sheet）；返回物理单元格总数。

    预算从机器合同读取：Sheet 数、每 Sheet 行/列、总物理单元格、单格字符数。
    """
    if book.nsheets > MAX_SHEET_COUNT:
        raise CollectionPlanContractError(
            "sheet_count_budget", f"工作簿 Sheet 数超过 {MAX_SHEET_COUNT} 安全上限"
        )
    total_cells = 0
    for index in range(book.nsheets):
        sheet = book.sheet_by_index(index)
        if sheet.nrows > MAX_ROWS_PER_SHEET:
            raise CollectionPlanContractError(
                "rows_budget",
                f"Sheet 行数超过 {MAX_ROWS_PER_SHEET} 安全上限",
                sheet=sheet.name,
            )
        if sheet.ncols > MAX_COLUMNS_PER_SHEET:
            raise CollectionPlanContractError(
                "columns_budget",
                f"Sheet 列数超过 {MAX_COLUMNS_PER_SHEET} 安全上限",
                sheet=sheet.name,
            )
        cells = 0
        for rowx in range(sheet.nrows):
            for colx in range(sheet.ncols):
                ctype, value = _cell(book, sheet, rowx, colx)
                if ctype == _EMPTY:
                    continue
                cells += 1
                if ctype == _TEXT and len(value) > MAX_STRING_CHARS:
                    raise CollectionPlanContractError(
                        "string_budget",
                        f"单元格文本超过 {MAX_STRING_CHARS} 字符安全上限",
                        sheet=sheet.name,
                        row=rowx + 1,
                    )
        total_cells += cells
        if total_cells > MAX_TOTAL_PHYSICAL_CELLS:
            raise CollectionPlanContractError(
                "physical_cells_budget",
                f"工作簿物理单元格总数超过 {MAX_TOTAL_PHYSICAL_CELLS} 安全上限",
            )
        book.unload_sheet(index)
    return total_cells


def _cell(book, sheet, rowx: int, colx: int) -> tuple[int, object]:
    """ragged_rows 下越界读返回空单元格（xlrd 数组不补齐）。"""
    if book.ragged_rows and colx >= sheet.row_len(rowx):
        return (_EMPTY, "")
    return (sheet.cell_type(rowx, colx), sheet.cell_value(rowx, colx))


# ---------- 事实解析 ----------

def _parse_fact_sheet(book, *, physical_cells: int) -> ParsedCollectionPlan:
    sheet = book.sheet_by_index(FACT_SHEET_INDEX)
    header_sha256 = _header_signature(book, sheet)

    orders: list[ParsedPlanOrder] = []
    issues: list[dict] = []
    seen_orders: dict[str, str] = {}
    fact_rows = 0
    node_count = 0

    for rowx in range(DATA_START_ROW_ONE_BASED - 1, sheet.nrows):
        cells = [_cell(book, sheet, rowx, colx) for colx in range(sheet.ncols)]
        if _row_fully_empty(cells):
            continue
        fact_rows += 1
        if fact_rows > MAX_FACT_ROWS:
            raise CollectionPlanContractError(
                "fact_rows_budget",
                f"计划事实行数超过 {MAX_FACT_ROWS} 安全上限",
                sheet=sheet.name,
                row=rowx + 1,
            )
        order, row_issues = _parse_order_row(
            cells, sheet_name=sheet.name, row_number=rowx + 1
        )
        issues.extend(row_issues)
        if order.external_order_no in seen_orders:
            issues.append(
                _issue(
                    "duplicate_order",
                    "blocker",
                    "同一订单编号在本文件重复出现",
                    row_key=order.row_key,
                    sheet=sheet.name,
                    row=rowx + 1,
                    field="订单编号",
                )
            )
            order = _with_extra_blocker(order, "duplicate_order")
        else:
            seen_orders[order.external_order_no] = order.row_key
        orders.append(order)
        node_count += len(order.nodes)
        if node_count > MAX_PLAN_NODES:
            raise CollectionPlanContractError(
                "plan_nodes_budget",
                f"计划节点总数超过 {MAX_PLAN_NODES} 安全上限",
            )

    semantic_hash = _semantic_hash(orders)
    return ParsedCollectionPlan(
        contract_version=CONTRACT_VERSION,
        header_sha256=header_sha256,
        semantic_hash=semantic_hash,
        requires_human_preview_confirmation=True,
        rows=tuple(orders),
        issues=tuple(issues),
        resource_metrics={
            "sheets": book.nsheets,
            "fact_rows": fact_rows,
            "plan_nodes": node_count,
            "physical_cells": physical_cells,
        },
    )


def _header_signature(book, sheet) -> str:
    """第 1 行（1-based）64 个表头：全部必须是文本；签名漂移失败关闭。"""
    headers: list[str] = []
    for colx in range(EXPECTED_COLUMN_COUNT):
        ctype, value = _cell(book, sheet, HEADER_ROW_ONE_BASED - 1, colx)
        if ctype == _EMPTY:
            # 缺失表头按空标签参与签名比对 → 必然漂移失败关闭。
            headers.append("")
        elif ctype != _TEXT:
            raise CollectionPlanContractError(
                "header_type",
                "表头单元格必须是文本",
                sheet=sheet.name,
                row=HEADER_ROW_ONE_BASED,
                field=f"col_{colx + 1}",
            )
        else:
            headers.append(str(value))
    raw = json.dumps(
        headers, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    signature = hashlib.sha256(raw).hexdigest()
    if signature != CONTRACT_HEADER_SIGNATURE:
        raise CollectionPlanContractError(
            "header_signature_mismatch",
            "表头签名与合同不符，文件失败关闭",
            sheet=sheet.name,
            row=HEADER_ROW_ONE_BASED,
        )
    return signature


def _row_fully_empty(cells: list[tuple[int, object]]) -> bool:
    return all(ctype == _EMPTY for ctype, _value in cells)


def _parse_order_row(
    cells: list[tuple[int, object]], *, sheet_name: str, row_number: int
) -> tuple[ParsedPlanOrder, list[dict]]:
    """解析一行 64 列事实行；行级数据质量问题以 blocker 返回。"""
    issues: list[dict] = []
    blocker_codes: list[str] = []

    def _block(code: str, message: str, *, field: str, sequence: int | None = None) -> None:
        issues.append(
            _issue(
                code,
                "blocker",
                message,
                sheet=sheet_name,
                row=row_number,
                field=field,
                sequence=sequence,
            )
        )
        blocker_codes.append(code)

    order_cell = _cell_at(cells, 0)
    project_cell = _cell_at(cells, 4)
    order_present = order_cell[0] == _TEXT and bool(str(order_cell[1]).strip())
    project_present = project_cell[0] == _TEXT and bool(str(project_cell[1]).strip())

    if not order_present and not project_present:
        # 未知/合计行：无法识别为事实行（或订单号单元格类型错误）。
        if order_cell[0] not in (_EMPTY, _TEXT):
            _block("order_no_type", "订单编号必须是文本单元格", field="订单编号")
        else:
            _block("unknown_row", "无法识别为计划事实行", field="订单编号")
        return _blocked_order_with_codes(
            _row_key(""), "", "", blocker_codes
        ), issues
    if not order_present:
        if order_cell[0] not in (_EMPTY, _TEXT):
            _block("order_no_type", "订单编号必须是文本单元格", field="订单编号")
        else:
            _block("order_no_empty", "订单编号去除首尾空白后不能为空", field="订单编号")
        return _blocked_order_with_codes(
            _row_key(str(order_cell[1]) if order_cell[0] == _TEXT else ""),
            str(order_cell[1]).strip() if order_cell[0] == _TEXT else "",
            str(project_cell[1]).strip() if project_cell[0] == _TEXT else "",
            blocker_codes,
        ), issues
    if not project_present:
        _block("missing_required_text", "必填文本位（项目名称）缺失", field="项目名称")
        return _blocked_order_with_codes(
            _row_key(str(order_cell[1])),
            str(order_cell[1]).strip(),
            "",
            blocker_codes,
        ), issues

    order_no = str(order_cell[1]).strip()
    project_name = str(project_cell[1]).strip()
    row_key = _row_key(order_no)
    if len(order_no) > ORDER_NO_MAX_CHARS:
        _block("order_no_too_long", "订单编号超过 128 字符上限", field="订单编号")
        return _blocked_order_with_codes(
            row_key, order_no, project_name, blocker_codes
        ), issues

    # 多余列（第 65 列起 0-based）非空 → 超出合同 → 拒绝。
    for colx in range(EXPECTED_COLUMN_COUNT, len(cells)):
        if cells[colx][0] != _EMPTY:
            _block("excess_columns", "数据行存在合同之外的列", field=f"col_{colx + 1}")
            break

    nodes: list[ParsedPlanNode] = []
    present_pairs: list[int] = []
    for slot in range(PAYMENT_SLOT_COUNT):
        date_idx = FIRST_DATE_POSITION_ONE_BASED - 1 + slot * 2
        amount_idx = FIRST_AMOUNT_POSITION_ONE_BASED - 1 + slot * 2
        date_cell = _cell_at(cells, date_idx)
        amount_cell = _cell_at(cells, amount_idx)
        sequence = slot + 1
        date_label = f"回款时间{sequence}"
        amount_label = f"回款金额{sequence}"
        date_value, date_blocked = _parse_date_cell(
            date_cell, _block, field=date_label, sequence=sequence
        )
        parsed_amount, amount_blocked = _parse_amount_cell(
            amount_cell, _block, field=amount_label, sequence=sequence
        )
        if date_blocked or amount_blocked:
            continue
        if date_value is None and parsed_amount is None:
            continue
        if date_value is None:
            _block("orphan_amount", "回款金额缺少对应的回款时间", field=amount_label, sequence=sequence)
            continue
        if parsed_amount is None:
            _block("orphan_date", "回款时间缺少对应的回款金额", field=date_label, sequence=sequence)
            continue
        present_pairs.append(sequence)
        nodes.append(
            ParsedPlanNode(
                sequence=sequence,
                planned_month=date_value,
                planned_amount=parsed_amount,
            )
        )

    if not blocker_codes and present_pairs and present_pairs != list(
        range(1, present_pairs[-1] + 1)
    ):
        _block("sequence_gap", "回款期次存在断档", field="回款时间")

    if blocker_codes:
        # 行级阻断：保留已通过校验的节点供预览展示，阻断订单不可应用。
        return (
            ParsedPlanOrder(
                row_key=row_key,
                external_order_no=order_no,
                source_project_name=project_name,
                order_amount=None,
                plan_total=(
                    _decimal_sum(node.planned_amount for node in nodes)
                    if nodes
                    else "0.00"
                ),
                nodes=tuple(nodes),
                warning_codes=(),
                blocker_codes=tuple(blocker_codes),
            ),
            issues,
        )

    plan_total = _decimal_sum(node.planned_amount for node in nodes)
    warning_codes: list[str] = []
    order_amount = _parse_order_amount(cells, issues=issues, row_key=row_key,
                                       sheet_name=sheet_name, row_number=row_number,
                                       warning_codes=warning_codes)
    if order_amount is not None and order_amount != plan_total:
        issues.append(
            _issue(
                "plan_total_mismatch",
                "warning",
                "计划合计与订单金额不一致，请人工核对",
                row_key=row_key,
                sheet=sheet_name,
                row=row_number,
                field="订单金额",
            )
        )
        warning_codes.append("plan_total_mismatch")

    return (
        ParsedPlanOrder(
            row_key=row_key,
            external_order_no=order_no,
            source_project_name=project_name,
            order_amount=order_amount,
            plan_total=plan_total,
            nodes=tuple(nodes),
            warning_codes=tuple(warning_codes),
            blocker_codes=(),
        ),
        issues,
    )


def _parse_date_cell(cell, block, *, field: str, sequence: int) -> tuple[str | None, bool]:
    """月份单元格：只接受文本 ``YYYY年M月``；输出月初 YYYY-MM。"""
    ctype, value = cell
    if ctype == _EMPTY or (ctype == _TEXT and not str(value).strip()):
        return None, False
    if ctype != _TEXT:
        block("invalid_month_type", "回款时间必须是文本单元格", field=field, sequence=sequence)
        return None, True
    match = MONTH_RE.match(str(value).strip())
    if match is None:
        block("invalid_month", "回款月份格式必须为 YYYY年M月", field=field, sequence=sequence)
        return None, True
    year = int(match.group(1))
    month = int(match.group(2))
    if not MIN_MONTH_YEAR <= year <= MAX_MONTH_YEAR:
        block("invalid_month", "回款月份年份超出合同范围", field=field, sequence=sequence)
        return None, True
    return f"{year:04d}-{month:02d}", False


def _parse_amount_cell(cell, block, *, field: str, sequence: int) -> tuple[str | None, bool]:
    """金额单元格：接受 number / text_decimal；Decimal(str()) 后校验，不静默 round。"""
    ctype, value = cell
    if ctype == _EMPTY or (ctype == _TEXT and not str(value).strip()):
        return None, False
    if ctype == _TEXT:
        text = str(value).strip()
        if AMOUNT_TEXT_RE.match(text) is None:
            block("amount_format", "回款金额必须是十进制定点文本", field=field, sequence=sequence)
            return None, True
        amount = Decimal(text)
    elif ctype == _NUMBER:
        # BIFF numeric 由 xlrd 暴露为 float：立即 Decimal(str(value))，不做浮点运算。
        amount = Decimal(str(value))
    else:
        block("amount_format", "回款金额单元格类型不受支持", field=field, sequence=sequence)
        return None, True
    if amount <= MIN_AMOUNT_EXCLUSIVE:
        block("non_positive_amount", "回款金额必须大于零", field=field, sequence=sequence)
        return None, True
    if amount >= MAX_AMOUNT_EXCLUSIVE:
        block("amount_oversized", "回款金额超出合同上限", field=field, sequence=sequence)
        return None, True
    if -amount.as_tuple().exponent > MAX_AMOUNT_SCALE:
        block("amount_scale", "回款金额小数位超过两位，禁止静默舍入", field=field, sequence=sequence)
        return None, True
    return format(amount, "f"), False


def _parse_order_amount(
    cells, *, issues: list[dict], row_key: str, sheet_name: str, row_number: int, warning_codes: list[str]
) -> str | None:
    """订单金额（validation-only）：只用于计划合计警告，不进入任何事实。"""
    ctype, value = _cell_at(cells, 9)
    if ctype == _EMPTY or (ctype == _TEXT and not str(value).strip()):
        return None
    try:
        if ctype == _TEXT:
            text = str(value).strip()
            if AMOUNT_TEXT_RE.match(text) is None:
                raise ValueError
            amount = Decimal(text)
        elif ctype == _NUMBER:
            amount = Decimal(str(value))
        else:
            raise ValueError
    except (ValueError, ArithmeticError):
        issues.append(
            _issue(
                "order_amount_invalid",
                "warning",
                "订单金额不是有效的十进制定点数值，无法比对计划合计",
                row_key=row_key,
                sheet=sheet_name,
                row=row_number,
                field="订单金额",
            )
        )
        warning_codes.append("order_amount_invalid")
        return None
    return format(amount, "f")


# ---------- 稳定派生 ----------

def _row_key(order_no: str) -> str:
    """稳定 row_key：绝不等于原始订单号（只存派生哈希）。"""
    raw = f"collection-plan-row:{order_no}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _with_extra_blocker(order: ParsedPlanOrder, code: str) -> ParsedPlanOrder:
    return ParsedPlanOrder(
        row_key=order.row_key,
        external_order_no=order.external_order_no,
        source_project_name=order.source_project_name,
        order_amount=order.order_amount,
        plan_total=order.plan_total,
        nodes=order.nodes,
        warning_codes=order.warning_codes,
        blocker_codes=tuple([*order.blocker_codes, code]),
    )


def _blocked_order_with_codes(
    row_key: str, order_no: str, project_name: str, blocker_codes: list[str]
) -> ParsedPlanOrder:
    return ParsedPlanOrder(
        row_key=row_key,
        external_order_no=order_no,
        source_project_name=project_name,
        order_amount=None,
        plan_total="0.00",
        nodes=(),
        warning_codes=(),
        blocker_codes=tuple(blocker_codes),
    )


def _semantic_hash(orders: list[ParsedPlanOrder]) -> str:
    """规范化摘要：字段名 + 类型标签 + 长度前缀 + 规范化值，顺序稳定。"""
    parts: list[str] = []
    parts.append("|".join(("contract_version", "text", str(len(CONTRACT_VERSION)), CONTRACT_VERSION)))
    for order in orders:
        parts.append("|".join(("order_no", "text", str(len(order.external_order_no)), order.external_order_no)))
        order_amount = order.order_amount or ""
        parts.append("|".join(("order_amount", "amount", str(len(order_amount)), order_amount)))
        for node in order.nodes:
            parts.append("|".join(("planned_month", "month", str(len(node.planned_month)), node.planned_month)))
            parts.append("|".join(("planned_amount", "amount", str(len(node.planned_amount)), node.planned_amount)))
    raw = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _decimal_sum(values) -> str:
    total = Decimal("0")
    for value in values:
        total += Decimal(value)
    return format(total, "f")


def _cell_at(cells: list[tuple[int, object]], colx: int) -> tuple[int, object]:
    if colx < len(cells):
        return cells[colx]
    return (_EMPTY, "")


def _issue(
    code: str,
    severity: str,
    message: str,
    *,
    row_key: str | None = None,
    sequence: int | None = None,
    sheet: str | None = None,
    row: int | None = None,
    field: str | None = None,
) -> dict:
    return {
        "code": code,
        "severity": severity,
        "row_key": row_key,
        "sequence": sequence,
        "message": message,
        "sheet": sheet,
        "row": row,
        "field": field,
    }
