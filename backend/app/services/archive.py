"""循环档案服务（板块 D，D-3/D-22）。

口径依据：docs/decisions/0002 D-22（2026-10-07 甲方确认）：
- 循环档案挂在 PN 上，一个 PN 一份档案；照片挂在 PN 档案，不按 SN 拍；
- 可上架判定 = 照片 ≥1 且检测报告 ≥1；上架本身是显式动作；
- 资料不全时，持 action_recycle_force_list 的账号可强制上架，
  必须填写原因，留标记与实名审计；
- 附件文件落本地目录（config.circulation_files_dir），元数据入库；
  删除附件后若资料不再齐全，普通"已上架"自动回退为"待补齐"。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.circulation import (
    CirculationArchive,
    CirculationAttachment,
    CirculationSnItem,
    LISTING_STATUS,
)
from app.models.dimensions import DimPart

ARCHIVE_ATTACHMENT_KINDS = ("photo", "report")
# 文件类型白名单：照片 jpg/jpeg/png/webp；检测报告 pdf（甲方样例即 PDF）
_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_REPORT_EXTS = {".pdf"}
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


class ArchiveValidationError(Exception):
    """档案操作校验失败（422，零写入）。"""


class ArchivePermissionError(Exception):
    """权限不足（403；如无 action_recycle_force_list 却试图强制上架）。"""


def _files_root() -> Path:
    root = Path(get_settings().circulation_files_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_or_create_archive(db: Session, pn_std: str, *, operator: str) -> CirculationArchive:
    pn = pn_std.strip().upper()
    archive = db.execute(
        select(CirculationArchive).where(CirculationArchive.pn_std == pn)
    ).scalar_one_or_none()
    if archive is None:
        archive = CirculationArchive(
            archive_id=str(uuid.uuid4()),
            pn_std=pn,
            part_id=_resolve_part(db, pn),
        )
        db.add(archive)
        db.flush()
    return archive


def _resolve_part(db: Session, pn_std: str) -> int | None:
    return db.execute(
        select(DimPart.id).where(DimPart.pn_std == pn_std)
    ).scalar_one_or_none()


def attachment_summary(db: Session, archive: CirculationArchive) -> dict:
    rows = db.execute(
        select(CirculationAttachment).where(
            CirculationAttachment.archive_id == archive.archive_id)
        .order_by(CirculationAttachment.uploaded_at)
    ).scalars().all()
    photos = sum(1 for a in rows if a.kind == "photo")
    reports = sum(1 for a in rows if a.kind == "report")
    return {
        "attachments": [
            {"attachment_id": a.attachment_id, "kind": a.kind,
             "original_filename": a.original_filename, "mime_type": a.mime_type,
             "size_bytes": a.size_bytes, "sha256": a.sha256,
             "uploaded_by": a.uploaded_by, "uploaded_at": a.uploaded_at.isoformat()}
            for a in rows
        ],
        "photo_count": photos,
        "report_count": reports,
        "requirements_met": photos >= 1 and reports >= 1,
    }


def add_attachment(
    db: Session, pn_std: str, kind: str, filename: str, content: bytes, *, operator: str
) -> CirculationAttachment:
    """上传附件：类型/大小校验 → 落盘 → 元数据入库（档案不存在则自动建档）。"""
    if kind not in ARCHIVE_ATTACHMENT_KINDS:
        raise ArchiveValidationError(f"附件种类只允许 {'/'.join(ARCHIVE_ATTACHMENT_KINDS)}")
    ext = Path(filename or "").suffix.lower()
    if kind == "photo" and ext not in _PHOTO_EXTS:
        raise ArchiveValidationError(f"照片只允许 {'/'.join(sorted(_PHOTO_EXTS))}，得到「{ext}」")
    if kind == "report" and ext not in _REPORT_EXTS:
        raise ArchiveValidationError(f"检测报告只允许 {'/'.join(sorted(_REPORT_EXTS))}，得到「{ext}」")
    if not content or len(content) > MAX_ATTACHMENT_BYTES:
        raise ArchiveValidationError(f"文件大小必须在 1 字节与 {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB 之间")

    archive = get_or_create_archive(db, pn_std, operator=operator)
    attachment_id = str(uuid.uuid4())
    rel_key = f"{archive.archive_id}/{attachment_id}{ext}"
    abs_path = _files_root() / rel_key
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(content)

    att = CirculationAttachment(
        attachment_id=attachment_id,
        archive_id=archive.archive_id,
        kind=kind,
        original_filename=Path(filename or "file").name,
        mime_type={".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                   ".webp": "image/webp", ".pdf": "application/pdf"}.get(ext, "application/octet-stream"),
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        storage_key=rel_key,
        uploaded_by=operator,
        uploaded_at=datetime.now(),
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def remove_attachment(db: Session, pn_std: str, attachment_id: str, *, operator: str) -> None:
    att = db.get(CirculationAttachment, attachment_id)
    if att is None or att.archive_id != _get_archive_id(db, pn_std):
        raise ArchiveValidationError("附件不存在或不属于该档案")
    (Path(get_settings().circulation_files_dir) / att.storage_key).unlink(missing_ok=True)
    db.delete(att)
    db.flush()
    # 删除后资料不再齐全的，普通"已上架"自动回退待补齐；强制上架保持显式决定
    archive = db.execute(
        select(CirculationArchive).where(CirculationArchive.pn_std == pn_std.strip().upper())
    ).scalar_one()
    summary = attachment_summary(db, archive)
    if archive.listing_status == "listed" and not summary["requirements_met"]:
        archive.listing_status = "pending"
    db.commit()


def _get_archive_id(db: Session, pn_std: str) -> str | None:
    return db.execute(
        select(CirculationArchive.archive_id).where(
            CirculationArchive.pn_std == pn_std.strip().upper())
    ).scalar_one_or_none()


def set_listing(
    db: Session, pn_std: str, action: str, *, operator: str,
    has_force_permission: bool, reason: str | None = None,
) -> CirculationArchive:
    """上架（list）/ 强制上架（force_list）/ 下架（delist）。

    - list：照片+检测报告必须齐全，否则 422（提示缺什么）；
    - force_list：资料不全时凭 action_recycle_force_list 使用，必须填原因；
    - delist：任意时刻可下架。
    """
    archive_id = _get_archive_id(db, pn_std)
    if archive_id is None:
        raise ArchiveValidationError(f"PN {pn_std} 尚无循环档案（先上传资料自动建档）")
    archive = db.get(CirculationArchive, archive_id)
    summary = attachment_summary(db, archive)

    if action == "delist":
        archive.listing_status = "delisted"
    elif action == "list":
        if not summary["requirements_met"]:
            missing = [k for k, n in (("照片", summary["photo_count"]),
                                      ("检测报告", summary["report_count"])) if n == 0]
            raise ArchiveValidationError(f"资料不全，缺少：{'、'.join(missing)}（补齐后可正常上架，或由授权账号强制上架）")
        archive.listing_status = "listed"
    elif action == "force_list":
        if not has_force_permission:
            raise ArchivePermissionError("强制上架需要 action_recycle_force_list 权限")
        if summary["requirements_met"]:
            raise ArchiveValidationError("资料已齐全，请使用正常上架（list），无需强制")
        if not reason or len(reason.strip()) < 5:
            raise ArchiveValidationError("强制上架必须填写原因（至少 5 字），留实名审计")
        archive.listing_status = "force_listed"
        archive.force_listed_by = operator
        archive.force_listed_at = datetime.now()
        archive.force_reason = reason.strip()
    else:
        raise ArchiveValidationError(f"未知上架动作：{action}")
    db.commit()
    db.refresh(archive)
    return archive


def list_sn_ledger(db: Session, *, pn_std: str | None = None,
                   lifecycle_status: str | None = None) -> list[CirculationSnItem]:
    """SN 台账查询（D-4：每件可查）。"""
    stmt = select(CirculationSnItem).order_by(CirculationSnItem.sn)
    if pn_std:
        stmt = stmt.where(CirculationSnItem.pn_std == pn_std.strip().upper())
    if lifecycle_status:
        if lifecycle_status not in ("pending_detection", "in_stock", "bad_stock", "retired"):
            raise ArchiveValidationError(f"未知生命周期状态：{lifecycle_status}")
        stmt = stmt.where(CirculationSnItem.lifecycle_status == lifecycle_status)
    return list(db.execute(stmt).scalars().all())
