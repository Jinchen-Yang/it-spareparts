"""实时检索文档与查询共用的归一化口径。"""

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def normalize(value: str | None) -> str:
    """兼容字符归一、大写，再把 Unicode 连续空白合成一个空格。"""
    return " ".join(unicodedata.normalize("NFKC", value or "").upper().split())


def compact(value: str | None) -> str:
    """沿用 pn_compact 的 ASCII 字母数字集合，先接受全角输入。"""
    return _NON_ALNUM.sub("", normalize(value))
