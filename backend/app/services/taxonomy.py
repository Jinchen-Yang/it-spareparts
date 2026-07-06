"""备件分类与品牌归一字典（甲方 2026-06-30 全球分类与品牌体系 v1，轻量 C）。

只放"轻量、确定性"的规则数据：分类用关键词 + 优先级 + 整机过滤 + 品牌归一。
不建 8 字段品牌 PIM、不建独立规则平台（那是 WP4，触发条件满足再上）。
分类引擎见 services/classify.py。
"""
import re

# ── 一级 / 二级分类（code → 中文名）。已应用两处改名：0207 NVMe/PCIe-SSD；07 电池三级属性 ──
CATEGORY_NAMES: dict[str, str] = {
    "01": "内存", "0101": "DDR5/PC5", "0102": "DDR4/PC4", "0103": "DDR3/PC3",
    "0104": "DDR2/PC2", "0199": "其他内存",
    "02": "硬盘", "0201": "SAS-HDD-2.5", "0202": "SAS-HDD-3.5", "0203": "SATA-HDD-2.5",
    "0204": "SATA-HDD-3.5", "0205": "SATA-SSD", "0206": "SAS-SSD",
    "0207": "NVMe/PCIe-SSD", "0208": "光纤FC硬盘", "0299": "其他硬盘",
    "03": "主板", "0301": "系统主板", "0302": "背板",
    "04": "卡", "0401": "阵列卡RAID", "0402": "HBA卡", "0403": "网卡NIC",
    "0404": "显卡GPU", "0405": "光纤卡FC", "0499": "其他适配卡",
    "05": "CPU", "0501": "Intel至强", "0502": "AMD", "0599": "其他处理器",
    "06": "电源", "07": "电池/超级电容", "08": "风扇/散热",
    "09": "线缆/光模块", "0901": "光模块", "0902": "线缆",
    "10": "其他备件/耗材",
}
BATTERY_SUBTYPES = ["CMOS电池", "RAID缓存电池", "缓存超级电容", "NVRAM电池", "控制器电池", "其他电池"]
COOLING_TYPES = ["FAN", "BLOWER", "HEATSINK", "FAN_HEATSINK", "LIQUID_COOLING",
                 "AIR_BAFFLE", "THERMAL_MODULE"]

# ── 分类引擎优先级（§六，解决关键词冲突）。整机过滤永远第一，其后按此序首命中即定。──
CLASSIFY_PRIORITY = ["07", "08", "06", "0901", "0902", "03", "04", "05", "01", "02", "10"]

# 形态决定词：一出现就压过优先级（解决 "Fan Power Cable" 含 Fan 却应归线缆）。
DECISIVE = {
    # 注：不放 " aoc"——超微加装卡前缀「AOC-」(Add-On Card) 会撞 AOC(Active Optical Cable)。
    # 真 AOC 线缆由 classify._AOC 正则(词界 aoc 不接连字符)判为决定词，AOC-xxx 卡不误伤。
    # 注：不放 " dac"——'Ethernet Server Adapter SFP+ DAC' 是网卡(DAC 只是配套线型)。纯 DAC 线
    # 缆仍由 0902 关键词 "dac" 在优先级里命中(无卡则归线缆)；网卡先被 _is_nic 识别，卡带 DAC 归卡。
    "0902": [" cable", "cable ", "power cord", "电源线", "mini-sas", "slimsas", "oculink"],
    # 主板形态决定词：含主板字样即归主板，压过 CPU/Processor 关键词
    # （"HP Mother Board…Intel E5 Processor" 是主板不是 CPU；氚云写「Mother Board」带空格）
    # "i/o board"/"io board"：System I/O board 是主板，别被 E5-xxxx 吞成 CPU
    "0301": ["motherboard", "mother board", "mainboard", "main board", "system board",
             "systemboard", "i/o board", "io board", "主板", "主逻辑板"],
}

