"""轻量确定性分类（轻量 C）：描述 → 备件品类 / 整机判定。

设计：高置信、确定性、低成本。只在关键词清晰时给品类，含糊一律返回 None 交人工——
不替代人工，是给 WP3 批量归类做"先自动建议、再人工按需确认"的第一道。

为什么不全靠 LLM 临场判类：稳定的搜索过滤 / 按类统计 / 批量分析需要结构化类别，
且每次 LLM 判类带 token/延迟/不一致/不可审计（见研判 §四）。

优先级解决关键词冲突（taxonomy.CLASSIFY_PRIORITY），形态决定词压过优先级
（"Fan Power Cable" 含 Fan 仍归线缆）。整机 vs FRU：命中任一备件组件即视为 FRU。
"""
from app.services import taxonomy as T

_DISK_SIGNAL = ["hdd", "ssd", "固态", "硬盘", "机械盘", "机械硬盘", "nvme", " disk "]
_MEM_SIGNAL = ["ddr", "dimm", "内存", "pc5", "pc4", "pc3", "pc2", "rdram", "sdram"]


def _norm(*parts: str) -> str:
    return " " + " ".join(p.lower() for p in parts if p) + " "


def classify_part(description: str | None, pn: str = "", brand: str = "") -> dict | None:
    """返回 {category_l1, l1_code, category_l2, l2_code, confidence} | {"whole_system": True} | None。

    None = 无法高置信判定，交人工。整机 = 不纳入备件治理（machine_or_part=整机 / 可排除）。
    """
    text = _norm(description or "", pn, brand)
    comp = _classify_component(text)
    if comp:
        l1 = comp[:2]
        is_l2 = len(comp) == 4
        return {"category_l1": T.CATEGORY_NAMES.get(l1), "l1_code": l1,
                "category_l2": T.CATEGORY_NAMES.get(comp) if is_l2 else None,
                "l2_code": comp if is_l2 else None, "confidence": "high"}
    if _has_whole_system(text):
        return {"whole_system": True}
    return None


def is_whole_system(description: str | None, pn: str = "", brand: str = "") -> bool:
    """整机本体（非 FRU）判定：含整机词且不命中任何备件组件分类。"""
    res = classify_part(description, pn, brand)
    return bool(res and res.get("whole_system"))


def _kw_hit(code: str, text: str) -> bool:
    return any(k in text for k in T.KEYWORDS.get(code, []))


def _classify_component(text: str) -> str | None:
    # 1) 形态决定词：一出现就定（cable 压过 Fan/Power）
    for code, toks in T.DECISIVE.items():
        if any(t in text for t in toks):
            return code
    # 2) 按优先级首命中
    for entry in T.CLASSIFY_PRIORITY:
        if entry == "02":                      # 硬盘：要求介质信号 + 能定接口才归类
            c = _classify_disk(text)
        elif entry == "01":                    # 内存
            c = _classify_memory(text)
        elif len(entry) == 2 and entry not in ("06", "07", "08", "10"):
            c = next((code for code in T.KEYWORDS
                      if len(code) == 4 and code.startswith(entry) and _kw_hit(code, text)), None)
        else:                                   # 06/07/08/10/0901/0902 单级
            c = entry if _kw_hit(entry, text) else None
        if c:
            return c
    return None


def _classify_disk(text: str) -> str | None:
    if not any(s in text for s in _DISK_SIGNAL):
        return None
    if any(t in text for t in ("nvme", "u.2", "u.3", "m.2", "pcie ssd", "e1.s", "e3.s", "edsff")):
        return "0207"
    if any(t in text for t in ("fibre channel", "fiber channel", "fc-al", "光纤硬盘")):
        return "0208"
    ssd = "ssd" in text or "固态" in text
    sas, sata, big = "sas" in text, "sata" in text, "3.5" in text
    if ssd:
        return "0206" if sas else ("0205" if sata else None)
    if sas:
        return "0202" if big else "0201"
    if sata:
        return "0204" if big else "0203"
    return None                                  # 是盘但接口不明 → 交人工


def _classify_memory(text: str) -> str | None:
    if not any(s in text for s in _MEM_SIGNAL):
        return None
    if "ddr5" in text or "pc5" in text:
        return "0101"
    if "ddr4" in text or "pc4" in text:
        return "0102"
    if "ddr3" in text or "pc3" in text:
        return "0103"
    if "ddr2" in text or "pc2" in text:
        return "0104"
    return "0199"


def _has_whole_system(text: str) -> bool:
    return any(t in text for t in T.WHOLE_SYSTEM_TOKENS)
