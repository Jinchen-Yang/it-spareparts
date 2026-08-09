"""集中配置。

分两类：
1. Settings —— 运行环境配置（数据库连接、上传目录、登录密钥等），由环境变量驱动。
2. 业务规则开关（§8）—— 待客户确认的口径集中在此，确认后改这里即可，不动逻辑。
"""
from datetime import date
from decimal import Decimal
from functools import lru_cache

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# DeepSeek v4 为混合思考模型；默认开思考（reasoning_content 流式回前端，灰色可折叠展示）。
# 要关思考省 token：在 .env 设 LLM_EXTRA_BODY='{"thinking": {"type": "disabled"}}'。
_DEFAULT_LLM_EXTRA_BODY = '{"thinking": {"type": "enabled"}}'
_DEFAULT_MANIFEST_KEY_ID = "dev-v1"
_DEFAULT_MANIFEST_KEY = "change-me-maintenance-manifest-key-dev-v1"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "IT 备件智能管理系统"
    api_prefix: str = "/api"
    app_port: int = 8000
    environment: str = "dev"   # dev | prod；prod 下禁止使用默认口令/密钥

    # 数据库：默认指向 docker-compose 中的 db 服务；本地裸跑可用 .env 覆盖
    database_url: str = "postgresql+psycopg://spareparts:spareparts@db:5432/spareparts"

    # 原始上传文件归档目录
    raw_file_dir: str = "./data/raw"

    # 最小登录（§0/§15）
    admin_password: str = "admin"        # 初始化管理员口令，部署时用 .env 覆盖
    secret_key: str = "change-me-in-env" # token 签名密钥
    token_ttl_hours: int = 12

    # CORS 允许来源（前端开发服务器）
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # ---- 二期 AI 定价助手（LLM）----
    # provider 抽象：openai_compatible 可对接 DeepSeek/Qwen/Kimi/GLM 等一切 OpenAI 兼容端点，
    # 换厂商只改 base_url+model+key；将来要接 Anthropic 在 provider.py 加分支即可
    llm_provider: str = "openai_compatible"
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-v4-flash"
    llm_api_key: str = ""              # 空 = 未配置，chat 接口返回降级提示
    # 随请求透传的额外参数(JSON)。换 Qwen 等端点时改成对应参数(如 {"enable_thinking": false})；
    # 设 {} = 明确不传；留空/不设 = 用默认(开思考)
    llm_extra_body: str = _DEFAULT_LLM_EXTRA_BODY
    llm_max_tool_iters: int = 8        # 一次问答最多工具往返轮数（文件流程需 4-6 轮）
    llm_timeout_seconds: int = 60
    llm_max_tokens: int | None = None  # 单次生成长度上限；None=不传（用端点默认）。防长答滚雪球/控成本
    llm_max_retries: int = 2           # 显式化 openai SDK 对 429/5xx 的指数退避重试次数（便于审计调参）
    enable_agent: bool = True

    # 成本/库存切换是独立生产闸门；代码、迁移与审批 manifest 都不会自动启用新口径。
    maintenance_cutover_enabled: bool = False
    maintenance_migration_max_body_bytes: int = 2_000_000
    maintenance_manifest_active_key_id: str = _DEFAULT_MANIFEST_KEY_ID
    maintenance_manifest_active_hmac_key: SecretStr = SecretStr(_DEFAULT_MANIFEST_KEY)
    maintenance_manifest_previous_hmac_keys_json: SecretStr = SecretStr("none")

    # ---- 三期 视觉识别（图片/扫描件 → 文本）----
    # 独立 key/端点，默认 通义 Qwen-VL（DashScope OpenAI 兼容）。空 = 未配置，图片走降级
    vision_api_key: str = ""
    vision_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    vision_model: str = "qwen-vl-max"
    vision_max_pages: int = 8          # 单次最多送几页图（扫描件 PDF / 多图）
    vision_timeout_seconds: int = 90

    @field_validator("llm_extra_body", mode="before")
    @classmethod
    def _extra_body_default(cls, v):
        # docker-compose 透传空字符串时回退到默认，避免悄悄打开思考模式
        return _DEFAULT_LLM_EXTRA_BODY if v is None or str(v).strip() == "" else v

    @field_validator("llm_extra_body", mode="after")
    @classmethod
    def _extra_body_valid_json(cls, v: str) -> str:
        # 启动期就校验合法性：非法 JSON 直接拒启（loud），不再每请求解析失败静默回退成
        # None——那会悄悄丢掉关思考标志、变慢变贵，与本配置反复声明的意图相悖（RUNTIME-6）。
        import json
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM_EXTRA_BODY 不是合法 JSON：{exc}；设为 '{{}}' 表示不传额外参数") from exc
        if not isinstance(parsed, dict):
            raise ValueError("LLM_EXTRA_BODY 必须是 JSON 对象，如 '{\"thinking\": {\"type\": \"disabled\"}}'")
        return v

    def llm_extra_body_dict(self) -> dict | None:
        """解析 LLM_EXTRA_BODY（构造期已校验合法）→ dict；空对象 {} 返回 None（不透传）。"""
        import json
        return json.loads(self.llm_extra_body) or None

    @model_validator(mode="after")
    def _manifest_keyring_is_valid(self):
        key_id = self.maintenance_manifest_active_key_id.strip()
        if not key_id or len(key_id) > 64:
            raise ValueError("MAINTENANCE_MANIFEST_ACTIVE_KEY_ID 无效")
        active_secret = self.maintenance_manifest_active_hmac_key.get_secret_value()
        if len(active_secret.encode("utf-8")) < 32:
            raise ValueError("maintenance manifest HMAC 密钥至少需要 32 字节")
        if active_secret == self.secret_key:
            raise ValueError("maintenance manifest HMAC 密钥必须独立于 SECRET_KEY")
        previous_keys = self._maintenance_manifest_previous_keys()
        if key_id in previous_keys:
            raise ValueError("active manifest key_id 不能同时出现在 previous keyring")
        for candidate_id, secret in previous_keys.items():
            if not candidate_id.strip() or len(candidate_id) > 64:
                raise ValueError("MAINTENANCE_MANIFEST_PREVIOUS_HMAC_KEYS 包含无效 key_id")
            if len(secret.encode("utf-8")) < 32:
                raise ValueError("maintenance manifest HMAC 密钥至少需要 32 字节")
            if secret == self.secret_key:
                raise ValueError("maintenance manifest 历史密钥必须独立于 SECRET_KEY")
        return self

    def _maintenance_manifest_previous_keys(self) -> dict[str, str]:
        import json
        raw = self.maintenance_manifest_previous_hmac_keys_json.get_secret_value().strip()
        if not raw or raw.lower() == "none":
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("MAINTENANCE_MANIFEST_PREVIOUS_HMAC_KEYS_JSON 不是合法 JSON") from exc
        if not isinstance(parsed, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in parsed.items()
        ):
            raise ValueError("MAINTENANCE_MANIFEST_PREVIOUS_HMAC_KEYS_JSON 必须是字符串映射")
        return parsed

    def maintenance_manifest_signing_material(self) -> tuple[str, bytes]:
        key_id = self.maintenance_manifest_active_key_id.strip()
        secret = self.maintenance_manifest_active_hmac_key.get_secret_value()
        return key_id, secret.encode("utf-8")

    def maintenance_manifest_verification_keys(self) -> dict[str, bytes]:
        previous = {
            key_id: secret.encode("utf-8")
            for key_id, secret in self._maintenance_manifest_previous_keys().items()
        }
        active_id, active_key = self.maintenance_manifest_signing_material()
        return {**previous, active_id: active_key}


