"""集中配置。

分两类：
1. Settings —— 运行环境配置（数据库连接、上传目录、登录密钥等），由环境变量驱动。
2. 业务规则开关（§8）—— 待客户确认的口径集中在此，确认后改这里即可，不动逻辑。
"""
from decimal import Decimal
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# DeepSeek v4 为混合思考模型；默认开思考（reasoning_content 流式回前端，灰色可折叠展示）。
# 要关思考省 token：在 .env 设 LLM_EXTRA_BODY='{"thinking": {"type": "disabled"}}'。
_DEFAULT_LLM_EXTRA_BODY = '{"thinking": {"type": "enabled"}}'


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


_DEFAULT_ADMIN_PW = "admin"
_DEFAULT_SECRET = "change-me-in-env"


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

# 含税口径：ex_tax（默认，按不含税算毛利）| as_is（原价粗算）
TAX_BASIS = "ex_tax"

# 目标毛利率（报价提示/低毛利标记用；整机拆解的"建议售价"=成本×1/(1-此值)）
TARGET_MARGIN = Decimal("0.20")

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

# 文件上传上限（MB）
MAX_UPLOAD_MB = 100
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
    ["broadcom", "lsi", "博通"],
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
                      "customer_contact", "customer_phone", "customer"],
    # 注：unit_price 在采购行=成本、销售行=售价(营收)同名 → sales 关 data_purchase_cost 时
    # 逐行 unit_price 一并遮掉（个别成交价对销售也不外露）。销售要的是「聚合成交参考价」，
    # 由 avg_sale_price / avg_sale_price_90d / ref_sale_price 提供（不在本组，sales 可见）。
    # ⚠️ 脱敏靠精确 key 匹配：服务层产出的派生键名必须逐字登记，差一字即漏（见下面成组补登）。
    "purchase_cost": ["unit_price", "avg_cost", "latest_cost", "matched_cost",
                      "weighted_avg_cost", "cost_moving_avg", "cost_fifo",
                      "line_amount", "inventory_value", "unit_cost",
                      "recent_purchase_price", "cost", "cost_amount",
                      # part_overview._profit_summary 的聚合成本派生键
                      "avg_purchase_cost", "avg_cost_moving", "avg_cost_fifo",
                      # part_overview.quick_pricing 的近期采购价窗口键（AI 批量查价/整机拆解）
                      "last_purchase_price", "recent_purchase_avg",
                      "recent_purchase_min", "recent_purchase_max",
                      # §8.8 采购分析面板派生价格/金额键（任何新派生键必须登记，否则对无
                      # data_purchase_cost 角色静默泄漏成本——见 2026-06-15 销售越权教训）
                      "total_amount", "amount", "price_ex", "price_inc",
                      "price_ex_min", "price_ex_max", "price_ex_last", "price_ex_avg",
                      "price_inc_min", "price_inc_max", "price_inc_last", "price_inc_avg",
                      "price_last"],
    # 毛利金额：能反推成本（profit.aggregate 两法派生键一并登记）
    "profit_amount": ["gross_profit", "gross_profit_moving", "gross_profit_fifo"],
    # 毛利率：见反推警告（_profit_summary 与 profit.aggregate 的两法派生键一并登记）
    "profit_rate":   ["gross_margin", "avg_margin", "margin_band",
                      "avg_margin_moving", "avg_margin_fifo",
                      "gross_margin_moving", "gross_margin_fifo"],
}

# 字段级脱敏的唯一真值源是 app/permissions.py 的 ROLE_TEMPLATES（按 data_* 开关）。
# 旧的 ROLE_FIELD_VISIBILITY 表已于 2026-06-15 删除——它是 security._hidden_fields 的旧
# token 回退表，与 permissions 模板口径相反，导致"同一角色因 token 新旧而脱敏结果相反"。
# 现回退也走 permissions.template_for(role)，单一真值源。要改"某角色看不看成本/毛利"，
# 改 permissions.ROLE_TEMPLATES 即可；逐单销售成交明细的隐藏另在 security.anonymize_sales_rows。
