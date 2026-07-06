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
# 端口词放宽（含中文交换机的接入/汇聚/核心/PoE/端口计数）；排除交换机的 FRU 部件
# （电源/风扇/背板/单板/线卡/主板/引擎）——那些是备件不是整机。
_SWITCH_EXCLUDE = ("kvm", "nvme switch", "pcie switch", "pci-e switch", "sas switch", "usb switch")
_SWITCH_FRU = ("power supply", "psu", " fan ", "fan tray", "fan module", "风扇", "背板",
               "backplane", "line card", "line-card", "线卡", "interface card", "接口板",
               "supervisor engine", "主板", "system board", "单板", "交换机电源", "交换机风扇")
_SWITCH_PORTS = ("base-t", "base-sr", "base-lr", "1000base", "10ge", "25ge", "40ge", "100ge",
                 "gbe port", "以太网交换", "数据中心交换", "堆叠", "接入交换", "汇聚交换",
                 "核心交换", "三层交换", "二层交换", "poe", "catalyst", "nexus", "10/100",
                 "-port", "ports", "端口", " gig ")
_SWITCH_WORD = re.compile(r"\bswitch\b", re.I)                # 词界，容忍 'switch,48-port' 这类
# 常见交换机整机型号（描述里可能没有 switch 字样）：华为 S/CE 系列、Cisco WS-C/Nexus。
# 尾部不设边界：华为 S5720S/S5735S 等型号带尾字母（S5720 是型号，-52P-SI-AC 是配置后缀）。
_SWITCH_MODELS = re.compile(
    r"(?<![a-z0-9])(s57\d{2}|s58\d{2}|s67\d{2}|s6300|s6800|s5300|s5310|s7700|s9700|s12700|"
    r"ce58\d\d|ce68\d\d|ce88\d\d|ce128\d\d|ws-c\d|n[35679]k-)", re.I)


def _is_network_switch(text: str) -> bool:
    if not (bool(_SWITCH_WORD.search(text)) or "交换机" in text or bool(_SWITCH_MODELS.search(text))):
        return False
    if any(t in text for t in _SWITCH_EXCLUDE) or any(t in text for t in _SWITCH_FRU):
        return False
    return any(t in text for t in _SWITCH_PORTS)


# RAID 缓存/电池模块（如「Cache 4GB For Smart Array」「Avago Cache for … RAID controller」）：
# 是阵列卡的缓存/电池，不是阵列卡本身 → 电池(07)。要求 cache 后接 for + 有 raid/array 语境。
_RAID_CACHE = re.compile(r"\bcache\b.{0,15}\bfor\b", re.I)


def _is_raid_cache(text: str) -> bool:
    """真·缓存/电池**模块**（不是阵列卡本体）：'Cache … For … RAID' 语义是给某卡的模块 → 电池。

    但 'SR450C RAID卡 … Cache For RH5288'（卡, For 指服务器机型）不算模块——用"缓存词是否在卡本体
    名词之前"区分：缓存词在前=模块(电池)；卡本体名词在前=卡(甲方定，其缓存只是规格)。
    """
    if not ("raid" in text or "array" in text or "阵列" in text):
        return False
    if not (bool(_RAID_CACHE.search(text)) or any(t in text for t in
            ("cachevault", "cachecade", "flash-backed", "flash backed", "阵列卡电池", "阵列卡缓存"))):
        return False
    ci = min([text.find(t) for t in ("cache", "缓存", "cachevault", "flash-backed")
              if t in text] or [10**9])
    ni = min([text.find(t) for t in ("raid card", "raid 卡", "raid卡", "阵列卡", "controller")
              if t in text] or [10**9])
    return ci <= ni                          # 缓存词在卡本体名词之前 → 模块(电池)


# 阵列卡/控制器**本体**（甲方 2026-07-06 定：RAID 卡自带超级电容/BBWC/FBWC/电池只是备电特性，仍归卡）。
# 判据：raid/阵列卡 语境 + 卡本体名词(controller/card/卡/adapter/HBA)或 RAID 级别枚举。与 _is_raid_cache
# 互补：'Cache…For…' 是模块(电池)；'RAID Card…SuperCap/include Cable' 是卡本体。
_RAID_LEVELS = re.compile(r"raid\s*[0-6](?:\s*[,/]\s*[0-9]+){1,}", re.I)   # RAID0,1,5,6,10,50,60


def _is_raid_controller(text: str) -> bool:
    if not any(t in text for t in ("raid", "megaraid", "perc", "smart array", "serveraid", "阵列卡")):
        return False
    return bool(_RAID_LEVELS.search(text)) or any(
        n in text for n in ("controller", "card", "卡", "adapter", "hba", "阵列卡"))


# 电源(自带内置风扇)优先于风扇：'Power Supply 600W … and fan' 是电源；'Fan Module/for PSU' 才是风扇。
_PSU_WATT = re.compile(r"\d{2,4}\s*w\b", re.I)


def _is_power_supply(text: str) -> bool:
    return ("power supply" in text and bool(_PSU_WATT.search(text))
            and not any(t in text for t in ("fan module", "fan assembly", "fan tray",
                                            "风扇模块", "cooling module", "冷却模块")))


# 真·光模块（收发器）：SFP/QSFP/XFP 光学件，不是带 SFP 口的网卡/HBA/接口卡。有 transceiver/收发器/GBIC
# 即判；或 SFP 形态 + 光学特征(optical/波长/短长波/单模/base-sr) 且无卡/接口本体名词。
_TRX_WORDS = ("transceiver", "收发器", "gbic")
_TRX_FORM = ("sfp", "qsfp", "xfp", "cfp", "osfp")
_TRX_OPTICAL = ("optical", "光模块", "光纤收发", "850nm", "1310nm", "1550nm", "短波", "长波",
                " swl", " lwl", "short wave", "long wave", "shortwave", "longwave", "单模", "多模",
                "base-sr", "base-lr", "base-sx", "base-lx", "base-zr", "base-er")
