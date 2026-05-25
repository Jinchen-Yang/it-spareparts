"""集中配置。

分两类：
1. Settings —— 运行环境配置（数据库连接、上传目录、登录密钥等），由环境变量驱动。
2. 业务规则开关（§8）—— 待客户确认的口径集中在此，确认后改这里即可，不动逻辑。
"""
from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "IT 备件智能管理系统"
    api_prefix: str = "/api"
    app_port: int = 8000

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


@lru_cache
def get_settings() -> Settings:
    return Settings()


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
# 决定哪些采购构成成本批次层。要把维保算进成本，就把 "维保需求" 加进来
COST_PURCHASE_TYPES = ["销售订单", "指定采购"]   # 默认排除 维保需求 ← 待确认

# 计入营收的销售业务类型（实测：备件销售/销售换货/整机销售）
REVENUE_BUSINESS_TYPES = ["备件销售"]            # 排除 销售换货/整机销售 ← 待确认

# 含税口径：ex_tax（默认，按不含税算毛利）| as_is（原价粗算）
TAX_BASIS = "ex_tax"

# 目标毛利率（报价提示/低毛利标记用）
TARGET_MARGIN = Decimal("0.20")

# 只统计已生效（入库不过滤，业务查询过滤）
ACTIVE_STATUS_ONLY = True

# 导入模式：skip（默认，重复行跳过）| upsert（重复行更新可修复字段）
DEFAULT_IMPORT_MODE = "skip"

# 文件上传上限（MB）
MAX_UPLOAD_MB = 100


# ============================================================
# §8.5 权限预留（第一期只埋钩子，不实现权限功能）
# 全部默认关闭：接口输出与不加钩子时完全一致。接入权限时改这里 + 实现 data_scope。
# ============================================================
ENABLE_RBAC = False              # 第一期关闭，全员看全量
ENABLE_ACCESS_LOG = False        # 审计第一期关闭

PHASE1_BYPASS_ROLE = "phase1_full_access"   # 第一期统一上下文，语义=临时全量
GUEST_ROLE = "guest"                         # 认证失败兜底角色，绝不可是 admin
MASK_VALUE = None                            # 脱敏字段统一置 null（保留字段名）
MASK_TEXT_FOR_EXPORT = "无权限查看"          # 导出场景占位文字

# 字段组：权限按"组"控制，不按单字段，避免漏字段被反推
FIELD_GROUPS = {
    "supplier_info": ["supplier_id", "supplier_name", "supplier_code",
                      "supplier_type", "supplier_contact", "supplier_phone", "supplier"],
    "customer_info": ["customer_id", "customer_name", "customer_city",
                      "customer_contact", "customer_phone", "customer"],
    # 注：unit_price 在采购行=成本、销售行=售价(营收)，名字相同。RBAC 开启时此组
    # 会连销售售价一并遮掉（偏"过度遮蔽"=安全方向）。真启用 RBAC 前需按路径区分或改字段名。
    "purchase_cost": ["unit_price", "avg_cost", "latest_cost", "matched_cost",
                      "weighted_avg_cost", "cost_moving_avg", "cost_fifo",
                      "line_amount", "inventory_value", "unit_cost",
                      "recent_purchase_price", "cost", "cost_amount"],
    "profit_amount": ["gross_profit"],                  # 毛利金额：能反推成本
    "profit_rate":   ["gross_margin", "avg_margin", "margin_band"],  # 毛利率：见反推警告
}

# 角色 → 字段组可见性。ENABLE_RBAC=False 时不生效，仅占位。
# ⚠️ 反推风险：销售知道售价(营收),若再给精确毛利率/毛利金额则成本被还原。
#    sales 看 profit_rate 仅在分档(margin_band)时安全 —— 待客户拍板(§清单 C)。
ROLE_FIELD_VISIBILITY = {
    "admin":     {"supplier_info": True,  "customer_info": True,  "purchase_cost": True,  "profit_amount": True,  "profit_rate": True},
    "boss":      {"supplier_info": True,  "customer_info": True,  "purchase_cost": True,  "profit_amount": True,  "profit_rate": True},
    "sales":     {"supplier_info": False, "customer_info": True,  "purchase_cost": False, "profit_amount": False, "profit_rate": False},
    "purchaser": {"supplier_info": True,  "customer_info": False, "purchase_cost": True,  "profit_amount": False, "profit_rate": False},
    "readonly":  {"supplier_info": True,  "customer_info": True,  "purchase_cost": True,  "profit_amount": True,  "profit_rate": True},
}
