"""Controlled acceptance-report workflow for stable maintenance projects."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import unicodedata
from uuid import uuid4
import xml.etree.ElementTree as ElementTree
import zipfile

from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.maintenance_manager import (
    BusinessFile,
    BusinessFileDownloadAudit,
    BusinessFileLink,
    MaintenanceAcceptanceDeliverable,
    MaintenanceAcceptanceOperation,
)
from app.models.maintenance_project import MaintenanceProject
from app.security import FULL_SCOPE_ROLES, UserContext
from app.services import maintenance_project_assignments as assignments


MAX_ACCEPTANCE_FILE_BYTES = 20 * 1024 * 1024
_MAX_ZIP_MEMBERS = 1024
_MAX_ZIP_EXPANDED_BYTES = 100 * 1024 * 1024
_MAX_ZIP_RATIO = 100
_ALLOWED_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
_PDF_ACTIVE_TOKENS = (
    b"/javascript",
    b"/js",
    b"/launch",
    b"/embeddedfile",
    b"/openaction",
    b"/richmedia",
)


class MaintenanceAcceptanceError(Exception):
    """Base user-safe acceptance workflow error."""

    status_code = 400


class MaintenanceAcceptanceNotFound(MaintenanceAcceptanceError):
    status_code = 404


class MaintenanceAcceptanceConflict(MaintenanceAcceptanceError):
    status_code = 409


class MaintenanceAcceptanceTooLarge(MaintenanceAcceptanceError):
    status_code = 413


class MaintenanceAcceptanceUnsupported(MaintenanceAcceptanceError):
    status_code = 415


def _now() -> datetime:
    return datetime.now(UTC)


def _required_text(value: str | None, label: str, limit: int) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise MaintenanceAcceptanceError(f"{label}不能为空")
    if len(cleaned) > limit:
        raise MaintenanceAcceptanceError(f"{label}不能超过 {limit} 个字符")
    return cleaned


def _safe_filename(filename: str | None) -> tuple[str, str, str]:
    normalized = unicodedata.normalize("NFC", str(filename or "")).strip()
    if (
        not normalized
        or len(normalized) > 256
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or any(unicodedata.category(char).startswith("C") for char in normalized)
    ):
        raise MaintenanceAcceptanceUnsupported("附件文件名不安全")
    extension = Path(normalized).suffix.lower()
    expected_mime = _ALLOWED_TYPES.get(extension)
    if expected_mime is None:
        raise MaintenanceAcceptanceUnsupported(
            "仅支持 PDF、DOCX、XLSX、PNG、JPG/JPEG 验收附件"
        )
    return normalized, extension, expected_mime


def _assert_safe_zip(data: bytes, *, extension: str) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            infos = package.infolist()
            if not infos or len(infos) > _MAX_ZIP_MEMBERS:
                raise MaintenanceAcceptanceUnsupported("Office 附件结构异常")
            expanded = 0
            names: set[str] = set()
            for info in infos:
                name = info.filename
                path = PurePosixPath(name)
                if (
                    not name
                    or name.startswith(("/", "\\"))
                    or "\\" in name
                    or ".." in path.parts
                    or info.flag_bits & 0x1
                ):
                    raise MaintenanceAcceptanceUnsupported("Office 附件包含不安全路径或加密内容")
                expanded += info.file_size
                if expanded > _MAX_ZIP_EXPANDED_BYTES:
                    raise MaintenanceAcceptanceUnsupported("Office 附件解压后体积异常")
                if info.file_size and info.compress_size == 0:
                    raise MaintenanceAcceptanceUnsupported("Office 附件压缩结构异常")
                if info.compress_size and info.file_size / info.compress_size > _MAX_ZIP_RATIO:
                    raise MaintenanceAcceptanceUnsupported("Office 附件压缩比异常")
                lower_name = name.lower()
                if lower_name in names:
                    raise MaintenanceAcceptanceUnsupported("Office 附件包含重复文件成员")
                names.add(lower_name)

            required = "word/document.xml" if extension == ".docx" else "xl/workbook.xml"
            if "[content_types].xml" not in names or required not in names:
                raise MaintenanceAcceptanceUnsupported("Office 附件类型与扩展名不匹配")
            forbidden_parts = (
                "vbaproject.bin",
                "/embeddings/",
                "/externallinks/",
                "connections.xml",
                "customui/",
            )
            if any(any(marker in name for marker in forbidden_parts) for name in names):
                raise MaintenanceAcceptanceUnsupported("Office 附件含宏、嵌入对象或外部数据连接")

            for info in infos:
                lower = info.filename.lower()
                if not lower.endswith((".xml", ".rels")):
                    continue
                payload = package.read(info)
                folded = payload.lower()
                if b"<!doctype" in folded or b"<!entity" in folded:
                    raise MaintenanceAcceptanceUnsupported("Office 附件包含不安全 XML 声明")
                try:
                    root = ElementTree.fromstring(payload)
                except ElementTree.ParseError as exc:
                    raise MaintenanceAcceptanceUnsupported(
                        "Office 附件包含损坏的 XML"
                    ) from exc
                if lower.endswith(".rels") and any(
                    attribute.rsplit("}", 1)[-1].lower() == "targetmode"
                    and str(value).strip().lower() == "external"
                    for element in root.iter()
                    for attribute, value in element.attrib.items()
                ):
                    raise MaintenanceAcceptanceUnsupported("Office 附件包含外部链接")
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:
        raise MaintenanceAcceptanceUnsupported("Office 附件内容损坏或格式不正确") from exc


def validate_attachment(
    *,
    filename: str | None,
    mime_type: str | None,
    content: bytes,
) -> tuple[str, str, str]:
    safe_name, extension, expected_mime = _safe_filename(filename)
    actual_mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    if actual_mime != expected_mime:
        raise MaintenanceAcceptanceUnsupported("附件扩展名与 MIME 类型不匹配")
    if not content:
        raise MaintenanceAcceptanceUnsupported("附件内容为空")
    if len(content) > MAX_ACCEPTANCE_FILE_BYTES:
        raise MaintenanceAcceptanceTooLarge("单个验收附件不得超过 20MB")

    if extension == ".pdf":
        folded = content.lower()
        if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-2048:]:
            raise MaintenanceAcceptanceUnsupported("PDF 内容损坏或格式不正确")
        if any(token in folded for token in _PDF_ACTIVE_TOKENS):
            raise MaintenanceAcceptanceUnsupported("PDF 含脚本、启动动作或嵌入文件")
    elif extension in {".docx", ".xlsx"}:
        _assert_safe_zip(content, extension=extension)
    else:
        try:
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                if getattr(image, "n_frames", 1) != 1:
                    raise MaintenanceAcceptanceUnsupported("不接受多帧图片附件")
                if image.width <= 0 or image.height <= 0 or image.width * image.height > 40_000_000:
                    raise MaintenanceAcceptanceUnsupported("图片像素尺寸异常")
                expected_format = "PNG" if extension == ".png" else "JPEG"
                if image.format != expected_format:
                    raise MaintenanceAcceptanceUnsupported("图片内容与扩展名不匹配")
        except (
            Image.DecompressionBombError,
            UnidentifiedImageError,
            OSError,
            ValueError,
        ) as exc:
            raise MaintenanceAcceptanceUnsupported("图片内容损坏或格式不正确") from exc
    return safe_name, extension, expected_mime


def _root() -> Path:
    root = (Path(get_settings().raw_file_dir).resolve() / "maintenance_acceptance")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _resolved_object_path(object_key: str) -> Path:
    key = str(object_key or "").strip()
    if not key or "\\" in key:
        raise MaintenanceAcceptanceConflict("附件存储路径无效")
    relative = PurePosixPath(key)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise MaintenanceAcceptanceConflict("附件存储路径无效")
    expected_prefix = ("maintenance_acceptance",)
    if relative.parts[:1] != expected_prefix:
        raise MaintenanceAcceptanceConflict("附件不属于受控验收存储区")
    raw_root = Path(get_settings().raw_file_dir).resolve()
    target = (raw_root / Path(*relative.parts)).resolve()
    controlled = _root()
    try:
        target.relative_to(controlled)
    except ValueError as exc:
        raise MaintenanceAcceptanceConflict("附件存储路径越界") from exc
    return target


def _operation_key(
    *, operator: str, operation_type: str, deliverable_id: str, client_key: str
) -> str:
    key = _required_text(client_key, "Idempotency-Key", 128)
    return hashlib.sha256(
        f"{operator}\x1f{operation_type}\x1f{deliverable_id}\x1f{key}".encode("utf-8")
    ).hexdigest()


def _payload_hash(payload: dict) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _existing_operation(
    db: Session,
    *,
    operation_key: str,
    payload_hash: str,
) -> dict | None:
    row = db.scalar(
        select(MaintenanceAcceptanceOperation).where(
            MaintenanceAcceptanceOperation.operation_key == operation_key
        )
    )
    if row is None:
        return None
    if row.payload_hash != payload_hash:
        raise MaintenanceAcceptanceConflict("同一 Idempotency-Key 不能用于不同请求")
    return {**row.result_json, "replayed": True}


def _locked_deliverable(
    db: Session,
    *,
    project_id: str,
) -> MaintenanceAcceptanceDeliverable:
    row = db.scalar(
        select(MaintenanceAcceptanceDeliverable)
        .where(
            MaintenanceAcceptanceDeliverable.project_id == project_id,
            MaintenanceAcceptanceDeliverable.deliverable_type == "acceptance_report",
        )
        .with_for_update()
    )
    if row is None:
        # 2026-08-25 客户拍板：验收"只是个上传的地方"，没有截止日概念——
        # 首次上传自动建默认交付行，不再依赖月度全量表先配置截止日
        # （此前"截止日未配置即关闭通道"导致业务根本无法上传）。
        row = MaintenanceAcceptanceDeliverable(
            deliverable_id=str(uuid4()),
            project_id=project_id,
            deliverable_type="acceptance_report",
            due_date=None,
            submission_status="not_submitted",
            approval_status="not_reviewed",
            configuration_state="configured",
            version=1,
        )
        db.add(row)
        db.flush()
    # 截止日只作展示（有则显示、无则"—"），不再是上传/提交的前置条件。
    return row


def _active_attachments(
    db: Session, deliverable_id: str
) -> list[tuple[BusinessFileLink, BusinessFile]]:
    return list(
        db.execute(
            select(BusinessFileLink, BusinessFile)
            .join(BusinessFile, BusinessFile.file_id == BusinessFileLink.file_id)
            .where(
                BusinessFileLink.entity_type == "maintenance_acceptance_deliverable",
                BusinessFileLink.entity_id == deliverable_id,
                BusinessFileLink.archived_at.is_(None),
                BusinessFile.security_state == "active",
            )
            .order_by(BusinessFile.uploaded_at, BusinessFile.file_id)
        )
    )


def deliverable_dict(
    db: Session,
    row: MaintenanceAcceptanceDeliverable,
) -> dict:
    attachments = _active_attachments(db, row.deliverable_id)
    return {
        "deliverable_id": row.deliverable_id,
        "project_id": row.project_id,
        "deliverable_type": row.deliverable_type,
        "due_date": row.due_date.isoformat() if row.due_date else None,
        "submission_status": row.submission_status,
        "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
        "submitted_by": row.submitted_by,
        "approval_status": row.approval_status,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "approved_by": row.approved_by,
        "rejection_reason": row.rejection_reason,
        "configuration_state": row.configuration_state,
        "version": row.version,
        "review_policy": "submit_takes_effect",
        "attachments": [
            {
                "file_id": file.file_id,
                "original_filename": file.original_filename,
                "mime_type": file.mime_type,
                "size_bytes": file.size_bytes,
                "sha256": file.sha256,
                "uploaded_by": file.uploaded_by,
                "uploaded_at": file.uploaded_at.isoformat(),
            }
            for _link, file in attachments
        ],
    }


def project_acceptance(db: Session, *, project_id: str) -> dict:
    project = db.get(MaintenanceProject, project_id)
    if project is None:
        raise MaintenanceAcceptanceNotFound("维保项目不存在")
    row = db.scalar(
        select(MaintenanceAcceptanceDeliverable).where(
            MaintenanceAcceptanceDeliverable.project_id == project_id,
            MaintenanceAcceptanceDeliverable.deliverable_type == "acceptance_report",
        )
    )
    if row is None:
        return {
            "deliverable_id": None,
            "project_id": project_id,
            "deliverable_type": "acceptance_report",
            "due_date": None,
            "submission_status": "not_submitted",
            "submitted_at": None,
            "submitted_by": None,
            "approval_status": "not_reviewed",
            "approved_at": None,
            "approved_by": None,
            "rejection_reason": None,
            "configuration_state": "configured",
            "version": 0,
            "review_policy": "submit_takes_effect",
            "attachments": [],
        }
    return deliverable_dict(db, row)


def search_acceptance(
    db: Session,
    *,
    user_ctx: UserContext,
    q: str,
    submission_status: str | None,
    approval_status: str | None,
    page: int,
    page_size: int,
) -> dict:
    filters = []
    if user_ctx.role not in FULL_SCOPE_ROLES:
        # 2026-08-25 行级口径对齐：与直达路由 can_access_project 共用同一
        # 谓词（负责人 ∪ 台账 salesperson）——此前只用负责人口径，sales
        # 按台账销售名能直达上传/查看，却在列表里搜不到。
        filters.append(assignments.accessible_project_condition(user_ctx))
    search = str(q or "").strip()
    if search:
        filters.append(
            or_(
                MaintenanceProject.project_code.icontains(search, autoescape=True),
                MaintenanceProject.display_name.icontains(search, autoescape=True),
            )
        )
    statement = (
        select(MaintenanceProject, MaintenanceAcceptanceDeliverable)
        .outerjoin(
            MaintenanceAcceptanceDeliverable,
            (MaintenanceAcceptanceDeliverable.project_id == MaintenanceProject.project_id)
            & (MaintenanceAcceptanceDeliverable.deliverable_type == "acceptance_report"),
        )
        .where(*filters)
    )
    if submission_status:
        if submission_status == "not_configured":
            statement = statement.where(MaintenanceAcceptanceDeliverable.deliverable_id.is_(None))
        else:
            statement = statement.where(
                MaintenanceAcceptanceDeliverable.submission_status == submission_status
            )
    if approval_status:
        statement = statement.where(
            MaintenanceAcceptanceDeliverable.approval_status == approval_status
        )
    count = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = db.execute(
        statement.order_by(
            MaintenanceAcceptanceDeliverable.due_date.asc().nulls_last(),
            MaintenanceProject.project_code,
            MaintenanceProject.project_id,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "rows": [
            {
                "project_id": project.project_id,
                "project_code": project.project_code,
                "display_name": project.display_name,
                "acceptance": (
                    deliverable_dict(db, deliverable)
                    if deliverable is not None
                    else project_acceptance(db, project_id=project.project_id)
                ),
            }
            for project, deliverable in rows
        ],
        "total": int(count),
        "page": page,
        "page_size": page_size,
    }


def upload_attachment(
    db: Session,
    *,
    project_id: str,
    operator: str,
    client_key: str,
    filename: str | None,
    mime_type: str | None,
    content: bytes,
) -> tuple[dict, Path | None]:
    """一个上传口（2026-08-25 客户口径）：传文件即落库——自动 SHA-256、
    自动建交付行、自动挂项目，不做版本号/乐观锁（此前的版本握手只制造了
    version 0/1 跳变类 bug）。幂等由两道互补机制保证：客户端幂等键
    （可选）+ 行锁内同 sha256 内容去重（双击/超时重试不产生重复附件）。"""
    safe_name, extension, safe_mime = validate_attachment(
        filename=filename,
        mime_type=mime_type,
        content=content,
    )
    digest = hashlib.sha256(content).hexdigest()
    deliverable = _locked_deliverable(db, project_id=project_id)
    operation_key = _operation_key(
        operator=operator,
        operation_type="attachment_upload",
        deliverable_id=deliverable.deliverable_id,
        client_key=client_key,
    )
    payload_hash = _payload_hash(
        {
            "filename": safe_name,
            "mime_type": safe_mime,
            "size": len(content),
            "sha256": digest,
        }
    )
    replay = _existing_operation(
        db, operation_key=operation_key, payload_hash=payload_hash
    )
    if replay is not None:
        return replay, None
    # 2026-08-25 内容去重（替代被删的版本握手）：行锁内查该交付行 active
    # 链接中是否已有同 sha256 附件——有则按幂等回放形态直接返回存档结果
    # （path=None 不落盘、不建行/链接），双击/超时重试不产生重复附件；
    # 无则正常落库。保持「只传文件本身」口径，无需客户端幂等键。
    for _dup_link, duplicate in _active_attachments(db, deliverable.deliverable_id):
        if duplicate.sha256 == digest:
            return {
                "replayed": True,
                "project_id": project_id,
                "deliverable_id": deliverable.deliverable_id,
                "file_id": duplicate.file_id,
                "version": deliverable.version,
                "original_filename": duplicate.original_filename,
                "mime_type": duplicate.mime_type,
                "size_bytes": duplicate.size_bytes,
                "sha256": duplicate.sha256,
            }, None
    # 2026-08-24 客户拍板：提交即生效，无需独立审批。审批锁定随之取消——
    # 生效后仍可补充附件（走下方提交/版本链），完整操作留审计。

    file_id = str(uuid4())
    object_key = f"maintenance_acceptance/{file_id[:2]}/{file_id}{extension}"
    path = _resolved_object_path(object_key)
    file_row = BusinessFile(
        file_id=file_id,
        storage_provider="local",
        object_key=object_key,
        original_filename=safe_name,
        mime_type=safe_mime,
        size_bytes=len(content),
        sha256=digest,
        security_state="active",
        uploaded_by=operator,
        version=1,
    )
    link = BusinessFileLink(
        link_id=str(uuid4()),
        file_id=file_id,
        entity_type="maintenance_acceptance_deliverable",
        entity_id=deliverable.deliverable_id,
        relation_type="evidence",
        acl_scope="project_members",
        created_by=operator,
    )
    deliverable.version += 1
    result = {
        "replayed": False,
        "project_id": project_id,
        "deliverable_id": deliverable.deliverable_id,
        "file_id": file_id,
        "version": deliverable.version,
        "original_filename": safe_name,
        "mime_type": safe_mime,
        "size_bytes": len(content),
        "sha256": digest,
    }
    operation = MaintenanceAcceptanceOperation(
        operation_id=str(uuid4()),
        operation_key=operation_key,
        payload_hash=payload_hash,
        operation_type="attachment_upload",
        deliverable_id=deliverable.deliverable_id,
        project_id=project_id,
        result_json=result,
        operated_by=operator,
    )
    db.add_all([file_row, link, operation])
    db.flush()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temp_name = tempfile.mkstemp(prefix=".upload-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return result, path


def submit_acceptance(
    db: Session,
    *,
    project_id: str,
    expected_version: int,
    operator: str,
    client_key: str,
) -> dict:
    deliverable = _locked_deliverable(db, project_id=project_id)
    operation_key = _operation_key(
        operator=operator,
        operation_type="submit",
        deliverable_id=deliverable.deliverable_id,
        client_key=client_key,
    )
    payload_hash = _payload_hash({"expected_version": expected_version})
    replay = _existing_operation(db, operation_key=operation_key, payload_hash=payload_hash)
    if replay is not None:
        return replay
    if deliverable.version != expected_version and not (
        # 同 upload：0 → 首次自动建行 1 的合法跳变。
        expected_version == 0 and deliverable.version == 1
        and deliverable.submission_status == "not_submitted"
    ):
        raise MaintenanceAcceptanceConflict("验收记录版本已变化，请刷新后重试")
    if not _active_attachments(db, deliverable.deliverable_id):
        raise MaintenanceAcceptanceConflict("至少上传一个有效附件后才能提交")
    # 2026-08-24 客户拍板：验收开放给销售/项目经理/维保负责人，提交即生效
    # （免独立审批）。已生效的报告可随时重新提交新版本或补充附件；
    # 版本乐观锁 + 幂等键 + 操作日志保证可追溯。历史遗留 not_reviewed/
    # rejected 状态同样允许直接重新提交覆盖。

    now = _now()
    deliverable.submission_status = "submitted"
    deliverable.submitted_at = now
    deliverable.submitted_by = operator
    deliverable.approval_status = "approved"
    deliverable.approved_at = now
    deliverable.approved_by = operator
    deliverable.rejection_reason = None
    deliverable.version += 1
    result = {
        "replayed": False,
        "project_id": project_id,
        "deliverable_id": deliverable.deliverable_id,
        "submission_status": "submitted",
        "approval_status": "approved",
        "version": deliverable.version,
    }
    db.add(
        MaintenanceAcceptanceOperation(
            operation_id=str(uuid4()),
            operation_key=operation_key,
            payload_hash=payload_hash,
            operation_type="submit",
            deliverable_id=deliverable.deliverable_id,
            project_id=project_id,
            result_json=result,
            operated_by=operator,
        )
    )
    db.flush()
    return result


def controlled_download(
    db: Session,
    *,
    file_id: str,
    operator: str,
) -> tuple[bytes, BusinessFile]:
    joined = db.execute(
        select(BusinessFile, BusinessFileLink, MaintenanceAcceptanceDeliverable)
        .join(BusinessFileLink, BusinessFileLink.file_id == BusinessFile.file_id)
        .join(
            MaintenanceAcceptanceDeliverable,
            MaintenanceAcceptanceDeliverable.deliverable_id == BusinessFileLink.entity_id,
        )
        .where(
            BusinessFile.file_id == file_id,
            BusinessFile.security_state == "active",
            BusinessFileLink.entity_type == "maintenance_acceptance_deliverable",
            BusinessFileLink.archived_at.is_(None),
        )
    ).first()
    if joined is None:
        raise MaintenanceAcceptanceNotFound("验收附件不存在或已归档")
    file_row, link, deliverable = joined
    if file_row.storage_provider != "local":
        raise MaintenanceAcceptanceConflict("当前存储载体不支持受控下载")
    path = _resolved_object_path(file_row.object_key)
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise MaintenanceAcceptanceConflict("验收附件存储不可用") from exc
    digest = hashlib.sha256(content).hexdigest()
    if len(content) != file_row.size_bytes or digest != file_row.sha256:
        raise MaintenanceAcceptanceConflict("验收附件完整性校验失败，已阻止下载")
    db.add(
        BusinessFileDownloadAudit(
            file_id=file_row.file_id,
            link_id=link.link_id,
            deliverable_id=deliverable.deliverable_id,
            project_id=deliverable.project_id,
            downloaded_by=operator,
            sha256_at_download=digest,
        )
    )
    db.flush()
    return content, file_row


def file_project_id(db: Session, *, file_id: str) -> str:
    project_id = db.scalar(
        select(MaintenanceAcceptanceDeliverable.project_id)
        .join(
            BusinessFileLink,
            BusinessFileLink.entity_id
            == MaintenanceAcceptanceDeliverable.deliverable_id,
        )
        .where(
            BusinessFileLink.file_id == file_id,
            BusinessFileLink.entity_type == "maintenance_acceptance_deliverable",
            BusinessFileLink.archived_at.is_(None),
        )
    )
    if project_id is None:
        raise MaintenanceAcceptanceNotFound("验收附件不存在或已归档")
    return str(project_id)


def archive_attachment(
    db: Session,
    *,
    project_id: str,
    file_id: str,
    operator: str,
) -> dict:
    """删除验收附件（2026-08-25 客户口径：能传也能删）。

    软删：归档 file ↔ 交付行 的链接（页面立即消失，CHECK 要求
    archived_at/by/reason 三件套），文件字节与审计保留可追溯；
    同一文件可重新上传（新链接）。删除写操作台账
    （operation_type='attachment_archive'）；已提交的验收至少保留
    一份 active 附件——删最后一份返回 409，可先上传新附件再删。
    """
    deliverable = _locked_deliverable(db, project_id=project_id)
    link = db.scalar(
        select(BusinessFileLink).where(
            BusinessFileLink.file_id == file_id,
            BusinessFileLink.entity_type == "maintenance_acceptance_deliverable",
            BusinessFileLink.entity_id == deliverable.deliverable_id,
            BusinessFileLink.archived_at.is_(None),
        ).with_for_update()
    )
    if link is None:
        raise MaintenanceAcceptanceNotFound("附件不存在或已删除")
    if deliverable.submission_status == "submitted":
        remaining = [
            active_file
            for _active_link, active_file in _active_attachments(
                db, deliverable.deliverable_id
            )
            if active_file.file_id != file_id
        ]
        if not remaining:
            raise MaintenanceAcceptanceConflict(
                "已提交的验收至少保留一份附件，可先上传新附件再删除"
            )
    now = _now()
    link.archived_at = now
    link.archived_by = operator
    link.archive_reason = f"用户删除（{operator}）"
    deliverable.version += 1
    result = {
        "file_id": file_id,
        "project_id": project_id,
        "archived": True,
        "archived_at": now.isoformat(),
        "archived_by": operator,
        "version": deliverable.version,
    }
    # 操作台账（与 upload/submit 同一 helper 模式）：删除非幂等重放场景，
    # 每次删除是独立审计事件，operation_key 以一次性键保证唯一。
    db.add(
        MaintenanceAcceptanceOperation(
            operation_id=str(uuid4()),
            operation_key=_operation_key(
                operator=operator,
                operation_type="attachment_archive",
                deliverable_id=deliverable.deliverable_id,
                client_key=f"archive-{file_id}-{uuid4()}",
            ),
            payload_hash=_payload_hash({"file_id": file_id}),
            operation_type="attachment_archive",
            deliverable_id=deliverable.deliverable_id,
            project_id=project_id,
            result_json=result,
            operated_by=operator,
        )
    )
    db.flush()
    return result