_TRX_CARD_NOUN = ("adapter", "controller", "hba", "raid", "阵列", " card", "网卡", "riser",
                  "mezzanine", "接口", "interface", " nic", "smartio", "i/o", "gateway")


def _is_transceiver(text: str) -> bool:
    if any(w in text for w in _TRX_WORDS):
        return True
    return (any(f in text for f in _TRX_FORM) and any(o in text for o in _TRX_OPTICAL)
            and not any(n in text for n in _TRX_CARD_NOUN))


# 网卡(NIC/以太网/InfiniBand 适配卡)：型号码 + 短语 + adapter/card 搭网络信号。识别不足会让带
# SFP 口的网卡被 0901 光模块吞掉（审计最大错分簇）。跑在 FC/GPU 之后、泛关键词之前。
_NIC_MODELS = re.compile(
    r"(?<![a-z0-9])(connectx|x520|x540|x550|x710|x722|xxv710|82598|82599|57810|57840|"
    r"57414|57416|i350|i340|nc55[0-9]|nc36[0-9]|nc37[0-9])(?![a-z0-9])", re.I)
_NIC_PHRASES = ("ethernet adapter", "ethernet card", "network adapter", "network card",
                "network interface", "converged network", "host channel adapter", "infiniband",
                "flexiblelom", "flexlom", "ocp nic", "10gbe", "25gbe", "40gbe", "100gbe", "网卡")
_NIC_SIGNAL = ("ethernet", "10gb", "25gb", "40gb", "100gb", "gbe", "sfp", "qsfp", "infiniband", "以太")


def _is_nic(text: str) -> bool:
    if bool(_NIC_MODELS.search(text)) or re.search(r"(?<![a-z])nic(?![a-z])", text):
        return True
    if any(p in text for p in _NIC_PHRASES):
        return True
    # 「…adapter / …card / 网卡」+ 网络信号（FC/GPU 已在前面剥离，此处 adapter 多为网卡）
    return ("adapter" in text or "card" in text or "网卡" in text) \
        and any(s in text for s in _NIC_SIGNAL)


def _classify_card(text: str) -> str | None:
    """04 卡 的二级判定：FC 先于 NIC 先于泛 HBA。**带盘介质词(HDD/SSD/固态/drive)的 FC 是硬盘不是卡**。"""
    disk = any(s in text for s in _DISK_SIGNAL) or " drive " in text
    if not disk and (any(k in text for k in ("qle", "lpe", "qme", "fibre channel", "fiber channel",
                                             "光纤卡", "fc hba")) or _FC_SPEED.search(text)):
        return "0405"
    if _is_gpu(text):                                       # GPU 型号码整词匹配，防 'a100'⊂'msa1000'
        return "0404"
    if _is_nic(text):                                       # 以太网/IB 网卡（含带 SFP 口的）→ 网卡
        return "0403"
    if any(t in text for t in ("interface card", "接口板", "接口卡", "i/o module", "io module",
                               "i/o模块", "io模块", "network module", "line card", "smartio",
                               "gateway module")):           # 交换机线卡/IO 模块/接口板 → 其他适配卡
        return "0499"
    for code in ("0403", "0401", "0402", "0499"):           # NIC/RAID/HBA/其他
        if _kw_hit(code, text):
            return code
    return None


_AOC_CABLE = re.compile(r"(?<![a-z])aoc(?![a-z-])", re.I)   # AOC 有源光缆(词界)；排除超微 AOC- 卡前缀


def _is_cache_battery(text: str) -> bool:
    """RAID 缓存电池/超级电容模块（HP FBWC/BBWC「Battery … W/ Cable」）：本体是电池，附带线缆——
    别被形态决定词 cable 抢成线缆。要求有 battery/电池 头词 + 缓存/FBWC/带线 语境。"""
    return ("battery" in text or "电池" in text) and any(
        t in text for t in ("cache", "smart storage", "fbwc", "fwbc", "bbwc",
                            "w/ cable", "with cable", "缓存"))


def _classify_component(text: str) -> str | None:
    # 0a) 阵列卡本体（带 supercap/battery/FBWC/include Cable 只是备电/附件）→ 卡；
    #     但 'Cache … For …' 是缓存/电池**模块** → 归电池(见 0c)，故此处排除 _is_raid_cache。
    if _is_raid_controller(text) and not _is_raid_cache(text):
        return _classify_card(text) or "0401"
    # 0b) 缓存电池带线缆：本体是电池，先于 cable 决定词
    if _is_cache_battery(text):
        return "07"
    # 0c) 电源自带内置风扇 → 电源（先于风扇优先级；'Fan Module/for PSU' 不在此列）
    if _is_power_supply(text) and ("fan" in text or "风扇" in text):
        return "06"
    # 1) 形态决定词：一出现就定（cable 压过 Fan/Power；AOC 有源光缆按词界判，不误伤 AOC- 卡）
    for code, toks in T.DECISIVE.items():
        if any(t in text for t in toks):
            return code
    if _AOC_CABLE.search(text):
        return "0902"
    # 真·光模块(收发器)优先于 FC 卡：'SFP+ Transceiver' 是光模块(0901)，不是光纤卡
    if _is_transceiver(text):
        return "0901"
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
