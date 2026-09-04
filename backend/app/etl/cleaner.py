"""逐字段清洗与标准化（§7.1）。

约定：
- 空值（None/空串/'nan'）→ 返回 None，不报错。
- 非空但不合法 → 抛 ValueError，由 validate 层捕获并记 sys_import_error。
- 金额/数量/税率分别按 0.01 / 0.001 / 0.0001 精度 quantize。
"""
import numbers
import re
from datetime import date
from decimal import Decimal, InvalidOperation

import pandas as pd

from app import config

# 共享数值列精度 Numeric(14, scale)：整数位上限 = 14 - scale（见 models/_types.py）。
_NUMERIC_PRECISION = 14

# 氚云内部 V 码 token：V + ≥10 位大写字母数字（实测含字母，如 V0230LD000000000）
_VCODE_TOKEN = re.compile(r"^V[0-9A-Z]{10,}$")
_THOUSANDS = re.compile(r"[,\s¥￥$]")
_CATEGORY_PLACEHOLDER = "原始系统"          # 占位垃圾（采购/部分销售）
_SUPPLIER_SUFFIX = re.compile(r"（[^（）]*）")  # 全角括号后缀，如（质保三个月）


def _is_blank(x) -> bool:
    if x is None:
        return True
    if isinstance(x, float) and pd.isna(x):
        return True
    s = str(x).strip()
    return s == "" or s.lower() == "nan"


def clean_str(x) -> str | None:
    if _is_blank(x):
        return None
    return str(x).strip()


def clean_category(x) -> str | None:
    """产品大类/小类：占位 '原始系统*' 视为空（§4.3 实测 64% 销售大类为占位）。"""
    s = clean_str(x)
    if s is None or s.startswith(_CATEGORY_PLACEHOLDER):
        return None
    return s


def standardize_pn(raw) -> tuple[str | None, str | None, bool]:
    """策略 B：去 V 码 token 及其后内容。返回 (pn_std, pn_raw, needs_review)。

    - 命中 V 码 token：截断该 token 及其后，取前段为 pn_std，needs_review=False。
    - 含空格但无 V 码（真正歧义）：pn_std = 原始大写值(保留空格)，needs_review=True。
    - 空值：(None, None, False)。
    """
    if _is_blank(raw):
        return None, None, False
    pn_raw = str(raw).strip()
    s = pn_raw.upper()
    tokens = s.split()
    kept: list[str] = []
    for t in tokens:
        if _VCODE_TOKEN.match(t):
            break
        kept.append(t)
    if kept and len(kept) < len(tokens):       # 命中 V 码并截断
        std = " ".join(kept)
        # 截断后通常单 token；若仍含空格仍视为干净（真实多段在 V 码前）
        return std, pn_raw, False
    if " " in s:                                # 含空格但无 V 码 → 歧义，保留原值待人工
        return s, pn_raw, True
    return s, pn_raw, False


def parse_rate(x) -> Decimal | None:
    """'13.0%'→0.1300，'0.0%'→0，0.13→0.1300。

    界防+纠偏（2026-07-04 实测：氚云历史导出税率列有 '1300.0%'×1488 行/'600.0%'——
    百分号字段被二次放大，真实意图 13%/6%）：解析后 ≥1 的值按「多乘了 100」纠正一次；
    纠正后仍不在 [0,1) → 抛 ValueError（调用方置 None，走 税金/不含税 反推兜底）。
    绝不把 ≥10 的值放行到 Numeric(5,4)——一行会毒死整批 INSERT（22536 行历史导入实翻过车）。"""
    if _is_blank(x):
        return None
    s = str(x).strip()
    try:
        if s.endswith("%"):
            val = Decimal(s[:-1].strip()) / Decimal(100)
        else:
            val = Decimal(s)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"税率非法: {x!r}") from exc
    if Decimal(1) <= val <= Decimal(100):
        val = val / Decimal(100)
    if not (Decimal(0) <= val < Decimal(1)):
        raise ValueError(f"税率超界: {x!r}")
    return val.quantize(Decimal("0.0001"))


def _parse_decimal(
    x,
    places: str,
    label: str,
    *,
    rounding: str | None = None,
) -> Decimal | None:
    if _is_blank(x):
        return None
    s = _THOUSANDS.sub("", str(x).strip())
    try:
        value = Decimal(s)
        val = (
            value.quantize(Decimal(places))
            if rounding is None
            else value.quantize(Decimal(places), rounding=rounding)
        )
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label}非数字: {x!r}") from exc
    # 越列限保护：超出 Numeric(14, scale) 可表达范围时抛 ValueError 走坏行隔离，
    # 而非流到 DB 抛未捕获的 DataError 毒化整批导入事务（审计 2026-06-28 I-4）。
    scale = abs(Decimal(places).as_tuple().exponent)
    if abs(val) >= Decimal(10) ** (_NUMERIC_PRECISION - scale):
        raise ValueError(f"{label}超出取值范围({val}): {x!r}")
    return val


def parse_money(x, *, rounding: str | None = None) -> Decimal | None:
    return _parse_decimal(x, "0.01", "金额", rounding=rounding)


def parse_qty(x) -> Decimal | None:
    return _parse_decimal(x, "0.001", "数量")