# 每个分类的识别关键词（小写匹配；来自 §一~§三 "识别词/识别规则"）。
KEYWORDS: dict[str, list[str]] = {
    "0101": ["ddr5", "pc5", "4800", "5600", "6400", "7200", "mrdimm"],
    "0102": ["ddr4", "pc4", "2133p", "2400t", "2666v", "2933y", "3200aa", "rdimm", "lrdimm"],
    "0103": ["ddr3", "pc3", "pc3l", "8500", "10600", "12800", "14900"],
    "0104": ["ddr2", "pc2", "5300", "4200"],
    "0199": ["rdram", "sdram", "nvdimm", "optane persistent", "cxl memory", "内存条"],
    "0201": ["sas", "2.5", "sff", "10k", "15k", "savvio", "enterprise performance"],
    "0202": ["sas", "3.5", "lff", "nearline sas", "nl-sas", "exos", "ultrastar", "mg series"],
    "0203": ["sata", "2.5"], "0204": ["sata", "3.5"],
    "0205": ["sata", "ssd", "固态"], "0206": ["sas", "ssd", "固态"],
    "0207": ["nvme", "pcie ssd", "u.2", "u.3", "m.2 nvme", "e1.s", "e3.s", "edsff", "gen4", "gen5"],
    "0208": ["fibre channel disk", "fiber channel disk", "fc-al", "2gb fc", "4gb fc", "光纤硬盘"],
    "0299": ["scsi", "ultra320", "ultra160", "ide", "pata", "satadom", "pcie闪存卡"],
    "0301": ["system board", "systemboard", "motherboard", "mainboard", "planar",
             "processor board", "cpu board", "logic board", "server board", "主板", "主逻辑板"],
    "0302": ["backplane", "midplane", "centerplane", "背板"],
    "0401": ["raid controller", "array controller", "megaraid", "perc", "smart array",
             "serveraid", "raid卡", "raid 卡", "raid card", "阵列卡", "raid-on-chip"],
    "0402": ["hba", "host bus adapter", "it mode", "jbod controller", "sas adapter",
             "sas controller", "sas hba"],
    "0403": ["nic", "network interface", "ethernet adapter", "lan adapter", "network adapter",
             "ocp nic", "flexiblelom", "flexlom", "网卡", "converged network", "infiniband adapter"],
    # 注：GPU 型号码(a100/h100/rtx…) 不放子串关键词——会误命中 MSA1000 等；改由 classify._is_gpu 整词匹配
    "0404": ["gpu", "graphics card", "video card", "graphics adapter", "tesla", "quadro",
             "radeon instinct", "firepro", "显卡"],
    # 注：不放 "16g fc"/"32g fc"——它们既是 FC 卡代际也是 FC 硬盘描述里的接口速率，会把
    # 「EMC HDD 4G FC」这类光纤硬盘误吞进卡。FC 卡由 classify._classify_card 判(带盘介质词即非卡)。
    "0405": ["fc hba", "fibre channel adapter", "fiber channel adapter",
             "emulex lpe", "qlogic qle", "光纤卡"],
    "0499": ["pcie riser", "riser card", "mezzanine", "sas expander", "nvme switch",
             "nvme retimer", "dpu", "smartnic", "fpga", "crypto accelerator", "tpm module",
             "m.2 boot", "memory riser", "扩展卡"],
    "0501": ["xeon", "e5-", "e7-", "silver ", "gold ", "platinum ", "bronze ", "xeon scalable",
             "至强"],
    "0502": ["epyc", "opteron", "threadripper pro", "ryzen embedded"],
    "0599": ["power4", "power5", "power6", "power7", "power8", "power9", "powerpc", "sparc",
             "itanium", "atom", "intel core", "ampere altra", "thunderx", "kunpeng", "鲲鹏",
             "loongson", "龙芯", "phytium", "飞腾", "处理器", "cpu"],
    "06": ["power supply", "psu", "hot plug power", "hot swap power", "crps", "redundant power",
           "power module", "voltage regulator module", "vrm", "电源"],
    "07": ["battery", "bbu", "battery backup", "raid battery", "cmos battery", "rtc battery",
           "supercapacitor", "super capacitor", "super cap", "cachevault", "fbwc",
           "smart storage battery", "电池", "超级电容"],
    "08": ["fan", "blower", "cooling fan", "hot swap fan", "fan cage", "fan tray", "heat sink",
           "heatsink", "thermal module", "cooling module", "liquid cooling", "air baffle",
           "heat pipe", "radiator", "风扇", "散热"],
    "0901": ["sfp", "sfp+", "sfp28", "qsfp", "qsfp28", "qsfp-dd", "osfp", "xfp", "gbic",
             "transceiver", "optical module", "base-sr", "base-lr", "fc sfp", "光模块"],
    "0902": ["cable", "mini-sas", "sff-8087", "sff-8643", "sff-8644", "slimsas", "oculink",
             "nvme cable", "sata cable", "dac", "aoc", "twinax", "power cord", "线缆", "跳线"],
    "10": ["bezel", "caddy", "tray", "blank", "rail kit", "air baffle", "chassis", "cover",
           "bracket", "ilo", "idrac", "dvd-rom", "标签", "导热硅脂", "导热垫", "防尘网",
           "面板", "挡板", "托架", "假条", "螺丝", "导轨"],
}

