"""智能体文件原语层。

设计思想（借鉴 Claude Code 等智能体框架）：客户文件格式千变万化——询价单、
整机配置（Word/Excel/PDF/txt/图片）——**不写死解析规则**，给模型"眼睛"
（inspect/read_document：看结构与原样内容，自己判断表头/型号列/拆件）和
"手"（write_excel 回填模板 / write_report 生成美化报表）。

安全边界：不提供任意代码执行（多用户后端 exec = RCE）；上传文件只读，写操作
一律产出新 file_id（绝不改写原上传件）；file_id 白名单正则防路径穿越；
扩展名白名单防可执行文件。
"""
import hashlib
import json
import os
import re
import tempfile
import unicodedata
import uuid
import zipfile
from copy import deepcopy
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import column_index_from_string, get_column_letter

from app.config import get_settings
from app.db import SessionLocal
from app.models.agent_artifact import AgentArtifact

_LEGACY_FILE_ID = re.compile(r"^[a-f0-9]{12}$")
_MAX_UPLOAD_MB = 20
_PREVIEW_ROWS = 8
_PREVIEW_COLS = 12
_MAX_READ_ROWS = 200
_MAX_WRITE_CELLS = 3000
_CELL_TRUNC = 60
_DOC_CHAR_CAP = 60_000          # read_document 文本上限，防超长文件撑爆上下文

# 上传扩展名白名单（小写，无点）
_TEXT_EXT = {"txt", "csv", "md"}
_IMG_EXT = {"jpg", "jpeg", "png", "webp", "bmp"}
_ALLOWED_EXT = {"xlsx", "docx", "pdf"} | _TEXT_EXT | _IMG_EXT
_MIME_BY_EXT = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
    "txt": "text/plain; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "bmp": "image/bmp",
}
_FORMULA_PREFIXES = ("=", "+", "-", "@")


class FileError(Exception):
    """文件层业务错误（消息可直接回给模型/用户）。"""


class ArtifactV2Disabled(FileError):
    """Stable fail-closed signal used by HTTP and tool adapters."""


class ArtifactUnavailable(FileError):
    """File cannot be served; ``reason_code`` is safe for structured audit."""

    def __init__(self, message: str, reason_code: str):
        super().__init__(message)
        self.reason_code = reason_code


_ARTIFACT_V2_DISABLED_MESSAGE = "Artifact Delivery v2 已停用"
_VERIFIED_OWNER_PROOF = object()


def require_artifact_v2_enabled() -> None:
    if not get_settings().agent_artifact_v2_enabled:
        raise ArtifactV2Disabled(_ARTIFACT_V2_DISABLED_MESSAGE)


def artifact_reason_code(exc: FileError) -> str:
    if isinstance(exc, ArtifactV2Disabled):
        return "v2_disabled"
    if isinstance(exc, ArtifactUnavailable):
        return exc.reason_code
    return "validation_failed"


class VerifiedArtifactOwner:
    """Opaque actor derived from a verified ``UserContext``.

    Python has no module-private constructors, so construction additionally requires a
    module-owned sentinel. Besides the owner subject, the actor holds a defensive copy of
    the server-derived authorization context used for generated scope and source checks.
    """

    __slots__ = ("_context", "_sub")

    def __init__(self, sub: str, context: Any, proof: object):
        if proof is not _VERIFIED_OWNER_PROOF:
            raise TypeError("VerifiedArtifactOwner must come from verified_artifact_owner")
        self._sub = sub
        self._context = context

    @property
    def sub(self) -> str:
        return self._sub


@dataclass(frozen=True)
class StoredObject:
    path: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ArtifactDownload:
    path: Path
    filename: str
    media_type: str
    size_bytes: int
    sha256: str


class ArtifactStore(Protocol):
    """Artifact object-store seam; the first implementation is local and atomic."""

    def publish_bytes(
        self,
        storage_key: str,
        content: bytes,
        *,
        validator: Callable[[Path], None] | None = None,
    ) -> StoredObject: ...

    def path_for(self, storage_key: str) -> Path: ...

    def inspect(self, storage_key: str) -> StoredObject: ...

    def remove(self, storage_key: str) -> None: ...