_DEFAULT_ADMIN_PW = "admin"
_DEFAULT_SECRET = "change-me-in-env"
# PostgreSQL 事务级 advisory lock：导入/成本重算取独占锁，导出取共享锁，
# 保证资源预检与后续 ORM 物化看到同一批业务数据。
DATA_CHANGE_ADVISORY_LOCK_KEY = 0x5350_4152  # "SPAR"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def check_security(settings: "Settings") -> list[str]:
    """返回默认弱口令/密钥的告警列表。prod 环境下由 main 启动时拒绝启动。"""
    warns = []
    if settings.admin_password == _DEFAULT_ADMIN_PW:
        warns.append("ADMIN_PASSWORD 仍为默认值 'admin'")
    if settings.secret_key == _DEFAULT_SECRET:
        warns.append("SECRET_KEY 仍为默认值（token 可被离线伪造）")
    if (
        settings.maintenance_manifest_active_hmac_key.get_secret_value()
        == _DEFAULT_MANIFEST_KEY
    ):
        warns.append("维保 manifest 仍使用默认开发签名密钥")
    if "spareparts:spareparts" in settings.database_url:
        warns.append("数据库使用默认弱口令 spareparts:spareparts")
    return warns


# ============================================================
# §8 业务规则开关 —— 待客户确认项集中于此
# ============================================================