# ── 整机过滤词典（§四）：命中=整机本体则不纳入备件治理（machine_or_part=整机 / 可排除）──
WHOLE_SYSTEM_TOKENS = [
    "rack server", "tower server", "blade server", "compute node", "server node",
    "mainframe", "minicomputer", "unix server", "power system", "sparc server",
    "proliant", "poweredge", "thinksystem", "system x", "cisco ucs", "primergy",
    "sun fire", "superserver", "fusionserver", "all-in-one", "hyperconverged",
    "hci appliance", "vxrail", "simplivity", "flexpod", "exadata", "exalogic",
    "tape library", "tape autoloader", "virtual tape library", "storever library",
    "disk enclosure", "storage enclosure", "expansion enclosure", "drive enclosure",
    "disk shelf", "storage shelf", "jbod enclosure", "disk array", "storage array",
    "san array", "nas appliance", "整机", "服务器整机", "扩展柜", "盘柜", "磁带库",
]
# 反误杀：含整机词但其实是 FRU（用于某整机的部件）→ 仍按备件分类，不判整机。
# 实现：只要命中任一备件组件分类，即视为 FRU；纯整机词无组件 → 整机。

# ── 品牌归一（§二+§三 "建议归一"，alias→norm，consolidated）。用于 resolver 召回扩展。──
BRAND_ALIASES: dict[str, str] = {
    "hp": "HPE", "hp enterprise": "HPE", "hewlett packard enterprise": "HPE",
    "dellemc": "Dell EMC", "华三": "H3C", "新华三": "H3C", "浪潮": "Inspur",
    "超微": "Supermicro", "super micro computer": "Supermicro", "曙光": "Sugon", "中科曙光": "Sugon",
    "hynix": "SK hynix", "micron technology": "Micron", "kioxia": "KIOXIA", "toshiba memory": "KIOXIA",
    "wd": "Western Digital", "wdc": "Western Digital", "sandisk": "SanDisk",
    "lsi": "Broadcom", "lsi logic": "Broadcom", "avago": "Broadcom", "emulex": "Broadcom",
    "adaptec": "Microchip", "pmc": "Microchip", "pmc-sierra": "Microchip",
    "mellanox": "NVIDIA", "qlogic": "Marvell", "aquantia": "Marvell",
    "finisar": "Coherent", "ii-vi": "Coherent", "oclaro": "Lumentum", "fiberstore": "FS",
}

