"""备件描述标准化（甲方 2026-06-30）：按类别套固定模板、字段顺序/单位统一、品牌归一。

**不是 PIM**。流程：原始描述 → 抽字段 → 单位/品牌归一 → 分类 → 模板拼标准描述 → 低置信交人工。
只产"建议"，绝不自动改写人工值——采购在编辑页确认后才落库（locked_fields 锁定，重导不覆盖）。

一期精确覆盖硬盘 / 内存两类模板（用户主诉的三大件）；其余类目同此结构按需扩。
硬盘模板：{容量} {接口速率} {转速} {缓存} {尺寸} {接口} {介质}
内存模板：{容量} {DDR代际-频率} {模组} {Rank} {ECC}
"""
import re

from app.services import classify, normalize_templates, spec_extract
from app.services import taxonomy as T

_CJK = re.compile(r"[一-鿿]")


def normalize_brand(raw: str | None) -> tuple[str | None, str | None]:
    """raw(中/英/缩写) → (brand_norm 英文规范名, brand_zh 中文显示)。识别不了则原样。"""
    if not raw:
        return (None, None)
    norm = T.BRAND_CANON.get(raw.strip().lower())
    if norm:
        return (norm, T.BRAND_ZH.get(norm))
    return (raw.strip(), raw.strip() if _CJK.search(raw) else None)


def _brand_from_text(text: str) -> str | None:
    """描述里夹带的品牌（如 '希捷 8TB…' / '…HDD 东芝'）。"""
    low = text.lower()
    for k, v in T.BRAND_CANON.items():
        if k in low:
            return v
    return None


def normalize_part(description: str | None, pn: str = "", brand: str = "") -> dict:
    """返回标准化建议：canonical_description + 一级/二级分类 + 品牌归一 + 结构化字段。"""
    cls = classify.classify_part(description or "", pn, brand) or {}
    raw = {s["spec_key"]: s for s in spec_extract.extract(description)}
    f = {
        "capacity": _v(raw, "capacity"),
        "interface_speed": _fmt_speed(raw.get("speed")),
        "rpm": _v(raw, "rpm"),
        "cache": _v(raw, "cache"),
        "form_factor": _fmt_ff(raw.get("form_factor")),
        "interface_type": _v(raw, "interface"),
        "media_type": _v(raw, "part_type"),
        "generation": _v(raw, "generation"),
        "frequency": _v(raw, "frequency"),
        "rank": _v(raw, "rank"),
    }
    bnorm, bzh = normalize_brand(brand) if brand else (None, None)
    if not bnorm:
        bnorm = _brand_from_text(description or "")
        bzh = T.BRAND_ZH.get(bnorm) if bnorm else None
    # 硬盘/内存走结构化字段模板；其余类目按 l2/l1 分发到 normalize_templates
    canon = _render(f)
    if canon is None and not cls.get("whole_system"):
        canon = _template_canonical(cls.get("l2_code"), cls.get("l1_code"), description or "")
    return {
        "canonical_description": canon,
        "category_l1": cls.get("category_l1"),
        "category_l2": cls.get("category_l2"),
        "whole_system": bool(cls.get("whole_system")),
        "brand_raw": brand or None,
        "brand_norm": bnorm,
        "brand_zh": bzh,
        "fields": {k: v for k, v in f.items() if v},
    }


def _template_canonical(l2_code: str | None, l1_code: str | None, desc: str) -> str | None:
    fn = normalize_templates.RENDERERS.get(l2_code) or normalize_templates.RENDERERS_L1.get(l1_code)
    if not fn:
        return None
    try:
        return fn(desc)
    except Exception:  # noqa: BLE001  模板抽取失败不应炸接口，退回交人工
        return None


def _v(raw: dict, key: str) -> str | None:
    return (raw.get(key) or {}).get("spec_value")


def _fmt_speed(spec: dict | None) -> str | None:
    if not spec:
        return None
    n = spec.get("numeric_value")
    if n is None:
        return spec.get("spec_value")
    fv = float(n)
    return f"{int(fv) if fv == int(fv) else fv}Gb/s"   # 12 → 12Gb/s


def _fmt_ff(spec: dict | None) -> str | None:
    if not spec:
        return None
    v = spec.get("spec_value")
    return f"{v}-inch" if v in ("2.5", "3.5") else v    # 2.5 → 2.5-inch；RDIMM 等原样


def _render(f: dict) -> str | None:
    media = f.get("media_type")
    if media in ("HDD", "SSD"):
        if not (f.get("capacity") and f.get("interface_type")):
            return None                                 # 缺关键项不强行，交人工
        seg = [f["capacity"]]
        for k in ("interface_speed", "rpm"):
            if f.get(k):
                seg.append(f[k])
        if f.get("cache"):
            seg.append(f"{f['cache']} Cache")
        if f.get("form_factor"):
            seg.append(f["form_factor"])
        seg += [f["interface_type"], media]
        return " ".join(seg)
    if media == "RAM":
        if not (f.get("capacity") and f.get("generation")):
            return None
        gen = f["generation"]
        if f.get("frequency"):
            gen = f"{gen}-{f['frequency']}"             # DDR4-2666
        seg = [f["capacity"], gen]
        if f.get("form_factor"):
            seg.append(f["form_factor"])
        if f.get("rank"):
            seg.append(f["rank"])
        if f.get("form_factor") in ("RDIMM", "LRDIMM"):
            seg.append("ECC")                            # 寄存器内存即 ECC
        return " ".join(seg)
    return None