class LocalArtifactStore:
    """Filesystem store with same-directory staging and atomic publication."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, storage_key: str) -> Path:
        key = str(storage_key or "")
        pure = PurePosixPath(key)
        if not key or pure.is_absolute() or ".." in pure.parts or "\\" in key:
            raise FileError("文件存储定位无效")
        root = self.root.resolve()
        path = (root / Path(*pure.parts)).resolve()
        if not path.is_relative_to(root):
            raise FileError("文件存储定位无效")
        return path

    def publish_bytes(
        self,
        storage_key: str,
        content: bytes,
        *,
        validator: Callable[[Path], None] | None = None,
    ) -> StoredObject:
        final_path = self.path_for(storage_key)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        temp_dir = self.root / ".tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="artifact-", suffix=".part", dir=temp_dir)
        temp_path = Path(temp_name)
        published = False
        digest = hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as handle:
                view = memoryview(content)
                for offset in range(0, len(view), 1024 * 1024):
                    chunk = view[offset:offset + 1024 * 1024]
                    handle.write(chunk)
                    digest.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if validator is not None:
                validator(temp_path)
            if final_path.exists():
                raise FileError("文件发布冲突，请重试")
            os.replace(temp_path, final_path)
            published = True
            dir_fd = os.open(final_path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            return StoredObject(
                path=final_path,
                size_bytes=final_path.stat().st_size,
                sha256=digest.hexdigest(),
            )
        except Exception:
            temp_path.unlink(missing_ok=True)
            if published:
                final_path.unlink(missing_ok=True)
            raise

    def inspect(self, storage_key: str) -> StoredObject:
        path = self.path_for(storage_key)
        if not path.is_file():
            raise FileError("文件不存在或已清理")
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
        except OSError as exc:
            raise FileError("文件暂时不可读取") from exc
        return StoredObject(path=path, size_bytes=size, sha256=digest.hexdigest())

    def remove(self, storage_key: str) -> None:
        self.path_for(storage_key).unlink(missing_ok=True)


def _dir() -> Path:
    # 必须落在持久卷内：生产的持久卷挂在 raw_file_dir(/app/data/raw)，放它的【子目录】才不会
    # 随容器重建被清空——否则每次部署/重启都会清掉 AI 生成的报价单，下载链接全部 404。
    # raw 导入文件是 raw_file_dir/{hash}.xlsx 平铺，agent_files 子目录与之不冲突。
    d = Path(get_settings().raw_file_dir) / "agent_files"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meta_path(file_id: str) -> Path:
    return _dir() / f"{file_id}.meta.json"


def _ext_of(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _safe_filename(filename: str, default: str = "artifact") -> str:
    """Keep readable Unicode while removing path/header/control ambiguity."""
    raw = unicodedata.normalize("NFKC", str(filename or default))
    raw = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in raw)
    raw = re.sub(r"[\\/:*?\"<>|]", "_", raw)
    raw = re.sub(r"\s+", " ", raw).strip().lstrip(".").strip()
    if not raw:
        raw = default
    if len(raw) > 180:
        suffix = Path(raw).suffix[:20]
        raw = f"{raw[:180 - len(suffix)].rstrip()}{suffix}"
    return raw


def _is_legacy_id(file_id: str) -> bool:
    return bool(_LEGACY_FILE_ID.fullmatch(file_id))


def _storage_key(file_id: str, ext: str) -> str:
    if ext not in _ALLOWED_EXT:
        raise FileError("文件类型元数据无效")
    if _is_legacy_id(file_id):
        raise FileError("新制品标识无效")
    return f"objects/{file_id}.{ext}"


def get_artifact_store() -> ArtifactStore:
    """Resolve the active store; kept as a seam for object-store replacement/tests."""
    return LocalArtifactStore(_dir())


def _data_path(file_id: str, ext: str) -> Path:
    fid = _check_id(file_id)
    if ext not in _ALLOWED_EXT:
        raise FileError("文件类型元数据无效")
    if _is_legacy_id(fid):
        return _dir() / f"{fid}.{ext}"
    return get_artifact_store().path_for(_storage_key(fid, ext))


def _check_id(file_id: str) -> str:
    fid = str(file_id or "").strip().lower()
    if _is_legacy_id(fid):
        return fid
    try:
        parsed = uuid.UUID(fid)
    except (ValueError, AttributeError) as exc:
        raise FileError("非法 file_id") from exc
    canonical = str(parsed)
    if canonical != fid:
        raise FileError("非法 file_id")
    return canonical


def _query_artifact(db, file_id: str) -> AgentArtifact | None:
    if _is_legacy_id(file_id):
        return None
    return db.get(AgentArtifact, file_id)


def _find_artifact_meta(file_id: str, *, require_ready: bool) -> dict | None:
    if _is_legacy_id(file_id):
        return None
    require_artifact_v2_enabled()
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        row = _query_artifact(db, file_id)
        if row is None:
            return None
        if row.expires_at <= now and row.status == "ready":
            row.status = "expired"
            db.commit()
        if require_ready and row.status != "ready":
            reason = row.status if row.status in {"expired", "failed"} else "not_ready"
            raise ArtifactUnavailable("文件不存在或不可下载", reason)
        extra = dict(row.extra_meta or {})
        return {
            **extra,
            "file_id": row.id,
            "filename": row.filename,
            "ext": _ext_of(row.filename),
            "kind": row.kind,
            "sensitivity": row.sensitivity,
            "operated_by": row.owner_sub,
            "owner_sub": row.owner_sub,
            "media_type": row.media_type,
            "size_bytes": row.size_bytes,
            "sha256": row.sha256,
            "status": row.status,
            "storage_key": row.storage_key,
            "source_ids": list(row.source_ids or []),
            "access_scope": dict(row.access_scope or {}),
            "created_at": row.created_at,
            "expires_at": row.expires_at,
        }


def _artifact_meta(file_id: str, *, require_ready: bool) -> dict:
    meta = _find_artifact_meta(file_id, require_ready=require_ready)
    if meta is None:
        raise ArtifactUnavailable("文件不存在或已清理", "not_found")
    return meta


def _verify_artifact(meta: dict) -> StoredObject:
    fid = meta["file_id"]
    ext = meta.get("ext", "")
    if meta.get("filename") != _safe_filename(meta.get("filename", "")):
        raise FileError("文件元数据校验失败")
    if meta.get("media_type") != _MIME_BY_EXT.get(ext):
        raise FileError("文件元数据校验失败")
    expected_key = _storage_key(fid, ext)
    if meta.get("storage_key") != expected_key:
        raise FileError("文件元数据校验失败")
    try:
        stored = get_artifact_store().inspect(expected_key)
    except FileError as exc:
        raise ArtifactUnavailable("文件对象不存在或已清理", "object_missing") from exc
    if stored.size_bytes != meta.get("size_bytes") or stored.sha256 != meta.get("sha256"):
        raise ArtifactUnavailable("文件完整性校验失败", "integrity_failed")
    return stored


def _load_meta(file_id: str) -> dict:
    fid = _check_id(file_id)
    meta = _find_artifact_meta(fid, require_ready=True)
    if meta is not None:
        _verify_artifact(meta)
        return meta
    if not _is_legacy_id(fid):
        raise ArtifactUnavailable("文件不存在或已清理", "not_found")
    p = _meta_path(fid)
    if not p.exists():
        raise ArtifactUnavailable("文件不存在或已清理", "not_found")
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FileError("文件元数据损坏") from exc
    ext = str(meta.get("ext", "")).lower()
    if ext not in _ALLOWED_EXT or not _data_path(fid, ext).is_file():
        raise ArtifactUnavailable("文件不存在或已清理", "object_missing")
    meta["filename"] = _safe_filename(meta.get("filename", f"{fid}.{ext}"))
    meta["media_type"] = _MIME_BY_EXT[ext]
    return meta


def _save_meta(file_id: str, meta: dict) -> None:
    """Legacy sidecar writer retained only for old 12-character URL compatibility."""
    fid = _check_id(file_id)
    if not _is_legacy_id(fid):
        raise FileError("新制品必须使用数据库元数据")
    _meta_path(file_id).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def artifact_info(file_id: str) -> dict:
    """Return structured, non-path metadata for a new Artifact."""
    fid = _check_id(file_id)
    db_meta = _find_artifact_meta(fid, require_ready=False)
    if db_meta is not None:
        return {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in db_meta.items()
            if key in {
                "file_id", "filename", "media_type", "size_bytes", "sha256",
                "status", "source_ids", "created_at", "expires_at", "kind",
                "sensitivity",
            }
        }
    if _is_legacy_id(fid):
        meta = _load_meta(fid)
        path = _data_path(fid, meta["ext"])
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        return {
            "file_id": fid,
            "filename": meta["filename"],
            "media_type": meta["media_type"],
            "size_bytes": path.stat().st_size,
            "sha256": content_hash,
            "status": "ready",
            "source_ids": [],
            "created_at": meta.get("created_at"),
            "expires_at": None,
        }
    raise FileError("文件不存在或已清理")


def _artifact_ref(info: dict) -> dict:
    """Stable transport shape for tools/SSE/message persistence consumers."""
    artifact_id = info["file_id"]
    return {
        "id": artifact_id,
        "filename": info["filename"],
        "mime_type": info["media_type"],
        "size_bytes": info["size_bytes"],
        "sha256": info["sha256"],
        "status": info["status"],
        "sensitivity": info["sensitivity"],
        "download_url": f"/api/agent/files/{artifact_id}",
    }


def _validate_staged_file(path: Path, ext: str) -> None:
    if ext == "xlsx":
        with path.open("rb") as handle:
            workbook = load_workbook(handle, read_only=True, data_only=True)
            workbook.close()
        return
    if ext == "docx":
        if not zipfile.is_zipfile(path):
            raise FileError("无法解析 docx（文件容器损坏）")
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise FileError("无法解析 docx（缺少 Word 文档结构）")
        return
    if ext == "pdf":
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise FileError("无法解析 pdf（文件签名不匹配）")
        return
    if ext in _IMG_EXT:
        from PIL import Image

        expected = "JPEG" if ext in {"jpg", "jpeg"} else ext.upper()
        with Image.open(path) as image:
            image.verify()
            if image.format != expected:
                raise FileError("图片扩展名与实际格式不一致")
        return
    if ext in _TEXT_EXT:
        data = path.read_bytes()
        if b"\x00" in data:
            raise FileError("文本文件包含二进制内容")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FileError("文本文件必须使用 UTF-8 编码") from exc


def snapshot_access_scope(user_ctx: Any) -> dict:
    """Capture server-authenticated visibility; never accept this from model arguments."""
    from app import permissions

    stable_owner_sub(user_ctx)
    permission_source = (
        permissions.effective(user_ctx.role, None)
        if user_ctx.permissions is None
        else user_ctx.permissions
    )
    effective = permissions.runtime_safe(permission_source)
    data_permissions = {key: bool(effective.get(key, False)) for key in permissions.DATA_GROUPS}
    page_permissions = {key: bool(effective.get(key, False)) for key in permissions.PAGE_KEYS}
    visible_field_groups = sorted({
        group
        for key, groups in permissions.DATA_GROUPS.items()
        if data_permissions[key]
        for group in groups
    })
    required = sorted(
        key for key, enabled in effective.items()
        if enabled and (key.startswith("data_") or key.startswith("page_"))
    )
    own_customers = bool(effective.get("own_customers_only"))
    row_subject = (
        _canonical_salesperson_subject(user_ctx.salesperson_name)
        if own_customers
        else None
    )
    if own_customers and not row_subject:
        raise FileError("本人客户范围制品缺少已绑定的销售主体")
    return {
        "version": 1,
        "policy": "current_scope_dominates",
        "required_permissions": required,
        "data_permissions": data_permissions,
        "page_permissions": page_permissions,
        "visible_field_groups": visible_field_groups,
        "row_scope": "own_customers" if own_customers else "all",
        "row_subject": row_subject,
    }


def _default_access_scope(kind: str) -> dict:
    if kind == "upload":
        return {"version": 1, "policy": "owner_only", "required_permissions": []}
    # Generated output without a server-authenticated snapshot is explicitly unclassified.
    return {
        "version": 1,
        "policy": "unclassified_deny",
        "required_permissions": [],
    }


def _derive_sensitivity(kind: str, access_scope: dict) -> str:
    """Derive classification server-side; callers cannot lower it through tool arguments."""
    if kind == "upload" or access_scope.get("policy") != "current_scope_dominates":
        return "critical"
    from app import permissions

    rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    required = access_scope.get("required_permissions")
    if not isinstance(required, list):
        return "critical"
    levels = [
        permissions.PERMISSION_META.get(key, {}).get("sensitivity")
        for key in required
    ]
    known = [level for level in levels if level in rank]
    return max(known, key=rank.__getitem__) if known else "high"


def stable_owner_sub(user_ctx: Any) -> str:
    """Return a non-spoofable owner subject or fail closed for shared/guest identities."""
    subject = str(getattr(user_ctx, "user_id", None) or "").strip()
    if (
        not getattr(user_ctx, "is_authenticated", False)
        or getattr(user_ctx, "authn", None) != "sys_user"
        or not getattr(user_ctx, "has_stable_subject", False)
        or not subject
    ):
        raise FileError("创建或访问制品需要实名系统账号")
    return subject


def verified_artifact_owner(user_ctx: Any) -> VerifiedArtifactOwner:
    """Create the only accepted artifact-write identity from authenticated context."""
    return VerifiedArtifactOwner(
        stable_owner_sub(user_ctx),
        deepcopy(user_ctx),
        _VERIFIED_OWNER_PROOF,
    )


def _verified_owner_sub(owner: VerifiedArtifactOwner) -> str:
    if not isinstance(owner, VerifiedArtifactOwner):
        raise FileError("创建制品需要已验证身份")
    if stable_owner_sub(owner._context) != owner.sub:
        raise FileError("创建制品需要已验证身份")
    return owner.sub


def _generated_scope(owner: VerifiedArtifactOwner) -> dict:
    _verified_owner_sub(owner)
    return snapshot_access_scope(owner._context)


def _canonical_source_id(source_id: str, owner: VerifiedArtifactOwner) -> str:
    checked = _check_id(source_id)
    if not access_allowed(checked, owner._context):
        raise FileError("无权引用来源制品")
    meta = _load_meta(checked)
    return meta.get("file_id", checked)


def _mark_artifact_ready(artifact_id: str) -> None:
    with SessionLocal.begin() as db:
        row = db.get(AgentArtifact, artifact_id)
        if row is None or row.status != "validating":
            raise FileError("文件发布状态冲突")
        row.status = "ready"


def _mark_artifact_validating(artifact_id: str) -> None:
    with SessionLocal.begin() as db:
        row = db.get(AgentArtifact, artifact_id)
        if row is None or row.status != "prepared":
            raise FileError("文件发布状态冲突")
        row.status = "validating"


def _mark_artifact_failed(artifact_id: str) -> None:
    with SessionLocal.begin() as db:
        row = db.get(AgentArtifact, artifact_id)
        if row is not None and row.status != "ready":
            row.status = "failed"


def _publish_artifact(
    content: bytes,
    filename: str,
    *,
    kind: str,
    owner: VerifiedArtifactOwner,
    source_ids: list[str] | None = None,
    extra_meta: dict | None = None,
) -> dict:
    require_artifact_v2_enabled()
    safe_name = _safe_filename(filename)
    ext = _ext_of(safe_name)
    if ext not in _ALLOWED_EXT:
        raise FileError("文件类型不受支持")
    artifact_id = str(uuid.uuid4())
    storage_key = _storage_key(artifact_id, ext)
    created_at = datetime.now(timezone.utc)
    expires_at = created_at + timedelta(days=get_settings().agent_artifact_retention_days)
    expected_hash = hashlib.sha256(content).hexdigest()
    owner_sub = _verified_owner_sub(owner)
    sources = [_canonical_source_id(source_id, owner) for source_id in (source_ids or [])]
    if kind == "upload":
        resolved_scope = _default_access_scope(kind)
    elif kind == "generated":
        resolved_scope = _generated_scope(owner)
    else:
        raise FileError("文件类型不受支持")
    with SessionLocal.begin() as db:
        db.add(AgentArtifact(
            id=artifact_id,
            owner_sub=owner_sub,
            filename=safe_name,
            media_type=_MIME_BY_EXT[ext],
            size_bytes=len(content),
            sha256=expected_hash,
            status="prepared",
            storage_key=storage_key,
            kind=kind,
            sensitivity=_derive_sensitivity(kind, resolved_scope),
            source_ids=sources,
            access_scope=resolved_scope,
            extra_meta=dict(extra_meta or {}),
            created_at=created_at,
            expires_at=expires_at,
        ))

    store = get_artifact_store()
    published = False
    try:
        _mark_artifact_validating(artifact_id)
        stored = store.publish_bytes(
            storage_key,
            content,
            validator=lambda path: _validate_staged_file(path, ext),
        )
        published = True
        if stored.size_bytes != len(content) or stored.sha256 != expected_hash:
            raise FileError("文件发布完整性校验失败")
        _mark_artifact_ready(artifact_id)
    except Exception as exc:  # noqa: BLE001 - internal detail is deliberately hidden
        if published:
            try:
                store.remove(storage_key)
            except Exception:  # noqa: BLE001 - best-effort orphan cleanup
                pass
        try:
            _mark_artifact_failed(artifact_id)
        except Exception:  # noqa: BLE001 - preserve the stable public error on DB outage
            pass
        raise FileError("文件发布失败，请稍后重试") from exc
    return artifact_info(artifact_id)


def access_allowed(file_id: str, user_ctx: Any) -> bool:
    """Re-authorize owner and current visibility against the creation snapshot."""
    from app import config, permissions

    try:
        subject = stable_owner_sub(user_ctx)
    except FileError:
        return False
    fid = _check_id(file_id)
    meta = _find_artifact_meta(fid, require_ready=False)
    if meta is None and _is_legacy_id(fid):
        meta = _load_meta(fid)
        owner_ok = meta.get("operated_by") == subject
        if not owner_ok:
            return False
        # Old uploads are classified as immutable user inputs. Old generated files lack a
        # trustworthy visibility snapshot and therefore fail closed for every role.
        if meta.get("kind") == "upload":
            return True
        return False
    if meta is None:
        raise FileError("文件不存在或已清理")
    owner_ok = meta.get("operated_by") == subject
    if not owner_ok:
        return False
    scope = meta.get("access_scope") or {}
    policy = scope.get("policy")
    if meta.get("kind") == "upload":
        return policy == "owner_only"
    if policy == "unclassified_deny":
        return False
    if policy != "current_scope_dominates" or scope.get("version") != 1:
        return False
    if not config.ENABLE_RBAC and user_ctx.role == config.PHASE1_BYPASS_ROLE:
        return True
    permission_source = (
        permissions.effective(user_ctx.role, None)
        if user_ctx.permissions is None
        else user_ctx.permissions
    )
    current = permissions.runtime_safe(permission_source)
    required = scope.get("required_permissions")
    if not isinstance(required, list) or not all(isinstance(key, str) for key in required):
        return False
    allowed_keys = set(permissions.DATA_GROUPS) | set(permissions.PAGE_KEYS)
    if any(key not in allowed_keys or not current.get(key, False) for key in required):
        return False
    data_snapshot = scope.get("data_permissions")
    page_snapshot = scope.get("page_permissions")
    visible_snapshot = scope.get("visible_field_groups")
    if not isinstance(data_snapshot, dict) or set(data_snapshot) != set(permissions.DATA_GROUPS):
        return False
    if not isinstance(page_snapshot, dict) or set(page_snapshot) != set(permissions.PAGE_KEYS):
        return False
    if not isinstance(visible_snapshot, list) or not all(isinstance(group, str) for group in visible_snapshot):
        return False
    if any(bool(granted) and not current.get(key, False) for key, granted in data_snapshot.items()):
        return False
    if any(bool(granted) and not current.get(key, False) for key, granted in page_snapshot.items()):
        return False
    current_visible_groups = {
        group
        for key, groups in permissions.DATA_GROUPS.items()
        if current.get(key, False)
        for group in groups
    }
    if not set(visible_snapshot).issubset(current_visible_groups):
        return False
    snapshot_row_scope = scope.get("row_scope")
    if snapshot_row_scope not in {"all", "own_customers"}:
        return False
    current_is_own = bool(current.get("own_customers_only", False))
    # An all-customers result cannot be reopened after the account becomes own-only.
    if snapshot_row_scope == "all" and current_is_own:
        return False
    if snapshot_row_scope == "own_customers" and current_is_own:
        snapshot_subject = scope.get("row_subject")
        current_subject = _canonical_salesperson_subject(user_ctx.salesperson_name)
        if (
            not isinstance(snapshot_subject, str)
            or not snapshot_subject
            or not current_subject
            or snapshot_subject != current_subject
        ):
            return False
    return True


def _cell_str(v) -> str:
    if v is None:
        return ""
    s = v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v)
    return s[:_CELL_TRUNC]


def _safe_spreadsheet_value(value):
    """Neutralize formula-like untrusted text while leaving real numbers unchanged."""
    if isinstance(value, str) and value.lstrip().startswith(_FORMULA_PREFIXES):
        return f"'{value}"
    return value


def _canonical_salesperson_subject(value: Any) -> str | None:
    """Canonical identity bound to own-customer row scope (not a display label)."""
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", str(value))
    normalized = re.sub(r"\s+", " ", normalized).strip().casefold()
    return normalized or None


def save_upload(content: bytes, filename: str, owner: VerifiedArtifactOwner) -> dict:
    """保存上传文件（多格式），返回 file_id + 类型/概览（供注入对话上下文）。"""
    require_artifact_v2_enabled()
    _verified_owner_sub(owner)
    safe_name = _safe_filename(filename, "上传文件")
    ext = _ext_of(safe_name)
    if ext == "xls":
        raise FileError("不支持旧版 .xls，请用 Excel 另存为 .xlsx")
    if ext not in _ALLOWED_EXT:
        raise FileError(f"不支持的文件类型 .{ext}（支持：Excel/Word/PDF/txt/图片）")
    if len(content) > _MAX_UPLOAD_MB * 1024 * 1024:
        raise FileError(f"文件超过 {_MAX_UPLOAD_MB}MB 上限")

    extra_meta: dict = {}
    if ext == "xlsx":
        # xlsx 校验可解析并带回 sheet 概览
        try:
            wb = load_workbook(BytesIO(content), read_only=True, data_only=True)
            extra_meta["sheets"] = [{"name": ws.title, "n_rows": ws.max_row or 0,
                                     "n_cols": ws.max_column or 0} for ws in wb.worksheets]
            wb.close()
        except Exception as exc:  # noqa: BLE001
            raise FileError(f"无法解析 xlsx（可能损坏，请用 Excel 重新另存）: {type(exc).__name__}") from exc
    else:
        # Validate signatures/encoding before creating a metadata row.
        check_dir = _dir() / ".tmp"
        check_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="upload-check-", suffix=f".{ext}", dir=check_dir)
        check_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            _validate_staged_file(check_path, ext)
        finally:
            check_path.unlink(missing_ok=True)

    artifact = _publish_artifact(
        content,
        safe_name,
        kind="upload",
        owner=owner,
        extra_meta=extra_meta,
    )
    out = {"file_id": artifact["file_id"], "filename": safe_name, "ext": ext,
           "file_kind": ("表格" if ext == "xlsx" else "Word" if ext == "docx"
                         else "PDF" if ext == "pdf" else "图片" if ext in _IMG_EXT else "文本"),
           "artifact": _artifact_ref(artifact)}
    if "sheets" in extra_meta:
        out["sheets"] = extra_meta["sheets"]
    return out


# ============================================================
# 读取（xlsx 结构化 + 通用 read_document）
# ============================================================

def _require_xlsx(fid: str, meta: dict):
    if meta.get("ext") != "xlsx":
        raise FileError(f"该文件是 .{meta.get('ext')}，结构化读取仅限 Excel；请用 read_document 读取内容")


def inspect_file(file_id: str) -> dict:
    """看 Excel 结构：sheet 列表 + 每 sheet 前几行原样预览。"""
    fid = _check_id(file_id)
    meta = _load_meta(fid)
    _require_xlsx(fid, meta)
    wb = load_workbook(_data_path(fid, "xlsx"), read_only=True, data_only=True)
    sheets = []
    for ws in wb.worksheets[:5]:
        preview = [[_cell_str(c.value) for c in row]
                   for row in ws.iter_rows(min_row=1, max_row=_PREVIEW_ROWS,
                                           max_col=min(ws.max_column or 1, _PREVIEW_COLS))]
        sheets.append({"name": ws.title, "n_rows": ws.max_row or 0,
                       "n_cols": ws.max_column or 0, "preview_rows_1_to_n": preview})
    wb.close()
    return {"file_id": fid, "filename": meta.get("filename"), "sheets": sheets,
            "note": "preview 为前几行原样数据(1-based 行号)；更多行用 read_file_rows"}


def read_rows(file_id: str, sheet: str | None, start_row: int, max_rows: int) -> dict:
    """分页读取 Excel 行（1-based）。"""
    fid = _check_id(file_id)
    meta = _load_meta(fid)
    _require_xlsx(fid, meta)
    wb = load_workbook(_data_path(fid, "xlsx"), read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet else wb.worksheets[0]
    except KeyError:
        names = [w.title for w in wb.worksheets]
        wb.close()
        raise FileError(f"sheet 不存在: {sheet!r}，可选: {names}")
    start = max(int(start_row or 1), 1)
    n = min(int(max_rows or 50), _MAX_READ_ROWS)
    rows = [[_cell_str(c.value) for c in row]
            for row in ws.iter_rows(min_row=start, max_row=start + n - 1,
                                    max_col=min(ws.max_column or 1, 30))]
    total = ws.max_row or 0
    wb.close()
    return {"file_id": fid, "sheet": ws.title, "start_row": start,
            "rows": rows, "total_rows": total}


def preview(file_id: str, max_rows: int = 200) -> dict:
    """文件预览（前端在线预览用）：xlsx 返回各 sheet 行数据（截断 max_rows×30列）；
    图片返回 kind=image（前端走下载端点取图）；其余 kind=other（仅可下载）。
    归属校验由 API 层做（与 download 同一把关），本函数只读内容。"""
    fid = _check_id(file_id)
    meta = _load_meta(fid)
    ext = meta.get("ext", "")
    filename = meta.get("filename", f"{fid}.{ext}")
    if ext != "xlsx":
        kind = "image" if ext in _IMG_EXT else "other"
        return {"file_id": fid, "filename": filename, "kind": kind, "ext": ext}
    # 坏/半损 xlsx 可能通过上传校验(只读维度)却在逐格迭代时抛 ParseError/BadZipFile(非 FileError)，
    # 不裹会让预览端点裸冒 500 → 统一转 FileError，端点据此返干净 404（与 save_upload 一致）
    try:
        wb = load_workbook(_data_path(fid, "xlsx"), read_only=True, data_only=True)
        sheets = []
        for ws in wb.worksheets[:10]:
            total = ws.max_row or 0
            rows = [[_cell_str(c.value) for c in row]
                    for row in ws.iter_rows(min_row=1, max_row=min(total, max_rows),
                                            max_col=min(ws.max_column or 1, 30))]
            sheets.append({"name": ws.title, "rows": rows,
                           "total_rows": total, "truncated": total > max_rows})
        wb.close()
    except FileError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FileError(f"无法解析 xlsx（可能损坏，请重新另存）: {type(exc).__name__}") from exc
    return {"file_id": fid, "filename": filename, "kind": "table", "ext": ext, "sheets": sheets}


def _read_docx(path: Path) -> str:
    from docx import Document
    doc = Document(str(path))
    parts: list[str] = [p.text for p in doc.paragraphs if p.text.strip()]
    for ti, table in enumerate(doc.tables):
        parts.append(f"[表格{ti + 1}]")
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _read_pdf(path: Path) -> tuple[str, bool]:
    """返回 (文本, 是否疑似扫描件)。文字层为空/极少 → 扫描件，转视觉。"""
    import pdfplumber
    parts: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for pi, page in enumerate(pdf.pages[:30]):
            txt = page.extract_text() or ""
            parts.append(f"[第{pi + 1}页]\n{txt}" if txt.strip() else f"[第{pi + 1}页](无文字层)")
            for table in page.extract_tables() or []:
                for row in table:
                    cells = [(c or "").strip() for c in row]
                    if any(cells):
                        parts.append(" | ".join(cells))
    text = "\n".join(parts)
    scanned = len(re.sub(r"\s", "", text.replace("无文字层", ""))) < 20
    return text, scanned


def _read_image_or_scanned(path: Path, hint: str) -> str:
    """图片/扫描件 → Qwen-VL 识别（无 key 优雅降级）。"""
    from app.agent import provider
    try:
        return provider.vision_extract([path], hint)
    except provider.VisionNotConfigured:
        return ("【未配置视觉模型】该文件是图片/扫描件，需配置 VISION_API_KEY（通义 Qwen-VL）"
                "后才能识别。文字版 Word/Excel/PDF/txt 不受影响。")


def read_document(file_id: str) -> dict:
    """通用读取：把任意支持格式抽成文本喂给模型（拆件/解析由模型完成）。"""
    fid = _check_id(file_id)
    meta = _load_meta(fid)
    ext = meta.get("ext", "")
    vision_used = False
    if ext in _TEXT_EXT:
        text = _data_path(fid, ext).read_bytes().decode("utf-8", errors="replace")
    elif ext == "xlsx":
        wb = load_workbook(_data_path(fid, "xlsx"), read_only=True, data_only=True)
        chunks = []
        for ws in wb.worksheets:
            chunks.append(f"[工作表 {ws.title}]")
            for row in ws.iter_rows(max_row=min(ws.max_row or 0, 300), max_col=min(ws.max_column or 1, 30)):
                cells = [_cell_str(c.value) for c in row]
                if any(cells):
                    chunks.append(" | ".join(cells))
        wb.close()
        text = "\n".join(chunks)
    elif ext == "docx":
        text = _read_docx(_data_path(fid, "docx"))
    elif ext == "pdf":
        text, scanned = _read_pdf(_data_path(fid, "pdf"))
        if scanned:
            vision_used = True
            text = _read_image_or_scanned(_data_path(fid, "pdf"),
                                          "这是一份扫描件，请逐字识别其中的全部文本、表格、型号与参数。")
    elif ext in _IMG_EXT:
        vision_used = True
        text = _read_image_or_scanned(
            _data_path(fid, ext),
            "请识别图片中的全部文字、表格、设备型号、品牌与参数配置，按原结构输出。")
    else:
        raise FileError(f"不支持读取 .{ext}")

    truncated = len(text) > _DOC_CHAR_CAP
    return {"file_id": fid, "filename": meta.get("filename"), "ext": ext,
            "vision_used": vision_used, "truncated": truncated,
            "content": text[:_DOC_CHAR_CAP]}


# ============================================================
# 写（模板回填 write_excel / 美化报表 write_report）
# ============================================================

def _col_index(col) -> int:
    """列定位：支持 "A"/"G" 字母或 1-based 数字。"""
    if isinstance(col, int) or (isinstance(col, str) and col.isdigit()):
        idx = int(col)
        if idx < 1 or idx > 16384:
            raise FileError(f"列号超界: {col}")
        return idx
    try:
        return column_index_from_string(str(col).strip().upper())
    except Exception as exc:  # noqa: BLE001
        raise FileError(f"无法识别列: {col!r}（用字母如 'G' 或 1-based 数字）") from exc


def write_excel(base_file_id: str | None, sheet: str | None,
                cells: list[dict], output_name: str | None,
                owner: VerifiedArtifactOwner) -> dict:
    """按模型指令写单元格，产出新文件（不动原件）。用于**回填客户模板**（保留原格式）。"""
    require_artifact_v2_enabled()
    _verified_owner_sub(owner)
    if not cells:
        raise FileError("cells 不能为空")
    if len(cells) > _MAX_WRITE_CELLS:
        raise FileError(f"单次最多写 {_MAX_WRITE_CELLS} 个单元格")

    base_name = ""
    if base_file_id:
        base = _canonical_source_id(base_file_id, owner)
        bmeta = _load_meta(base)
        _require_xlsx(base, bmeta)
        wb = load_workbook(_data_path(base, "xlsx"))  # 保留原格式/公式
        base_name = bmeta.get("filename", "")
    else:
        wb = Workbook()

    ws = (wb[sheet] if sheet in wb.sheetnames else wb.create_sheet(sheet)) if sheet else wb.worksheets[0]

    written = 0
    for c in cells:
        try:
            row, col = int(c["row"]), _col_index(c["col"])
        except (KeyError, TypeError, ValueError) as exc:
            raise FileError(f"cells 项格式错: {c!r}（需 row/col/value）") from exc
        if row < 1 or row > 1_048_576:
            raise FileError(f"行号超界: {row}")
        ws.cell(row=row, column=col, value=_safe_spreadsheet_value(c.get("value")))
        written += 1

    name = output_name or (f"回填_{base_name}" if base_name else "结果.xlsx")
    if not name.lower().endswith(".xlsx"):
        name += ".xlsx"
    buffer = BytesIO()
    wb.save(buffer)
    wb.close()
    artifact = _publish_artifact(
        buffer.getvalue(),
        name,
        kind="generated",
        owner=owner,
        source_ids=[base_file_id] if base_file_id else [],
        extra_meta={"base_file_id": base_file_id},
    )
    ref = _artifact_ref(artifact)
    download_url = f"/api/agent/files/{artifact['file_id']}"
    return {"file_id": artifact["file_id"], "filename": artifact["filename"],
            "cells_written": written, "download_url": download_url, "artifact": ref}


# 美化报表样式常量
_HEADER_FILL = PatternFill("solid", fgColor="4F46E5")
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
_TITLE_FONT = Font(bold=True, size=14, color="111827")
_ZEBRA_FILL = PatternFill("solid", fgColor="F5F6FF")
_WARN_FILL = PatternFill("solid", fgColor="FFF3E0")    # 需确认=橙
_BAD_FILL = PatternFill("solid", fgColor="FDECEA")     # 未找到=红
_THIN = Side(style="thin", color="E5E7EB")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_WARN_KW = ("需确认", "待确认", "确认")
_BAD_KW = ("未找到", "无库存", "不存在", "未匹配")


def write_report(title: str | None, headers: list[str], rows: list[list],
                 output_name: str | None, owner: VerifiedArtifactOwner,
                 money_cols: list[int] | None = None) -> dict:
    """生成**美化报表**（BOM/报价单等）：表头配色、边框、自适应列宽、金额格式、
    冻结表头、斑马纹、状态行条件配色。headers=列名；rows=与列对齐的二维数组；
    money_cols=金额列的 0-based 下标（数字格式+右对齐）。"""
    require_artifact_v2_enabled()
    _verified_owner_sub(owner)
    if not headers:
        raise FileError("headers 不能为空")
    if len(rows) > 5000:
        raise FileError("报表最多 5000 行")
    money = set(money_cols or [])
    ncol = len(headers)

    wb = Workbook()
    ws = wb.worksheets[0]
    ws.title = "报表"
    r0 = 1
    if title:
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
        tc = ws.cell(row=1, column=1, value=_safe_spreadsheet_value(title))
        tc.font = _TITLE_FONT
        tc.alignment = Alignment(horizontal="left", vertical="center")
        ws.row_dimensions[1].height = 26
        r0 = 2

    # 表头
    for j, h in enumerate(headers, start=1):
        c = ws.cell(row=r0, column=j, value=_safe_spreadsheet_value(str(h)))
        c.fill, c.font, c.border = _HEADER_FILL, _HEADER_FONT, _BORDER
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[r0].height = 22

    widths = [len(str(h)) for h in headers]
    for i, row in enumerate(rows):
        rr = r0 + 1 + i
        rowtext = " ".join(str(v) for v in row if v is not None)
        hit_bad = any(k in rowtext for k in _BAD_KW)
        hit_warn = not hit_bad and any(k in rowtext for k in _WARN_KW)
        base_fill = _BAD_FILL if hit_bad else _WARN_FILL if hit_warn else (_ZEBRA_FILL if i % 2 else None)
        for j in range(ncol):
            val = row[j] if j < len(row) else None
            c = ws.cell(row=rr, column=j + 1, value=_safe_spreadsheet_value(val))
            c.border = _BORDER
            if base_fill:
                c.fill = base_fill
            if j in money and isinstance(val, (int, float)):
                c.number_format = '#,##0.00'
                c.alignment = Alignment(horizontal="right")
            else:
                c.alignment = Alignment(vertical="center", wrap_text=isinstance(val, str) and len(str(val)) > 20)
            widths[j] = max(widths[j], min(len(_cell_str(val)), 50))

    for j, w in enumerate(widths, start=1):
        # 中文偏宽：粗略 ×1.6 + 余量；上限防超宽
        ws.column_dimensions[get_column_letter(j)].width = min(max(w * 1.6 + 2, 8), 60)
    ws.freeze_panes = ws.cell(row=r0 + 1, column=1)

    name = output_name or (title or "报表")
    if not name.lower().endswith(".xlsx"):
        name += ".xlsx"
    buffer = BytesIO()
    wb.save(buffer)
    wb.close()
    artifact = _publish_artifact(
        buffer.getvalue(),
        name,
        kind="generated",
        owner=owner,
        extra_meta={"report": True},
    )
    ref = _artifact_ref(artifact)
    download_url = f"/api/agent/files/{artifact['file_id']}"
    return {"file_id": artifact["file_id"], "filename": artifact["filename"],
            "rows_written": len(rows), "download_url": download_url, "artifact": ref}


def owner_of(file_id: str) -> str | None:
    """文件创建者（发布时记录的 operated_by）；文件不存在抛 FileError。归属校验用。"""
    fid = _check_id(file_id)
    if _is_legacy_id(fid):
        return _load_meta(fid).get("operated_by")
    return _artifact_meta(fid, require_ready=False).get("operated_by")


def get_download_info(file_id: str) -> ArtifactDownload:
    """Resolve and integrity-check a ready Artifact without exposing its storage key."""
    fid = _check_id(file_id)
    meta = _load_meta(fid)
    if _is_legacy_id(fid):
        path = _data_path(fid, meta.get("ext", ""))
        content = path.read_bytes()
        return ArtifactDownload(
            path=path,
            filename=_safe_filename(meta.get("filename", f"{fid}.{meta['ext']}")),
            media_type=_MIME_BY_EXT[meta["ext"]],
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
    stored = _verify_artifact(meta)
    return ArtifactDownload(
        path=stored.path,
        filename=meta["filename"],
        media_type=meta["media_type"],
        size_bytes=meta["size_bytes"],
        sha256=meta["sha256"],
    )


def get_download(file_id: str) -> tuple[Path, str]:
    """下载定位：返回 (路径, 文件名)。归属校验在 API 层（见 api/agent.download）。"""
    download = get_download_info(file_id)
    return download.path, download.filename
