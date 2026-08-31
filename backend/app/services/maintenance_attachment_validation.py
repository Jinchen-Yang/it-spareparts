"""Explicit validation policies for maintenance-domain attachments."""

from __future__ import annotations

import io
import unicodedata
import xml.etree.ElementTree as ElementTree
import zipfile
from pathlib import Path, PurePosixPath

from PIL import Image, UnidentifiedImageError


MAX_MAINTENANCE_ATTACHMENT_BYTES = 20 * 1024 * 1024

_COLLECTION_EVIDENCE_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
_MAX_ZIP_MEMBERS = 1024
_MAX_ZIP_EXPANDED_BYTES = 100 * 1024 * 1024
_MAX_ZIP_RATIO = 100
_PDF_ACTIVE_TOKENS = (
    b"/javascript",
    b"/js",
    b"/launch",
    b"/embeddedfile",
    b"/openaction",
    b"/richmedia",
)


class AttachmentValidationError(Exception):
    """An attachment violates the selected domain policy."""


class AttachmentTooLarge(AttachmentValidationError):
    """An attachment exceeds the shared 20 MB size limit."""


def _safe_filename(filename: str | None) -> tuple[str, str]:
    normalized = unicodedata.normalize("NFC", str(filename or "")).strip()
    if (
        not normalized
        or len(normalized) > 256
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or any(unicodedata.category(char).startswith("C") for char in normalized)
    ):
        raise AttachmentValidationError("附件文件名不安全")
    return normalized, Path(normalized).suffix.lower()


def _validated_basics(*, filename: str | None, content: bytes) -> tuple[str, str]:
    safe_name, extension = _safe_filename(filename)
    if not content:
        raise AttachmentValidationError("附件内容为空")
    if len(content) > MAX_MAINTENANCE_ATTACHMENT_BYTES:
        raise AttachmentTooLarge("单个附件不得超过 20MB")
    return safe_name, extension


def _normalized_mime(mime_type: str | None) -> str:
    return str(mime_type or "").split(";", 1)[0].strip().lower()


def validate_acceptance_attachment(
    *, filename: str | None, mime_type: str | None, content: bytes
) -> tuple[str, str, str]:
    """Acceptance attachments intentionally allow every file type."""
    safe_name, extension = _validated_basics(filename=filename, content=content)
    stored_mime = _normalized_mime(mime_type) or "application/octet-stream"
    return safe_name, extension, stored_mime


def _assert_safe_office_package(data: bytes, *, extension: str) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            infos = package.infolist()
            if not infos or len(infos) > _MAX_ZIP_MEMBERS:
                raise AttachmentValidationError("Office 回款凭证结构异常")
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
                    raise AttachmentValidationError(
                        "Office 回款凭证包含不安全路径或加密内容"
                    )
                expanded += info.file_size
                if expanded > _MAX_ZIP_EXPANDED_BYTES:
                    raise AttachmentValidationError("Office 回款凭证解压后体积异常")
                if info.file_size and info.compress_size == 0:
                    raise AttachmentValidationError("Office 回款凭证压缩结构异常")
                if (
                    info.compress_size
                    and info.file_size / info.compress_size > _MAX_ZIP_RATIO
                ):
                    raise AttachmentValidationError("Office 回款凭证压缩比异常")
                lower_name = name.lower()
                if lower_name in names:
                    raise AttachmentValidationError("Office 回款凭证包含重复文件成员")
                names.add(lower_name)

            required = (
                "word/document.xml"
                if extension == ".docx"
                else "xl/workbook.xml"
            )
            if "[content_types].xml" not in names or required not in names:
                raise AttachmentValidationError("Office 回款凭证类型与扩展名不匹配")
            forbidden_parts = (
                "vbaproject.bin",
                "/embeddings/",
                "/externallinks/",
                "connections.xml",
                "customui/",
            )
            if any(
                any(marker in name for marker in forbidden_parts)
                for name in names
            ):
                raise AttachmentValidationError(
                    "Office 回款凭证含宏、嵌入对象或外部数据连接"
                )

            for info in infos:
                lower = info.filename.lower()
                if not lower.endswith((".xml", ".rels")):
                    continue
                payload = package.read(info)
                folded = payload.lower()
                if b"<!doctype" in folded or b"<!entity" in folded:
                    raise AttachmentValidationError(
                        "Office 回款凭证包含不安全 XML 声明"
                    )
                try:
                    root = ElementTree.fromstring(payload)
                except ElementTree.ParseError as exc:
                    raise AttachmentValidationError(
                        "Office 回款凭证包含损坏的 XML"
                    ) from exc
                if lower.endswith(".rels") and any(
                    attribute.rsplit("}", 1)[-1].lower() == "targetmode"
                    and str(value).strip().lower() == "external"
                    for element in root.iter()
                    for attribute, value in element.attrib.items()
                ):
                    raise AttachmentValidationError("Office 回款凭证包含外部链接")
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:
        raise AttachmentValidationError(
            "Office 回款凭证内容损坏或格式不正确"
        ) from exc


def _assert_safe_collection_content(content: bytes, *, extension: str) -> None:
    if extension == ".pdf":
        folded = content.lower()
        if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-2048:]:
            raise AttachmentValidationError("PDF 回款凭证内容损坏或格式不正确")
        if any(token in folded for token in _PDF_ACTIVE_TOKENS):
            raise AttachmentValidationError(
                "PDF 回款凭证含脚本、启动动作或嵌入文件"
            )
        return
    if extension in {".docx", ".xlsx"}:
        _assert_safe_office_package(content, extension=extension)
        return
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            if getattr(image, "n_frames", 1) != 1:
                raise AttachmentValidationError("不接受多帧图片回款凭证")
            if (
                image.width <= 0
                or image.height <= 0
                or image.width * image.height > 40_000_000
            ):
                raise AttachmentValidationError("图片回款凭证像素尺寸异常")
            expected_format = "PNG" if extension == ".png" else "JPEG"
            if image.format != expected_format:
                raise AttachmentValidationError("图片回款凭证内容与扩展名不匹配")
    except (
        Image.DecompressionBombError,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise AttachmentValidationError(
            "图片回款凭证内容损坏或格式不正确"
        ) from exc


def validate_collection_evidence_attachment(
    *, filename: str | None, mime_type: str | None, content: bytes
) -> tuple[str, str, str]:
    """Collection evidence accepts only the documented document/image types."""
    safe_name, extension = _validated_basics(filename=filename, content=content)
    expected_mime = _COLLECTION_EVIDENCE_TYPES.get(extension)
    if expected_mime is None:
        raise AttachmentValidationError(
            "回款凭证仅支持 PDF、DOCX、XLSX、PNG、JPG/JPEG"
        )
    if _normalized_mime(mime_type) != expected_mime:
        raise AttachmentValidationError("回款凭证扩展名与 MIME 类型不匹配")
    _assert_safe_collection_content(content, extension=extension)
    return safe_name, extension, expected_mime
