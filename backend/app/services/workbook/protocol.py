"""签名覆盖导出范围和行基线；隐藏元数据仅承担协议，不代替权限。"""

from __future__ import annotations

import hashlib
import hmac
import json

from app.config import get_settings
from app.services.workbook.schema import WorkbookError, json_value

TOKEN_HEADER = "_row_token"
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_ZIP_ENTRIES = 2048
MAX_SHEET_ROWS = 20000
MAX_SHEET_COLUMNS = 256


def canonical(value) -> str:
    return json.dumps(json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sign(value) -> str:
    return hmac.new(get_settings().secret_key.encode("utf-8"), canonical(value).encode("utf-8"), hashlib.sha256).hexdigest()


def row_token(schema, export_id, sheet, entity_id, base_version) -> str:
    return sign(["workbook-row-v1", schema.name, str(schema.version), str(export_id), sheet, entity_id, base_version])


def write_metadata(wb, metadata: dict, manifest: dict) -> None:
    ws = wb.create_sheet("_meta")
    values = {k: str(v) for k, v in metadata.items()}
    payload = canonical(manifest)
    for index, offset in enumerate(range(0, len(payload), 20000)):
        values[f"payload_{index:06d}"] = payload[offset:offset + 20000]
    values["signature"] = sign(values)
    for key, value in values.items():
        ws.append([key, value])
        ws.cell(ws.max_row, 2).data_type = "s"
    ws.sheet_state = "hidden"


def read_metadata(wb) -> tuple[dict, dict]:
    try:
        ws = wb["_meta"]
        if ws.max_column != 2 or ws.max_row > 20000:
            raise ValueError
        values = {}
        for key, value in ws.iter_rows(values_only=True):
            if not isinstance(key, str) or key in values or not isinstance(value, str):
                raise ValueError
            values[key] = value
        signature = values.pop("signature")
        if not hmac.compare_digest(signature, sign(values)):
            raise ValueError
        for key in ("schema_name", "schema_version", "export_id", "actor", "exported_at", "date_system"):
            if key not in values:
                raise ValueError
        if values["date_system"] not in {"1900", "1904"}:
            raise ValueError
        payload = "".join(values[key] for key in sorted(values) if key.startswith("payload_"))
        manifest = json.loads(payload)
        return {k: v for k, v in values.items() if not k.startswith("payload_")}, manifest
    except (KeyError, ValueError, TypeError):
        raise WorkbookError("请使用系统导出的模板（元数据缺失或签名无效）") from None