def parse_int(x) -> int | None:
    """序号等：非法返回 None（不阻断，§4.3）。"""
    if _is_blank(x):
        return None
    try:
        return int(float(str(x).strip()))
    except (ValueError, TypeError, OverflowError):   # "inf"/"1e999" → int() 抛 OverflowError
        return None


def parse_date(x) -> date | None:
    if _is_blank(x):
        return None
    # Excel 日期序列号：日期列丢失日期格式时 openpyxl 返回裸数字，pd.to_datetime 会按纳秒纪元
    # 塌缩到 1970-01-01（静默数据损坏）。数字一律按 Excel 纪元(1899-12-30)还原（审计 I-3）。
    if isinstance(x, numbers.Number) and not isinstance(x, bool):
        if not (1 <= x <= 100000):           # 合理 Excel 序列号区间（约 1900–2173），越界判非法
            raise ValueError(f"日期非法: {x!r}")
        return pd.to_datetime(x, unit="D", origin="1899-12-30").date()
    ts = pd.to_datetime(x, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"日期非法: {x!r}")
    return ts.date()


def normalize_supplier_name(raw) -> tuple[str | None, str | None]:
    """返回 (name_raw, name_normalized)。规范名去全角括号后缀（质保/采购等）。"""
    name_raw = clean_str(raw)
    if name_raw is None:
        return None, None
    normalized = _SUPPLIER_SUFFIX.sub("", name_raw).strip()
    return name_raw, (normalized or name_raw)


_SOURCE_TYPE_MAP = {
    "销售订单": "销售订单", "指定采购": "指定采购", "维保需求": "维保需求",
    # 兼容旧模版/其它写法
    "销售": "销售订单", "备货": "指定采购", "维保": "维保需求", "回收": "回收",
}


def normalize_source_type(raw) -> str | None:
    """采购类型标准化（§7.1，实测真实值：销售订单/维保需求/指定采购）。"""
    s = clean_str(raw)
    if s is None:
        return None
    for k, v in _SOURCE_TYPE_MAP.items():
        if k in s:
            return v
    return s


def parse_tax_inclusive(x) -> bool | None:
    """氚云「是否含税」列：'含税'→True，'不含'/'未税'→False，空→None。

    顺序重要：'不含' 含子串 '含'，必须先判 '不含'/'未税' 再判 '含'。
    """
    s = clean_str(x)
    if s is None:
        return None
    if "不含" in s or "未税" in s:
        return False
    if "含税" in s or s == "含":
        return True
    return None


def derive_tax_rate(amount_ex_tax, tax_amount) -> Decimal | None:
    """税率列常空 → 用 税金/不含税金额 反推（quantize 0.0001）。不含单税金=0 → 返回 0。

    结果限定在 [0, 1)：脏数据（税金/不含税金额 填反、单位错、退货抵扣）会算出 ≥1 或负值，
    若原样写入 FPurchaseOrder.tax_rate(Rate=Numeric(5,4)，上限 9.9999) 会溢出并 poison 整批
    导入事务。越界一律返回 None，让分析层回退 ANALYSIS_FALLBACK_VAT 兜底。
    """
    if amount_ex_tax is None or tax_amount is None:
        return None
    try:
        ex = Decimal(amount_ex_tax)
        if ex == 0:
            return None
        r = (Decimal(tax_amount) / ex).quantize(Decimal("0.0001"))
    except (InvalidOperation, ZeroDivisionError, TypeError):
        return None
    return r if Decimal(0) <= r < Decimal(1) else None


def classify_source_channel(name_raw, name_normalized, supplier_type=None) -> str:
    """采购来源渠道（从供应商名派生；甲方可在 config 调关键词）。

    口径=采购"从哪类来源进货"：淘宝/京东/拼多多/闲鱼/个人/正规供应商。
    **不**用 供应商类型 短路——实测它几乎人人都是「底层回收商」(二手件回收商=整个供货基盘)，
    与"淘宝 vs 正规 vs 个人"是正交维度，若按它分类会把所有供应商都归成「回收」，失去意义。
    优先级：名称关键词(淘宝/京东/…) → 含企业词→正规供应商 → 短名(纯人名)→个人 → 兜底正规供应商。
    关键词在 name_raw 上匹配（括号内常含真实公司名），人名长度判断用 name_normalized。结果可人工修正。
    （supplier_type 入参保留备用，当前不参与分类——回收/维修 信息仍在 dim_supplier.supplier_type 列。）
    """
    raw = clean_str(name_raw)
    if raw is None:
        return config.SOURCE_CHANNEL_UNKNOWN
    for label, kws in config.SOURCE_CHANNEL_NAME_KEYWORDS:
        if any(k in raw for k in kws):
            return label
    # 中文企业词按子串；英文企业词按整词（避免 'inc' 误命中 Prince/Vince 这类人名罗马字）
    low = raw.lower()
    en = "|".join(re.escape(w) for w in config.SOURCE_CHANNEL_COMPANY_WORDS_EN)
    if any(w in raw for w in config.SOURCE_CHANNEL_COMPANY_WORDS) or \
            (en and re.search(rf"\b(?:{en})\b", low)):
        return config.SOURCE_CHANNEL_DEFAULT
    base = clean_str(name_normalized) or raw
    if len(base) <= 5:
        return config.SOURCE_CHANNEL_PERSONAL
    return config.SOURCE_CHANNEL_DEFAULT
