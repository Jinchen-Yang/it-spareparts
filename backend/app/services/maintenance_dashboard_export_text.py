"""维保数据看板导出的纯文本防公式模块（Lane B / R34，D-0024）。

生产 api.maintenance 没有 _safe_formula_only，且该 API 体量过大不可 import——
看板导出在此自持一份等价实现。规则与 main api/maintenance 的金臣规则一致：
只对真正像公式的文本加撇号，业务负值（-48V、+/-0.5mm 等）不误伤。

API 层捕获 ExportCellLimitError 转 413；本模块不依赖 FastAPI。
"""

import re


class ExportCellLimitError(ValueError):
    """单元格（含转义后）超过 Excel 安全上限；API 层转换为 413。"""


MAX_CSV_CELL_CHARS = 32_767

_INVALID_CSV_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f￾￿]")

# 金臣规则（同 #424）：只转义真正像公式的值，不给普通文本加撇号。
# 一律转义：= @ 开头（忽略前导空白）、Tab/CR/LF 开头。
# +/- 开头（可有前导空白）：去掉符号后的空白再判断，以下任一成立才转义：
#   函数调用（SUM(、cmd(）；| 或 !；独立引用词——单元格 A1/$B$2/A1:B3、整列 A:A/$A:$B、
#   整行 1:1/$1:$3；裸 TRUE/FALSE/PI 等名称；整段可解析为常量表达式；或整段仅由
#   数字/空白/运算符组成且含括号（+1 (800) 555-1234）。
_CSV_FUNCTION_CALL = re.compile(r"(?<![A-Za-z0-9_.])[A-Za-z_][A-Za-z0-9_.]*\s*\(")
_CSV_DDE_CHARS = re.compile(r"[|!]")
_CSV_CELL_REF = r"\$?[A-Za-z]{1,3}\$?\d+"
_CSV_REFERENCE_TOKEN = (
    rf"{_CSV_CELL_REF}(?::{_CSV_CELL_REF})?"   # A1、$B$2、A1:B3
    r"|\$?[A-Za-z]{1,3}:\$?[A-Za-z]{1,3}"      # 整列 A:A、$A:$B、XFD:XFD
    r"|\$?\d+:\$?\d+"                          # 整行 1:1、$1:$3（+12:30 同形，可接受）
)
_CSV_CELL_REFERENCE = re.compile(
    rf"(?<![A-Za-z0-9_.$])(?:{_CSV_REFERENCE_TOKEN})(?![A-Za-z0-9_.(])"
)
_CSV_BARE_NAME = re.compile(
    r"(?i)(?:TRUE|FALSE|PI|NA|RAND|NOW|TODAY)(?![A-Za-z0-9_.])"
)
_CSV_EXPR_TOKEN = re.compile(
    r"(?P<num>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r'|(?P<str>"(?:[^"]|"")*")'
    r"|(?P<op><=|>=|<>|[-+*/^&=<>%()])"
)
# 白名单先行：整段仅数字/空白/运算符/括号且含 "(" 才视为括号数字启发式。
# 旧写法两侧字符类含 "(" 时 fullmatch 失败按位置二次回溯（ReDoS）；先线性
# fullmatch 再查 "("，32k 对抗输入从数秒降到毫秒级。
_CSV_NUMERIC_CHARS = re.compile(r"[\d\s.+\-*/^%&<>=()]*")
_CSV_BINARY_OPS = frozenset({"+", "-", "*", "/", "^", "&", "=", "<", ">", "<=", ">=", "<>"})


def _csv_expression_tokens(text):
    """符号后整段切成常量表达式记号；首记号前不得有空白（由调用方 lstrip 负责）。"""
    tokens, pos, text = [], 0, text.rstrip()
    while pos < len(text):
        if pos and text[pos].isspace():
            pos += 1
            continue
        match = _CSV_EXPR_TOKEN.match(text, pos)
        if match is None:
            return None
        tokens.append((match.lastgroup, match.group()))
        pos = match.end()
    return tokens


def _is_constant_formula(text):
    """整段是否为 Excel 可求值的常量表达式（单个数字不算，-1.500/+1E+5 原样）。"""
    tokens = _csv_expression_tokens(text)
    if not tokens or (len(tokens) == 1 and tokens[0][0] == "num"):
        return False
    index = 0

    def operand():
        nonlocal index
        while index < len(tokens) and tokens[index][1] in ("+", "-"):
            index += 1
        if index >= len(tokens):
            return False
        kind, token = tokens[index]
        if kind in ("num", "str"):
            index += 1
        elif token == "(":
            index += 1
            if not expression() or index >= len(tokens) or tokens[index][1] != ")":
                return False
            index += 1
        else:
            return False
        while index < len(tokens) and tokens[index][1] == "%":
            index += 1
        return True

    def expression():
        nonlocal index
        if not operand():
            return False
        while index < len(tokens) and tokens[index][1] in _CSV_BINARY_OPS:
            index += 1
            if not operand():
                return False
        return True

    try:
        return expression() and index == len(tokens)
    except RecursionError:
        return True  # 超深括号嵌套：按公式转义，绝不 500


def _signed_value_is_formula(rest):
    """rest：+/- 之后的文本；先去掉符号后的空白（+ A1、+ 1+1、-\\tA1 也是公式）。"""
    rest = rest.lstrip()
    return bool(
        _CSV_FUNCTION_CALL.search(rest)
        or _CSV_DDE_CHARS.search(rest)
        or _CSV_CELL_REFERENCE.search(rest)
        or _CSV_BARE_NAME.match(rest)
        or _is_constant_formula(rest)
        or (
            _CSV_NUMERIC_CHARS.fullmatch(rest) is not None
            and "(" in rest
        )
    )


def safe_formula_only(v):
    """清理控制字符与长度上限，但只对真正像公式的文本加撇号。

    非字符串原样返回（Decimal/None/数字不经过净化）。
    超限（净化前或转义后）抛 ExportCellLimitError，由 API 层转 413。
    """
    if not isinstance(v, str):
        return v
    value = _INVALID_CSV_CONTROLS.sub("", v)
    # 长度闸门必须在公式启发式之前：备注/描述无上限，Excel 单元格最大 32767；
    # 超长对抗串若先跑括号数字正则会卡住 GIL（见 _CSV_NUMERIC_CHARS 注释）。
    if len(value) > MAX_CSV_CELL_CHARS:
        raise ExportCellLimitError(
            f"CSV 单元格超过 {MAX_CSV_CELL_CHARS} 字符安全上限"
        )
    probe = value.lstrip()
    head = probe[:1]
    if value[:1] in ("\t", "\r", "\n"):
        value = "'" + value
    elif head in ("=", "@") or (
        head in ("+", "-") and _signed_value_is_formula(probe[1:])
    ):
        value = "'" + value
    # 加撇号后仍不得超过 Excel 单元格上限。
    if len(value) > MAX_CSV_CELL_CHARS:
        raise ExportCellLimitError(
            f"CSV 单元格超过 {MAX_CSV_CELL_CHARS} 字符安全上限"
        )
    return value
