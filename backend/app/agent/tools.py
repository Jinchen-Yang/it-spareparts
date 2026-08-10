"""智能体工具层：把一期查询服务包成 LLM 工具（OpenAI function 格式）。

设计原则：
- 工具结果只来自库内真实数据；异常包成 {"error": ...} 让模型自恢复（换词重搜/向用户澄清）。
- 所有调用过 record_access_log，但只审计能力名与参数形状，不复制客户/模型提供的参数值。
- 输出过 apply_field_visibility（RBAC 关闭时原样；将来收紧销售/采购可见字段零改动）。
"""
import hashlib
import ipaddress
import json
import logging
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from types import MappingProxyType
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, security
from app.agent import limits, skills
from app.config import get_settings
from app.services import (agent_files, inventory, maintenance_cost, part_overview,
                          part_resolver, profit, purchase_analysis, purchase_query)

_log = logging.getLogger("agent")

# 各工具入参上限的单一真值源（TOOLS-5）：clamp 与工具描述都引用这些常量，
# 避免"描述写最大 20、代码 clamp 到别的值"两处漂移。clamp 本身仍保留（不信模型输入）。
_RANK_ROWS = 50            # get_profit_ranking 返回行上限
_BULK_MAX = 60             # lookup_prices_bulk 单次型号数上限
_SEARCH_LIMIT_MAX = 20     # search_parts 返回条数上限
_RECENT_LIMIT_MAX = 50     # list_recent_purchases 返回条数上限
_RECENT_DAYS_MAX = 365     # list_recent_purchases 时间窗上限（天）
_READ_ROWS_MAX = agent_files._MAX_READ_ROWS   # read_file_rows 行数上限（真值源在 agent_files）
_WRITE_CELLS_MAX = agent_files._MAX_WRITE_CELLS
_REPORT_ROWS_MAX = agent_files._MAX_REPORT_ROWS

_QUERY_CHARS_MAX = 500
_PN_CHARS_MAX = 256
_FILE_ID_CHARS_MAX = 64
_SHEET_CHARS_MAX = 128
_OUTPUT_NAME_CHARS_MAX = 255
_PROJECT_CHARS_MAX = 255
_FILTER_CHARS_MAX = 500
_SKILL_ID_CHARS_MAX = 128
_REPORT_COLUMNS_MAX = 64
_REPORT_CELL_CHARS_MAX = 2_000
_SCALAR_CELL_SCHEMA = {"type": ["string", "integer", "number", "boolean", "null"]}

# Backward-compatible module projections; the single enforcement source is ``agent.limits``.
MAX_PUBLIC_TRACE_ENTRIES = limits.MAX_PUBLIC_TRACE_ENTRIES
MAX_ARTIFACT_IDS_PER_TRACE_ENTRY = limits.MAX_ARTIFACT_IDS_PER_TRACE_ENTRY
MAX_PUBLIC_ARG_KEYS = limits.MAX_PUBLIC_ARG_KEYS


class ToolEffect(str, Enum):
    """Capability effects allowed in the read-only Agent boundary."""

    BUSINESS_READ = "business_read"
    FILE_READ = "file_read"
    ARTIFACT_CREATE = "artifact_create"


class DataSensitivity(str, Enum):
    """Highest-sensitivity data a capability can place in a provider context."""

    INTERNAL = "internal"
    BUSINESS_CONFIDENTIAL = "business_confidential"
    CUSTOMER_FILE = "customer_file"


class EgressSource(str, Enum):
    """Logical payload source on one declared egress edge."""

    CONVERSATION_CONTEXT = "conversation_context"
    TOOL_RESULT = "tool_result"
    CUSTOMER_FILE = "customer_file"
    VISION_OCR = "vision_ocr"


class EgressDestination(str, Enum):
    """Independently configured provider trust zones."""

    PRIMARY_MODEL = "primary_model"
    VISION_PROVIDER = "vision_provider"


EGRESS_POLICY_VERSION = "egress-v1"
RETENTION_NO_ADDITIONAL_EGRESS_ARCHIVE = "no_additional_egress_archive_v1"
ALLOWED_RETENTION_POLICIES = frozenset({RETENTION_NO_ADDITIONAL_EGRESS_ARCHIVE})
PURPOSE_BUSINESS_ASSISTANCE = "business_assistance"
PURPOSE_DOCUMENT_ASSISTANCE = "document_assistance"
PURPOSE_CONVERSATION_ASSISTANCE = "conversation_assistance"
PROJECTION_TOOL_RESULT_JSON = "tool_result_json_v1"
PROJECTION_CUSTOMER_FILE_RESULT_JSON = "customer_file_result_json_v1"
PROJECTION_CONVERSATION_JSON = "conversation_messages_json_v1"
PROJECTION_VISION_INPUT = "vision_input_binary_v1"
PROJECTION_VISION_OCR_RESULT = "vision_ocr_result_json_v1"
BUSINESS_RESULT_MAX_BYTES = limits.BUSINESS_RESULT_MAX_BYTES
CUSTOMER_FILE_RESULT_MAX_BYTES = limits.CUSTOMER_FILE_RESULT_MAX_BYTES
CONVERSATION_CONTEXT_MAX_BYTES = limits.CONVERSATION_CONTEXT_MAX_BYTES
VISION_INPUT_MAX_BYTES = limits.VISION_INPUT_MAX_BYTES
VISION_OCR_RESULT_MAX_BYTES = limits.VISION_OCR_RESULT_MAX_BYTES


@dataclass(frozen=True)
class EgressEdge:
    """One immutable data-flow edge evaluated before a capability is exposed or run."""

    source: EgressSource
    destination: EgressDestination
    sensitivity: DataSensitivity
    purpose: str
    projection_id: str
    max_bytes: int
    policy_version: str
    retention_policy: str


EGRESS_EDGE_CONTRACT_REGISTRY = frozenset({
    # Read-only business/system results returned to the primary assistant model.
    EgressEdge(
        source=EgressSource.TOOL_RESULT,
        destination=EgressDestination.PRIMARY_MODEL,
        sensitivity=DataSensitivity.INTERNAL,
        purpose=PURPOSE_BUSINESS_ASSISTANCE,
        projection_id=PROJECTION_TOOL_RESULT_JSON,
        max_bytes=BUSINESS_RESULT_MAX_BYTES,
        policy_version=EGRESS_POLICY_VERSION,
        retention_policy=RETENTION_NO_ADDITIONAL_EGRESS_ARCHIVE,
    ),
    EgressEdge(
        source=EgressSource.TOOL_RESULT,
        destination=EgressDestination.PRIMARY_MODEL,
        sensitivity=DataSensitivity.BUSINESS_CONFIDENTIAL,
        purpose=PURPOSE_BUSINESS_ASSISTANCE,
        projection_id=PROJECTION_TOOL_RESULT_JSON,
        max_bytes=BUSINESS_RESULT_MAX_BYTES,
        policy_version=EGRESS_POLICY_VERSION,
        retention_policy=RETENTION_NO_ADDITIONAL_EGRESS_ARCHIVE,
    ),
    # Customer-file projections have a distinct purpose, projection and byte ceiling.
    EgressEdge(
        source=EgressSource.CUSTOMER_FILE,
        destination=EgressDestination.PRIMARY_MODEL,
        sensitivity=DataSensitivity.CUSTOMER_FILE,
        purpose=PURPOSE_DOCUMENT_ASSISTANCE,
        projection_id=PROJECTION_CUSTOMER_FILE_RESULT_JSON,
        max_bytes=CUSTOMER_FILE_RESULT_MAX_BYTES,
        policy_version=EGRESS_POLICY_VERSION,
        retention_policy=RETENTION_NO_ADDITIONAL_EGRESS_ARCHIVE,
    ),
    # All conversation history is conservatively customer-file sensitive in v1.
    EgressEdge(
        source=EgressSource.CONVERSATION_CONTEXT,
        destination=EgressDestination.PRIMARY_MODEL,
        sensitivity=DataSensitivity.CUSTOMER_FILE,
        purpose=PURPOSE_CONVERSATION_ASSISTANCE,
        projection_id=PROJECTION_CONVERSATION_JSON,
        max_bytes=CONVERSATION_CONTEXT_MAX_BYTES,
        policy_version=EGRESS_POLICY_VERSION,
        retention_policy=RETENTION_NO_ADDITIONAL_EGRESS_ARCHIVE,
    ),
    # Vision input and its OCR result are separate, explicitly admitted boundaries.
    EgressEdge(
        source=EgressSource.CUSTOMER_FILE,
        destination=EgressDestination.VISION_PROVIDER,
        sensitivity=DataSensitivity.CUSTOMER_FILE,
        purpose=PURPOSE_DOCUMENT_ASSISTANCE,
        projection_id=PROJECTION_VISION_INPUT,
        max_bytes=VISION_INPUT_MAX_BYTES,
        policy_version=EGRESS_POLICY_VERSION,
        retention_policy=RETENTION_NO_ADDITIONAL_EGRESS_ARCHIVE,
    ),
    EgressEdge(
        source=EgressSource.VISION_OCR,
        destination=EgressDestination.PRIMARY_MODEL,
        sensitivity=DataSensitivity.CUSTOMER_FILE,
        purpose=PURPOSE_DOCUMENT_ASSISTANCE,
        projection_id=PROJECTION_VISION_OCR_RESULT,
        max_bytes=VISION_OCR_RESULT_MAX_BYTES,
        policy_version=EGRESS_POLICY_VERSION,
        retention_policy=RETENTION_NO_ADDITIONAL_EGRESS_ARCHIVE,
    ),
})


@dataclass(frozen=True, slots=True)
class ProviderProfileSnapshot:
    """Immutable, non-secret request authority captured from one Settings object."""

    destination: EgressDestination
    adapter: str
    origin: str
    base_path: str
    model: str
    request_options_json: str
    timeout_seconds: float
    max_retries: int
    max_tokens: int | None
    max_pages: int | None
    enabled: bool
    admitted: bool

    @property
    def base_url(self) -> str:
        return f"{self.origin}{self.base_path}"


@dataclass(frozen=True, slots=True)
class RuntimePolicyLease:
    """One Agent-run policy epoch, including exact primary/Vision profile snapshots.

    API keys are kept only in process memory, excluded from repr/equality/fingerprints, and never
    copied into telemetry. Capturing them with the profile removes a settings TOCTOU while each
    guarded HTTP attempt still checks that the live credential has not changed.
    """

    fingerprint: str
    primary: ProviderProfileSnapshot
    vision: ProviderProfileSnapshot
    max_tool_iters: int
    primary_api_key: str = field(repr=False, compare=False)
    vision_api_key: str = field(repr=False, compare=False)


ALLOWED_TOOL_EFFECTS = frozenset(ToolEffect)
ALLOWED_EGRESS_SOURCES = frozenset(EgressSource)
ALLOWED_EGRESS_DESTINATIONS = frozenset(EgressDestination)
ALLOWED_DATA_SENSITIVITIES = frozenset(DataSensitivity)
_SENSITIVITY_RANK = MappingProxyType({
    DataSensitivity.INTERNAL: 0,
    DataSensitivity.BUSINESS_CONFIDENTIAL: 1,
    DataSensitivity.CUSTOMER_FILE: 2,
})
STABLE_SUBJECT_EFFECTS = frozenset({ToolEffect.FILE_READ, ToolEffect.ARTIFACT_CREATE})
ToolHandler = Callable[..., dict]
ToolPermission = Callable[[security.UserContext], bool]


@dataclass(frozen=True)
class ToolBudget:
    """Immutable per-capability argument budget.

    Field-specific limits use tuples instead of dicts so a frozen ``ToolSpec`` cannot retain a
    mutable policy object. Generic payload/depth/node limits also constrain free-form cell values.
    """

    max_payload_bytes: int
    max_depth: int
    max_nodes: int
    max_any_string_chars: int
    max_query_chars: int | None = None
    max_pn_chars: int | None = None
    max_limit: int | None = None
    max_days: int | None = None
    max_page: int | None = None
    max_rows: int | None = None
    max_items: int | None = None
    max_cells: int | None = None
    max_sheets: int | None = None
    max_output_name_chars: int | None = None
    max_sheet_name_chars: int | None = None
    max_columns: int | None = None
    string_limits: tuple[tuple[str, int], ...] = ()
    integer_ranges: tuple[tuple[str, int, int], ...] = ()
    collection_limits: tuple[tuple[str, int], ...] = ()
    collection_string_limits: tuple[tuple[str, int], ...] = ()
    row_width_limits: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class ToolValidationFailure:
    code: str
    message: str


ToolArgumentValidator = Callable[[object, dict, ToolBudget], ToolValidationFailure | None]


@dataclass(frozen=True)
class ToolSpec:
    """Single registration unit for one model-visible server capability."""

    schema: Mapping[str, object]
    handler: ToolHandler
    effects: frozenset[ToolEffect]
    egress: tuple[EgressEdge, ...]
    sensitivity: DataSensitivity
    permission_id: str
    permission: ToolPermission
    enabled: bool
    budget: ToolBudget
    validator: ToolArgumentValidator
    implementation_version: str

    @property
    def name(self) -> str:
        function = self.schema.get("function")
        if not isinstance(function, Mapping):
            return ""
        name = function.get("name")
        return name if isinstance(name, str) else ""