# ── 品牌规范化（标准描述用英文规范名；一期只要 raw→norm，不分颗粒/芯片/集团）──
# 原始写法(小写) → 英文规范名。键覆盖中文/缩写/全称变体。
BRAND_CANON: dict[str, str] = {
    "东芝": "Toshiba", "toshiba": "Toshiba", "toshiba corp.": "Toshiba",
    "希捷": "Seagate", "seagate": "Seagate", "seagate technology": "Seagate",
    "西数": "Western Digital", "西部数据": "Western Digital", "wd": "Western Digital",
    "wdc": "Western Digital", "western digital": "Western Digital",
    "惠普": "HPE", "hp": "HPE", "hpe": "HPE", "hewlett packard enterprise": "HPE",
    "戴尔": "Dell", "dell": "Dell", "dell emc": "Dell", "dellemc": "Dell",
    "三星": "Samsung", "samsung": "Samsung",
    "镁光": "Micron", "美光": "Micron", "micron": "Micron",
    "海力士": "SK hynix", "hynix": "SK hynix", "sk hynix": "SK hynix", "skhynix": "SK hynix",
    "金士顿": "Kingston", "kingston": "Kingston",
    "英特尔": "Intel", "intel": "Intel", "联想": "Lenovo", "lenovo": "Lenovo",
    "华为": "Huawei", "huawei": "Huawei", "浪潮": "Inspur", "inspur": "Inspur",
    "思科": "Cisco", "cisco": "Cisco", "超微": "Supermicro", "supermicro": "Supermicro",
    "富士通": "Fujitsu", "fujitsu": "Fujitsu", "铠侠": "KIOXIA", "kioxia": "KIOXIA",
    "新华三": "H3C", "华三": "H3C", "h3c": "H3C",
    "netapp": "NetApp", "网域存储": "NetApp", "ibm": "IBM",
    "oracle": "Oracle", "sun": "Oracle", "甲骨文": "Oracle",
    "hitachi": "Hitachi", "日立": "Hitachi", "hds": "Hitachi", "emc": "EMC",
    "nvidia": "NVIDIA", "英伟达": "NVIDIA", "amd": "AMD", "超威": "AMD",
}
# 英文规范名 → 中文显示名（界面另显中文，标准描述用英文）。
BRAND_ZH: dict[str, str] = {
    "Toshiba": "东芝", "Seagate": "希捷", "Western Digital": "西部数据", "HPE": "惠普",
    "Dell": "戴尔", "Samsung": "三星", "Micron": "镁光", "SK hynix": "海力士",
    "Kingston": "金士顿", "Intel": "英特尔", "Lenovo": "联想", "Huawei": "华为",
    "Inspur": "浪潮", "Cisco": "思科", "Supermicro": "超微", "Fujitsu": "富士通",
    "KIOXIA": "铠侠", "H3C": "新华三", "NetApp": "网域存储", "Oracle": "甲骨文",
    "Hitachi": "日立", "NVIDIA": "英伟达",
}


# ── 品牌识别（氚云品牌是「中文（English）」合并格式：三星（Samsung）/海力士（SK hynix））──
_CJK = re.compile(r"[一-鿿]")
# 子串安全键：≥4 字符英文 或 中文（不会误嵌别词），长键优先；短英文键（hp/wd）只整词匹配
_CANON_SAFE = sorted([(k, v) for k, v in BRAND_CANON.items() if len(k) >= 4 or _CJK.search(k)],
                     key=lambda kv: len(kv[0]), reverse=True)
_CANON_SHORT = {k: v for k, v in BRAND_CANON.items() if len(k) < 4 and not _CJK.search(k)}


def recognize_brand(text: str | None) -> str | None:
    """从文本（品牌字段或描述）识别已知规范品牌；识别不了 → None。"""
    if not text:
        return None
    low = text.lower()
    for k, v in _CANON_SAFE:
        if k in low:
            return v
    toks = set(re.split(r"[^a-z0-9]+", low))
    for k, v in _CANON_SHORT.items():
        if k in toks:
            return v
    return None


def resolve_brand(brand: str | None, description: str | None) -> tuple[str | None, str | None]:
    """品牌优先级：①字段认出的 ②描述认出的 ③字段原样。返回 (brand_norm 英文, brand_zh 中文)。"""
    n = recognize_brand(brand) or recognize_brand(description)
    if n:
        return (n, BRAND_ZH.get(n))
    if brand and brand.strip():
        b = brand.strip()
        return (b, b if _CJK.search(b) else None)
    return (None, None)
