"""The evidence collector must neither leak request values nor drop nanosecond logs."""
import datetime as dt
import importlib.util
import json
import re
from pathlib import Path
from unittest.mock import Mock

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


@pytest.mark.parametrize("args", [[], ["--app-container", "selected-app"],
                                  ["--frontend-container", "selected-frontend"]])
def test_explicit_app_and_frontend_sources_required_before_any_io(monkeypatch, args):
    docker = Mock(side_effect=AssertionError("must not touch Docker without both allowlists"))
    logs = Mock(side_effect=AssertionError("must not read logs without both allowlists"))
    monkeypatch.setattr(collector.subprocess, "check_output", docker)
    monkeypatch.setattr(collector, "iter_log_records", logs)
    with pytest.raises(SystemExit) as caught:
        collector.main(args)
    assert caught.value.code == 2
    docker.assert_not_called()
    logs.assert_not_called()


def test_main_reads_only_explicit_deduplicated_sources(monkeypatch, capsys):
    def docker_output(args, **kwargs):
        assert kwargs == {"text": True}
        if args[:2] == ["docker", "exec"]:
            assert args[2] in {"selected-app", "selected-app-older"}
            return json.dumps(["/api/parts/{part_id}"])
        assert args == ["docker", "inspect", "--format",
                        '{"Name": {{json .Name}}, "LogPath": {{json .LogPath}}}',
                        "selected-frontend", "selected-frontend-older"]
        return "\n".join(json.dumps({"Name": "/" + name, "LogPath": "/synthetic/" + name + ".log"})
                         for name in args[-2:])

    docker = Mock(side_effect=docker_output)
    logs = Mock(return_value=[])
    monkeypatch.setattr(collector.subprocess, "check_output", docker)
    monkeypatch.setattr(collector, "iter_log_records", logs)
    collector.main(["--app-container", "selected-app", "--app-container", "selected-app",
                    "--app-container", "selected-app-older",
                    "--frontend-container", "selected-frontend",
                    "--frontend-container", "selected-frontend",
                    "--frontend-container", "selected-frontend-older"])
    assert docker.call_count == 3
    assert logs.call_count == 2
    output = capsys.readouterr().out
    result = json.loads(output)
    assert [source["container"] for source in result["sources"]] == [
        "selected-frontend", "selected-frontend-older"]
    assert result["endpoints"] == []
    assert "/synthetic/" not in output


def _record(elapsed, path="/api/parts/private-pn?q=private-query", *, status=200, upstream="0.1"):
    return {"time": "2026-10-29T12:00:00.123456789Z",
            "log": f'192.0.2.17 "GET {path} HTTP/1.1" status={status} '
                   f'request_time={elapsed} upstream_time={upstream}'}


def _aggregate(sources):
    return collector.aggregate_sources(
        sources, collector.compile_routes(["/api/parts/{part_id}"]),
        dt.datetime(2026, 10, 29, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 10, 30, tzinfo=dt.timezone.utc))


def test_per_source_endpoints_remain_separate_from_selected_source_combination():
    result = _aggregate([
        ("selected-current", [_record(value) for value in [1, 2, 3, 4, 5]] +
         [_record(0, "/api/unmapped/private-business?secret=private-query")]),
        ("selected-older", [_record(100, status=504, upstream="-")]),
    ])
    current, older = result["sources"]
    current_part = next(row for row in current["endpoints"] if row["route"] == "/api/parts/{part_id}")
    older_part = older["endpoints"][0]
    combined = next(row for row in result["endpoints"] if row["route"] == "/api/parts/{part_id}")
    assert (current["requests"], older["requests"]) == (6, 1)
    assert (current_part["n"], current_part["p50_s"], current_part["p95_s"]) == (5, 3, 5)
    assert (older_part["n"], older_part["p50_s"], older_part["p95_s"]) == (1, 100, 100)
    assert (combined["n"], combined["p50_s"], combined["p95_s"], combined["total_s"]) == (6, 3, 100, 115)
    assert current_part["status"] == {"200": 5}
    assert older_part["status"] == {"504": 1}
    assert combined["status"] == {"200": 5, "504": 1}
    assert older_part["upstream"] is None and combined["upstream"]["n"] == 5
    assert current["endpoints"][1]["route"] == "<unmapped-api>"
    encoded = json.dumps(result, allow_nan=False)
    for private_value in ("private-pn", "private-query", "private-business", "192.0.2.17", "?secret="):
        assert private_value not in encoded


def test_invalid_durations_and_outside_window_do_not_corrupt_quantiles():
    records = [_record(value) for value in ["NaN", "inf", "-1", ".", "9" * 400]]
    records += [_record(0, upstream="NaN"), _record(2, upstream="-"),
                _record(90, "/api/chat/private-session")]
    outside = _record(9999)
    outside["time"] = "2026-10-28T12:00:00Z"
    result = _aggregate([("selected", records + [outside])])
    source = result["sources"][0]
    endpoint = result["endpoints"][0]
    assert source["requests"] == 2 and source["without_timing"] == 5 and source["excluded_chat"] == 1
    assert (endpoint["n"], endpoint["p50_s"], endpoint["p95_s"], endpoint["max_s"]) == (2, 0, 2, 2)
    assert endpoint["upstream"] is None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("values", [[], [-1], [float("nan")], [float("inf")]])
def test_metrics_reject_invalid_populations(values):
    with pytest.raises(ValueError, match="finite nonnegative"):
        collector.metrics(values)