# 成本法（甲方确认：移动加权 + FIFO，均为时间序列法，引擎两种都算）
# active 法写入 f_sales_line.matched_cost 等字段，供三维度聚合默认口径
COST_METHOD = "moving_avg"                       # moving_avg | fifo

# FIFO/移动加权 排队范围
FIFO_SCOPE = "global"                            # global（按型号全局，默认；采购无仓库字段）| warehouse

# 期初库存成本口径（7w 库存快照无历史成本/批次）
# none：销售早于任何采购批次 → no_cost；fallback_recent：用该型号最近采购价兜底
OPENING_COST_POLICY = "fallback_recent"          # none | fallback_recent

# 计入成本的采购类型（真实导出值：销售订单/维保需求/指定采购）
# 决定哪些采购构成成本批次层。
COST_PURCHASE_TYPES = ["销售订单", "指定采购", "维保需求"]   # 甲方确认：维保采购价计入成本(影响均价)

# 计入营收的销售业务类型（实测：备件销售/销售换货/整机销售）
# 甲方确认(v1)：仅"备件销售"计营收；维保/换货/整机销售不计——维保营收以后单独开模块再算
REVENUE_BUSINESS_TYPES = ["备件销售"]

# 历史兼容配置：正式业务计算已固定双口径 + 13%，不再允许此项改变计算事实。
TAX_BASIS = "ex_tax"

# 正式利润统一税率（甲方 2026-07-11：采购与销售统一按 13% 做业务计算口径，
# 与 2026 年施行的《增值税法》一般货物 13% 一致）。
# 含税÷1.13 → 未税；未税×1.13 → 含税。
# 原则：**不覆盖原始单据的 tax_rate / 0% / 空税率**（保留可追溯），仅用于生成"未税计算字段"。
# 销售 unit_price 恒含税 → 一律 ÷1.13；采购 unit_price 口径跟随头表 is_tax_inclusive：
# 只有明确含税单才 ÷1.13；明确不含税或口径未知均按未税原值。旧的"逐单 tax_rate 换算"
# 因大量 0%/空税率会静默虚高未税额，已弃用，统一到此常量。
PROFIT_VAT_RATE = Decimal("0.13")

# ---- 维保出库成本（客户 2026-07 确认口径，docs/维保出库成本核算-开发方案.md §0）----
# 注意：与销售毛利的 ex_tax/移动加权是"刻意不同"的两套口径（客户拍板），不是疏漏。
MAINT_COST_START_DATE = date(2024, 1, 1)          # 项目成本起算日；此前出库行不计价（cost_source=NULL）
MAINT_TRACE_MAX_MONTHS = 3                        # 均价追溯上限（月）；≥1 个月前端必须标注追溯月数
MAINT_SALES_REF_BUSINESS_TYPES = ["备件销售"]      # "没有采购有销售"参考池的业务类型（维保类销售单是一口价占位，无真实单价）
MAINT_POOL_EXCLUDE_PNS = ["一批备件"]              # 打包采购占位 PN（实测 13 行 2,333 万），不进任何价格池
MAINT_TAX_PREFERENCE = "inc_first"                # 同一取价层含税/不含税并存时优先含税（inc_first | ex_first）
# ---- v2（§16，1633 行财务手工价黄金样本校准）----
MAINT_PRICE_WINDOW_DAYS = 7                       # ±窗口天数：出库日 ±7 天内最近采购价优先于当月均价（同距取更早、同日加权）
MAINT_BUDGET_WARN_PCT = Decimal("0.20")           # 盈亏看板黄灯阈值：剩余预算占比 ≤20% 报警（用户口径"只剩 20% 就报警"）
MAINT_EXPENSE_ACTIVE_STATUS = "已结束"             # 报销单生效口径（其余枚举待全量数据确认）

# 目标毛利率（报价提示/低毛利标记用；整机拆解的"建议售价"=成本×1/(1-此值)）
TARGET_MARGIN = Decimal("0.20")