# OpenAI wire schemas are definitions only. Registration happens once in TOOL_SPECS below;
# TOOLS and _REGISTRY remain compatibility projections for existing callers/tests.
_OPENAI_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_parts",
            "description": (
                "搜索备件，返回按匹配度排序的候选。四种查询方式都支持：①型号 PN（容错连字符/"
                "大小写/后缀差异/历史别名，如 4089RT vs 4089-RT）；②整段标准描述（如 '8TB 6Gb/s "
                "7.2K 256MB Cache 3.5-inch SATA HDD'——找同描述的所有型号）；③规格词组合（如 "
                "'8TB 7.2K SATA' 或 'Seagate 8TB'，词序无关、全部命中即返回；写法差异自动互通："
                "6Gbps=6Gb/s、3.5寸=3.5inch=3.5-inch、7200rpm=7.2K、8T=8TB）；④品牌/描述关键词。"
                "每条带 score(0~1)与 match_reason。low_confidence=true 表示没有可靠匹配，"
                "此时应把候选列给用户确认，不要擅自选择；同规格多品牌会轮播展示，指定品牌请把"
                "品牌词加进查询。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "用户原话中的型号或描述，如 'super 4089RT-x 准系统'"},
                    "limit": {"type": "integer",
                              "description": f"返回条数，默认 10，最大 {_SEARCH_LIMIT_MAX}"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_part_overview",
            "description": (
                "获取某型号的完整全景：基本信息(描述/品牌/品类)、近20单采购(供应商/单价，含税口径"
                "跟随订单标识)、近20单销售(客户/单价)、分仓库存、**替代料（通用号自动成组：查任一"
                "号能看到组内全部互替号，每个号带当前库存合计——缺货时用它找可替代供货）**、"
                "两种成本法(移动加权/FIFO)平均成本与毛利率、历史询价区间、近90天销售速率。"
                "报价、采购压价、解释型号、找替代都以此为依据。pn_std 必须是 search_parts 返回的准确值。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"pn_std": {"type": "string", "description": "标准型号，精确值"}},
                "required": ["pn_std"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_file",
            "description": (
                "查看用户上传的 xlsx 结构：sheet 列表 + 每个 sheet 前几行原样数据(1-based 行号)。"
                "客户文件格式千变万化——由你自己判断表头在第几行、哪列是型号、哪列是数量，"
                "不要假设固定格式。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"file_id": {"type": "string", "description": "上传时返回的 file_id"}},
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file_rows",
            "description": "分页读取 xlsx 行数据（1-based）。先 inspect_file 看结构再按需读取全部数据行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "sheet": {"type": "string", "description": "sheet 名，省略=第一个"},
                    "start_row": {"type": "integer", "description": "起始行(1-based)，默认1"},
                    "max_rows": {"type": "integer",
                                 "description": f"读取行数，默认50，最大{_READ_ROWS_MAX}"},
                },
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_prices_bulk",
            "description": (
                "批量查价（询价单场景核心工具）：对每个型号文本做近似解析并返回 最近采购价/日期、"
                "近期加权成交参考价(ref_sale_price)、近90天均售价、库存合计。每项带 status：ok=唯一命中已附价格；ambiguous=多规格变体"
                "（带候选列表，需逐项给用户确认或在结果中标注）；not_found=没找到。"
                f"一次最多 {_BULK_MAX} 个，超过请分批。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "queries": {"type": "array", "items": {"type": "string"},
                                "description": "型号文本数组（可以是客户原话写法）"},
                },
                "required": ["queries"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_excel",
            "description": (
                "写 Excel 并生成下载文件（绝不改写原上传件，总是产出新 file_id）。"
                "两种用法：①回填客户模板：传 base_file_id，在其副本上写（如在右侧空列追加"
                "'最近采购价/库存/备注'列头和数据，原格式保留）；②新建报价单：不传 base_file_id，"
                "自己规划表头和数据行。cells 用 1-based 行号 + 列字母(如 'G')或数字。"
                "完成后把返回的 download_url 告诉用户。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "base_file_id": {"type": "string", "description": "基于哪个上传文件回填，可省略"},
                    "sheet": {"type": "string", "description": "sheet 名，省略=第一个；不存在则新建"},
                    "cells": {
                        "type": "array",
                        "minItems": 1,
                        "description": "[{row:3, col:'G', value:1700}, ...] 最多3000个",
                        "items": {
                            "type": "object",
                            "properties": {
                                "row": {"type": "integer", "minimum": 1, "maximum": 1_048_576},
                                "col": {"type": ["string", "integer"]},
                                "value": _SCALAR_CELL_SCHEMA,
                            },
                            "required": ["row", "col", "value"],
                        },
                    },
                    "output_name": {"type": "string", "description": "下载文件名，如 '报价单-XX公司.xlsx'"},
                },
                "required": ["cells"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": (
                "只在本机读取上传文件内容（Word/PDF/txt/Excel/图片均可），不会调用外部视觉服务。"
                "图片或扫描 PDF 返回 requires_vision=true；只有显式可见的 "
                "read_document_with_vision 才能做外部视觉识别。整机配置拆解场景先用本工具："
                "拿到文本后你自己判断里面有哪些设备/部件，按 品牌+型号+规格+数量 拆成清单。"
                "Excel 若要按行列精确定位用 inspect_file/read_file_rows，要整体内容用本工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"file_id": {"type": "string"}},
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document_with_vision",
            "description": (
                "对 requires_vision=true 的本人图片/扫描 PDF 使用外部视觉供应商识别。"
                "这是显式客户文件外发能力：只有部署已声明模型信任区并授权外部文件外发时才可见。"
                "先调用 read_document；普通文字文件不要调用本工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"file_id": {"type": "string"}},
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_report",
            "description": (
                "生成**美化** Excel 报表并返回下载链接（表头配色、边框、自适应列宽、金额格式、"
                "冻结表头、斑马纹；备注含'需确认'/'未找到'的行自动标橙/红）。"
                "整机拆解报价单、批量查价结果等用它（比 write_excel 好看）。"
                "headers=列名数组；rows=与列对齐的二维数组（每行一个数组）；money_cols=金额列的"
                "0基下标数组（会按千分位+两位小数格式化）。完成后把 download_url 告诉用户。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "报表标题，可省略，如 'XX公司整机配置报价单'"},
                    "headers": {"type": "array", "minItems": 1,
                                "items": {"type": "string"},
                                "description": "列名，如 ['序号','部件','品牌','型号','数量','匹配PN','近15天采购均价','库存','近期成交参考价','备注']"},
                    "rows": {"type": "array", "items": {
                                "type": "array", "items": _SCALAR_CELL_SCHEMA},
                             "description": "数据行，每行一个数组，顺序与 headers 对齐"},
                    "money_cols": {"type": "array", "items": {"type": "integer"},
                                   "description": "金额列的 0 基下标，如 [6,8]"},
                    "output_name": {"type": "string", "description": "下载文件名"},
                },
                "required": ["headers", "rows"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recent_purchases",
            "description": (
                "查最近的采购记录（跨型号时间线，按采购日期倒序）：日期/供应商/型号/数量/单价"
                "（含 is_tax_inclusive 标识该单价是含税还是不含税）。"
                "回答'最近买了什么''XX 最近进价多少笔'这类问题用它；"
                "查某一个型号的完整行情仍用 get_part_overview。"
                f"query 可按 型号/描述/品牌 关键词过滤；days 默认 30 天，最大 {_RECENT_DAYS_MAX}。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "关键词过滤（型号/描述/品牌），可省略"},
                    "days": {"type": "integer",
                             "description": f"最近多少天，默认 30，最大 {_RECENT_DAYS_MAX}"},
                    "supplier": {"type": "string", "description": "供应商名过滤，可省略"},
                    "limit": {"type": "integer",
                              "description": f"返回条数，默认 20，最大 {_RECENT_LIMIT_MAX}"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_profit_ranking",
            "description": (
                "利润聚合排名（维度三选一：part=按型号 / salesperson=按销售员 / customer=按客户），"
                f"含营收、两种成本法的毛利与毛利率。按营收降序，最多返回前{_RANK_ROWS}行。可选日期范围过滤。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dimension": {"type": "string", "enum": ["part", "salesperson", "customer"]},
                    "date_from": {"type": "string", "description": "起始日期 YYYY-MM-DD，可选"},
                    "date_to": {"type": "string", "description": "截止日期 YYYY-MM-DD，可选"},
                },
                "required": ["dimension"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_purchase_analysis",
            "description": (
                "采购分析聚合（早会/批量采购计划的核心数据）：窗口内逐型号统计——采购次数/总量/"
                "价格区间与最近价(含税、不含税分列，均为单据原值)/价格趋势(up|down|flat|new)/"
                "来源渠道拆分(淘宝/京东/个人/正规供应商)/系统初筛建议(批量补库=频发、谈价=价格在涨、"
                "偶发)。KPI 含采购总额(含税/不含税双口径)、单数、来源构成。rows 按采购次数降序。"
                "「哪些型号该批量采购」「最近采购花了多少」「XX 采购价走势」用它。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "统计窗口天数，默认 30，最大 365"},
                    "q": {"type": "string", "description": "型号/描述关键词过滤，可省略"},
                    "supplier": {"type": "string", "description": "供应商名过滤，可省略"},
                    "top": {"type": "integer", "description": "返回型号行数，默认 20，最大 50"},
                    "exclude_designated": {"type": "boolean",
                                           "description": "是否排除「指定采购」大额补库单，默认 true（聚焦应急采购）"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_inventory",
            "description": (
                "分页查询库存（锚定动态口径=最近盘点/快照做期初+之后单据流水实时推算，型号级）："
                "按 型号/描述/品牌 关键词过滤，返回 动态可用数量/期初(锚点日)/期初后入库/出库(销售+维保)/"
                "分仓参考(快照行,含单位成本与库存金额)。「XX 还有多少库存」用它；"
                "单个型号的完整行情（含通用号库存）用 get_part_overview。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "型号/描述/品牌关键词，可省略"},
                    "limit": {"type": "integer", "description": "返回条数，默认 20，最大 50"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_maintenance_board",
            "description": (
                "维保合同预算消耗参考：备件先分实际采购参考/估算参考/成本缺失，再与生效报销费用组成"
                "已知支出参考。incomplete_cost=成本不完整；"
                "expense_data_unavailable=项目追踪工作簿报销明细尚未建立费用全量数据水位；两者的 "
                "remaining/remaining_pct 都为空，严禁自行推断红黄绿或盈亏；"
                "仅成本与费用数据都完整时才给 red=预算已用完或超预算、"
                "yellow=预算余量≤20%、green=预算余量>20%、no_budget=无正预算。"
                "同时返回合同级含税/未税收入、归一备件成本、备件毛利及毛利率；两套口径"
                "独立 fail closed。parts_profit_status_inc/ex 中 complete_estimated 必须标注"
                "含估算；missing_revenue/missing_tax_rate/invalid_tax_rate/"
                "ambiguous_revenue/incomplete_cost/filtered_scope 均不得把 null 当 0。"
                "合同级贡献毛利使用独立 contribution_profit/margin/status 字段；"
                "contribution_status_inc/ex 为 expense_data_unavailable 或"
                "expense_tax_unknown 时必须保持为空，不得包装成正式财务毛利。"
                "合同额、预算与余量仅按利润权限返回；无利润权限时不返回这些金额、决策状态、"
                "双口径毛利及其状态、状态计数或筛选结果，改按最近出库日期排列。"
                "需要项目成本页面权限。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": [
                            "incomplete_cost",
                            "expense_data_unavailable",
                            "red",
                            "yellow",
                            "green",
                            "no_budget",
                        ],
                        "description": "按预算消耗参考状态过滤，可省略=全部",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_maintenance_projects",
            "description": (
                "维保项目成本汇总（项目维度）：实际采购参考/估算参考按含税与不含税分列、"
                "缺失成本行、成本完整性、含税/未税归一备件成本、已知成本混合原值兼容参考、出库行数/数量/覆盖率/"
                "成本来源分布(direct=专属采购直配、window=±7天最近价、month_avg=当月均价、"
                "trace_avg=估算追溯均价、sales_ref=估算销售参考、"
                "pool_purchase/pool_sales=互通池同伴历史均价、"
                "purchase_history/sales_history=本PN历史参考、none=成本缺失)/关联销售订单与合同额参考。"
                "合同额参考仅按利润权限返回，空值不等于源数据缺失。"
                "无成本权限时按项目名排序后再截取 top，不按隐藏成本排名。"
                "需要项目成本页面权限。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "项目名关键词过滤，可省略"},
                    "top": {"type": "integer", "description": "返回项目数，默认 20，最大 50"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_maintenance_lines",
            "description": (
                "单个维保项目的出库明细（追单据用）：维保单号/日期/需求类型/仓库/PN/数量/退货/"
                "单价/金额/cost_tier(actual|estimated|missing)/原始成本来源与税口径/"
                "置信度(high|medium|low)/取价月/距采购天数/关联采购单/异常标记。"
                "cost_tier 是权威事实层级；missing 时单价和金额为空，原始来源/税口径仅供诊断，"
                "不得据此自行恢复金额或重判 actual/estimated。"
                "project 必须是 get_maintenance_projects 返回的准确项目名。需要项目成本页面权限。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "项目名，精确值"},
                    "month": {"type": "string", "description": "按月过滤 YYYY-MM，可省略"},
                    "page": {"type": "integer", "description": "页码，默认 1（每页 50 行）"},
                },
                "required": ["project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_cancellation_stats",
            "description": (
                "采购取消/作废统计（按月/季/年）：各期间总单数、取消单数、取消率、取消金额。"
                "判断采购流程质量与供应异常（某月取消率突增=供应/审批出问题）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "granularity": {"type": "string", "enum": ["month", "quarter", "year"],
                                    "description": "统计粒度，默认 month"},
                    "days": {"type": "integer", "description": "只统计最近 N 天，可省略=全部"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": (
                "列出你（按当前登录角色）可用的业务技能剧本：采购批量计划分析、老板经营速览、"
                "配件行情简报、维保成本与预算检查等。接到复杂/多步的业务任务时先看这里有没有现成打法。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_skill",
            "description": (
                "获取指定技能的完整剧本：你的身份背景、建议的数据获取路径（用哪些工具、拿到什么）、"
                "分析框架、输出建议。剧本是指导不是死流程——结合用户实际问题灵活裁剪。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"skill": {"type": "string",
                                         "description": "技能 id，来自 list_skills"}},
                "required": ["skill"],
            },
        },
    },
]


def _jsonable(data):
    """date/Decimal 等转 JSON 可序列化（工具结果要回灌给模型）。"""
    return json.loads(json.dumps(data, ensure_ascii=False, default=str, allow_nan=False))


def _parse_date(value: object) -> date | None:
    """Parse one optional canonical ISO date without silently broadening a query."""
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("invalid ISO date")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("invalid ISO date")
    return parsed


def _search_parts(db: Session, args: dict, ctx: security.UserContext) -> dict:
    q = str(args.get("query", "")).strip()
    if not q:
        return {"error": "query 不能为空"}
    limit = min(int(args.get("limit") or 10), _SEARCH_LIMIT_MAX)
    # Agent tools are declared BUSINESS_READ.  Resolver miss telemetry normally persists the
    # caller's raw query, which would both violate that effect contract and retain model/user
    # supplied customer text.  Dispatch already emits a value-free, shape-only audit event.
    return part_resolver.resolve(
        db,
        q,
        limit=limit,
        operated_by=ctx.role,
        log_miss=False,
    )


def _get_part_overview(db: Session, args: dict, ctx: security.UserContext) -> dict:
    pn = str(args.get("pn_std", "")).strip()
    data = part_overview.get_overview(db, pn, ctx)
    if data is None:
        return {"error": f"型号不存在: {pn}。请先用 search_parts 找到准确 pn_std。"}
    return security.apply_field_visibility(data, ctx)


def _get_profit_ranking(db: Session, args: dict, ctx: security.UserContext) -> dict:
    # 防恶性竞争：销售角色禁用按销售员/客户的排名（直接暴露同事经营数据）
    if security.is_scoped_sales(ctx):
        return {"error": "无权限：销售角色不能查看按客户/销售员的经营排名（防恶性竞争）。"
                          "可以问某个型号的行情价或你自己的成交。"}
    dim = args.get("dimension", "part")
    if dim not in ("part", "salesperson", "customer"):
        dim = "part"
    date_from = _parse_date(args.get("date_from"))
    date_to = _parse_date(args.get("date_to"))
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValueError("invalid date range")
    data = profit.aggregate(db, dim, date_from, date_to, False, ctx)
    rows = data.get("rows", [])
    if len(rows) > _RANK_ROWS:
        data = {**data, "rows": rows[:_RANK_ROWS],
                "note": f"共 {len(rows)} 行，仅返回营收前 {_RANK_ROWS} 行"}
    return security.apply_field_visibility(data, ctx)


def _owns(ctx: security.UserContext, file_id: str | None) -> bool:
    """Named files are owner-only for every role and require a stable authenticated subject."""
    if not file_id:
        return True
    if ctx.authn != "sys_user" or not ctx.user_id:
        return False
    try:
        owner = agent_files.owner_of(file_id)
    except agent_files.FileError:
        # Missing and other-owner IDs are intentionally indistinguishable.
        return False
    return bool(owner) and owner == ctx.user_id


_NO_ACCESS = {"error": "无权访问该文件（非本人上传/生成）"}


def _inspect_file(db: Session, args: dict, ctx: security.UserContext) -> dict:
    fid = str(args.get("file_id", ""))
    if not _owns(ctx, fid):
        return _NO_ACCESS
    return agent_files.inspect_file(fid)


def _read_file_rows(db: Session, args: dict, ctx: security.UserContext) -> dict:
    if not _owns(ctx, str(args.get("file_id", ""))):
        return _NO_ACCESS
    return agent_files.read_rows(
        str(args.get("file_id", "")), args.get("sheet"),
        int(args.get("start_row") or 1), int(args.get("max_rows") or 50))


def _lookup_prices_bulk(db: Session, args: dict, ctx: security.UserContext) -> dict:
    queries = args.get("queries")
    if not isinstance(queries, list) or not queries:
        return {"error": "queries 需为非空字符串数组"}
    if len(queries) > _BULK_MAX:
        return {"error": f"一次最多 {_BULK_MAX} 个（收到 {len(queries)}），请分批调用"}
    results, counts = [], {"ok": 0, "ambiguous": 0, "not_found": 0}
    for raw in queries:
        q = str(raw).strip()
        if not q:
            continue
        r = part_resolver.resolve(
            db,
            q,
            limit=3,
            operated_by=ctx.role,
            log_miss=False,
        )
        cands = [{"pn_std": i["pn_std"], "description": i["description"], "score": i["score"]}
                 for i in r["items"][:3]]
        if not r["items"] or r["low_confidence"]:
            counts["not_found"] += 1
            results.append({"query": q, "status": "not_found", "candidates": cands})
        elif r["ambiguous"]:
            counts["ambiguous"] += 1
            results.append({"query": q, "status": "ambiguous", "candidates": cands})
        else:
            top = r["items"][0]
            counts["ok"] += 1
            results.append({"query": q, "status": "ok", "pn_std": top["pn_std"],
                            "description": top["description"], "score": top["score"],
                            **part_overview.quick_pricing(db, top["pn_std"])})
    # 按角色脱敏：sales 看不到采购成本（quick_pricing 的 last/recent_purchase_* 进 purchase_cost 组），
    # 但保留售价聚合（avg_sale_price_90d/ref_sale_price）。与其它工具出口一致。
    return security.apply_field_visibility({"results": results, "summary": counts}, ctx)


def _list_recent_purchases(db: Session, args: dict, ctx: security.UserContext) -> dict:
    limit = min(int(args.get("limit") or 20), _RECENT_LIMIT_MAX)
    days = min(int(args.get("days") or 30), _RECENT_DAYS_MAX)
    data = purchase_query.recent_purchases(
        db, ctx, q=args.get("query"), days=days,
        supplier=args.get("supplier"), page=1, page_size=limit)
    if data["total"] > limit:
        data["note"] = f"共 {data['total']} 条，仅返回最近 {limit} 条；可加 query/supplier 过滤"
    return security.apply_field_visibility(data, ctx)


def _write_excel(db: Session, args: dict, ctx: security.UserContext) -> dict:
    if not _owns(ctx, args.get("base_file_id")):   # 基于他人文件回填 = 变相读他人文件
        return _NO_ACCESS
    return agent_files.write_excel(
        args.get("base_file_id"), args.get("sheet"),
        args.get("cells") or [], args.get("output_name"), ctx.user_id)


def _read_document(db: Session, args: dict, ctx: security.UserContext) -> dict:
    fid = str(args.get("file_id", ""))
    if not _owns(ctx, fid):
        return _NO_ACCESS
    return agent_files.read_document(fid)


def _read_document_with_vision(
    db: Session,
    args: dict,
    ctx: security.UserContext,
    *,
    _policy_lease: RuntimePolicyLease,
) -> dict:
    fid = str(args.get("file_id", ""))
    if not _owns(ctx, fid):
        return _NO_ACCESS
    spec = _SPEC_BY_NAME.get("read_document_with_vision")

    def authorize_vision_attempt() -> bool:
        """Refresh identity, page, capability and concrete Artifact owner per HTTP attempt."""
        try:
            live_ctx = refresh_runtime_context(db, ctx)
            return bool(
                live_ctx is not None
                and live_ctx == ctx
                and security.page_allowed(live_ctx, "page_chat")
                and isinstance(spec, ToolSpec)
                and _allowed(spec, live_ctx)
                and _schema_egress_allowed(spec)
                and _owns(live_ctx, fid)
            )
        except Exception:  # noqa: BLE001 -- authorization failures always deny without values
            return False

    return agent_files.read_document_with_vision(
        fid,
        policy_lease=_policy_lease,
        attempt_authorizer=authorize_vision_attempt,
    )


def _write_report(db: Session, args: dict, ctx: security.UserContext) -> dict:
    # 归属对称性（TOOLS-6）：本工具只新建报表、不读任何既有文件，故无需 _owns 校验
    # （对照 _write_excel：它有 base_file_id 回填 = 变相读他人文件，故必须 _owns）。
    # ⚠️ 若将来给 write_report 加 base_file_id 之类的"读既有文件"能力，必须同步补 _owns。
    headers = args.get("headers")
    rows = args.get("rows")
    if not isinstance(headers, list) or not headers:
        return {"error": "headers 需为非空数组"}
    if not isinstance(rows, list):
        return {"error": "rows 需为二维数组"}
    return agent_files.write_report(
        args.get("title"), [str(h) for h in headers], rows,
        args.get("output_name"), ctx.user_id,
        money_cols=args.get("money_cols") if isinstance(args.get("money_cols"), list) else None)


# ── v1.5.0 新工具：数据层全面接入（采购分析/库存/维保/取消统计）+ 技能剧本 ──

_MAINT_PAGE_ERR = {"error": "无权限：你的账号未开通「项目成本（维保出库）」页面权限，无法查询维保成本数据。"}


def _get_purchase_analysis(db: Session, args: dict, ctx: security.UserContext) -> dict:
    days = min(int(args.get("days") or 30), 365)
    top = min(int(args.get("top") or 20), 50)
    excl = args.get("exclude_designated")
    data = purchase_analysis.analysis(
        db, ctx, days=days, exclude_designated=True if excl is None else bool(excl),
        q=args.get("q"), supplier=args.get("supplier"), top=top)
    for r in data.get("rows", []):
        r.pop("daily", None)           # 逐日火花线数组对模型无用且费 token
    return security.apply_field_visibility(data, ctx)


def _get_inventory(db: Session, args: dict, ctx: security.UserContext) -> dict:
    limit = min(int(args.get("limit") or 20), 50)
    data = inventory.list_dynamic(db, args.get("query"), page=1, page_size=limit, user_ctx=ctx)
    if data.get("total", 0) > limit:
        data["note"] = f"共 {data['total']} 个型号，仅返回动态库存最高的前 {limit} 个；可加 query 过滤"
    return security.apply_field_visibility(data, ctx)


def _get_maintenance_board(db: Session, args: dict, ctx: security.UserContext) -> dict:
    if not security.page_allowed(ctx, "page_maintenance"):
        return _MAINT_PAGE_ERR
    if security.is_scoped_sales(ctx):
        return {
            "error": "无权限：受限销售账号不能查看合同级维保数据。"
            "可以查看本人范围的项目事实。"
        }
    st = args.get("status")
    data = maintenance_cost.board(
        db, None, None,
        st if st in (
            "incomplete_cost",
            "expense_data_unavailable",
            "red",
            "yellow",
            "green",
            "no_budget",
        ) else None,
        user_ctx=ctx,
    )
    return security.apply_field_visibility(data, ctx)


def _get_maintenance_projects(db: Session, args: dict, ctx: security.UserContext) -> dict:
    if not security.page_allowed(ctx, "page_maintenance"):
        return _MAINT_PAGE_ERR
    top = min(int(args.get("top") or 20), 50)
    data = maintenance_cost.projects_aggregate(db, None, None, args.get("q"), user_ctx=ctx)
    rows = data.get("rows", [])
    if len(rows) > top:
        sort_label = "按项目名" if data.get("ranking_restricted") else "成本最高"
        data = {**data, "rows": rows[:top],
                "note": f"共 {len(rows)} 个项目，仅返回{sort_label}的前 {top} 个；可用 q 过滤"}
    return security.apply_field_visibility(data, ctx)


def _get_maintenance_lines(db: Session, args: dict, ctx: security.UserContext) -> dict:
    if not security.page_allowed(ctx, "page_maintenance"):
        return _MAINT_PAGE_ERR
    project = str(args.get("project", "")).strip()
    if not project:
        return {"error": "project 不能为空（用 get_maintenance_projects 拿准确项目名）"}
    month = args.get("month")
    data = maintenance_cost.project_lines(db, project, month if month else None,
                                          None, None, max(int(args.get("page") or 1), 1), 50,
                                          user_ctx=ctx)
    return security.apply_field_visibility(data, ctx)


def _get_cancellation_stats(db: Session, args: dict, ctx: security.UserContext) -> dict:
    gran = args.get("granularity") or "month"
    days = args.get("days")
    data = purchase_analysis.cancellation_stats(
        db, ctx, granularity=gran, days=int(days) if days else None)
    return security.apply_field_visibility(data, ctx)


def _list_skills(db: Session, args: dict, ctx: security.UserContext) -> dict:
    return {"skills": skills.available(ctx)}


def _get_skill(db: Session, args: dict, ctx: security.UserContext) -> dict:
    return skills.get(str(args.get("skill", "")).strip(), ctx)


def _allow(_ctx: security.UserContext) -> bool:
    return True


@dataclass(frozen=True)
class _PagePermission:
    """Reuse the API page contract when deciding which capabilities a model may see."""

    page_key: str
    deny_scoped_sales: bool = False

    @property
    def policy_id(self) -> str:
        suffix = ":deny_scoped_sales" if self.deny_scoped_sales else ""
        return f"page:{self.page_key}{suffix}"

    def __call__(self, ctx: security.UserContext) -> bool:
        if not security.page_allowed(ctx, self.page_key):
            return False
        return not (self.deny_scoped_sales and security.is_scoped_sales(ctx))


def _permission_policy_id(permission: ToolPermission) -> str | None:
    if permission is _allow:
        return "allow"
    if isinstance(permission, _PagePermission):
        return permission.policy_id
    return None


def _freeze_json(value: object) -> object:
    """Return a recursively immutable canonical JSON value."""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    """Return a fresh mutable JSON projection for SDK/model-facing callers."""
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _index_schemas(schemas: list[dict]) -> dict[str, Mapping[str, object]]:
    indexed: dict[str, Mapping[str, object]] = {}
    for schema in schemas:
        function = schema.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(name, str) or not name or name in indexed:
            raise RuntimeError("Agent tool schemas must have unique non-empty names")
        frozen = _freeze_json(schema)
        if not isinstance(frozen, Mapping):  # pragma: no cover - input is statically a dict
            raise RuntimeError("Agent tool schema must be an object")
        indexed[name] = frozen
    return indexed


_SCHEMA_BY_NAME = _index_schemas(_OPENAI_SCHEMAS)


def _schema(name: str) -> Mapping[str, object]:
    try:
        return _SCHEMA_BY_NAME[name]
    except KeyError as exc:  # import-time wiring defect, never a user-facing error
        raise RuntimeError(f"Missing Agent tool schema: {name}") from exc


def _effects(*effects: ToolEffect) -> frozenset[ToolEffect]:
    """Build an immutable capability-effect declaration."""
    return frozenset(effects)


def _primary_egress(
    sensitivity: DataSensitivity,
    source: EgressSource = EgressSource.TOOL_RESULT,
) -> tuple[EgressEdge, ...]:
    customer_file = source is EgressSource.CUSTOMER_FILE
    return (EgressEdge(
        source=source,
        destination=EgressDestination.PRIMARY_MODEL,
        sensitivity=sensitivity,
        purpose=(PURPOSE_DOCUMENT_ASSISTANCE if customer_file else PURPOSE_BUSINESS_ASSISTANCE),
        projection_id=(
            PROJECTION_CUSTOMER_FILE_RESULT_JSON
            if customer_file
            else PROJECTION_TOOL_RESULT_JSON
        ),
        max_bytes=(CUSTOMER_FILE_RESULT_MAX_BYTES if customer_file else BUSINESS_RESULT_MAX_BYTES),
        policy_version=EGRESS_POLICY_VERSION,
        retention_policy=RETENTION_NO_ADDITIONAL_EGRESS_ARCHIVE,
    ),)


def _conversation_context_edge() -> EgressEdge:
    return EgressEdge(
        source=EgressSource.CONVERSATION_CONTEXT,
        destination=EgressDestination.PRIMARY_MODEL,
        sensitivity=DataSensitivity.CUSTOMER_FILE,
        purpose=PURPOSE_CONVERSATION_ASSISTANCE,
        projection_id=PROJECTION_CONVERSATION_JSON,
        max_bytes=CONVERSATION_CONTEXT_MAX_BYTES,
        policy_version=EGRESS_POLICY_VERSION,
        retention_policy=RETENTION_NO_ADDITIONAL_EGRESS_ARCHIVE,
    )


def _vision_egress() -> tuple[EgressEdge, ...]:
    # The original customer bytes/images go only to Vision. The extracted OCR text then crosses
    # a separate boundary into the primary model context; both edges must be authorized.
    return (
        EgressEdge(
            source=EgressSource.CUSTOMER_FILE,
            destination=EgressDestination.VISION_PROVIDER,
            sensitivity=DataSensitivity.CUSTOMER_FILE,
            purpose=PURPOSE_DOCUMENT_ASSISTANCE,
            projection_id=PROJECTION_VISION_INPUT,
            max_bytes=VISION_INPUT_MAX_BYTES,
            policy_version=EGRESS_POLICY_VERSION,
            retention_policy=RETENTION_NO_ADDITIONAL_EGRESS_ARCHIVE,
        ),
        EgressEdge(
            source=EgressSource.VISION_OCR,
            destination=EgressDestination.PRIMARY_MODEL,
            sensitivity=DataSensitivity.CUSTOMER_FILE,
            purpose=PURPOSE_DOCUMENT_ASSISTANCE,
            projection_id=PROJECTION_VISION_OCR_RESULT,
            max_bytes=VISION_OCR_RESULT_MAX_BYTES,
            policy_version=EGRESS_POLICY_VERSION,
            retention_policy=RETENTION_NO_ADDITIONAL_EGRESS_ARCHIVE,
        ),
    )


_ARGS_INVALID = ToolValidationFailure(
    "AGENT_TOOL_ARGS_INVALID",
    "工具参数不符合安全约束",
)
_BUDGET_EXCEEDED = ToolValidationFailure(
    "AGENT_TOOL_BUDGET_EXCEEDED",
    "工具参数超过安全预算",
)
_VALIDATOR_FAILED = ToolValidationFailure(
    "AGENT_TOOL_VALIDATOR_FAILED",
    "工具参数安全校验失败",
)


def _json_shape(value: object, depth: int = 1) -> tuple[int, int]:
    """Return (maximum depth, node count) for JSON-compatible input."""
    if isinstance(value, dict):
        child_shapes = [_json_shape(item, depth + 1) for item in value.values()]
    elif isinstance(value, list):
        child_shapes = [_json_shape(item, depth + 1) for item in value]
    else:
        child_shapes = []
    return (
        max([depth, *(item[0] for item in child_shapes)]),
        1 + sum(item[1] for item in child_shapes),
    )


def _schema_type_matches(value: object, expected: object) -> bool:
    expected_types = expected if isinstance(expected, (list, tuple)) else [expected]
    for item in expected_types:
        if item == "object" and isinstance(value, dict):
            return True
        if item == "array" and isinstance(value, list):
            return True
        if item == "string" and isinstance(value, str):
            return True
        if item == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if item == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if item == "boolean" and isinstance(value, bool):
            return True
        if item == "null" and value is None:
            return True
    return False


def _matches_schema(value: object, schema: object) -> bool:
    """Small fail-closed validator for the JSON-Schema subset used by Agent tools."""
    if not isinstance(schema, Mapping):
        return False
    if not schema:  # explicitly unconstrained JSON value, still covered by generic budgets
        return True
    expected = schema.get("type")
    if expected is not None and not _schema_type_matches(value, expected):
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            return False
        if isinstance(maximum, (int, float)) and value > maximum:
            return False
    if isinstance(value, dict) and expected == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, Mapping) or not isinstance(required, (list, tuple)):
            return False
        if any(key not in value for key in required):
            return False
        # Model output is untrusted: undeclared keys never flow to a handler.
        if any(not isinstance(key, str) or key not in properties for key in value):
            return False
        return all(_matches_schema(item, properties[key]) for key, item in value.items())
    if isinstance(value, list) and expected == "array":
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            return False
        if isinstance(maximum_items, int) and len(value) > maximum_items:
            return False
        item_schema = schema.get("items")
        return item_schema is None or all(_matches_schema(item, item_schema) for item in value)
    return True


def _valid_file_reference(value: object) -> bool:
    if not isinstance(value, str) or value != value.strip():
        return False
    if re.fullmatch(r"[a-f0-9]{12}", value):
        return True
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return value.lower() in {str(parsed), parsed.hex}


def _valid_excel_column(value: object) -> bool:
    if isinstance(value, int) and not isinstance(value, bool):
        return 1 <= value <= 16_384
    if not isinstance(value, str) or value != value.strip() or not value:
        return False
    if value.isdigit():
        return 1 <= int(value) <= 16_384
    letters = value.upper()
    if not re.fullmatch(r"[A-Z]{1,3}", letters):
        return False
    number = 0
    for char in letters:
        number = number * 26 + ord(char) - ord("A") + 1
    return 1 <= number <= 16_384


def _validate_tool_arguments(
    args: object,
    parameters: dict,
    budget: ToolBudget,
) -> ToolValidationFailure | None:
    """Validate schema and resource budgets before any handler or file/database operation."""
    if not isinstance(args, dict) or not _matches_schema(args, parameters):
        return _ARGS_INVALID
    try:
        date_from = _parse_date(args.get("date_from"))
        date_to = _parse_date(args.get("date_to"))
    except ValueError:
        return _ARGS_INVALID
    if date_from is not None and date_to is not None and date_from > date_to:
        return _ARGS_INVALID
    for key in ("file_id", "base_file_id"):
        if key in args and not _valid_file_reference(args[key]):
            return _ARGS_INVALID
    cells = args.get("cells")
    if isinstance(cells, list) and any(
        not isinstance(cell, dict) or not _valid_excel_column(cell.get("col"))
        for cell in cells
    ):
        return _ARGS_INVALID
    try:
        encoded = json.dumps(
            args,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        max_depth, nodes = _json_shape(args)
    except (TypeError, ValueError, RecursionError):
        return _ARGS_INVALID
    if (
        len(encoded) > budget.max_payload_bytes
        or max_depth > budget.max_depth
        or nodes > budget.max_nodes
    ):
        return _BUDGET_EXCEEDED

    def too_long(value: object, limit: int | None) -> bool:
        return limit is not None and isinstance(value, str) and len(value) > limit

    def too_large(value: object, limit: int | None) -> bool:
        return limit is not None and isinstance(value, list) and len(value) > limit

    def over_positive(value: object, limit: int | None) -> bool:
        return (
            limit is not None
            and isinstance(value, int)
            and not isinstance(value, bool)
            and (value < 1 or value > limit)
        )

    stack = [args]
    while stack:
        current = stack.pop()
        if isinstance(current, str) and len(current) > budget.max_any_string_chars:
            return _BUDGET_EXCEEDED
        if isinstance(current, dict):
            if any(not isinstance(key, str) for key in current):
                return _ARGS_INVALID
            if any(len(key) > budget.max_any_string_chars for key in current):
                return _BUDGET_EXCEEDED
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)

    if any(too_long(args.get(key), budget.max_query_chars) for key in ("query", "q")):
        return _BUDGET_EXCEEDED
    if too_long(args.get("pn_std"), budget.max_pn_chars):
        return _BUDGET_EXCEEDED
    if any(over_positive(args.get(key), budget.max_limit) for key in ("limit", "top")):
        return _BUDGET_EXCEEDED
    if over_positive(args.get("days"), budget.max_days):
        return _BUDGET_EXCEEDED
    if over_positive(args.get("page"), budget.max_page):
        return _BUDGET_EXCEEDED
    if over_positive(args.get("max_rows"), budget.max_rows):
        return _BUDGET_EXCEEDED
    if too_large(args.get("rows"), budget.max_rows):
        return _BUDGET_EXCEEDED
    if any(
        too_large(args.get(key), budget.max_items)
        for key in ("queries", "headers", "money_cols", "items")
    ):
        return _BUDGET_EXCEEDED
    if too_large(args.get("cells"), budget.max_cells):
        return _BUDGET_EXCEEDED
    if too_large(args.get("sheets"), budget.max_sheets):
        return _BUDGET_EXCEEDED
    if too_long(args.get("output_name"), budget.max_output_name_chars):
        return _BUDGET_EXCEEDED
    if too_long(args.get("sheet"), budget.max_sheet_name_chars):
        return _BUDGET_EXCEEDED
    if budget.max_columns is not None:
        if too_large(args.get("headers"), budget.max_columns):
            return _BUDGET_EXCEEDED
        rows = args.get("rows")
        if isinstance(rows, list) and any(
            isinstance(row, list) and len(row) > budget.max_columns for row in rows
        ):
            return _BUDGET_EXCEEDED

    for key, limit in budget.string_limits:
        if too_long(args.get(key), limit):
            return _BUDGET_EXCEEDED
    for key, minimum, maximum in budget.integer_ranges:
        value = args.get(key)
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and not minimum <= value <= maximum
        ):
            return _BUDGET_EXCEEDED
    for key, limit in budget.collection_limits:
        if too_large(args.get(key), limit):
            return _BUDGET_EXCEEDED
    for key, limit in budget.collection_string_limits:
        values = args.get(key)
        if isinstance(values, list) and any(
            isinstance(value, str) and len(value) > limit for value in values
        ):
            return _BUDGET_EXCEEDED
    for key, limit in budget.row_width_limits:
        values = args.get(key)
        if isinstance(values, list) and any(
            isinstance(value, list) and len(value) > limit for value in values
        ):
            return _BUDGET_EXCEEDED
    return None


def _budget(
    *,
    payload: int = 64 * 1024,
    depth: int = 8,
    nodes: int = 2_000,
    string_chars: int = _REPORT_CELL_CHARS_MAX,
    **kwargs,
) -> ToolBudget:
    return ToolBudget(
        max_payload_bytes=payload,
        max_depth=depth,
        max_nodes=nodes,
        max_any_string_chars=string_chars,
        **kwargs,
    )


_TOOL_BUDGETS = MappingProxyType({
    "search_parts": _budget(max_query_chars=_QUERY_CHARS_MAX, max_limit=_SEARCH_LIMIT_MAX),
    "get_part_overview": _budget(max_pn_chars=_PN_CHARS_MAX),
    "inspect_file": _budget(
        string_limits=(("file_id", _FILE_ID_CHARS_MAX),),
        max_sheets=agent_files._MAX_INSPECT_SHEETS,
    ),
    "read_file_rows": _budget(
        max_rows=_READ_ROWS_MAX,
        max_sheet_name_chars=_SHEET_CHARS_MAX,
        string_limits=(("file_id", _FILE_ID_CHARS_MAX),),
        integer_ranges=(("start_row", 1, 1_048_576),),
    ),
    "lookup_prices_bulk": _budget(
        max_items=_BULK_MAX,
        collection_string_limits=(("queries", _QUERY_CHARS_MAX),),
    ),
    "write_excel": _budget(
        payload=4 * 1024 * 1024,
        nodes=25_000,
        max_cells=_WRITE_CELLS_MAX,
        max_sheet_name_chars=31,
        max_output_name_chars=_OUTPUT_NAME_CHARS_MAX,
        string_limits=(("base_file_id", _FILE_ID_CHARS_MAX),),
    ),
    "read_document": _budget(string_limits=(("file_id", _FILE_ID_CHARS_MAX),)),
    "read_document_with_vision": _budget(
        string_limits=(("file_id", _FILE_ID_CHARS_MAX),),
    ),
    "write_report": _budget(
        payload=8 * 1024 * 1024,
        depth=10,
        nodes=400_000,
        max_rows=_REPORT_ROWS_MAX,
        max_items=_REPORT_COLUMNS_MAX,
        max_output_name_chars=_OUTPUT_NAME_CHARS_MAX,
        max_columns=_REPORT_COLUMNS_MAX,
        string_limits=(("title", _OUTPUT_NAME_CHARS_MAX),),
        collection_string_limits=(("headers", _OUTPUT_NAME_CHARS_MAX),),
        row_width_limits=(("rows", _REPORT_COLUMNS_MAX),),
    ),
    "list_recent_purchases": _budget(
        max_query_chars=_QUERY_CHARS_MAX,
        max_limit=_RECENT_LIMIT_MAX,
        max_days=_RECENT_DAYS_MAX,
        string_limits=(("supplier", _FILTER_CHARS_MAX),),
    ),
    "get_profit_ranking": _budget(
        string_limits=(("date_from", 10), ("date_to", 10)),
    ),
    "get_purchase_analysis": _budget(
        max_query_chars=_QUERY_CHARS_MAX,
        max_limit=50,
        max_days=_RECENT_DAYS_MAX,
        string_limits=(("supplier", _FILTER_CHARS_MAX),),
    ),
    "get_inventory": _budget(max_query_chars=_QUERY_CHARS_MAX, max_limit=50),
    "get_maintenance_board": _budget(),
    "get_maintenance_projects": _budget(max_query_chars=_QUERY_CHARS_MAX, max_limit=50),
    "get_maintenance_lines": _budget(
        max_page=100_000,
        string_limits=(("project", _PROJECT_CHARS_MAX), ("month", 7)),
    ),
    "get_cancellation_stats": _budget(max_days=3_650),
    "list_skills": _budget(),
    "get_skill": _budget(string_limits=(("skill", _SKILL_ID_CHARS_MAX),)),
})


def _spec(
    name: str,
    handler: ToolHandler,
    effects: frozenset[ToolEffect],
    egress: tuple[EgressEdge, ...],
    sensitivity: DataSensitivity,
    permission_id: str,
    permission: ToolPermission,
    enabled: bool = True,
    implementation_version: str = "1",
) -> ToolSpec:
    return ToolSpec(
        schema=_schema(name),
        handler=handler,
        effects=effects,
        egress=egress,
        sensitivity=sensitivity,
        permission_id=permission_id,
        permission=permission,
        enabled=enabled,
        budget=_TOOL_BUDGETS[name],
        validator=_validate_tool_arguments,
        implementation_version=implementation_version,
    )


# Single registration source. Effects describe facts at the service boundary, not the
# implementation detail that a read may write an access log or that an Artifact gets a new ID.
TOOL_SPECS: tuple[ToolSpec, ...] = (
    _spec("search_parts", _search_parts, _effects(ToolEffect.BUSINESS_READ),
          _primary_egress(DataSensitivity.BUSINESS_CONFIDENTIAL), DataSensitivity.BUSINESS_CONFIDENTIAL,
          "page:page_parts", _PagePermission("page_parts")),
    _spec("get_part_overview", _get_part_overview, _effects(ToolEffect.BUSINESS_READ),
          _primary_egress(DataSensitivity.BUSINESS_CONFIDENTIAL), DataSensitivity.BUSINESS_CONFIDENTIAL,
          "page:page_parts", _PagePermission("page_parts")),
    _spec("inspect_file", _inspect_file, _effects(ToolEffect.FILE_READ),
          _primary_egress(DataSensitivity.CUSTOMER_FILE, EgressSource.CUSTOMER_FILE),
          DataSensitivity.CUSTOMER_FILE, "allow", _allow),
    _spec("read_file_rows", _read_file_rows, _effects(ToolEffect.FILE_READ),
          _primary_egress(DataSensitivity.CUSTOMER_FILE, EgressSource.CUSTOMER_FILE),
          DataSensitivity.CUSTOMER_FILE, "allow", _allow),
    _spec("lookup_prices_bulk", _lookup_prices_bulk, _effects(ToolEffect.BUSINESS_READ),
          _primary_egress(DataSensitivity.BUSINESS_CONFIDENTIAL), DataSensitivity.BUSINESS_CONFIDENTIAL,
          "page:page_parts", _PagePermission("page_parts")),
    _spec("write_excel", _write_excel,
          _effects(ToolEffect.FILE_READ, ToolEffect.ARTIFACT_CREATE),
          _primary_egress(DataSensitivity.CUSTOMER_FILE, EgressSource.CUSTOMER_FILE),
          DataSensitivity.CUSTOMER_FILE, "allow", _allow),
    _spec("read_document", _read_document, _effects(ToolEffect.FILE_READ),
          _primary_egress(DataSensitivity.CUSTOMER_FILE, EgressSource.CUSTOMER_FILE),
          DataSensitivity.CUSTOMER_FILE, "allow", _allow),
    _spec("read_document_with_vision", _read_document_with_vision,
          _effects(ToolEffect.FILE_READ), _vision_egress(),
          DataSensitivity.CUSTOMER_FILE, "allow", _allow),
    _spec("write_report", _write_report, _effects(ToolEffect.ARTIFACT_CREATE),
          _primary_egress(DataSensitivity.BUSINESS_CONFIDENTIAL), DataSensitivity.BUSINESS_CONFIDENTIAL,
          "allow", _allow),
    _spec("list_recent_purchases", _list_recent_purchases,
          _effects(ToolEffect.BUSINESS_READ), _primary_egress(DataSensitivity.BUSINESS_CONFIDENTIAL),
          DataSensitivity.BUSINESS_CONFIDENTIAL, "page:page_purchases",
          _PagePermission("page_purchases")),
    _spec("get_profit_ranking", _get_profit_ranking,
          _effects(ToolEffect.BUSINESS_READ), _primary_egress(DataSensitivity.BUSINESS_CONFIDENTIAL),
          DataSensitivity.BUSINESS_CONFIDENTIAL,
          "page:page_profit:deny_scoped_sales",
          _PagePermission("page_profit", deny_scoped_sales=True),
          implementation_version="2"),
    _spec("get_purchase_analysis", _get_purchase_analysis,
          _effects(ToolEffect.BUSINESS_READ), _primary_egress(DataSensitivity.BUSINESS_CONFIDENTIAL),
          DataSensitivity.BUSINESS_CONFIDENTIAL, "page:page_purchases",
          _PagePermission("page_purchases")),
    _spec("get_inventory", _get_inventory, _effects(ToolEffect.BUSINESS_READ),
          _primary_egress(DataSensitivity.BUSINESS_CONFIDENTIAL), DataSensitivity.BUSINESS_CONFIDENTIAL,
          "page:page_inventory", _PagePermission("page_inventory")),
    _spec("get_maintenance_board", _get_maintenance_board,
          _effects(ToolEffect.BUSINESS_READ), _primary_egress(DataSensitivity.BUSINESS_CONFIDENTIAL),
          DataSensitivity.BUSINESS_CONFIDENTIAL,
          "page:page_maintenance:deny_scoped_sales",
          _PagePermission("page_maintenance", deny_scoped_sales=True)),
    _spec("get_maintenance_projects", _get_maintenance_projects,
          _effects(ToolEffect.BUSINESS_READ), _primary_egress(DataSensitivity.BUSINESS_CONFIDENTIAL),
          DataSensitivity.BUSINESS_CONFIDENTIAL, "page:page_maintenance",
          _PagePermission("page_maintenance")),
    _spec("get_maintenance_lines", _get_maintenance_lines,
          _effects(ToolEffect.BUSINESS_READ), _primary_egress(DataSensitivity.BUSINESS_CONFIDENTIAL),
          DataSensitivity.BUSINESS_CONFIDENTIAL, "page:page_maintenance",
          _PagePermission("page_maintenance")),
    _spec("get_cancellation_stats", _get_cancellation_stats,
          _effects(ToolEffect.BUSINESS_READ), _primary_egress(DataSensitivity.BUSINESS_CONFIDENTIAL),
          DataSensitivity.BUSINESS_CONFIDENTIAL, "page:page_purchases",
          _PagePermission("page_purchases")),
    _spec("list_skills", _list_skills, _effects(ToolEffect.BUSINESS_READ),
          _primary_egress(DataSensitivity.INTERNAL), DataSensitivity.INTERNAL, "allow", _allow),
    _spec("get_skill", _get_skill, _effects(ToolEffect.BUSINESS_READ),
          _primary_egress(DataSensitivity.INTERNAL), DataSensitivity.INTERNAL, "allow", _allow),
)


def _valid_effects(effects: object) -> bool:
    return (
        isinstance(effects, frozenset)
        and bool(effects)
        and all(isinstance(effect, ToolEffect) for effect in effects)
        and effects.issubset(ALLOWED_TOOL_EFFECTS)
    )


def _valid_egress(edges: object) -> bool:
    return (
        isinstance(edges, tuple)
        and all(
            isinstance(edge, EgressEdge)
            and isinstance(edge.source, EgressSource)
            and edge.source in ALLOWED_EGRESS_SOURCES
            and isinstance(edge.destination, EgressDestination)
            and edge.destination in ALLOWED_EGRESS_DESTINATIONS
            and isinstance(edge.sensitivity, DataSensitivity)
            and edge.sensitivity in ALLOWED_DATA_SENSITIVITIES
            and isinstance(edge.purpose, str)
            and bool(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", edge.purpose))
            and isinstance(edge.projection_id, str)
            and bool(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", edge.projection_id))
            and isinstance(edge.max_bytes, int)
            and not isinstance(edge.max_bytes, bool)
            and edge.max_bytes > 0
            and isinstance(edge.policy_version, str)
            and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", edge.policy_version))
            and isinstance(edge.retention_policy, str)
            and edge.retention_policy in ALLOWED_RETENTION_POLICIES
            # Metadata is an enforcement contract, not an extensible label. Future purposes,
            # projections, versions or byte ceilings require an explicit code-review change to
            # this immutable registry before they can become model-visible.
            and edge in EGRESS_EDGE_CONTRACT_REGISTRY
            for edge in edges
        )
    )


def _valid_budget(budget: object) -> bool:
    if not isinstance(budget, ToolBudget):
        return False
    positive_required = (
        budget.max_payload_bytes,
        budget.max_depth,
        budget.max_nodes,
        budget.max_any_string_chars,
    )
    optional_limits = (
        budget.max_query_chars,
        budget.max_pn_chars,
        budget.max_limit,
        budget.max_days,
        budget.max_page,
        budget.max_rows,
        budget.max_items,
        budget.max_cells,
        budget.max_sheets,
        budget.max_output_name_chars,
        budget.max_sheet_name_chars,
        budget.max_columns,
    )
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0
           for value in positive_required):
        return False
    if any(value is not None and (
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
    ) for value in optional_limits):
        return False
    pairs = (
        budget.string_limits,
        budget.collection_limits,
        budget.collection_string_limits,
        budget.row_width_limits,
    )
    if any(
        not isinstance(items, tuple)
        or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not item[0]
            or not isinstance(item[1], int)
            or isinstance(item[1], bool)
            or item[1] <= 0
            for item in items
        )
        for items in pairs
    ):
        return False
    return (
        isinstance(budget.integer_ranges, tuple)
        and all(
            isinstance(item, tuple)
            and len(item) == 3
            and isinstance(item[0], str)
            and bool(item[0])
            and all(isinstance(value, int) and not isinstance(value, bool) for value in item[1:])
            and item[1] <= item[2]
            for item in budget.integer_ranges
        )
    )


def _valid_classification(spec: object) -> bool:
    return (
        isinstance(spec, ToolSpec)
        and _valid_effects(spec.effects)
        and _valid_egress(spec.egress)
        # Empty is a structurally valid data-flow declaration, but this runtime exposes only
        # model tools that return through at least one explicitly bounded primary-model edge.
        and bool(spec.egress)
        and any(
            edge.destination is EgressDestination.PRIMARY_MODEL
            and edge.source in {
                EgressSource.TOOL_RESULT,
                EgressSource.CUSTOMER_FILE,
                EgressSource.VISION_OCR,
            }
            for edge in spec.egress
        )
        and isinstance(spec.sensitivity, DataSensitivity)
        and spec.sensitivity in ALLOWED_DATA_SENSITIVITIES
        and spec.sensitivity == max(
            (edge.sensitivity for edge in spec.egress),
            key=_SENSITIVITY_RANK.__getitem__,
        )
        and isinstance(spec.permission_id, str)
        and bool(spec.permission_id)
        and _permission_policy_id(spec.permission) == spec.permission_id
        and callable(spec.handler)
        and _valid_budget(spec.budget)
        and callable(spec.validator)
        and isinstance(spec.implementation_version, str)
        and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", spec.implementation_version))
    )


def _has_stable_subject(spec: ToolSpec, ctx: security.UserContext) -> bool:
    if not (spec.effects & STABLE_SUBJECT_EFFECTS):
        return True
    return ctx.authn == "sys_user" and bool(ctx.user_id)


def _index_specs(specs: tuple[ToolSpec, ...]) -> dict[str, ToolSpec]:
    indexed: dict[str, ToolSpec] = {}
    for spec in specs:
        if not spec.name or spec.name in indexed:
            raise RuntimeError("Agent ToolSpecs must have unique non-empty schema names")
        if (
            not _valid_classification(spec)
            or not isinstance(spec.enabled, bool)
            or not callable(spec.permission)
        ):
            raise RuntimeError(f"Agent ToolSpec has an invalid policy classification: {spec.name}")
        indexed[spec.name] = spec
    if set(indexed) != set(_SCHEMA_BY_NAME):
        raise RuntimeError("Every Agent schema must have exactly one ToolSpec")
    return indexed


_SPEC_BY_NAME = MappingProxyType(_index_specs(TOOL_SPECS))

CAPABILITY_POLICY_VERSION = "v1"


def _callable_id(value: object) -> str:
    module = getattr(value, "__module__", None)
    qualname = getattr(value, "__qualname__", None)
    if not isinstance(module, str) or not module or not isinstance(qualname, str) or not qualname:
        raise ValueError("Capability policy callable lacks a stable identifier")
    return f"{module}:{qualname}"


def _budget_metadata(budget: ToolBudget) -> dict:
    return {
        "max_payload_bytes": budget.max_payload_bytes,
        "max_depth": budget.max_depth,
        "max_nodes": budget.max_nodes,
        "max_any_string_chars": budget.max_any_string_chars,
        "max_query_chars": budget.max_query_chars,
        "max_pn_chars": budget.max_pn_chars,
        "max_limit": budget.max_limit,
        "max_days": budget.max_days,
        "max_page": budget.max_page,
        "max_rows": budget.max_rows,
        "max_items": budget.max_items,
        "max_cells": budget.max_cells,
        "max_sheets": budget.max_sheets,
        "max_output_name_chars": budget.max_output_name_chars,
        "max_sheet_name_chars": budget.max_sheet_name_chars,
        "max_columns": budget.max_columns,
        "string_limits": budget.string_limits,
        "integer_ranges": budget.integer_ranges,
        "collection_limits": budget.collection_limits,
        "collection_string_limits": budget.collection_string_limits,
        "row_width_limits": budget.row_width_limits,
    }


def capability_policy_fingerprint(
    specs: tuple[ToolSpec, ...] | None = None,
) -> str:
    """Hash canonical, non-secret policy metadata for task/audit correlation.

    Descriptions, argument values and object identities are intentionally excluded. Stable
    callable identifiers, implementation versions, budgets, permission IDs and function
    parameter contracts are included so durable work can be invalidated when enforcement or
    accepted-input semantics change. Registration order is normalized by capability name.
    """
    selected = TOOL_SPECS if specs is None else specs
    names: set[str] = set()
    entries: list[dict] = []
    for spec in selected:
        if (
            not _valid_classification(spec)
            or not spec.name
            or spec.name in names
            or not isinstance(spec.enabled, bool)
        ):
            raise ValueError("Cannot fingerprint invalid Agent capability policy")
        function = spec.schema.get("function")
        parameters = function.get("parameters") if isinstance(function, Mapping) else None
        if not isinstance(parameters, Mapping):
            raise ValueError("Cannot fingerprint Agent capability without parameters")
        names.add(spec.name)
        entries.append({
            "name": spec.name,
            "parameters": _thaw_json(parameters),
            "handler": _callable_id(spec.handler),
            "validator": _callable_id(spec.validator),
            "implementation_version": spec.implementation_version,
            "budget": _budget_metadata(spec.budget),
            "effects": sorted(effect.value for effect in spec.effects),
            "egress": sorted(
                ({
                    "source": edge.source.value,
                    "destination": edge.destination.value,
                    "sensitivity": edge.sensitivity.value,
                    "purpose": edge.purpose,
                    "projection_id": edge.projection_id,
                    "max_bytes": edge.max_bytes,
                    "policy_version": edge.policy_version,
                    "retention_policy": edge.retention_policy,
                } for edge in spec.egress),
                key=lambda edge: (
                    edge["destination"], edge["source"], edge["sensitivity"],
                    edge["purpose"], edge["projection_id"], edge["policy_version"],
                    edge["retention_policy"], edge["max_bytes"],
                ),
            ),
            "sensitivity": spec.sensitivity.value,
            "permission_id": spec.permission_id,
            "enabled": spec.enabled,
        })
    canonical = json.dumps(
        {
            "version": CAPABILITY_POLICY_VERSION,
            "stable_subject_effects": sorted(effect.value for effect in STABLE_SUBJECT_EFFECTS),
            "capabilities": sorted(entries, key=lambda entry: entry["name"]),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


CAPABILITY_POLICY_FINGERPRINT = capability_policy_fingerprint()

# Backward-compatible projections. New code must use TOOL_SPECS/tools_for/dispatch so an
# accidentally appended schema or handler cannot bypass effect and permission policy.
TOOLS: list[dict] = [_thaw_json(spec.schema) for spec in TOOL_SPECS]
_REGISTRY = MappingProxyType({spec.name: spec.handler for spec in TOOL_SPECS})


def _runtime_subject_allowed(ctx: security.UserContext) -> bool:
    # In RBAC deployments, shared/legacy credentials are not revocable named principals. They
    # receive zero business/file capabilities; runtime identity refresh also stops before model
    # egress, and dispatch always fails closed.
    return (
        not config.ENABLE_RBAC
        or (ctx.authn == "sys_user" and bool(ctx.user_id))
    )


def _allowed(spec: object, ctx: security.UserContext) -> bool:
    """Evaluate one capability fail closed; permission bugs never widen access."""
    if not isinstance(spec, ToolSpec) or not _runtime_subject_allowed(ctx):
        return False
    if (spec.enabled is not True or not _valid_classification(spec)
            or not _has_stable_subject(spec, ctx)):
        return False
    if not callable(spec.permission):
        return False
    try:
        return bool(spec.permission(ctx))
    except Exception as exc:  # noqa: BLE001 -- policy failures deny without logging user data
        _log.error(
            "agent capability permission failed name=%s exception_type=%s",
            spec.name,
            type(exc).__name__,
        )
        return False


def tools_for(ctx: security.UserContext) -> list[dict]:
    """Return only schemas the current context may call.

    This is the model-facing half of the policy. ``dispatch`` repeats the same check because
    tool names and arguments returned by a model are untrusted input.
    """
    if not _runtime_subject_allowed(ctx):
        return []
    return [
        _thaw_json(spec.schema)
        for spec in TOOL_SPECS
        if _allowed(spec, ctx)
        and _schema_egress_allowed(spec)
    ]


def _capability_denied() -> dict:
    # Do not disclose whether a name exists but is disabled/forbidden.
    return {"error": "未知工具或无权限", "kind": "capability_denied"}


def _identity_denied() -> dict:
    return {
        "error": "身份状态已失效，请重新登录",
        "kind": "identity_denied",
        "code": "AGENT_IDENTITY_STALE",
        "retriable": False,
    }


def _reload_dispatch_context(
    db: Session,
    ctx: security.UserContext,
) -> security.UserContext | None:
    """Reload the active DB identity for every RBAC-enabled tool execution.

    The request context is only a signed snapshot. A long-running Agent loop must observe a
    role/permission change, account disable, or token revocation before the next handler runs.
    """
    from app import permissions
    from app.db import SessionLocal
    from app.models.system import SysUser

    if not config.ENABLE_RBAC:
        # Legacy deployments can still run business-read tools. Named file/artifact capabilities
        # remain guarded by stable-subject and owner checks in _allowed/_owns.
        return ctx
    if (
        ctx.authn != "sys_user"
        or not ctx.user_id
        or ctx.token_version is None
    ):
        return None
    try:
        # Never consult the business handler's long-lived transaction/identity map here. A fresh
        # short session observes disable/role/permission/token-version commits made by another
        # request before this exact security boundary, then closes immediately.
        with SessionLocal() as identity_db:
            user = identity_db.scalar(
                select(SysUser)
                .where(SysUser.username == ctx.user_id)
                .execution_options(populate_existing=True)
            )
            if (
                user is None
                or not user.is_active
                or int(user.token_version or 0) != int(ctx.token_version)
            ):
                return None
            current_permissions = permissions.runtime_safe(
                permissions.effective_for_user(user)
            )
            return security.UserContext(
                user_id=user.username,
                role=user.role,
                salesperson_name=user.salesperson_name,
                permissions=current_permissions,
                ding_user_id=user.ding_user_id,
                is_authenticated=True,
                authn="sys_user",
                token_version=int(user.token_version or 0),
            )
    except Exception as exc:  # noqa: BLE001 -- identity lookup errors fail closed
        _log.error(
            "agent identity reload failed exception_type=%s",
            type(exc).__name__,
        )
        return None


def refresh_runtime_context(
    db: Session,
    ctx: security.UserContext,
) -> security.UserContext | None:
    """Public fail-closed identity refresh used before every model call and dispatch."""
    return _reload_dispatch_context(db, ctx)


def _normalize_provider_origin(value: object, *, allow_path: bool) -> str | None:
    """Normalize one HTTP(S) origin without ever accepting embedded credentials or secrets."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or any(char.isspace() for char in raw):
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (not allow_path and parsed.path not in {"", "/"})
    ):
        return None
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    except (UnicodeError, AttributeError):
        return None
    if not hostname or any(char in hostname for char in "/?#@"):
        return None
    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 80 if scheme == "http" else 443
    suffix = "" if port is None or port == default_port else f":{port}"
    return f"{scheme}://{host}{suffix}"


def _normalized_origins(raw: object) -> tuple[tuple[str, ...], int]:
    if not isinstance(raw, str) or not raw.strip():
        return (), 0
    origins: set[str] = set()
    invalid = 0
    for item in re.split(r"[,\s]+", raw.strip()):
        if not item:
            continue
        origin = _normalize_provider_origin(item, allow_path=False)
        if origin is None:
            invalid += 1
        else:
            origins.add(origin)
    return tuple(sorted(origins)), invalid


def _is_loopback_origin(origin: str) -> bool:
    try:
        hostname = urlsplit(origin).hostname
        if hostname == "localhost":
            return True
        return bool(hostname and ipaddress.ip_address(hostname).is_loopback)
    except ValueError:
        return False


def _transport_allowed(settings, origin: str) -> bool:
    if origin.startswith("https://"):
        return True
    return (
        origin.startswith("http://")
        and getattr(settings, "environment", "dev") != "prod"
        and bool(getattr(settings, "agent_allow_loopback_http", False))
        and _is_loopback_origin(origin)
    )


_PROVIDER_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,127}")
_FORBIDDEN_PROVIDER_ENV = (
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "OPENAI_CUSTOM_HEADERS",
    "OPENAI_LOG",
    "SSLKEYLOGFILE",
)


def provider_ambient_environment_clean() -> bool:
    """Reject SDK/TLS ambient knobs that bypass httpx ``trust_env=False``."""
    return not any(os.environ.get(name) for name in _FORBIDDEN_PROVIDER_ENV)


def _normalize_provider_model(value: object) -> str | None:
    if not isinstance(value, str) or value != value.strip():
        return None
    return value if _PROVIDER_MODEL_RE.fullmatch(value) else None


def _normalized_models(raw: object) -> tuple[tuple[str, ...], int]:
    if not isinstance(raw, str) or not raw.strip():
        return (), 0
    models: set[str] = set()
    invalid = 0
    for item in re.split(r"[,\s]+", raw.strip()):
        if not item:
            continue
        model = _normalize_provider_model(item)
        if model is None:
            invalid += 1
        else:
            models.add(model)
    return tuple(sorted(models)), invalid


def _bounded_int_metadata(
    value: object,
    *,
    minimum: int,
    maximum: int,
    allow_none: bool = False,
) -> int | None | str:
    if allow_none and value is None:
        return None
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        return "invalid"
    return value


def _primary_request_options_metadata(settings) -> tuple[str, bool]:
    try:
        raw = settings.llm_extra_body_dict()
        normalized = config.normalize_llm_request_options({} if raw is None else raw)
        canonical = json.dumps(
            {} if normalized is None else normalized,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (AttributeError, TypeError, ValueError):
        return "invalid", False
    return canonical, True


def _runtime_config_metadata(settings) -> dict:
    provider_name = getattr(settings, "llm_provider", None)
    environment = getattr(settings, "environment", None)
    return {
        "environment": (
            environment if environment in {"dev", "test", "prod"} else "invalid"
        ),
        "enable_agent": getattr(settings, "enable_agent", None) is True,
        "llm_provider": (
            "openai_compatible" if provider_name == "openai_compatible" else "invalid"
        ),
        "llm_timeout_seconds": _bounded_int_metadata(
            getattr(settings, "llm_timeout_seconds", None), minimum=1, maximum=300
        ),
        "llm_max_retries": _bounded_int_metadata(
            getattr(settings, "llm_max_retries", None), minimum=0, maximum=5
        ),
        "llm_max_tokens": _bounded_int_metadata(
            getattr(settings, "llm_max_tokens", None),
            minimum=1,
            maximum=65_536,
            allow_none=True,
        ),
        "llm_max_tool_iters": _bounded_int_metadata(
            getattr(settings, "llm_max_tool_iters", None), minimum=1, maximum=16
        ),
        "vision_timeout_seconds": _bounded_int_metadata(
            getattr(settings, "vision_timeout_seconds", None), minimum=1, maximum=300
        ),
        "vision_max_pages": _bounded_int_metadata(
            getattr(settings, "vision_max_pages", None), minimum=1, maximum=16
        ),
        "ambient_provider_environment_clean": provider_ambient_environment_clean(),
    }


def _runtime_config_allowed(metadata: Mapping[str, object]) -> bool:
    return (
        metadata.get("enable_agent") is True
        and metadata.get("llm_provider") == "openai_compatible"
        and metadata.get("environment") != "invalid"
        and metadata.get("ambient_provider_environment_clean") is True
        and all(
            metadata.get(field) != "invalid"
            for field in (
                "llm_timeout_seconds",
                "llm_max_retries",
                "llm_max_tokens",
                "llm_max_tool_iters",
                "vision_timeout_seconds",
                "vision_max_pages",
            )
        )
    )


def _destination_attributes(
    destination: EgressDestination,
) -> tuple[str, str, str, str, str, str]:
    if destination is EgressDestination.PRIMARY_MODEL:
        return (
            "llm_base_url",
            "llm_model",
            "llm_approved_models",
            "llm_private_base_urls",
            "llm_approved_external_base_urls",
            "llm_api_key",
        )
    return (
        "vision_base_url",
        "vision_model",
        "vision_approved_models",
        "vision_private_base_urls",
        "vision_approved_external_base_urls",
        "vision_api_key",
    )


def _destination_trust_zone(settings, destination: EgressDestination) -> str:
    field = (
        "llm_trust_zone"
        if destination is EgressDestination.PRIMARY_MODEL
        else "vision_trust_zone"
    )
    return str(getattr(settings, field, "unknown"))


def _destination_policy_metadata(settings, destination: EgressDestination) -> dict:
    (
        base_field,
        model_field,
        approved_models_field,
        private_field,
        external_field,
        api_key_field,
    ) = _destination_attributes(destination)
    configured_url = getattr(settings, base_field, "")
    origin = _normalize_provider_origin(configured_url, allow_path=True)
    base_path = _provider_base_path(configured_url)
    model = _normalize_provider_model(getattr(settings, model_field, ""))
    approved_models, invalid_models = _normalized_models(
        getattr(settings, approved_models_field, "")
    )
    operator_asserted_private_origins, invalid_private = _normalized_origins(
        getattr(settings, private_field, "")
    )
    external_origins, invalid_external = _normalized_origins(
        getattr(settings, external_field, "")
    )
    options_json, options_valid = (
        _primary_request_options_metadata(settings)
        if destination is EgressDestination.PRIMARY_MODEL
        else ("{}", True)
    )
    return {
        "trust_zone": _destination_trust_zone(settings, destination),
        "origin": origin or "invalid",
        "base_path": base_path if base_path is not None else "invalid",
        "model": model or "invalid",
        "approved_models": approved_models,
        "invalid_approved_model_count": invalid_models,
        "request_options_json": options_json,
        "request_options_valid": options_valid,
        "api_key_configured": bool(getattr(settings, api_key_field, "")),
        # v1 verifies syntax and exact origin equality only.  It does not attest the network
        # route, Tailnet peer, DNS answer or TLS peer identity; production enablement remains
        # blocked on #225.  Name the weaker trust basis in every policy snapshot/fingerprint so a
        # durable executor cannot mistake an operator label for endpoint attestation.
        "private_trust_basis": "operator_assertion_only_v1",
        "private_endpoint_identity_attested": False,
        "operator_asserted_private_origins": operator_asserted_private_origins,
        "approved_external_origins": external_origins,
        "invalid_private_origin_count": invalid_private,
        "invalid_approved_external_origin_count": invalid_external,
    }


def _destination_allowed(settings, destination: EgressDestination) -> bool:
    if (
        destination is EgressDestination.PRIMARY_MODEL
        and not bool(getattr(settings, "agent_model_context_egress_enabled", False))
    ):
        return False
    runtime_metadata = _runtime_config_metadata(settings)
    if not _runtime_config_allowed(runtime_metadata):
        return False
    metadata = _destination_policy_metadata(settings, destination)
    origin = metadata["origin"]
    if (
        origin == "invalid"
        or metadata["base_path"] == "invalid"
        or metadata["model"] == "invalid"
        or metadata["invalid_approved_model_count"] != 0
        or metadata["model"] not in metadata["approved_models"]
        or metadata["request_options_valid"] is not True
        or metadata["api_key_configured"] is not True
        or not _transport_allowed(settings, origin)
    ):
        return False
    trust_zone = metadata["trust_zone"]
    if trust_zone == "private":
        return (
            metadata["invalid_private_origin_count"] == 0
            and origin in metadata["operator_asserted_private_origins"]
        )
    if trust_zone == "approved_external":
        return (
            metadata["invalid_approved_external_origin_count"] == 0
            and origin in metadata["approved_external_origins"]
        )
    return False


def _unattested_private_development_allowed(settings) -> bool:
    """Narrow test/dev escape hatch; deliberately impossible in production."""
    return (
        getattr(settings, "environment", None) in {"dev", "test"}
        and getattr(
            settings,
            "agent_allow_unattested_private_for_development",
            False,
        ) is True
    )


def _model_context_egress_enabled() -> bool:
    return _destination_allowed(get_settings(), EgressDestination.PRIMARY_MODEL)


RUNTIME_POLICY_VERSION = "v5"


def runtime_policy_fingerprint(settings=None) -> str:
    """Hash canonical provider/run authority without credential or customer values."""
    selected = get_settings() if settings is None else settings
    runtime_metadata = _runtime_config_metadata(selected)
    canonical = json.dumps(
        {
            "version": RUNTIME_POLICY_VERSION,
            "capability_policy_fingerprint": CAPABILITY_POLICY_FINGERPRINT,
            "model_context_egress_enabled": bool(
                getattr(selected, "agent_model_context_egress_enabled", False)
            ),
            "external_file_egress_enabled": bool(
                getattr(selected, "agent_external_file_egress_enabled", False)
            ),
            "allow_loopback_http": bool(
                getattr(selected, "agent_allow_loopback_http", False)
            ),
            "allow_unattested_private_for_development": bool(
                getattr(
                    selected,
                    "agent_allow_unattested_private_for_development",
                    False,
                )
            ),
            "runtime": runtime_metadata,
            "destinations": {
                destination.value: _destination_policy_metadata(selected, destination)
                for destination in EgressDestination
            },
            "edge_policy": {
                "approved_external_customer_file_mode": "disabled_pending_per_user_consent",
                "private_customer_file_mode": "attestation_required_or_dev_test_override",
                "all_declared_edges_must_pass": True,
                "conversation_context_sensitivity": DataSensitivity.CUSTOMER_FILE.value,
                "conversation_context_edge": {
                    "purpose": _conversation_context_edge().purpose,
                    "projection_id": _conversation_context_edge().projection_id,
                    "max_bytes": _conversation_context_edge().max_bytes,
                    "policy_version": _conversation_context_edge().policy_version,
                    "retention_policy": _conversation_context_edge().retention_policy,
                },
                "global_external_file_switch_is_not_user_consent": True,
                "admitted_contracts": sorted(
                    ({
                        "source": edge.source.value,
                        "destination": edge.destination.value,
                        "sensitivity": edge.sensitivity.value,
                        "purpose": edge.purpose,
                        "projection_id": edge.projection_id,
                        "max_bytes": edge.max_bytes,
                        "policy_version": edge.policy_version,
                        "retention_policy": edge.retention_policy,
                    } for edge in EGRESS_EDGE_CONTRACT_REGISTRY),
                    key=lambda edge: (
                        edge["destination"], edge["source"], edge["sensitivity"],
                        edge["purpose"], edge["projection_id"], edge["policy_version"],
                        edge["retention_policy"], edge["max_bytes"],
                    ),
                ),
            },
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _provider_profile_snapshot(
    settings,
    destination: EgressDestination,
) -> ProviderProfileSnapshot:
    runtime_metadata = _runtime_config_metadata(settings)
    destination_metadata = _destination_policy_metadata(settings, destination)
    primary = destination is EgressDestination.PRIMARY_MODEL
    timeout = runtime_metadata[
        "llm_timeout_seconds" if primary else "vision_timeout_seconds"
    ]
    retries = runtime_metadata["llm_max_retries"] if primary else 0
    max_tokens = runtime_metadata["llm_max_tokens"] if primary else None
    max_pages = None if primary else runtime_metadata["vision_max_pages"]
    return ProviderProfileSnapshot(
        destination=destination,
        adapter=str(runtime_metadata["llm_provider"]),
        origin=(
            str(destination_metadata["origin"])
            if destination_metadata["origin"] != "invalid"
            else ""
        ),
        base_path=(
            str(destination_metadata["base_path"])
            if destination_metadata["base_path"] != "invalid"
            else ""
        ),
        model=(
            str(destination_metadata["model"])
            if destination_metadata["model"] != "invalid"
            else ""
        ),
        request_options_json=(
            str(destination_metadata["request_options_json"])
            if destination_metadata["request_options_valid"] is True
            else "invalid"
        ),
        timeout_seconds=(float(timeout) if isinstance(timeout, int) else 0.0),
        max_retries=(int(retries) if isinstance(retries, int) else 0),
        max_tokens=(
            int(max_tokens)
            if isinstance(max_tokens, int) and not isinstance(max_tokens, bool)
            else None
        ),
        max_pages=(
            int(max_pages)
            if isinstance(max_pages, int) and not isinstance(max_pages, bool)
            else None
        ),
        enabled=runtime_metadata["enable_agent"] is True,
        # A profile lease represents the exact edge used by this adapter, not merely a matching
        # destination. In particular, prod private operator assertions are insufficient for the
        # customer-file-classified primary prompt and Vision input edges.
        admitted=(
            primary_model_call_allowed(settings)
            if primary
            else vision_provider_call_allowed(settings)
        ),
    )


def capture_runtime_policy_lease(settings=None) -> RuntimePolicyLease:
    """Capture one immutable policy epoch from exactly one Settings object."""
    selected = get_settings() if settings is None else settings
    runtime_metadata = _runtime_config_metadata(selected)
    max_tool_iters = runtime_metadata["llm_max_tool_iters"]
    return RuntimePolicyLease(
        fingerprint=runtime_policy_fingerprint(selected),
        primary=_provider_profile_snapshot(selected, EgressDestination.PRIMARY_MODEL),
        vision=_provider_profile_snapshot(selected, EgressDestination.VISION_PROVIDER),
        max_tool_iters=(
            int(max_tool_iters)
            if isinstance(max_tool_iters, int) and not isinstance(max_tool_iters, bool)
            else 0
        ),
        primary_api_key=str(getattr(selected, "llm_api_key", "")),
        vision_api_key=str(getattr(selected, "vision_api_key", "")),
    )


def runtime_policy_lease_current(
    lease: object,
    settings=None,
) -> bool:
    """Revalidate a run lease without accepting a raw/model-provided fingerprint string."""
    if type(lease) is not RuntimePolicyLease:
        return False
    selected = get_settings() if settings is None else settings
    return runtime_policy_fingerprint(selected) == lease.fingerprint


def _edge_egress_allowed(edge: EgressEdge, settings=None) -> bool:
    if not _valid_egress((edge,)):
        return False
    selected = get_settings() if settings is None else settings
    if not _destination_allowed(selected, edge.destination):
        return False
    if (
        edge.sensitivity is DataSensitivity.CUSTOMER_FILE
        and _destination_trust_zone(selected, edge.destination) == "approved_external"
    ):
        # A deployment-wide switch cannot represent the current user's informed consent. v1 has
        # no verifiable per-user grant/revocation record, so approved-external customer-file edges
        # remain unavailable even if the legacy/global switch is true. Private customer-file edges
        # have their own attestation/dev-only rule below; exact origin is never sufficient alone.
        return False
    if (
        edge.sensitivity is DataSensitivity.CUSTOMER_FILE
        and _destination_trust_zone(selected, edge.destination) == "private"
    ):
        metadata = _destination_policy_metadata(selected, edge.destination)
        if metadata.get("private_endpoint_identity_attested") is not True:
            # Exact origin + operator label is not permission to release customer files.  v1 has
            # no attestation implementation; only an explicit dev/test harness can exercise the
            # provider adapter until #225 binds a verifiable private endpoint identity.
            return _unattested_private_development_allowed(selected)
    return True


def primary_model_call_allowed(settings=None) -> bool:
    """Authorize a primary-model call containing any current conversation context.

    v1 has no durable per-message provenance. User text may contain pasted customer data and
    persisted assistant history may contain summaries derived from a prior customer-file tool.
    Therefore every prompt/history payload is conservatively classified as CUSTOMER_FILE until
    provenance-aware messages exist. Approved-external prompt egress stays disabled until a
    verifiable per-user consent/revocation record exists; a global environment switch is not consent.
    """
    return _edge_egress_allowed(_conversation_context_edge(), settings=settings)


def vision_provider_call_allowed(settings=None) -> bool:
    """Re-evaluate customer-file authorization directly before Vision network egress."""
    return _edge_egress_allowed(_vision_egress()[0], settings=settings)


def _provider_base_path(value: object) -> str | None:
    """Return one canonical ASCII base path, or fail closed.

    Provider base URLs are configuration, not arbitrary navigation targets. Percent-encoded,
    relative, repeated-separator and dot-segment paths are rejected so an SDK request cannot use
    URL normalization to escape the configured endpoint prefix after the origin check succeeds.
    The root path is represented as an empty prefix.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    path = parsed.path or ""
    if any(ord(char) < 0x21 or ord(char) > 0x7E for char in path):
        return None
    if "%" in path or "\\" in path:
        return None
    if path in {"", "/"}:
        return ""
    if not path.startswith("/"):
        return None
    segments = path.split("/")[1:]
    if segments and segments[-1] == "":
        segments.pop()
    if not segments or any(segment in {"", ".", ".."} for segment in segments):
        return None
    return f"/{'/'.join(segments)}"


def provider_http_request_allowed(
    profile: object,
    request_url: object,
    settings=None,
) -> bool:
    """Authorize one *actual* SDK HTTP attempt against the live provider profile.

    This is intentionally stricter than an origin-only allowlist: the request must target the
    exact ``chat/completions`` path under the currently configured base URL. The transport calls
    it for every attempt, including SDK retries, before the guarded delegate/network send.
    """
    selected = get_settings() if settings is None else settings
    if profile == "primary":
        if not primary_model_call_allowed(selected):
            return False
        base_field = "llm_base_url"
    elif profile == "vision":
        if not vision_provider_call_allowed(selected):
            return False
        base_field = "vision_base_url"
    else:
        return False

    configured_url = getattr(selected, base_field, "")
    configured_origin = _normalize_provider_origin(configured_url, allow_path=True)
    actual_origin = _normalize_provider_origin(request_url, allow_path=True)
    configured_path = _provider_base_path(configured_url)
    actual_path = _provider_base_path(request_url)
    if (
        configured_origin is None
        or actual_origin != configured_origin
        or configured_path is None
        or actual_path is None
    ):
        return False
    expected_path = f"{configured_path}/chat/completions"
    return actual_path == expected_path


def _json_projection(value: object) -> tuple[str, int] | None:
    """Serialize the declared JSON projection once and measure its actual UTF-8 wire bytes."""
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError):
        return None
    return serialized, len(serialized.encode("utf-8"))


def primary_model_payload_allowed(messages: object) -> bool:
    """Enforce the conversation edge's byte ceiling before every primary-model request."""
    projected = _json_projection(messages)
    return projected is not None and projected[1] <= _conversation_context_edge().max_bytes


def _tool_result_edges(name: object) -> tuple[EgressEdge, ...]:
    if not isinstance(name, str):
        return ()
    spec = _SPEC_BY_NAME.get(name)
    if not isinstance(spec, ToolSpec) or not _valid_egress(spec.egress):
        return ()
    return tuple(
        edge for edge in spec.egress
        if edge.destination is EgressDestination.PRIMARY_MODEL
        and edge.source in {
            EgressSource.TOOL_RESULT,
            EgressSource.CUSTOMER_FILE,
            EgressSource.VISION_OCR,
        }
    )


def tool_result_egress_allowed(name: object) -> bool:
    """Live-check every result edge immediately before a tool result can enter model context."""
    result_edges = _tool_result_edges(name)
    return bool(result_edges) and all(_edge_egress_allowed(edge) for edge in result_edges)


def capability_result_release_allowed(
    name: object,
    ctx: security.UserContext,
) -> bool:
    """Authorize one result against the latest named principal and every live result edge."""
    if not isinstance(name, str):
        return False
    spec = _SPEC_BY_NAME.get(name)
    return (
        isinstance(spec, ToolSpec)
        and _allowed(spec, ctx)
        and tool_result_egress_allowed(name)
    )


def serialize_tool_result_for_model(name: object, result: object) -> str | None:
    """Return a bounded tool-result projection, or ``None`` before it can enter model context."""
    result_edges = _tool_result_edges(name)
    if not result_edges or not tool_result_egress_allowed(name):
        return None
    projected = _json_projection(result)
    if projected is None:
        return None
    serialized, size_bytes = projected
    if (
        any(size_bytes > edge.max_bytes for edge in result_edges)
        or not tool_result_egress_allowed(name)
    ):
        return None
    return serialized


def vision_provider_payload_allowed(size_bytes: object) -> bool:
    """Cheap raw-size preflight; exact serialized Vision projection is checked separately."""
    return (
        isinstance(size_bytes, int)
        and not isinstance(size_bytes, bool)
        and 0 <= size_bytes <= _vision_egress()[0].max_bytes
    )


def vision_provider_projection_allowed(payload: object) -> bool:
    """Enforce the exact app-controlled Vision JSON/data-URL projection byte ceiling."""
    projected = _json_projection(payload)
    return projected is not None and projected[1] <= _vision_egress()[0].max_bytes


def vision_ocr_payload_allowed(text: object) -> bool:
    """Bound raw OCR text at the provider seam; wrapped tool-result JSON is checked again later."""
    return (
        isinstance(text, str)
        and len(text.encode("utf-8")) <= _vision_egress()[1].max_bytes
    )


def _schema_egress_allowed(spec: ToolSpec) -> bool:
    return _valid_egress(spec.egress) and all(
        _edge_egress_allowed(edge) for edge in spec.egress
    )


def _first_denied_edge(spec: ToolSpec) -> EgressEdge | None:
    return next((edge for edge in spec.egress if not _edge_egress_allowed(edge)), None)


def _external_egress_denied() -> dict:
    return {
        "error": "该文件可能需要外部视觉识别，但部署未授权客户文件外发",
        "kind": "external_egress_denied",
    }


def _model_context_egress_denied() -> dict:
    return {
        "error": "模型目标信任区未知或未授权业务/文件数据进入模型上下文",
        "kind": "model_context_egress_denied",
        "code": "AGENT_MODEL_EGRESS_DENIED",
        "retriable": False,
    }


def _argument_validation_denied(failure: ToolValidationFailure) -> dict:
    return {
        "error": failure.message,
        "kind": "validation_error",
        "code": failure.code,
        "retriable": False,
    }


def _sensitivity_egress_denied() -> dict:
    return {
        "error": "当前模型目标未获授权接收客户文件内容",
        "kind": "sensitivity_egress_denied",
    }


def audit_name(name: object) -> str:
    """Return a log-safe capability label; never log a model-invented tool name."""
    if not isinstance(name, str):
        return "unknown"
    spec = _SPEC_BY_NAME.get(name)
    return spec.name if isinstance(spec, ToolSpec) else "unknown"


def audit_summary(name: str, args: object) -> dict:
    """Describe argument shape without copying customer/model-provided values into logs."""
    spec = _SPEC_BY_NAME.get(name)
    summary: dict = {"arg_count": len(args) if isinstance(args, dict) else 0, "arg_keys": []}
    if not isinstance(spec, ToolSpec) or not isinstance(args, dict):
        return summary
    function = spec.schema.get("function")
    parameters = function.get("parameters") if isinstance(function, Mapping) else None
    properties = parameters.get("properties") if isinstance(parameters, Mapping) else None
    declared = set(properties) if isinstance(properties, Mapping) else set()
    # Iterate the small trusted schema, never an unbounded forged args mapping.
    keys = sorted(key for key in declared if key in args)[:MAX_PUBLIC_ARG_KEYS]
    summary.update({
        "effects": (
            sorted(effect.value for effect in spec.effects)
            if _valid_effects(spec.effects)
            else ["unclassified"]
        ),
        "egress_edges": (
            [
                {
                    "source": edge.source.value,
                    "destination": edge.destination.value,
                    "sensitivity": edge.sensitivity.value,
                    "purpose": edge.purpose,
                    "projection_id": edge.projection_id,
                    "max_bytes": edge.max_bytes,
                    "policy_version": edge.policy_version,
                    "retention_policy": edge.retention_policy,
                }
                for edge in spec.egress
            ]
            if _valid_egress(spec.egress)
            else ["unclassified"]
        ),
        "sensitivity": (
            spec.sensitivity.value
            if isinstance(spec.sensitivity, DataSensitivity)
            else "unclassified"
        ),
        "permission_id": (
            spec.permission_id
            if isinstance(spec.permission_id, str) and spec.permission_id
            else "unclassified"
        ),
        "arg_keys": keys,
    })
    collection_counts = {
        key: len(args[key]) for key in keys if isinstance(args[key], (list, dict))
    }
    string_lengths = {key: len(args[key]) for key in keys if isinstance(args[key], str)}
    if collection_counts:
        summary["collection_counts"] = collection_counts
    if string_lengths:
        summary["string_lengths"] = string_lengths
    return summary


def _sanitize_audit_summary(name: str, value: object) -> dict:
    """Validate an already-shaped summary without trusting caller-provided metadata or values."""
    safe = audit_summary(name, {})
    if not isinstance(value, dict):
        return safe
    spec = _SPEC_BY_NAME.get(name)
    if not isinstance(spec, ToolSpec):
        return safe
    function = spec.schema.get("function")
    parameters = function.get("parameters") if isinstance(function, Mapping) else None
    properties = parameters.get("properties") if isinstance(parameters, Mapping) else None
    declared = set(properties) if isinstance(properties, Mapping) else set()
    keys = value.get("arg_keys")
    if isinstance(keys, list):
        safe["arg_keys"] = sorted({
            key for key in keys[:MAX_PUBLIC_ARG_KEYS]
            if isinstance(key, str) and key in declared
        })
    count = value.get("arg_count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        safe["arg_count"] = count
    for field in ("collection_counts", "string_lengths"):
        raw_counts = value.get(field)
        if not isinstance(raw_counts, dict):
            continue
        counts = {}
        for key in safe["arg_keys"]:
            count = raw_counts.get(key)
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                counts[key] = count
        if counts:
            safe[field] = counts
    return safe


def artifact_ids_from_result(result: object) -> list[str]:
    """Extract only opaque, format-validated artifact identifiers from a tool result."""
    if not isinstance(result, dict):
        return []
    ids: list[str] = []
    for key in ("artifact_id", "file_id"):
        value = result.get(key)
        if _valid_file_reference(value) and value not in ids:
            ids.append(value)
    return ids


def source_file_ids_from_tool_exchange(args: object, result: object) -> tuple[str, ...]:
    """Capture the concrete legacy files whose bytes shaped one released tool result.

    This server-only record is never logged, persisted or exposed to the model/SSE stream. Inputs
    have already passed the capability validator, but canonicalization is repeated so future or
    alternate dispatchers cannot smuggle an ambiguous reference into a later authorization check.
    """
    values: list[object] = []
    if isinstance(args, dict):
        values.extend(args.get(key) for key in ("file_id", "base_file_id") if key in args)
    if isinstance(result, dict):
        values.extend(result.get(key) for key in ("artifact_id", "file_id") if key in result)
    source_ids: list[str] = []
    for value in values:
        try:
            canonical = agent_files.canonical_file_id(value)
        except agent_files.FileError:
            continue
        if canonical == value and canonical not in source_ids:
            source_ids.append(canonical)
    return tuple(source_ids[:4])


def artifact_id_release_allowed(artifact_id: object, ctx: security.UserContext) -> bool:
    """Authorize a legacy filesystem Artifact ID for one stable named owner.

    This is intentionally narrow.  #220 will replace it with source-scope/current-access
    provenance; until then shared/fallback identities and missing metadata fail closed.
    """
    if (
        ctx.authn != "sys_user"
        or not isinstance(ctx.user_id, str)
        or not ctx.user_id.strip()
    ):
        return False
    try:
        canonical = agent_files.canonical_file_id(artifact_id)
        return canonical == artifact_id and agent_files.owner_of(canonical) == ctx.user_id
    except agent_files.FileError:
        return False


def source_file_ids_release_allowed(
    source_file_ids: object,
    ctx: security.UserContext,
) -> bool:
    """Fresh owner/existence authorization for every file that shaped a model-visible result."""
    return bool(
        isinstance(source_file_ids, tuple)
        and len(source_file_ids) <= 4
        and len(source_file_ids) == len(set(source_file_ids))
        and all(artifact_id_release_allowed(file_id, ctx) for file_id in source_file_ids)
    ) or source_file_ids == ()


def fresh_artifact_authorizer(
    original_ctx: security.UserContext,
) -> Callable[[object], bool]:
    """Build a fail-closed projector guard that refreshes principal state per Artifact ID.

    The request context is only a signed snapshot.  Artifact IDs cross a later, independent
    disclosure boundary than the tool result itself, so account disable/token revocation/role
    changes must be observed again at the exact projector/API DTO seam.  In RBAC mode
    ``refresh_runtime_context`` uses its own short-lived SessionLocal and never trusts a long-lived
    handler transaction's identity map.
    """
    def authorize(artifact_id: object) -> bool:
        try:
            live_ctx = refresh_runtime_context(None, original_ctx)
            return bool(
                live_ctx is not None
                and security.page_allowed(live_ctx, "page_chat")
                and artifact_id_release_allowed(artifact_id, live_ctx)
            )
        except Exception as exc:  # noqa: BLE001 -- projector authorization fails closed
            _log.error(
                "agent artifact authorization refresh failed exception_type=%s",
                type(exc).__name__,
            )
            return False

    return authorize


def authorized_artifact_ids_from_result(
    result: object,
    ctx: security.UserContext,
) -> list[str]:
    return [
        artifact_id
        for artifact_id in artifact_ids_from_result(result)
        if artifact_id_release_allowed(artifact_id, ctx)
    ]


def safe_tool_trace_entry(
    name: object,
    args: object,
    artifact_ids: object = None,
    *,
    args_are_shape: bool = False,
) -> dict:
    """Build a value-free trace entry suitable for SSE, checkpoints and durable storage."""
    safe_name = audit_name(name)
    shape = (
        _sanitize_audit_summary(safe_name, args)
        if args_are_shape
        else audit_summary(safe_name, args)
    )
    # The marker contains no customer value and lets every downstream seam validate an already
    # shaped record again without degrading it into an empty legacy/raw-argument summary.
    entry = {"name": safe_name, "args": shape, "args_are_shape": True}
    if isinstance(artifact_ids, list):
        safe_ids = [
            value for value in artifact_ids[:MAX_ARTIFACT_IDS_PER_TRACE_ENTRY]
            if _valid_file_reference(value)
        ]
        if safe_ids:
            entry["artifact_ids"] = list(dict.fromkeys(safe_ids))
    return entry


def sanitize_tool_trace(trace: object) -> list[dict]:
    """Fail closed when a legacy/future caller hands persistence a raw tool trace."""
    if not isinstance(trace, list):
        return []
    clean: list[dict] = []
    for item in trace[:MAX_PUBLIC_TRACE_ENTRIES]:
        if not isinstance(item, dict):
            continue
        clean.append(safe_tool_trace_entry(
            item.get("name"),
            item.get("args"),
            item.get("artifact_ids"),
            args_are_shape=item.get("args_are_shape") is True,
        ))
    return clean


def dispatch(
    db: Session,
    name: str,
    args: object,
    ctx: security.UserContext,
    *,
    _policy_lease: object = None,
) -> dict:
    """执行一次工具调用：审计 → 派发 → 内部异常脱敏后回灌（不让对话崩掉、也不泄实现细节）。

    业务错由各工具显式 return {"error": 文案}（如"型号不存在""query 不能为空"）——这些是给
    模型自恢复的安全文案，原样回灌。这里的 except 只兜底**未预期的内部异常**（如 SQLAlchemyError
    会把 SQL 语句 + 表/列名带出）：服务端日志只保留能力名与异常类型，参数值和异常消息均不落日志；
    回灌给模型/用户的只有固定脱敏文案。否则裸异常经工具结果 → tool 消息 → SSE delta 直达终端用户。
    """
    spec = _SPEC_BY_NAME.get(name)
    audit = audit_summary(name, args)
    safe_name = audit_name(name)
    if not isinstance(spec, ToolSpec):
        security.record_access_log(ctx, f"agent_tool_denied:{safe_name}", "agent", audit)
        return _capability_denied()
    current_ctx = _reload_dispatch_context(db, ctx)
    if current_ctx is None:
        security.record_access_log(
            ctx,
            f"agent_tool_identity_denied:{safe_name}",
            "agent",
            audit,
        )
        return _identity_denied()
    if not _allowed(spec, current_ctx):
        security.record_access_log(
            current_ctx, f"agent_tool_denied:{safe_name}", "agent", audit
        )
        return _capability_denied()
    function = spec.schema.get("function")
    parameters = function.get("parameters") if isinstance(function, Mapping) else None
    if not isinstance(parameters, Mapping):  # import-time checks should make this unreachable
        failure = _VALIDATOR_FAILED
    else:
        try:
            failure = spec.validator(args, parameters, spec.budget)
            if failure is not None and not isinstance(failure, ToolValidationFailure):
                failure = _VALIDATOR_FAILED
        except Exception as exc:  # noqa: BLE001 -- fail closed and log no argument values
            _log.error(
                "agent tool validator failed name=%s exception_type=%s",
                safe_name,
                type(exc).__name__,
            )
            failure = _VALIDATOR_FAILED
    if failure is not None:
        security.record_access_log(
            current_ctx,
            f"agent_tool_args_denied:{safe_name}",
            "agent",
            {**audit, "validation_code": failure.code},
        )
        return _argument_validation_denied(failure)
    denied_edge = _first_denied_edge(spec)
    if denied_edge is not None:
        if denied_edge.destination is EgressDestination.VISION_PROVIDER:
            action = "agent_tool_vision_egress_denied"
            response = _external_egress_denied()
        elif (
            denied_edge.sensitivity is DataSensitivity.CUSTOMER_FILE
            and _destination_trust_zone(get_settings(), denied_edge.destination)
            == "approved_external"
        ):
            action = "agent_tool_sensitivity_denied"
            response = _sensitivity_egress_denied()
        else:
            action = "agent_tool_model_egress_denied"
            response = _model_context_egress_denied()
        security.record_access_log(current_ctx, f"{action}:{safe_name}", "agent", audit)
        return response
    requires_provider_lease = any(
        edge.destination is EgressDestination.VISION_PROVIDER for edge in spec.egress
    )
    if requires_provider_lease and (
        type(_policy_lease) is not RuntimePolicyLease
        or not runtime_policy_lease_current(_policy_lease)
    ):
        security.record_access_log(
            current_ctx,
            f"agent_tool_vision_egress_denied:{safe_name}",
            "agent",
            audit,
        )
        return _external_egress_denied()
    security.record_access_log(current_ctx, f"agent_tool:{safe_name}", "agent", audit)
    try:
        # ToolSpec is the immutable execution authority. _REGISTRY is a read-only compatibility
        # projection and is deliberately never consulted for dispatch.
        if requires_provider_lease:
            return _jsonable(
                spec.handler(
                    db,
                    args,
                    current_ctx,
                    _policy_lease=_policy_lease,
                )
            )
        return _jsonable(spec.handler(db, args, current_ctx))
    except agent_files.FileError:
        # FileError text often embeds model/customer-controlled IDs, sheet names or cells.
        # Return a fixed, non-retriable contract and never copy the exception message.
        _log.info("agent file operation rejected name=%s", safe_name)
        return {
            "error": "文件参数、格式或状态不符合要求",
            "kind": "file_error",
            "code": "AGENT_FILE_REJECTED",
            "retriable": False,
        }
    except Exception as exc:  # noqa: BLE001 —— 异常消息/参数可能含客户数据，日志只留类型
        _log.error("agent tool failed name=%s exception_type=%s", safe_name, type(exc).__name__)
        return {"error": "工具执行失败，请换个方式或稍后重试", "retriable": True, "kind": "internal"}
