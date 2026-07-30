#!/usr/bin/env python3
"""Generate deterministic CycloneDX dependency summaries from committed locks."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import quote


def _normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _normalize_marker(marker: str) -> str:
    return " ".join(marker.strip().split())


def _parse_requirement(
    requirement: str,
    *,
    optional_group: str | None = None,
) -> tuple[str, tuple[str, ...], str, str]:
    requirement_body, separator, marker = requirement.partition(";")
    match = re.fullmatch(
        r"\s*([A-Za-z0-9_.-]+)"
        r"(?:\[([A-Za-z0-9_., -]+)\])?"
        r"\s*(.*?)\s*",
        requirement_body,
    )
    if not match:
        raise ValueError(f"unsupported project requirement: {requirement}")
    name = _normalize_name(match.group(1))
    extras = tuple(
        sorted(
            _normalize_name(extra.strip())
            for extra in (match.group(2) or "").split(",")
            if extra.strip()
        )
    )
    specifier = match.group(3).replace(" ", "")
    normalized_marker = _normalize_marker(marker) if separator else ""
    if optional_group is not None:
        extra_marker = f"extra == '{optional_group}'"
        normalized_marker = (
            f"({normalized_marker}) and {extra_marker}"
            if normalized_marker
            else extra_marker
        )
    return name, extras, specifier, normalized_marker


def _metadata_requirement(
    requirement: dict[str, object],
) -> tuple[str, tuple[str, ...], str, str]:
    extras = tuple(
        sorted(
            _normalize_name(str(extra))
            for extra in requirement.get("extras", [])
        )
    )
    return (
        _normalize_name(str(requirement["name"])),
        extras,
        str(requirement.get("specifier", "")).replace(" ", ""),
        _normalize_marker(str(requirement.get("marker", ""))),
    )


def _validate_project_lock(
    pyproject_path: Path,
    uv_lock_path: Path,
    requirements_path: Path,
) -> None:
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = pyproject.get("project", {})
    project_name = _normalize_name(str(project.get("name", "")))
    project_version = str(project.get("version", ""))
    production = {
        _parse_requirement(str(requirement))
        for requirement in project.get("dependencies", [])
    }
    optional = project.get("optional-dependencies", {})
    optional_requirements = {
        _parse_requirement(str(requirement), optional_group=str(group))
        for group, requirements in optional.items()
        for requirement in requirements
    }
    expected = production | optional_requirements
    uv_lock = tomllib.loads(uv_lock_path.read_text(encoding="utf-8"))
    roots = [
        package
        for package in uv_lock.get("package", [])
        if package.get("source", {}).get("editable") == "."
        and _normalize_name(str(package.get("name", ""))) == project_name
        and str(package.get("version", "")) == project_version
    ]
    if len(roots) != 1:
        raise ValueError(
            "pyproject.toml and uv.lock dependency metadata differ: "
            "editable project root is not unique"
        )
    root = roots[0]
    metadata = root.get("metadata", {})
    locked = {
        _metadata_requirement(requirement)
        for requirement in metadata.get("requires-dist", [])
    }
    provided_extras = sorted(
        str(extra) for extra in metadata.get("provides-extras", [])
    )
    if expected != locked or sorted(optional) != provided_extras:
        raise ValueError(
            "pyproject.toml and uv.lock dependency metadata differ"
        )
    locked_production_direct = {
        (
            _normalize_name(str(dependency["name"])),
            tuple(
                sorted(
                    _normalize_name(str(extra))
                    for extra in dependency.get("extra", [])
                )
            ),
        )
        for dependency in root.get("dependencies", [])
    }
    expected_production_direct = {
        (name, extras) for name, extras, _, _ in production
    }
    if locked_production_direct != expected_production_direct:
        raise ValueError(
            "pyproject.toml and uv.lock production dependencies differ"
        )
    selected = _requirement_versions(requirements_path)
    missing_direct = sorted(
        name
        for name, _ in expected_production_direct
        if name not in selected
    )
    if missing_direct:
        raise ValueError(
            "requirements.lock omits production direct dependencies: "
            + ", ".join(missing_direct)
        )


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _bom(components: list[dict[str, object]]) -> dict[str, object]:
    return {
        "bomFormat": "CycloneDX",
        "components": sorted(
            components,
            key=lambda item: str(item["bom-ref"]),
        ),
        "metadata": {
            "component": {
                "bom-ref": "it-spareparts",
                "name": "it-spareparts",
                "type": "application",
            }
        },
        "specVersion": "1.5",
        "version": 1,
    }


def _requirement_versions(lock_path: Path) -> dict[str, str]:
    requirements: dict[str, str] = {}
    pattern = re.compile(r"^([a-zA-Z0-9_.-]+)==([^ ;\\\\]+)")
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if not match:
            continue
        name = match.group(1).lower().replace("_", "-")
        version = match.group(2)
        if name in requirements:
            raise ValueError(f"duplicate locked requirement: {name}")
        requirements[name] = version
    if not requirements:
        raise ValueError("requirements.lock contains no packages")
    return requirements


def _python_components(
    uv_lock_path: Path,
    requirements_path: Path,
) -> list[dict[str, object]]:
    selected = _requirement_versions(requirements_path)
    lock = tomllib.loads(uv_lock_path.read_text(encoding="utf-8"))
    components: list[dict[str, object]] = []
    for package in lock.get("package", []):
        source = package.get("source", {})
        if "registry" not in source:
            continue
        name = package["name"]
        version = package["version"]
        normalized_name = name.lower().replace("_", "-")
        if selected.get(normalized_name) != version:
            continue
        hashes: set[tuple[str, str]] = set()
        artifacts = []
        if package.get("sdist"):
            artifacts.append(package["sdist"])
        artifacts.extend(package.get("wheels", []))
        for artifact in artifacts:
            algorithm, _, digest = artifact.get("hash", "").partition(":")
            if algorithm == "sha256" and len(digest) == 64:
                hashes.add(("SHA-256", digest))
        component: dict[str, object] = {
            "bom-ref": f"pkg:pypi/{quote(name)}@{quote(version)}",
            "name": name,
            "purl": f"pkg:pypi/{quote(name)}@{quote(version)}",
            "type": "library",
            "version": version,
        }
        if hashes:
            component["hashes"] = [
                {"alg": algorithm, "content": digest}
                for algorithm, digest in sorted(hashes)
            ]
        components.append(component)
    represented = {
        str(component["name"]).lower().replace("_", "-")
        for component in components
    }
    if represented != selected.keys():
        missing = sorted(selected.keys() - represented)
        extra = sorted(represented - selected.keys())
        raise ValueError(
            f"requirements/uv lock mismatch: missing={missing}, extra={extra}"
        )
    return components


def _npm_name(package_path: str) -> str:
    marker = "node_modules/"
    if marker not in package_path:
        raise ValueError(f"not an installed npm package path: {package_path}")
    return package_path.rsplit(marker, 1)[1]


def _npm_components(lock_path: Path) -> list[dict[str, object]]:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("lockfileVersion") != 3:
        raise ValueError("package-lock.json must use lockfileVersion 3")
    components: list[dict[str, object]] = []
    for package_path, package in lock.get("packages", {}).items():
        if not package_path or "node_modules/" not in package_path:
            continue
        name = _npm_name(package_path)
        version = package.get("version")
        integrity = package.get("integrity")
        if not isinstance(version, str) or not isinstance(integrity, str):
            raise ValueError(f"unlocked npm package: {package_path}")
        algorithm, separator, encoded = integrity.partition("-")
        if separator != "-" or algorithm != "sha512":
            raise ValueError(f"unsupported npm integrity: {package_path}")
        digest = base64.b64decode(encoded, validate=True).hex()
        if name.startswith("@") and "/" in name:
            namespace, package_name = name.split("/", 1)
            encoded_name = f"{quote(namespace, safe='')}/{quote(package_name)}"
        else:
            encoded_name = quote(name)
        purl = f"pkg:npm/{encoded_name}@{quote(version)}"
        component: dict[str, object] = {
            "bom-ref": f"{purl}?path={quote(package_path, safe='')}",
            "hashes": [{"alg": "SHA-512", "content": digest}],
            "name": name,
            "properties": [
                {
                    "name": "it-spareparts:package-lock-path",
                    "value": package_path,
                },
                {
                    "name": "it-spareparts:development-only",
                    "value": str(bool(package.get("dev"))).lower(),
                },
            ],
            "purl": purl,
            "type": "library",
            "version": version,
        }
        components.append(component)
    return components


def _expected(root: Path) -> dict[Path, dict[str, object]]:
    _validate_project_lock(
        root / "backend" / "pyproject.toml",
        root / "backend" / "uv.lock",
        root / "backend" / "requirements.lock",
    )
    return {
        root / "backend" / "dependency-sbom.cdx.json": _bom(
            _python_components(
                root / "backend" / "uv.lock",
                root / "backend" / "requirements.lock",
            )
        ),
        root / "frontend" / "dependency-sbom.cdx.json": _bom(
            [
                component
                for component in _npm_components(
                    root / "frontend" / "package-lock.json"
                )
                if next(
                    prop["value"]
                    for prop in component["properties"]
                    if prop["name"]
                    == "it-spareparts:development-only"
                )
                == "false"
            ]
        ),
    }


def _serialized(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _check(root: Path) -> int:
    failed = False
    for path, payload in _expected(root).items():
        expected = _serialized(payload)
        if not path.is_file() or path.read_bytes() != expected:
            print(f"stale-or-missing: {path}", file=sys.stderr)
            failed = True
            continue
        print(f"{path.name} sha256={hashlib.sha256(expected).hexdigest()}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.check:
            return _check(root)
        for path, payload in _expected(root).items():
            _write_json(path, payload)
            print(f"wrote {path}")
        return 0
    except (KeyError, TypeError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"invalid dependency locks: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
