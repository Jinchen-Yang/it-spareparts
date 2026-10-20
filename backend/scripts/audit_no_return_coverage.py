#!/usr/bin/env python3
"""D-32 coverage report. Reads DATABASE_URL; emits no credentials or DB writes."""

import argparse
import csv
import json
import sys

from sqlalchemy.exc import SQLAlchemyError

from app.db import engine
from app.services.maintenance_no_return_coverage import build_coverage_report


def _csv_value(value):
    if value is None or isinstance(value, (bool, dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, str) and (
        value.startswith(("\t", "\r", "\n"))
        or value.lstrip().startswith(("=", "+", "-", "@"))
    ):
        return "'" + value
    return value


def _write_csv(report):
    records = [
        {"record_type": "metadata", **report["metadata"]},
        {"record_type": "summary", **report["summary"]},
        *({"record_type": "project", **row} for row in report["projects"]),
        *({"record_type": "line", **row} for row in report["rows"]),
    ]
    fields = set().union(*(row.keys() for row in records))
    first = ["record_type", "project_id", "issue_id", "issue_line_id", "source"]
    writer = csv.DictWriter(sys.stdout, fieldnames=first + sorted(fields - set(first)))
    writer.writeheader()
    for row in records:
        writer.writerow({key: _csv_value(value) for key, value in row.items()})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="D-32 免返覆盖度只读报告")
    parser.add_argument("--project-id", action="append", default=[], help="可重复指定稳定项目 ID")
    parser.add_argument("--environment-label", default="unspecified", help="人工环境标签，不是连接串")
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    args = parser.parse_args(argv)
    try:
        report = build_coverage_report(
            engine, project_ids=tuple(args.project_id), environment_label=args.environment_label,
        )
    except (SQLAlchemyError, ValueError):
        print("覆盖报告读取失败；未输出连接详情，未执行任何业务写入。", file=sys.stderr)
        return 2
    if args.format == "csv":
        _write_csv(report)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
