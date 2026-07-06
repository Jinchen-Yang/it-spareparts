"""轻量确定性分类（轻量 C）：描述 → 备件品类 / 整机判定。

设计：高置信、确定性、低成本。只在关键词清晰时给品类，含糊一律返回 None 交人工——
不替代人工，是给 WP3 批量归类做"先自动建议、再人工按需确认"的第一道。

为什么不全靠 LLM 临场判类：稳定的搜索过滤 / 按类统计 / 批量分析需要结构化类别，
且每次 LLM 判类带 token/延迟/不一致/不可审计（见研判 §四）。

优先级解决关键词冲突（taxonomy.CLASSIFY_PRIORITY），形态决定词压过优先级
（"Fan Power Cable" 含 Fan 仍归线缆）。整机 vs FRU：命中任一备件组件即视为 FRU。
"""
import re

from app.services import taxonomy as T

_DISK_SIGNAL = ["hdd", "ssd", "固态", "硬盘", "机械盘", "机械硬盘", "nvme", " disk "]
_MEM_SIGNAL = ["ddr", "dimm", "内存", "pc5", "pc4", "pc3", "pc2", "rdram", "sdram"]
# GPU 显式词（子串安全） + GPU 型号码。边界用 (?<![\w-])...(?![\w-])：不仅整词，还排除
# 连字符型号内部（否则 'T4' 会命中 Intel 网卡 'I350-T4'、'A2' 命中各种 'xxx-A2' 料号）。
_GPU_WORDS = ["gpu", "graphics card", "video card", "graphics adapter", "tesla", "quadro",
              "radeon instinct", "firepro", "显卡", "instinct"]
_GPU_CODE = re.compile(r"(?<![A-Za-z0-9-])(A100|A800|A30|A40|A2|H100|H200|H800|V100|T4|L4|L40S?|"
                       r"MI\d{2,3}|RTX\s*\d{3,4}|GTX\s*\d{3,4})(?![A-Za-z0-9-])", re.I)


def _is_gpu(text: str) -> bool:
    return any(w in text for w in _GPU_WORDS) or bool(_GPU_CODE.search(text))


def _norm(*parts: str) -> str:
    return " " + " ".join(p.lower() for p in parts if p) + " "


def classify_part(description: str | None, pn: str = "", brand: str = "") -> dict | None:
    """返回 {category_l1, l1_code, category_l2, l2_code, confidence} | {"whole_system": True} | None。

    None = 无法高置信判定，交人工。整机 = 不纳入备件治理（machine_or_part=整机 / 可排除）。
    """
    text = _norm(description or "", pn, brand)
    if _is_network_switch(text):        # 带端口的以太网交换机是整机本体，不是备件（防被 SFP+ 吞成光模块）
        return {"whole_system": True}
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


_FC_SPEED = re.compile(r"\b\d{1,2}\s*g\s*fc\b", re.I)   # 4G FC / 8G FC / 16G FC = 光纤接口速率

# 网络交换机（带端口）= 整机；排除 KVM/NVMe/PCIe/SAS switch（这些是备件组件）。
_SWITCH_PORTS = ("base-t", "base-sr", "base-lr", "1000base", "10ge", "25ge", "40ge", "100ge",
                 "gbe port", "以太网交换", "数据中心交换", "堆叠")


def _is_network_switch(text: str) -> bool:
    return " switch " in text and any(t in text for t in _SWITCH_PORTS) \
        and not any(t in text for t in ("kvm", "nvme switch", "pcie switch", "pci-e switch", "sas switch"))


# RAID 缓存/电池模块（如「Cache 4GB For Smart Array」「Avago Cache for … RAID controller」）：
# 是阵列卡的缓存/电池，不是阵列卡本身 → 电池(07)。要求 cache 后接 for + 有 raid/array 语境。
_RAID_CACHE = re.compile(r"\bcache\b.{0,15}\bfor\b", re.I)


def _is_raid_cache(text: str) -> bool:
    return ("raid" in text or "array" in text or "阵列" in text) and (
        bool(_RAID_CACHE.search(text)) or any(t in text for t in
             ("cachevault", "cachecade", "flash-backed", "flash backed", "阵列卡电池", "阵列卡缓存")))


def _classify_card(text: str) -> str | None:
    """04 卡 的二级判定：FC 先于泛 HBA。**带盘介质词(HDD/SSD/固态/drive)的 FC 是硬盘不是卡**。"""
    disk = any(s in text for s in _DISK_SIGNAL) or " drive " in text
    if not disk and (any(k in text for k in ("qle", "lpe", "qme", "fibre channel", "fiber channel",
                                             "光纤卡", "fc hba")) or _FC_SPEED.search(text)):
        return "0405"
    if _is_gpu(text):                                       # GPU 型号码整词匹配，防 'a100'⊂'msa1000'
        return "0404"
    for code in ("0403", "0401", "0402", "0499"):           # NIC/RAID/HBA/其他
        if _kw_hit(code, text):
            return code
    return None


def _classify_component(text: str) -> str | None:
    # 1) 形态决定词：一出现就定（cable 压过 Fan/Power）
    for code, toks in T.DECISIVE.items():
        if any(t in text for t in toks):
            return code
    # RAID 缓存/电池模块优先于阵列卡：'Cache for … RAID' 是电池(07)，不是卡
    if _is_raid_cache(text):
        return "07"
    # 卡（NIC/HBA/FC/GPU…）即便带 SFP+/光纤连接器，也归卡——不被 0901 光模块/0902 线缆抢走
    card = _classify_card(text)
    # 2) 按优先级首命中
    for entry in T.CLASSIFY_PRIORITY:
        if entry in ("0901", "0902") and card:
            continue                            # 卡带连接器 → 归卡
        if entry == "04":                       # 卡：FC 先于 HBA；无卡词再走泛关键词
            c = card or next((code for code in T.KEYWORDS
                              if len(code) == 4 and code.startswith("04") and _kw_hit(code, text)), None)
        elif entry == "02":                     # 硬盘：要求介质信号 + 能定接口才归类
            c = _classify_disk(text)
        elif entry == "01":                     # 内存
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
    # NVMe 要显式证据；**M.2 不代表 NVMe**——存在 M.2 SATA SSD（如 Micron MTFDDAV 480GB SATA M.2）
    if any(t in text for t in ("nvme", "u.2", "u.3", "pcie ssd", "e1.s", "e3.s", "edsff")):
        return "0207"
    # 光纤FC硬盘：Fibre Channel / FC-AL / NG FC 速率(4G FC 等) / 光纤硬盘
    if any(t in text for t in ("fibre channel", "fiber channel", "fc-al", "光纤硬盘")) \
            or _FC_SPEED.search(text):
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
