"""检索证据限定完整 PN；相关性判断不改写提供商返回的来源链接。"""

import re
from urllib.parse import parse_qs, unquote, urlsplit

HYPHENS = str.maketrans({char: "-" for char in "‐‑‒–—―−﹘﹣－"})

def normalize_pn(raw):
    return re.sub(r"\s+", " ", (raw or "").translate(HYPHENS).replace("\u00a0", " ").strip()).upper()



def mentions_pn(text: str, pn: str) -> bool:
    identity = normalize_pn(pn)
    if not identity:
        return False
    pattern = re.escape(identity).replace(r"\-", r"\s*-\s*")
    before = r"(?<![A-Z0-9._+-])"
    after = r"(?![A-Z0-9]|\s*[-_.+]\s*[A-Z0-9])"
    if identity.isdigit():
        after += r"(?!\s*(?:MHZ|GHZ|MT/S|GB|TB|MB|RPM|WATT|W\b))"
    return re.search(before + pattern + after, normalize_pn(unquote(text))) is not None


def _url_mentions_pn(url: str, pn: str) -> bool:
    parsed = urlsplit(url)
    # HTML 文档扩展名不是料号修订；只在 URL 路径末尾剥离这两种后缀。
    path = re.sub(r"\.html?$", "", unquote(parsed.path), flags=re.I)
    return mentions_pn(path, pn) or any(
        normalize_pn(value) == normalize_pn(pn)
        for values in parse_qs(parsed.query).values()
        for value in values
    )


def _short_pn_context(text: str, context: dict) -> bool:
    fields = context.get("fields", {})
    # 数字/短料号不能仅凭出现同一串数字就当作商品证据。
    for key in ("brand", "manufacturer"):
        value = fields.get(key, "")
        if len(value) >= 2 and re.search(
            r"(?<![A-Z0-9])" + re.escape(value) + r"(?![A-Z0-9])", text, re.I
        ):
            return True
    categories = (
        r"内存|\b(?:MEMORY|RAM|DIMM|DDR[2345]|PC[2345])\b",
        r"硬盘|固态|\b(?:HDD|SSD|HARD DRIVE|HARD DISK)\b",
        r"处理器|\b(?:CPU|PROCESSOR|XEON|EPYC)\b",
        r"电源|\b(?:POWER SUPPLY|PSU)\b",
        r"网络设备|网卡|交换机|光模块|\b(?:NIC|ETHERNET|TRANSCEIVER|NETWORK ADAPTER)\b",
        r"主板|背板|\b(?:MOTHERBOARD|SYSTEM BOARD|BACKPLANE)\b",
    )
    category = f"{fields.get('category_major', '')} {fields.get('category_minor', '')}"
    return any(
        re.search(pattern, category, re.I) and re.search(pattern, text, re.I)
        for pattern in categories
    )


def _compatibility_reference(title: str, pn: str) -> bool:
    # “替代某 PN”中的完整编号是兼容对象，不能因此把第三方替代件认作原件。
    # 原件标题先给出自身 PN、随后说明兼容系统时仍允许使用该来源。
    markers = re.finditer(
        r"\b(?:replacement|substitute)\s+for\b|\bcompatible\s+with\b|"
        r"(?:替代|替换|代用|兼容|适用于|适配)",
        title,
        re.I,
    )
    return any(
        mentions_pn(title[match.end() :], pn) and not mentions_pn(title[: match.start()], pn)
        for match in markers
    )


def partition_evidence(evidence, pn: str, context: dict):
    short = len(re.sub(r"[^A-Z0-9]", "", normalize_pn(pn))) < 6 or normalize_pn(pn).isdigit()
    ranked, rejected = [], []
    for index, entry in enumerate(evidence):

        def reject(reason):
            rejected.append({"title": entry.title, "url": str(entry.url), "reason": reason})

        if not product_page(str(entry.url)):
            reject("homepage_or_listing")
            continue
        if _compatibility_reference(entry.title, pn):
            reject("compatibility_reference")
            continue
        title_hit = mentions_pn(entry.title, pn)
        url_hit = _url_mentions_pn(str(entry.url), pn)
        snippet_hit = mentions_pn(entry.snippet, pn)
        # 兼容表或替代品说明常提到目标 PN；摘要提及本身不能确立商品身份。
        if not (title_hit or url_hit):
            reject("pn_not_matched")
            continue
        if short and not (
            title_hit and _short_pn_context(f"{entry.title} {entry.snippet}", context)
        ):
            reject("short_pn_context_missing")
            continue
        score = 4 * title_hit + 2 * url_hit + snippet_hit
        ranked.append((-score, index, entry))
    return [entry for _, _, entry in sorted(ranked)], rejected


def relevant_evidence(evidence, pn: str, context: dict):
    return partition_evidence(evidence, pn, context)[0]


def product_page(url: str) -> bool:
    parsed = urlsplit(url)
    path = unquote(parsed.path).strip("/").lower()
    listing_segments = {
        "search",
        "catalog",
        "listing",
        "category",
        "collections",
        "sys-pd",
        "ram selector",
    }
    segments = {re.sub(r"\.html?$", "", part) for part in path.split("/")}
    if segments & listing_segments:
        return False
    if any(parse_qs(parsed.query).get(key) for key in ("id", "item", "product", "pid", "sku")):
        return True
    return bool(path) and path not in {
        "products",
        "product",
        "itm",
        "search",
        "catalog",
        "category",
        "collections",
        "sys-pd",
        "ram selector",
    }


def product_image_url(url: str) -> bool:
    path = unquote(urlsplit(url).path).lower()
    if path.endswith((".svg", ".ico", ".gif")):
        return False
    return not re.search(
        r"(?:^|[/_.-])(?:logo\w*|icons?|favicon|sprite|placeholder|avatar|banner|homepage)(?:[/_.-]|$)",
        path,
    )
