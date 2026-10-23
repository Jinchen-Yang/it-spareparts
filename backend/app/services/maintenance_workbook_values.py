"""Standard-library workbook value and identity codecs.

Signing keys are explicit inputs here. Keyring access, Excel objects, live
database validation and apply coordination remain in their adapters. Automatic
site identity deliberately increments the caller's occurrence counter.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date, datetime
from decimal import Decimal

from app.services.maintenance_workbook_protocol import (
    _META_CELL_SAFE_LENGTH,
    _META_CHUNK_SEP,
    _V2_META_SIGNATURE_DOMAIN,
)


def _num(value) -> float | str:
    return float(value) if value is not None else ""


def _global_cost_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")


def _v2_hash_value(value) -> str:
    """哈希输入归一化：Excel 往返后 Decimal 变 float、date 变 datetime，
    直接 json 序列化会导致导出侧与解析侧哈希不一致（checkpoint 潜伏 bug）。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float, Decimal)):
        return format(Decimal(str(value)).normalize(), "f")
    return str(value)


def _v2_hash(values: list[object]) -> str:
    return hashlib.sha256(json.dumps(
        [_v2_hash_value(v) for v in values],
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _v2_row_base_hash(values: dict[str, object], fields: tuple[str, ...]) -> str:
    """行级基线哈希（进 99_元数据）：缺行=作废时判定「服务端该行是否已变」。"""
    return _v2_hash([_v2_hash_value(values.get(name)) for name in fields])


def _v2_metadata_signature(metadata: dict[str, str], hmac_key: bytes) -> str:
    """Bind the complete export identity set to a server-only key.

    ``parts_row_ids`` / ``site_row_ids`` / ``expense_row_ids`` /
    ``receipt_row_ids`` drive the
    destructive "missing row = VOID" reconciliation.  Merely hiding those
    cells is not an integrity boundary, so every metadata field (including
    project and included sheets) is signed as one canonical envelope.
    """

    payload = json.dumps(
        {
            key: str(metadata[key])
            for key in sorted(metadata)
            if key != "metadata_hmac"
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(
        hmac_key,
        _V2_META_SIGNATURE_DOMAIN + payload,
        hashlib.sha256,
    ).hexdigest()


def _chunk_meta_value(key: str, value: str) -> list[tuple[str, str]]:
    """Excel 单元格硬上限 32767：超长值（大项目 parts_base_hashes 实测 40KB+）
    会被静默截断，签名必炸（2.7.0 生产回归）。按 30000 分块为 key@2/@3...，
    读取端 _v2_meta 合并后再参与验签/使用。首块保留原名，兼容短值零分块。"""
    text = str(value)
    if len(text) <= _META_CELL_SAFE_LENGTH:
        return [(key, text)]
    chunks = [text[i:i + _META_CELL_SAFE_LENGTH]
              for i in range(0, len(text), _META_CELL_SAFE_LENGTH)]
    return [(key, chunks[0])] + [
        (f"{key}{_META_CHUNK_SEP}{n + 2}", chunk)
        for n, chunk in enumerate(chunks[1:])
    ]


def _is_example_row(row) -> bool:
    """上传侧识别示例行：任一单元格为「示例」或以「【示例】」开头。"""
    for value in row or ():
        text = str(value or "").strip()
        if text == "示例" or text.startswith("【示例】"):
            return True
    return False


def _cell(row, index: dict[str, int], name: str):
    return row[index[name]] if name in index and index[name] < len(row) else None


def _v2_auto_site_prefix(project_id, export_id):
    scope = json.dumps([project_id, export_id], ensure_ascii=False)
    return "LY-AUTO-" + hashlib.sha256(scope.encode()).hexdigest()[:20] + "-"


def _v2_auto_site_no(project_id, export_id, raw_pn, issue_date, serial_number, demand_no,
                     quantity, flag, remark, occurrences):
    # Signed export scope separates independent workbooks; content+occurrence
    # survives sorting and retains physically distinct identical rows.
    identity = json.dumps([project_id, export_id, raw_pn, issue_date.isoformat(),
                           serial_number or "", demand_no or "", str(quantity), flag, remark], ensure_ascii=False)
    occurrences[identity] += 1
    digest = hashlib.sha256(f"{identity}|{occurrences[identity]}".encode()).hexdigest()[:24]
    return _v2_auto_site_prefix(project_id, export_id) + digest


def _encode_base_hashes(hashes: dict[str, str]) -> str:
    """行基线哈希图（entity_id -> sha256），用于缺行=作废的行级冲突判定。"""
    return ",".join(f"{eid}:{digest}" for eid, digest in sorted(hashes.items()))


def _decode_base_hashes(value: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in str(value or "").split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        eid, digest = chunk.rsplit(":", 1)
        out[eid] = digest
    return out


def _encode_row_ids(ids) -> str:
    """导出时行ID全集（P1，Codex review #272）：缺行=作废只允许命中
    「导出时存在、上传时消失」的行；导出之后新导入的行天然豁免，
    应用后原样重传（幂等重试）也不受影响。"""
    return ",".join(str(i) for i in sorted(ids))


def _decode_row_ids(value: str | None) -> set[str]:
    if not value:
        return set()
    return {part for part in value.split(",") if part}