# ---- 互通 PN 池（人工池，2026-07-13 起唯一真值；互通PN池价格分析 §15/§21）----
# 池由人工在「互通PN池管理」页创建维护，替代关系变化不再自动改池（自动重算已停用）。
# 稳定 group_id 序列语义保留：单调递增、退役 ID 永不复用。
# 约束价（采购上限/销售下限）统一未税入库；含税录入按统一 13% 口径换算（含税÷1.13），
# 原始录入值与口径保留可追溯。只用于池约束价换算，不触碰利润引擎的 PROFIT_VAT_RATE 调用方。
POOL_POLICY_VAT_RATE = Decimal("0.13")
POOL_OVERSIZE_MEMBERS = 30        # 成员超此数 → oversized 标记，需人工确认（生产最大池 59）
POOL_PREMIUM_WARN_PCT = Decimal("0.20")   # 采购/销售溢价率 ≥20% 视为"品牌溢价"候选（相对池基准）
# 标杆型号"供应可得"门槛（复审二轮 P1-5：原来只查"有采购单+有日期"太松，单样本/远古采购也算）：
# 必须 ≥N 张去重采购单（非单样本）、≥1 家供应商、且最近采购在 recent_days 窗口内。仍不代表可替换。
POOL_SUPPLY_MIN_ORDERS = 2        # 标杆型号至少 2 张去重采购单（排除单样本偶发）
POOL_SUPPLY_MIN_SUPPLIERS = 1     # 至少 1 家可识别供应商
POOL_SUPPLY_RECENT_DAYS = 365     # 最近采购须在此天数内（远古采购不算"供应可得"）
POOL_RANK_ANALYZE_CAP = 500       # 全局按节省额排名时最多分析多少个池（生产 ~40）；超限退回成员数排序

# 整机拆解/批量查价取"近 N 天采购价"窗口（客户要"最近15天采购价"）
RECENT_PURCHASE_DAYS = 15

# 成交价参考（销售出价用）：不给"建议售价"（销售自行把握加价），只给一个稳的成交价参考。
# 取近 REF_PRICE_MAX_N 条且 REF_PRICE_DAYS 天内的成交价，按名次线性加权平均：
# 最近一条权重最高、依次递减到 1（越近权重越高），弱化单笔异常价对参考的扰动。
# 均价/参考价只剔除 ¥0（赠送/换货/录入0价）；硬件成交价波动大，有真实售价即计入，不做离群裁剪。
REF_PRICE_DAYS = 30   # 取样时间窗（天）
REF_PRICE_MAX_N = 5   # 最多取最近几条

# 只统计已生效（入库不过滤，业务查询过滤）
ACTIVE_STATUS_ONLY = True
ACTIVE_STATUS = "已生效"   # "生效"状态字面量单一真值源（曾在 5 个 service 各定义一遍）

# 导入模式：skip（默认，重复行跳过）| upsert（重复行更新可修复字段）
DEFAULT_IMPORT_MODE = "skip"

# 库存导入排除仓（甲方 2026-07-03：坏品不进系统）——仓库名含任一关键词的行整行跳过，
# 计入导入报告 rows_excluded_warehouse，不算错误。注意「废品仓」甲方明确保留，勿加"废品"。
INVENTORY_EXCLUDED_WAREHOUSES = ("坏品",)

# 文件上传上限（MB）
MAX_UPLOAD_MB = 100
# 单次批量导入文件数上限
MAX_IMPORT_FILES = 20
# XLSX 本质是 ZIP。以下阈值在 openpyxl/pandas 之前只读中央目录校验，
# 防止超多成员、超大解压体积或异常压缩比消耗 worker 内存/CPU。
IMPORT_XLSX_MAX_WORKSHEETS = 100
IMPORT_XLSX_MAX_MEMBERS = 10_000
IMPORT_XLSX_MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
IMPORT_XLSX_MAX_COMPRESSION_RATIO = 200.0
IMPORT_XLSX_MAX_COLUMNS = 512
IMPORT_XLSX_MAX_DECLARED_CELLS = 5_000_000
# 单文件行数上限：超过则拒绝，避免 pandas 全量物化撑爆内存拖垮单 worker（审计 2026-06-28 I-2）。
# 取值兼顾 2G 容器内存与真实导出体量（中石化逐月导出单文件远低于此）。
IMPORT_MAX_ROWS = 300_000

# ============================================================
# §8.6 近似检索（二期）：PN 解析器口径（主数据治理候选发现的基础设施）
# ============================================================

