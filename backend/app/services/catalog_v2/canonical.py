"""catalog-v2 §6 的规范 UTF-8 JSON 编码与 SHA256。

序列化器逐字符编码，避免依赖 json.dumps 的默认转义。对象只保留调用者
传入的字段；hash 边界由下方各具名函数明确选择，不递归删除信封字段。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any


class CanonicalError(ValueError):
    """输入不能按 catalog-v2 的规范编码。"""


_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


def normalize_decimal(value: str | Decimal) -> str:
    """规范十进制文本；不用 normalize()，避免 Decimal context 舍入。"""
    if not isinstance(value, (str, Decimal)):
        raise CanonicalError("numeric_value must be a decimal string")
    if isinstance(value, str) and not _DECIMAL.fullmatch(value):
        raise CanonicalError("invalid decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise CanonicalError("invalid decimal string") from exc
    if not number.is_finite():
        raise CanonicalError("non-finite decimal is forbidden")
    if number.is_zero():
        return "0"
    result = format(number, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _array_key(field: str, item: Any) -> Any:
    if field in {"locked_fields", "selected_fields", "transaction_groups", "evidence_refs"}:
        if not isinstance(item, str):
            raise CanonicalError(f"{field} requires strings")
        return item
    if not isinstance(item, dict):
        raise CanonicalError(f"{field} requires objects")
    if field == "items":
        kind = item.get("kind")
        if kind not in {"node", "alias"}:
            raise CanonicalError("items kind must be node or alias")
        nested = item.get(kind)
        if not isinstance(nested, dict):
            raise CanonicalError("items requires its complete node or alias")
        identity = nested.get("part_id" if kind == "node" else "alias_id")
        if type(identity) is not int:
            raise CanonicalError("items identity must be an integer")
        return (0 if kind == "node" else 1, identity)
    key = {"specs": "key", "nodes": "part_id", "aliases": "alias_id", "alias_moves": "alias_id", "field_baselines": "field", "operations": "field"}[field]
    target = item.get(key)
    expected_type = int if field in {"nodes", "aliases", "alias_moves"} else str
    if type(target) is not expected_type:
        raise CanonicalError(f"invalid {field} target")
    if field == "operations":
        spec_key = item.get("key", "")
        if not isinstance(spec_key, str):
            raise CanonicalError("operation key must be a string")
        return (target, spec_key)
    return target


_ORDERED_ARRAYS = {
    "specs", "locked_fields", "nodes", "aliases", "items", "transaction_groups",
    "alias_moves", "selected_fields", "field_baselines", "operations", "evidence_refs",
}


def normalize(value: Any) -> Any:
    """复制输入并按协议语义排序、规范 numeric_value；展示文字不变。"""
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise CanonicalError("object keys must be strings")
        result = {}
        for key, item in value.items():
            _string(key)
            if key == "numeric_value" and item is not None:
                result[key] = normalize_decimal(item)
                continue
            normalized = normalize(item)
            if key in _ORDERED_ARRAYS:
                if not isinstance(normalized, list):
                    raise CanonicalError(f"{key} must be an array")
                keyed = [(_array_key(key, item), item) for item in normalized]
                if key == "locked_fields":
                    normalized = sorted(set(normalized))
                else:
                    targets = [target for target, _ in keyed]
                    if len(set(targets)) != len(targets):
                        raise CanonicalError(f"duplicate {key} target")
                    normalized = [item for _, item in sorted(keyed, key=lambda pair: pair[0])]
            result[key] = normalized
        return result
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, str):
        _string(value)
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    raise CanonicalError(f"unsupported JSON type: {type(value).__name__}")


def strict_loads(raw: str | bytes) -> Any:
    """解析 JSON 并拒绝重复键、浮点、非有限数与非 Unicode 标量。"""
    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise CanonicalError("duplicate object key")
            result[key] = value
        return result

    def forbidden_number(_value: str) -> Any:
        raise CanonicalError("floating point and non-finite JSON numbers are forbidden")

    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        value = json.loads(raw, object_pairs_hook=pairs_hook,
                           parse_float=forbidden_number, parse_constant=forbidden_number)
        # Only validate JSON scalars here. The DTO rejects unknown fields before
        # semantic normalization could expand an untrusted decimal exponent.
        _encode(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, CanonicalError):
            raise
        raise CanonicalError("invalid JSON") from exc


def _string(value: str) -> str:
    escapes = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}
    result = ['"']
    for char in value:
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF:
            raise CanonicalError("unpaired surrogate is not a Unicode scalar")
        result.append(escapes.get(char, f"\\u{code:04x}" if code < 32 else char))
    result.append('"')
    return "".join(result)


def _encode(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise CanonicalError("object keys must be strings")
        return "{" + ",".join(_string(key) + ":" + _encode(value[key]) for key in sorted(value)) + "}"
    raise CanonicalError(f"unsupported JSON type: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    """C(x): 无 BOM、无空白、无换行的规范 JSON UTF-8 字节。"""
    return _encode(normalize(value)).encode("utf-8")


def canonical_hash(value: Any) -> str:
    """H(x): 规范 JSON 字节的 SHA256 小写十六进制摘要。"""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def node_hash(node: Mapping[str, Any]) -> str:
    """仅排除该节点自身 content_hash；空值及完整规格都参与摘要。"""
    return canonical_hash({key: value for key, value in node.items() if key != "content_hash"})


_BATCH_FIELDS = (
    "schema_version", "source_namespace", "publication_epoch", "batch_id",
    "published_sequence", "previous_sequence", "transaction_groups", "items", "alias_moves",
)


def batch_hash(batch: Mapping[str, Any]) -> str:
    """逻辑完整批摘要；HTTP 信封、分片位置与 extensions 不参与。"""
    try:
        value = {key: batch[key] for key in _BATCH_FIELDS}
    except KeyError as exc:
        raise CanonicalError(f"missing batch field: {exc.args[0]}") from exc
    return canonical_hash(value)


def snapshot_hash(nodes: list[dict[str, Any]], aliases: list[dict[str, Any]]) -> str:
    """固定投影摘要；校验并保留节点摘要，排除已删除别名。"""
    for node in nodes:
        if node.get("content_hash") != node_hash(node):
            raise CanonicalError("node content_hash mismatch")
    return canonical_hash({
        "schema_version": 2,
        "source_namespace": "itsp",
        "nodes": nodes,
        "aliases": [alias for alias in aliases if alias.get("deleted") is not True],
    })


def segment_hash(nodes: list[dict[str, Any]], aliases: list[dict[str, Any]],
                 start_part_id: int, end_part_id_exclusive: int) -> str:
    """ID 半开区间的快照摘要，别名按所属 part_id 归段。"""
    return snapshot_hash(
        [node for node in nodes if start_part_id <= node["part_id"] < end_part_id_exclusive],
        [alias for alias in aliases if start_part_id <= alias["part_id"] < end_part_id_exclusive],
    )


def body_hash(body: Mapping[str, Any]) -> str:
    """请求体只排除自身 body_hash；业务 request_id/correlation_id 仍入摘要。"""
    return canonical_hash({key: value for key, value in body.items() if key != "body_hash"})


def bytes_hash(raw: bytes) -> str:
    """分片与完整 payload 对原始字节摘要，不额外 JSON 编码。"""
    return hashlib.sha256(raw).hexdigest()


C = canonical_bytes
H = canonical_hash
