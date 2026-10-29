"""The evidence collector must neither leak request values nor drop nanosecond logs."""
import importlib.util
import re
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "refresh_latency_collector",
    Path(__file__).resolve().parents[2] / "scripts" / "collect_refresh_latency.py",
)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


@pytest.mark.parametrize("fraction", ["", ".123", ".123456", ".123456789"])
def test_docker_timestamp_precision_does_not_drop_records(fraction):
    result = collector.parse_timestamp(f"2026-10-29T12:00:00{fraction}Z")
    assert result.isoformat().startswith("2026-10-29T12:00:00")
    assert result.utcoffset().total_seconds() == 0
    assert result.microsecond == (0 if not fraction else int(fraction[1:7].ljust(6, "0")))


def test_route_summary_excludes_identity_query_and_unmapped_path():
    routes = [(re.compile(r"/api/parts/[^/]+"), "/api/parts/{part_id}")]
    assert collector.normalize_path("/api/parts/private-pn?q=private-value", routes) == "/api/parts/{part_id}"
    assert collector.normalize_path("/api/unknown/private-pn?q=secret", routes) == "<unmapped-api>"


def test_nearest_rank_quantiles_retain_zero_and_slow_outlier():
    result = collector.metrics([0, 1, 2, 3, 40])
    assert result == {"n": 5, "p50_s": 2, "p95_s": 40, "max_s": 40, "total_s": 46}