# 品牌同义词组：查询命中组内任一写法时，整组其余写法都加入检索词
# （解决"super"找"超微/Supermicro"、"希捷"找 Seagate 描述等中英混写）
BRAND_SYNONYMS: list[list[str]] = [
    ["supermicro", "super", "超微"],
    ["seagate", "希捷"],
    ["western digital", "wd", "西数", "西部数据"],
    ["hpe", "hp", "惠普"],
    ["dell", "戴尔"],
    ["lenovo", "联想"],
    ["huawei", "华为"],
    ["h3c", "华三", "新华三"],
    ["cisco", "思科"],
    ["intel", "英特尔"],
    ["samsung", "三星"],
    ["hynix", "skhynix", "海力士"],
    ["micron", "镁光", "美光"],
    ["kingston", "金士顿"],
    ["toshiba", "东芝"],
    ["fujitsu", "富士通"],
    ["inspur", "浪潮"],
    ["sugon", "曙光"],
    ["ibm"],
    ["broadcom", "lsi", "avago", "emulex", "博通"],
    # 服务器备件常见跨标号召回（同一物理件不同 OEM/原厂标号），轻量 C 扩充
    ["marvell", "qlogic", "aquantia"],
    ["nvidia", "mellanox", "connectx", "英伟达"],
    ["microchip", "adaptec", "pmc", "pmc-sierra"],
    ["kioxia", "铠侠"],
    ["delta", "台达"],
    ["seagate", "希捷", "exos"],
]

# 搜索零命中时落 sys_audit_log（action=search_miss）——数据治理工单来源：缺别名/缺型号
SEARCH_MISS_LOG = True

# 解析结果置信度阈值：top1 低于该分视为"低置信"，前端/智能体应引导消歧而非直接采用
RESOLVE_LOW_CONFIDENCE = 0.35


# ============================================================
# §8.7 PN 主数据治理口径（整改）
# ============================================================

# 合并候选门槛（审核说明 §7.6）：resolver 得分低于此不入候选队列；
# 任何分数都不自动合并，一律人工审核
CANDIDATE_MIN_SCORE = 0.70

# 每个待审型号最多生成几条合并候选
CANDIDATE_TOP_N = 3

# 疑似重复组的垃圾 compact 过滤：pn_compact 短于此长度或纯数字（如 '3'/'CPU'/'15M'）
# 属于偶然碰撞而非真实重复，不进合并候选队列，按非标型号治理
DUP_COMPACT_MIN_LEN = 5

# 品牌占位符黑名单（命中者不入品牌字典、brand_id 置空并记质量问题）
BRAND_PLACEHOLDERS = ["待定", "无", "N/A", "NA", "其他", "其它", "暂无", "未知"]

# 审核积压提醒阈值（天）：候选超过 N 天未审核进入提醒（审核说明 §7.7）
CANDIDATE_STALE_DAYS = 7


# ============================================================
# §8.8 采购分析面板（早会/周会：识别频发应急采购→转批量；按来源拆分）
# ============================================================

# "频发"判定的默认采购次数阈值（窗口内某型号采购次数 ≥ 此值 → 高亮/计入"待计划"）。
# 阈值只影响标记与计数，不裁剪数据；用户可在面板上自行调整再判断。
ANALYSIS_FREQ_THRESHOLD = 3
ANALYSIS_DEFAULT_DAYS = 7        # 面板默认时间窗（天）
ANALYSIS_DAILY_MAX_DAYS = 31     # 逐日 sparkline 仅在窗口 ≤ 此天数时返回（更长窗只给次数/区间）
ANALYSIS_TOP_N = 100             # 主排行默认最多返回型号数（早会聚焦头部，超出在 KPI 提示）
# days 参数上限：聚合需整窗数据进内存，故上限收到约一年（前端最大也只到 30/365）。
ANALYSIS_MAX_DAYS = 366
# 取数硬上限：超过则截断并在 KPI 回 over_limit=True（防超大窗口拉爆内存）
ANALYSIS_MAX_LINES = 50000
# 默认排除的采购类型（指定采购=大额批量补库，库里一定有，不是要分析的应急采购）
ANALYSIS_EXCLUDE_SOURCE_TYPES = ["指定采购"]
# 含税单缺失税率时的兜底增值税率（行业 13%）；仅用于把含税价换算未税
ANALYSIS_FALLBACK_VAT = Decimal("0.13")

# 采购来源渠道分类（命中靠前者优先）。维修/回收来自「供应商类型」，其余按供应商名关键词。
SOURCE_CHANNEL_NAME_KEYWORDS = [
    ("淘宝", ["淘宝"]),
    ("京东", ["京东"]),
    ("拼多多", ["拼多多", "拼夕夕"]),
    ("闲鱼", ["闲鱼", "咸鱼"]),
    ("个人", ["个人"]),
]
# 中文企业词按子串匹配（中文无词边界问题）
SOURCE_CHANNEL_COMPANY_WORDS = ["公司", "有限", "科技", "实业", "商贸", "网络", "中心", "集团",
                                "电子", "数码", "技术", "贸易", "信息", "物资", "通信", "系统",
                                "设备", "厂"]
