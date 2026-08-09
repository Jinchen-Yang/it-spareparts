"""Shared authenticated JSON envelope for Agent control-plane evidence.

The implementation intentionally accepts only the RFC 8785 subset used by our
versioned domain schemas (objects, arrays, strings, booleans, null, and safe
integers). Floating-point values are rejected instead of being ambiguously rounded.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from collections.abc import Mapping


ENVELOPE_SCHEMA_VERSION = "integrity-envelope/v1"
CANONICALIZATION = "RFC8785"
ALGORITHM = "HMAC-SHA-256"
_DOMAIN_SEPARATOR = b"it-data:integrity-envelope/v1\x00"
_HEADER_KEYS = {
    "schema_version",
    "purpose",
    "payload_schema_version",
    "canonicalization",
    "algorithm",
    "key_id",
    "payload_sha256",
}
_KEY_STATUSES = {"active", "verify_only", "revoked"}
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 10_000
MAX_JSON_STRING_BYTES = 2 * 1024 * 1024
MAX_JSON_WORK = 100_000


class IntegrityError(ValueError):
    """Envelope cannot be safely authenticated under the current key policy."""


@dataclass(slots=True)
class _JsonBudget:
    """One canonicalization-wide budget, shared by every recursive branch."""

    nodes: int = 0
    string_bytes: int = 0
    work: int = 0

    def charge_node(self, *, depth: int) -> None:
        self.nodes += 1
        # Depth-weighted work bounds both a very wide value and repeated copying/
        # comparison along many nested paths.  It is deliberately separate from the
        # node cap so changing either guard cannot silently remove the other.
        self.work += depth + 1
        if self.nodes > MAX_JSON_NODES:
            raise IntegrityError("完整性载荷超过 JSON 节点预算")
        if self.work > MAX_JSON_WORK:
            raise IntegrityError("完整性载荷超过 JSON 工作量预算")

    def charge_string(self, value: str) -> None:
        # UTF-8 uses at least one byte per code point.  Reject an obviously oversized
        # value before allocating a second, potentially huge encoded copy.
        if len(value) > MAX_JSON_STRING_BYTES:
            raise IntegrityError("完整性载荷超过 JSON 字符串预算")
        self.string_bytes += len(value.encode("utf-8"))
        if self.string_bytes > MAX_JSON_STRING_BYTES:
            raise IntegrityError("完整性载荷超过 JSON 字符串预算")


@dataclass(frozen=True, slots=True)
class IntegrityKey:
    secret: bytes
    status: str

    def __post_init__(self) -> None:
        if len(self.secret) < 32:
            raise IntegrityError("完整性密钥长度不足")
        if self.status not in _KEY_STATUSES:
            raise IntegrityError("完整性密钥状态未知")


@dataclass(frozen=True, slots=True)
class IntegrityKeyring:
    active_key_id: str
    keys: Mapping[str, IntegrityKey]

    def __post_init__(self) -> None:
        object.__setattr__(self, "keys", MappingProxyType(dict(self.keys)))

    def signing_key(self) -> tuple[str, IntegrityKey]:
        key_id = str(self.active_key_id or "").strip()
        key = self.keys.get(key_id)
        if not key_id or key is None or key.status != "active":
            raise IntegrityError("没有可用的 active 完整性签发密钥")
        return key_id, key

    def verification_key(self, key_id: str) -> IntegrityKey:
        key = self.keys.get(key_id)
        if key is None or key.status not in {"active", "verify_only"}:
            raise IntegrityError("完整性密钥未知或已撤销")
        return key


def _validate_json(
    value: Any,
    *,
    depth: int = 0,
    budget: _JsonBudget | None = None,
) -> None:
    if depth > MAX_JSON_DEPTH:
        raise IntegrityError("完整性载荷嵌套过深")
    if budget is None:
        budget = _JsonBudget()
    budget.charge_node(depth=depth)
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise IntegrityError("完整性载荷包含非法 Unicode surrogate")
        budget.charge_string(value)
        return
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > 9_007_199_254_740_991:
            raise IntegrityError("完整性载荷整数超出 I-JSON 安全范围")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth=depth + 1, budget=budget)
        return
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise IntegrityError("完整性载荷对象键必须是字符串")
        for key, item in value.items():
            if not key.isascii():
                raise IntegrityError("受限 RFC8785 schema 的对象键必须是 ASCII")
            _validate_json(key, depth=depth + 1, budget=budget)
            _validate_json(item, depth=depth + 1, budget=budget)
        return
    raise IntegrityError("完整性载荷只允许 I-JSON 确定性类型")


def canonicalize(value: Any) -> bytes:
    """Return RFC 8785 bytes for the schema's deliberately restricted I-JSON subset."""
    _validate_json(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _mac_input(header: dict[str, Any], payload: dict[str, Any]) -> bytes:
    return _DOMAIN_SEPARATOR + canonicalize(header) + b"\x00" + canonicalize(payload)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def seal(
    payload: dict[str, Any],
    *,
    purpose: str,
    payload_schema_version: str,
    keyring: IntegrityKeyring,
    max_payload_bytes: int = 256 * 1024,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise IntegrityError("完整性载荷必须是对象")
    if not purpose or not payload_schema_version:
        raise IntegrityError("完整性 Envelope 缺少 purpose 或 payload schema")
    payload_bytes = canonicalize(payload)
    if len(payload_bytes) > max_payload_bytes:
        raise IntegrityError("完整性载荷超过大小预算")
    key_id, key = keyring.signing_key()
    header = {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "purpose": purpose,
        "payload_schema_version": payload_schema_version,
        "canonicalization": CANONICALIZATION,
        "algorithm": ALGORITHM,
        "key_id": key_id,
        "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
    }
    mac = hmac.new(key.secret, _mac_input(header, payload), hashlib.sha256).digest()
    return {"header": header, "payload": payload, "mac": _b64url(mac)}


def verify(
    envelope: dict[str, Any],
    *,
    allowed_purposes: set[str],
    allowed_payload_schemas: set[str],
    keyring: IntegrityKeyring,
    max_payload_bytes: int = 256 * 1024,
) -> dict[str, Any]:
    if not isinstance(envelope, dict) or set(envelope) != {"header", "payload", "mac"}:
        raise IntegrityError("完整性 Envelope 结构无效")
    header = envelope.get("header")
    payload = envelope.get("payload")
    mac = envelope.get("mac")
    if not isinstance(header, dict) or set(header) != _HEADER_KEYS:
        raise IntegrityError("完整性 Envelope header 无效")
    if not isinstance(payload, dict) or not isinstance(mac, str):
        raise IntegrityError("完整性 Envelope payload 或 mac 无效")
    if (
        header.get("schema_version") != ENVELOPE_SCHEMA_VERSION
        or header.get("canonicalization") != CANONICALIZATION
        or header.get("algorithm") != ALGORITHM
        or header.get("purpose") not in allowed_purposes
        or header.get("payload_schema_version") not in allowed_payload_schemas
    ):
        raise IntegrityError("完整性 Envelope purpose 或版本不兼容")
    key_id = header.get("key_id")
    if not isinstance(key_id, str):
        raise IntegrityError("完整性 Envelope key_id 无效")
    key = keyring.verification_key(key_id)
    payload_bytes = canonicalize(payload)
    if len(payload_bytes) > max_payload_bytes:
        raise IntegrityError("完整性载荷超过大小预算")
    digest = hashlib.sha256(payload_bytes).hexdigest()
    if not hmac.compare_digest(str(header.get("payload_sha256") or ""), digest):
        raise IntegrityError("完整性载荷摘要不匹配")
    expected = _b64url(
        hmac.new(key.secret, _mac_input(header, payload), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(mac, expected):
        raise IntegrityError("完整性 MAC 不匹配")
    # Do not return the caller-owned nested objects that were just authenticated.
    return json.loads(payload_bytes.decode("utf-8"))


def configured_keyring() -> IntegrityKeyring:
    """Load the process key policy without ever including secret values in errors."""
    from app.config import get_settings

    settings = get_settings()
    try:
        raw = json.loads(settings.agent_integrity_keys_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise IntegrityError("Agent 完整性密钥环配置无效") from exc
    if not isinstance(raw, dict):
        raise IntegrityError("Agent 完整性密钥环配置无效")
    keys: dict[str, IntegrityKey] = {}
    try:
        for key_id, spec in raw.items():
            if (
                not isinstance(key_id, str)
                or not key_id
                or not isinstance(spec, dict)
                or set(spec) != {"key", "status"}
                or not isinstance(spec["key"], str)
                or not isinstance(spec["status"], str)
            ):
                raise IntegrityError("Agent 完整性密钥环配置无效")
            encoded = spec["key"]
            padding = "=" * (-len(encoded) % 4)
            secret = base64.b64decode(
                encoded + padding,
                altchars=b"-_",
                validate=True,
            )
            keys[key_id] = IntegrityKey(secret=secret, status=spec["status"])
    except (ValueError, TypeError, base64.binascii.Error) as exc:
        raise IntegrityError("Agent 完整性密钥环配置无效") from exc
    return IntegrityKeyring(
        active_key_id=settings.agent_integrity_active_key_id,
        keys=keys,
    )
