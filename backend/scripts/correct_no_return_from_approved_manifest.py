#!/usr/bin/env python3
"""D-32 §3: dry-run by default; explicit, approved, single-entry execution."""

import argparse
import json
from pathlib import Path

from app.db import SessionLocal
from app.services.maintenance_no_return_approval import ApprovalError, apply_one, dry_run, load_manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--apply-one", metavar="ENTRY_ID")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--operator", help="有权实名系统账号 username（不是显示名）")
    args = parser.parse_args(argv)
    manifest = None
    try:
        if args.apply_one and (not args.manifest_sha256 or not args.operator):
            raise ApprovalError("apply_arguments_required", "执行须同时指定 --apply-one、--manifest-sha256、--operator")
        manifest = load_manifest(args.manifest)
        if args.manifest_sha256 is not None and args.manifest_sha256 != manifest.sha256:
            raise ApprovalError("manifest_sha256_mismatch", "清单内容与批准 SHA256 不符")
        with SessionLocal() as db:
            if args.apply_one:
                result = apply_one(db, manifest, entry_id=args.apply_one,
                                   manifest_sha256=args.manifest_sha256, operator=args.operator)
            else:
                result = dry_run(db, manifest, operator=args.operator)
    except ApprovalError as exc:
        result = {
            "mode": "apply-one" if args.apply_one else "dry-run",
            "manifest_sha256": manifest.sha256 if manifest else None,
            "entry_id": args.apply_one, **exc.detail,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result.get("status") in {"conflict", "rejected"} or any(
        row["status"] in {"conflict", "rejected"} for row in result.get("entries", [])
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())