# 英文企业词按"整词"匹配（避免 'inc' 误命中 Prince/Vince、'co' 误命中 company 名内子串）
SOURCE_CHANNEL_COMPANY_WORDS_EN = ["store", "inc", "ltd", "co", "corp", "llc"]
SOURCE_CHANNEL_PERSONAL = "个人"
SOURCE_CHANNEL_DEFAULT = "正规供应商"
SOURCE_CHANNEL_UNKNOWN = "未分类"


# ============================================================
# §8.5 权限（三期启用：防恶性竞争）
# ============================================================
# 三期开启：销售只看"匿名行情 + 自己明细"，查不到同事的客户/报价；老板/管理员看全量。
# 防御在数据层（行级匿名化 + 禁用按客户/销售员排名），不靠提示词。
ENABLE_RBAC = True
ENABLE_ACCESS_LOG = True          # 记录谁查了什么型号/客户，便于审计

PHASE1_BYPASS_ROLE = "phase1_full_access"   # 第一期统一上下文，语义=临时全量
GUEST_ROLE = "guest"                         # 认证失败兜底角色，绝不可是 admin
MASK_VALUE = None                            # 脱敏字段统一置 null（保留字段名）
MASK_TEXT_FOR_EXPORT = "无权限查看"          # 导出场景占位文字

