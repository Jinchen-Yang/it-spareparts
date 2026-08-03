#!/usr/bin/env python3
"""Fail-closed validation for downloaded release evidence artifacts."""

from __future__ import annotations

import csv
import contextlib
import io
import os
import pathlib
import posixpath
import stat
import sys
import tempfile
import zipfile
from collections.abc import Iterator
from typing import BinaryIO
from xml.etree import ElementTree


CSV_CONTENT_TYPE = "text/csv; charset=utf-8"
XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
ZIP_CONTENT_TYPE = "application/zip"
CSV_HEADER = [
    "合同",
    "关联项目",
    "order_count",
    "missing_detail_orders",
    "revenue_inc",
    "revenue_ex",
    "expense_inc",
    "expense_ex",
    "parts_cost_inc_tax",
    "parts_cost_ex_tax",
    "parts_gross_profit_inc",
    "parts_gross_profit_ex",
    "parts_gross_margin_inc",
    "parts_gross_margin_ex",
    "contribution_profit_inc",
    "contribution_profit_ex",
    "contribution_margin_inc",
    "contribution_margin_ex",
    "parts_profit_status_inc",
    "parts_profit_status_ex",
    "contribution_status_inc",
    "contribution_status_ex",
    "成本证据状态",
    "成本证据状态-含税",
    "成本证据状态-未税",
    "收入证据状态-含税",
    "收入证据状态-未税",
    "费用证据状态",
]
CONTENT_TYPES = {
    "csv": CSV_CONTENT_TYPE,
    "xlsx": XLSX_CONTENT_TYPE,
    "zip": ZIP_CONTENT_TYPE,
}
MAX_XML_BYTES = 16 * 1024 * 1024
MAX_XLSX_BYTES = 64 * 1024 * 1024
MAX_WORKSHEET_XML_BYTES = 48 * 1024 * 1024
MAX_ZIP_BYTES = 512 * 1024 * 1024
MAX_ZIP_MEMBERS = 500
MAX_XLSX_MEMBERS = 256
MAX_COMPRESSION_RATIO = 250
STREAM_CHUNK_BYTES = 64 * 1024
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOCUMENT_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)


def fail(message: str) -> None:
    raise SystemExit(f"invalid release artifact: {message}")


def content_type(headers: pathlib.Path) -> str:
    try:
        lines = headers.read_text(encoding="latin-1").splitlines()
    except OSError as exc:
        fail(f"cannot read headers: {exc}")
    values = [
        line.split(":", 1)[1].strip().lower()
        for line in lines
        if line.partition(":")[0].strip().lower() == "content-type"
    ]
    if len(values) != 1:
        fail("Content-Type must appear exactly once")
    return "; ".join(part.strip() for part in values[0].split(";"))


def artifact_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


@contextlib.contextmanager
def open_bounded_artifact(
    path: pathlib.Path,
    *,
    limit: int,
    size_error: str,
    label: str,
) -> Iterator[tuple[BinaryIO, int]]:
    try:
        before = path.lstat()
    except OSError as exc:
        fail(f"cannot stat {label}: {exc}")
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size <= 0
        or before.st_size > limit
    ):
        fail(size_error)

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        fail(f"cannot open {label} safely: {exc}")
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or artifact_identity(opened) != artifact_identity(before)
        ):
            fail(f"{label} changed before it could be opened safely")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            yield source, opened.st_size
            after = os.fstat(source.fileno())
            if artifact_identity(after) != artifact_identity(opened):
                fail(f"{label} changed while it was being validated")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def validate_xlsx(path: pathlib.Path) -> int:
    with open_bounded_artifact(
        path,
        limit=MAX_XLSX_BYTES,
        size_error="XLSX is empty or exceeds 64 MiB",
        label="XLSX",
    ) as (source, expected_size):
        raw = source.read(MAX_XLSX_BYTES + 1)
        if len(raw) != expected_size or source.read(1):
            fail("XLSX size changed while it was being read")
        return validate_xlsx_bytes(raw)


def safe_member_names(archive: zipfile.ZipFile) -> list[str]:
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        fail("ZIP contains duplicate members")
    for name in names:
        path = pathlib.PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or name.startswith("/")
            or ".." in path.parts
            or "." in path.parts
        ):
            fail("ZIP contains an unsafe member name")
    return names