# 字段组：权限按"组"控制，不按单字段，避免漏字段被反推
FIELD_GROUPS = {
    "supplier_info": ["supplier_id", "supplier_name", "supplier_code",
                      "supplier_type", "supplier_contact", "supplier_phone", "supplier",
                      # §8.8 采购来源渠道也属"从谁/从哪类进货"情报，随 data_supplier 一并遮
                      "source_channel", "channel"],
    "customer_info": ["customer_id", "customer_name", "customer_city",
                      "customer_contact", "customer_phone", "customer", "end_customer"],
    # 注：unit_price 在采购行=成本、销售行=售价(营收)同名 → sales 关 data_purchase_cost 时
    # 逐行 unit_price 一并遮掉（个别成交价对销售也不外露）。销售要的是「聚合成交参考价」，
    # 由 avg_sale_price / avg_sale_price_90d / ref_sale_price 提供（不在本组，sales 可见）。
    # ⚠️ 脱敏靠精确 key 匹配：服务层产出的派生键名必须逐字登记，差一字即漏（见下面成组补登）。
    "purchase_cost": ["unit_price", "avg_cost", "latest_cost", "matched_cost",
                      "weighted_avg_cost", "cost_moving_avg", "cost_fifo",
                      "cost_moving_avg_ex", "cost_moving_avg_inc",
                      "cost_fifo_ex", "cost_fifo_inc",
                      "revenue_costed_ex", "revenue_costed_inc",
                      "line_amount", "inventory_value", "unit_cost",
                      "recent_purchase_price", "cost", "cost_amount",
                      # part_overview._profit_summary 的聚合成本派生键
                      "avg_purchase_cost", "avg_cost_moving", "avg_cost_fifo",
                      # part_overview.quick_pricing 的近期采购价窗口键（AI 批量查价/整机拆解）
                      "last_purchase_price", "recent_purchase_avg",
                      "recent_purchase_min", "recent_purchase_max",
                      # §8.8 采购分析面板派生价格/金额键（任何新派生键必须登记，否则对无
                      # data_purchase_cost 角色静默泄漏成本——见 2026-06-15 销售越权教训）
                      "total_amount", "total_amount_inc", "total_amount_ex",
                      "amount", "amount_inc", "amount_ex", "price_ex", "price_inc",
                      "price_ex_min", "price_ex_max", "price_ex_last", "price_ex_avg",
                      "price_inc_min", "price_inc_max", "price_inc_last", "price_inc_avg",
                      "price_last",
                      # 维保项目成本派生键（maintenance_cost 聚合 cost_total/inc/ex + 明细行
                      # unit_cost/cost_amount 已在上；成本来源/取价元信息同样可反推成本口径，一并登记）
                      "cost_total", "cost_inc", "cost_ex",
                      "actual_cost_inc", "actual_cost_ex",
                      "estimated_cost_inc", "estimated_cost_ex",
                      "known_cost_total",
                      "actual_lines", "estimated_lines", "missing_cost_lines",
                      "cost_quality", "cost_tier", "by_source", "coverage_pct",
                      "cost_source", "cost_tax_basis", "price_month", "trace_months",
                      "linked_purchase_order_no",
                      # 维保双税成本底座与历史补价 provenance（均可反推采购/销售成本事实）
                      "unit_cost_inc_tax", "unit_cost_ex_tax",
                      "cost_amount_inc_tax", "cost_amount_ex_tax",
                      "parts_cost_inc_tax", "parts_cost_ex_tax",
                      "parts_cost_inc_tax_complete", "parts_cost_ex_tax_complete",
                      "parts_cost_inc_tax_quality", "parts_cost_ex_tax_quality",
                      "parts_cost_inc_tax_missing_lines", "parts_cost_ex_tax_missing_lines",
                      "reference_side", "reference_pool_group_id",
                      "reference_pool_version", "reference_sample_count",
                      "reference_from_date", "reference_to_date",
                      "reference_latest_date",
                      # 稳定项目成本证据与销售回退估算（#203）。这些派生金额、行数、
                      # 估算标记和展示口径均会暴露成本事实；逐字登记，保证未来任一响应
                      # 复用时也由 apply_field_visibility 统一失败关闭。
                      "site_requisition_priced_cost_ex_tax",
                      "site_requisition_priced_cost_inc_tax",
                      "sales_estimate_cost_ex_tax", "sales_estimate_cost_inc_tax",
                      "sales_estimate_lines", "sales_estimate_count",
                      "cost_progress_includes_sales_estimate", "cost_progress_label",
                      "cost_evidence_kind", "cost_is_estimate", "cost_source_label",
                      # 老板看板（dashboard/pool）派生成本键：采购额、采购价统计容器、
                      # 未税单价、池标杆成本、双端溢价、两级节省——全部反推采购成本，随 data_purchase_cost 遮。
                      # 容器级登记（purchase_price/benchmark/savings）避免与销售侧同名内层键(wavg/median)冲突。
                      "purchase_ex_tax", "purchase_inc_tax",
                      "purchase_price", "unit_price_ex_tax", "unit_price_inc_tax",
                      "amount_ex_tax", "amount_inc_tax",
                      "benchmark", "savings", "theoretical_saving", "supply_available_upper",
                      "theoretical_max", "unit_saving", "cost_ex_tax", "purchase_premium_pct",
                      # 订单拉通-采购侧一单一行的未税采购额（键名带 total_ 前缀，与上面容器键
                      # purchase_ex_tax 不同名，复审 P0：漏登记会绕过 data_purchase_cost 泄漏采购额）
                      "total_ex_tax", "total_inc_tax",
                      # 池成员"采购溢价判定"布尔：反推该型号采购价高于标杆（采购成本比较信号）
                      "brand_premium_purchase",
                      # 维保 v2（§16）：盈亏看板与取价元信息派生键（budget=合同额亦可反推毛利）
                      "spent", "spent_parts", "spent_expense", "budget",
                      "remaining", "remaining_pct", "low_conf_pct",
                      "price_distance_days", "confidence",
                      # 看板 v2（订单嵌套 parts / 池窗口指标）：池采购均价、采购指标容器
                      # （内含与销售侧同名的 total_amount/weighted_avg_unit_price 等，容器级遮，
                      # 沿用 purchase_price 容器先例）。pool_avg_delta* 两侧同名——销售侧行价
                      # 被本组遮（unit_price 同名先例）而池销售均价公开，差额不遮即可
                      # "均价+差额"反推行价，故差额随本组一起遮。
                      "pool_avg_purchase_price", "purchase_metrics",
                      "pool_avg_delta", "pool_avg_delta_pct",
                      # 数据治理汇总里的成本可追溯派生信号；数值虽非金额，仍会暴露
                      # 成本是否匹配/估算，随采购成本权限一起隐藏。
                      "sales_no_cost", "sales_fallback_cost", "traceable_pct",
                      # 与人工约束价的差额：价-差=约束价、价-差=行价，两头都能反推 →
                      # 双登记（本组 + pool_price_governance），任一组关闭即遮
                      "manual_limit_delta", "manual_limit_delta_pct"],
    # 毛利金额：能反推成本（profit.aggregate 两法派生键一并登记）
    # total_gross_profit = 订单拉通-销售侧一单一行毛利（键名带 total_ 前缀，与 gross_profit
    # 不同名，复审 P0：漏登记会绕过 data_profit 泄漏毛利）
    "profit_amount": ["gross_profit", "gross_profit_ex", "gross_profit_inc",
                      "gross_profit_moving", "gross_profit_moving_ex",
                      "gross_profit_moving_inc",
                      "gross_profit_fifo", "gross_profit_fifo_ex",
                      "gross_profit_fifo_inc",
                      "total_gross_profit", "total_gross_profit_ex",
                      "total_gross_profit_inc",
                      # 维保合同毛利、费用门禁和状态；状态本身也会泄漏盈亏/证据结论，
                      # 必须随 data_profit 一起递归遮蔽。不要登记通用 revenue_inc/ex：
                      # 普通销售营收使用同名键且本来公开；维保服务在受限分支结构化清空收入。
                      "parts_gross_profit_inc", "parts_gross_profit_ex",
                      "parts_profit_status_inc", "parts_profit_status_ex",
                      "expense_inc", "expense_ex",
                      # 费用快照完整性与证据状态本身会泄漏逐合同财务数据覆盖情况。
                      "expense_data_available", "expense_evidence_status",
                      "contribution_profit_inc", "contribution_profit_ex",
                      "contribution_status_inc", "contribution_status_ex",
                      # 维保预算消耗参考决策；禁止登记通用 status（会误伤流程状态）
                      "decision_status", "contract_amount", "total_contract_amount",
                      "budget", "remaining", "remaining_pct"],
    # 毛利率：见反推警告（_profit_summary 与 profit.aggregate 的两法派生键一并登记）
    "profit_rate":   ["gross_margin", "avg_margin", "margin_band",
                      "avg_margin_moving", "avg_margin_fifo",
                      "gross_margin_moving", "gross_margin_fifo",
                      "parts_gross_margin_inc", "parts_gross_margin_ex",
                      "contribution_margin_inc", "contribution_margin_ex",
                      # 数据治理汇总/指标里的盈亏派生结论同样属于利润信息。
                      "sales_neg_margin", "margin_computable_pct"],
    # 互通池价格治理（data_pool_price_governance，§12）：人工约束价及其原始录入值。
    # 关掉后管理页/池详情的约束价全为 null；Slice 2 起的越线差额/越线标记派生键
    # （delta_amount/delta_pct/relation_to_constraint/violation_count 等）产出时必须补登记到本组。
    "pool_price_governance": ["purchase_ceiling_ex_tax", "sales_floor_ex_tax",
                              "purchase_input_value", "sales_input_value",
                              # 看板 v2：约束价的契约名（池列表/详情/订单行参考）与越线计数。
                              # 越线计数随治理权限遮（防靠排序/颜色反推金额）；服务层另有
                              # 结构性降级——reference_status 对治理关闭者只给池均价口径
                              # （多行"可见价格×越线布尔"可二分逼出约束价原值）。
                              "max_purchase_price", "min_sale_price",
                              "purchase_violation_count", "sale_violation_count",
                              # DEV-06 早会纪律摘要：实际价、约束价、逐件/合计差额与
                              # 聚合次数/金额。API 另做结构性失败关闭，避免靠空值位置、
                              # 人员排序或记录条数反推约束；字段组是第二道纵深防线。
                              "actual_unit_ex_tax", "manual_limit_ex_tax",
                              "unit_gap", "total_gap", "purchase_total_gap",
                              "sales_total_gap", "violation_line_count",
                              # DEV-09A price-map：用独有容器名做第二道递归脱敏，避免把
                              # stats/value/relation 等全局通用键登记后误伤其它接口。
                              # delta/price_ex_tax 同时登记，防未来调用方拆平容器时漏遮。
                              "current_reference", "latest_raw_record", "quality_counts",
                              "delta_amount", "delta_pct", "price_ex_tax",
                              # 与约束价的差额（双登记，另见 purchase_cost 组注）
                              "manual_limit_delta", "manual_limit_delta_pct"],
}

# 字段级脱敏的唯一真值源是 app/permissions.py 的 ROLE_TEMPLATES（按 data_* 开关）。
# 旧的 ROLE_FIELD_VISIBILITY 表已于 2026-06-15 删除——它是 security._hidden_fields 的旧
# token 回退表，与 permissions 模板口径相反，导致"同一角色因 token 新旧而脱敏结果相反"。
# 现回退也走 permissions.template_for(role)，单一真值源。要改"某角色看不看成本/毛利"，
# 改 permissions.ROLE_TEMPLATES 即可；逐单销售成交明细的隐藏另在 security.anonymize_sales_rows。