def preflight_zip(
    archive: zipfile.ZipFile,
    *,
    max_members: int,
    max_member_bytes: int,
    max_total_bytes: int,
    required_suffix: str | None = None,
) -> list[zipfile.ZipInfo]:
    infos = archive.infolist()
    names = safe_member_names(archive)
    if not 1 <= len(infos) <= max_members:
        fail("ZIP member count is outside the permitted range")
    total = 0
    for info in infos:
        if info.is_dir():
            fail("ZIP contains a directory member")
        if required_suffix and not info.filename.lower().endswith(required_suffix):
            fail(f"ZIP member must end with {required_suffix}")
        if getattr(info, "flag_bits", 0) & 0x1:
            fail("ZIP contains an encrypted member")
        size = info.file_size
        compressed = info.compress_size
        if size < 0 or compressed < 0 or size > max_member_bytes:
            fail("ZIP member size is outside the permitted range")
        total += size
        if total > max_total_bytes:
            fail("ZIP expands beyond its permitted total size")
        if size > 1024 * 1024 and (
            compressed == 0 or size / compressed > MAX_COMPRESSION_RATIO
        ):
            fail("ZIP member compression ratio is unsafe")
    if len(names) != len(infos):
        fail("ZIP central directory is inconsistent")
    return infos


def read_member_bounded(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    limit: int,
) -> bytes:
    if info.file_size > limit:
        fail(f"{info.filename} exceeds its read limit")
    content = bytearray()
    try:
        with archive.open(info) as source:
            while True:
                chunk = source.read(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > limit or len(content) > info.file_size:
                    fail(f"{info.filename} expanded beyond its declared size")
    except zipfile.BadZipFile as exc:
        fail(f"ZIP CRC verification failed: {exc}")
    if len(content) != info.file_size:
        fail(f"{info.filename} size differs from the central directory")
    return bytes(content)


def read_named_member_bounded(
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    name: str,
    limit: int,
) -> bytes:
    info = next((candidate for candidate in infos if candidate.filename == name), None)
    if info is None:
        fail(f"ZIP member is missing: {name}")
    return read_member_bounded(archive, info, limit)


class BoundedXmlMember:
    def __init__(
        self,
        source: zipfile.ZipExtFile,
        info: zipfile.ZipInfo,
        limit: int,
    ) -> None:
        self.source = source
        self.info = info
        self.limit = limit
        self.consumed = 0
        self.scan_tail = b""

    def read(self, size: int = -1) -> bytes:
        requested = STREAM_CHUNK_BYTES if size < 0 else min(size, STREAM_CHUNK_BYTES)
        chunk = self.source.read(requested)
        if not chunk:
            return b""
        self.consumed += len(chunk)
        if self.consumed > self.limit or self.consumed > self.info.file_size:
            fail(f"{self.info.filename} expanded beyond its declared size")
        scan = (self.scan_tail + chunk).upper()
        if b"<!DOCTYPE" in scan:
            fail(f"{self.info.filename} XML contains a doctype")
        self.scan_tail = scan[-8:]
        return chunk


def parse_xml_member_streaming(
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    name: str,
    limit: int,
) -> ElementTree.Element:
    info = next((candidate for candidate in infos if candidate.filename == name), None)
    if info is None or info.file_size <= 0 or info.file_size > limit:
        fail(f"{name} XML is empty or oversized")
    try:
        with archive.open(info) as source:
            reader = BoundedXmlMember(source, info, limit)
            root = ElementTree.parse(reader).getroot()
            if reader.read(1):
                fail(f"{name} XML has trailing unread content")
            if reader.consumed != info.file_size:
                fail(f"{name} size differs from the central directory")
            return root
    except ElementTree.ParseError as exc:
        fail(f"{name} XML is malformed: {exc}")
    except zipfile.BadZipFile as exc:
        fail(f"ZIP CRC verification failed: {exc}")


def drain_member_streaming(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    limit: int,
) -> None:
    if info.file_size > limit:
        fail(f"{info.filename} exceeds its stream limit")
    try:
        with archive.open(info) as source:
            consumed = 0
            while True:
                chunk = source.read(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                consumed += len(chunk)
                if consumed > limit or consumed > info.file_size:
                    fail(
                        f"{info.filename} expanded beyond its declared size"
                    )
            if consumed != info.file_size:
                fail(
                    f"{info.filename} size differs from the central directory"
                )
    except zipfile.BadZipFile as exc:
        fail(f"ZIP CRC verification failed: {exc}")


def relationship_source(rels_name: str) -> str:
    if rels_name == "_rels/.rels":
        return ""
    marker = "/_rels/"
    if marker not in rels_name or not rels_name.endswith(".rels"):
        fail("XLSX relationship part path is malformed")
    parent, relationship_name = rels_name.rsplit(marker, 1)
    source_name = relationship_name.removesuffix(".rels")
    if not parent or not source_name:
        fail("XLSX relationship part path is malformed")
    return posixpath.join(parent, source_name)


def validate_relationship_closure(
    roots: dict[str, ElementTree.Element],
    names: set[str],
) -> None:
    for rels_name, root in roots.items():
        if not rels_name.endswith(".rels"):
            continue
        source_name = relationship_source(rels_name)
        if source_name and source_name not in names:
            fail("XLSX relationship source part is missing")
        relationship_ids: set[str] = set()
        for relationship in root.findall(
            f"{{{PACKAGE_REL_NS}}}Relationship"
        ):
            relationship_id = relationship.attrib.get("Id")
            relationship_type = relationship.attrib.get("Type")
            target = relationship.attrib.get("Target")
            target_mode = relationship.attrib.get("TargetMode")
            if (
                not relationship_id
                or relationship_id in relationship_ids
                or not relationship_type
                or not target
                or target_mode
                or "\\" in target
                or "\x00" in target
                or "#" in target
                or "?" in target
            ):
                fail("XLSX relationship is malformed or external")
            relationship_ids.add(relationship_id)
            if target.startswith("/"):
                resolved = posixpath.normpath(target.lstrip("/"))
            else:
                resolved = posixpath.normpath(
                    posixpath.join(posixpath.dirname(source_name), target)
                )
            if (
                not resolved
                or resolved == "."
                or resolved.startswith("../")
                or resolved.startswith("/")
                or resolved.endswith("/")
                or resolved not in names
            ):
                fail("XLSX relationship target is missing or unsafe")


def parse_xml(raw: bytes, name: str) -> ElementTree.Element:
    if not raw or len(raw) > MAX_XML_BYTES or b"<!DOCTYPE" in raw.upper():
        fail(f"{name} XML is empty, oversized, or contains a doctype")
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        fail(f"{name} XML is malformed: {exc}")


def validate_xlsx_bytes(raw: bytes) -> int:
    if not raw or len(raw) > MAX_XLSX_BYTES:
        fail("XLSX is empty or exceeds 64 MiB")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = preflight_zip(
                archive,
                max_members=MAX_XLSX_MEMBERS,
                max_member_bytes=MAX_WORKSHEET_XML_BYTES,
                max_total_bytes=MAX_XLSX_BYTES,
            )
            names = [info.filename for info in infos]
            name_set = set(names)
            required = {
                "[Content_Types].xml",
                "_rels/.rels",
                "xl/workbook.xml",
                "xl/_rels/workbook.xml.rels",
            }
            if not required.issubset(names):
                fail("XLSX is missing required OOXML package parts")
            roots: dict[str, ElementTree.Element] = {}
            for info in infos:
                if info.filename.endswith(".xml") or info.filename.endswith(
                    ".rels"
                ):
                    limit = (
                        MAX_WORKSHEET_XML_BYTES
                        if info.filename.startswith("xl/worksheets/")
                        and info.filename.endswith(".xml")
                        else MAX_XML_BYTES
                    )
                    roots[info.filename] = parse_xml_member_streaming(
                        archive,
                        infos,
                        info.filename,
                        limit,
                    )
                else:
                    drain_member_streaming(
                        archive,
                        info,
                        MAX_WORKSHEET_XML_BYTES,
                    )
            validate_relationship_closure(roots, name_set)
            content_types = roots["[Content_Types].xml"]
            overrides = {
                item.attrib.get("PartName"): item.attrib.get("ContentType")
                for item in content_types
                if item.tag.endswith("}Override")
            }
            if overrides.get("/xl/workbook.xml") != (
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet.main+xml"
            ):
                fail("XLSX workbook content type is not the non-macro format")
            package_rels = roots["_rels/.rels"]
            office_targets = {
                item.attrib.get("Target")
                for item in package_rels.findall(f"{{{PACKAGE_REL_NS}}}Relationship")
                if item.attrib.get("Type", "").endswith("/officeDocument")
            }
            if office_targets != {"xl/workbook.xml"}:
                fail("XLSX package relationship is not exact")
            workbook = roots["xl/workbook.xml"]
            workbook_rels = roots["xl/_rels/workbook.xml.rels"]
            relationships = {
                item.attrib.get("Id"): (
                    item.attrib.get("Target"),
                    item.attrib.get("TargetMode"),
                )
                for item in workbook_rels.findall(
                    f"{{{PACKAGE_REL_NS}}}Relationship"
                )
                if item.attrib.get("Type", "").endswith("/worksheet")
            }
            sheets = workbook.findall(f".//{{{SHEET_NS}}}sheet")
            if not sheets:
                fail("XLSX workbook has no worksheets")
            for sheet in sheets:
                relationship_id = sheet.attrib.get(
                    f"{{{DOCUMENT_REL_NS}}}id"
                )
                relationship = relationships.get(relationship_id)
                if not relationship:
                    fail("XLSX worksheet relationship is missing or unsafe")
                target, target_mode = relationship
                if not target or target_mode or "\\" in target:
                    fail("XLSX worksheet relationship is missing or unsafe")
                if target.startswith("/"):
                    member = posixpath.normpath(target.lstrip("/"))
                else:
                    member = posixpath.normpath(
                        posixpath.join("xl", target)
                    )
                if (
                    member.startswith("../")
                    or not member.startswith("xl/worksheets/")
                    or member.endswith("/")
                ):
                    fail("XLSX worksheet relationship escapes its package scope")
                if member not in name_set:
                    fail("XLSX worksheet target is missing")
            return len(sheets)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        fail(f"XLSX package cannot be opened: {exc}")


def validate_csv(path: pathlib.Path) -> tuple[int, int]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as source:
            rows = csv.reader(source)
            header = next(rows, None)
            if header != CSV_HEADER:
                fail("CSV header does not match the contract-profit export")
            count = 0
            for row in rows:
                if len(row) != len(CSV_HEADER) or not row[0].strip():
                    fail("CSV contains a malformed business row")
                count += 1
    except (OSError, UnicodeError, csv.Error) as exc:
        fail(f"CSV cannot be parsed: {exc}")
    if count == 0:
        fail("CSV has no business rows despite HTTP 200")
    return len(CSV_HEADER), count


def validate_zip(path: pathlib.Path) -> tuple[int, int]:
    try:
        with open_bounded_artifact(
            path,
            limit=MAX_ZIP_BYTES,
            size_error="batch ZIP is empty or exceeds 512 MiB",
            label="batch ZIP",
        ) as (source, _expected_size):
            with zipfile.ZipFile(source) as archive:
                infos = preflight_zip(
                    archive,
                    max_members=MAX_ZIP_MEMBERS,
                    max_member_bytes=MAX_XLSX_BYTES,
                    max_total_bytes=MAX_ZIP_BYTES,
                    required_suffix=".xlsx",
                )
                sheets = 0
                for info in infos:
                    sheets += validate_xlsx_bytes(
                        read_member_bounded(archive, info, MAX_XLSX_BYTES)
                    )
                return len(infos), sheets
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        fail(f"batch ZIP cannot be opened: {exc}")


def self_test_xlsx(target: str) -> bytes:
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            f"""<Relationships xmlns="{PACKAGE_REL_NS}">
<Relationship Id="rId1" Type="{DOCUMENT_REL_NS}/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "xl/workbook.xml",
            f"""<workbook xmlns="{SHEET_NS}" xmlns:r="{DOCUMENT_REL_NS}">
<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            f"""<Relationships xmlns="{PACKAGE_REL_NS}">
<Relationship Id="rId1" Type="{DOCUMENT_REL_NS}/worksheet" Target="{target}"/>
</Relationships>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{SHEET_NS}"><sheetData/></worksheet>',
        )
    return content.getvalue()


def expect_failure(callback: object) -> None:
    try:
        callback()  # type: ignore[operator]
    except SystemExit:
        return
    fail("self-test expected a malformed artifact to be rejected")


def run_self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="release-artifact-self-test.") as raw:
        root = pathlib.Path(raw)
        csv_headers = root / "csv.headers"
        xlsx_headers = root / "xlsx.headers"
        zip_headers = root / "zip.headers"
        csv_headers.write_text(
            f"Content-Type: {CSV_CONTENT_TYPE}\r\n",
            encoding="ascii",
        )
        xlsx_headers.write_text(
            f"Content-Type: {XLSX_CONTENT_TYPE}\r\n",
            encoding="ascii",
        )
        zip_headers.write_text(
            f"Content-Type: {ZIP_CONTENT_TYPE}\r\n",
            encoding="ascii",
        )
        csv_path = root / "valid.csv"
        with csv_path.open("w", encoding="utf-8-sig", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(CSV_HEADER)
            writer.writerow(["C-1", "P-1", "1", "0", *(["known"] * 24)])
        relative = self_test_xlsx("worksheets/sheet1.xml")
        absolute = self_test_xlsx("/xl/worksheets/sheet1.xml")
        for raw_xlsx in (relative, absolute):
            if validate_xlsx_bytes(raw_xlsx) != 1:
                fail("self-test XLSX sheet count is wrong")
        xlsx_path = root / "valid.xlsx"
        xlsx_path.write_bytes(absolute)
        zip_path = root / "valid.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("合同一.xlsx", absolute)
        if content_type(csv_headers) != CSV_CONTENT_TYPE:
            fail("self-test CSV Content-Type is wrong")
        validate_csv(csv_path)
        if content_type(xlsx_headers) != XLSX_CONTENT_TYPE:
            fail("self-test XLSX Content-Type is wrong")
        validate_xlsx_bytes(xlsx_path.read_bytes())
        if content_type(zip_headers) != ZIP_CONTENT_TYPE:
            fail("self-test ZIP Content-Type is wrong")
        validate_zip(zip_path)
        wrong_headers = root / "wrong.headers"
        wrong_headers.write_text("Content-Type: text/plain\r\n", encoding="ascii")
        expect_failure(
            lambda: (
                content_type(wrong_headers) == CSV_CONTENT_TYPE
                or fail("self-test rejected wrong MIME")
            )
        )
        garbage = root / "garbage.csv"
        garbage.write_text("not,a,contract,export\n", encoding="utf-8")
        expect_failure(lambda: validate_csv(garbage))
        expect_failure(lambda: validate_xlsx_bytes(b"PK fake"))
        expect_failure(
            lambda: validate_xlsx_bytes(
                self_test_xlsx("../../outside.xml")
            )
        )
    print("SELF_TEST_OK csv=1 xlsx=2 zip=1 rejected=4")


def main() -> None:
    if sys.argv[1:] == ["--self-test"]:
        run_self_test()
        return
    if len(sys.argv) != 4 or sys.argv[1] not in CONTENT_TYPES:
        fail("usage: validate_release_artifacts.py <csv|xlsx|zip> FILE HEADERS")
    kind = sys.argv[1]
    artifact = pathlib.Path(sys.argv[2])
    headers = pathlib.Path(sys.argv[3])
    if content_type(headers) != CONTENT_TYPES[kind]:
        fail(f"unexpected Content-Type for {kind}")
    if kind == "csv":
        columns, rows = validate_csv(artifact)
        print(f"VALID kind=csv columns={columns} rows={rows}")
    elif kind == "xlsx":
        sheets = validate_xlsx(artifact)
        print(f"VALID kind=xlsx sheets={sheets}")
    else:
        members, sheets = validate_zip(artifact)
        print(f"VALID kind=zip members={members} sheets={sheets}")


if __name__ == "__main__":
    main()
