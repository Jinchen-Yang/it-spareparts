"""Read Docker access logs on the production host; emit aggregate metrics only.

Requires explicit --app-container and --frontend-container allowlists; repeat
each option for multiple selected sources. Never discovers containers.
Reads OpenAPI route metadata without calling business endpoints, changing
configuration, or exporting log lines, query strings, identifiers or SQL parameters.
Unknown paths are grouped under a fixed placeholder, never printed verbatim.
"""
import argparse
import collections
import datetime as dt
import functools
import gzip
import json
import math
import pathlib
import re
import subprocess


def normalize_path(path, routes):
    path = path.split("?", 1)[0]
    for pattern, template in routes:
        if pattern.fullmatch(path):
            return template
    return "<unmapped-api>"


def parse_timestamp(value):
    # Docker emits nanoseconds; keep Python 3.10-compatible microseconds.
    stamp_text = re.sub(r"(\.\d{6})\d+", r"\1", value)
    return dt.datetime.fromisoformat(stamp_text.replace("Z", "+00:00"))


def metrics(values):
    if not values or any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("metrics require nonempty finite nonnegative durations")
    ordered = sorted(values)
    return {
        "n": len(values),
        "p50_s": ordered[math.ceil(len(values) * 0.50) - 1],
        "p95_s": ordered[math.ceil(len(values) * 0.95) - 1],
        "max_s": ordered[-1],
        "total_s": round(sum(values), 3),
    }


def compile_routes(templates):
    routes = []
    for template in sorted(templates, key=lambda t: ("{" in t, -len(t), t)):
        pattern = "".join("[^/]+" if part.startswith("{") else re.escape(part)
                          for part in re.split(r"(\{[^}]+\})", template))
        routes.append((re.compile(pattern), template))
    return routes


def _groups():
    return collections.defaultdict(lambda: {"request": [], "upstream": [], "status": collections.Counter()})


def _endpoints(grouped):
    endpoints = []
    for (method, path), group in grouped.items():
        endpoints.append({"method": method, "route": path,
                          **metrics(group["request"]), "status": dict(group["status"]),
                          "upstream": metrics(group["upstream"]) if group["upstream"] else None})
    return sorted(endpoints, key=lambda row: (-row["p95_s"], row["method"], row["route"]))


def _duration(value):
    if not re.fullmatch(r"\d+(?:\.\d+)?", value):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def aggregate_sources(source_records, routes, start, end):
    """Pure aggregation: source names and safe route templates are the only labels."""
    safe_route = functools.lru_cache(maxsize=8192)(lambda path: normalize_path(path, routes))
    request = re.compile(r'"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (\S+) HTTP/[^\"]+".*?status=(\d+).*?request_time=(\S+) upstream_time=(\S+)')
    combined = _groups()
    sources = []
    for name, records in source_records:
        grouped = _groups()
        count = no_timing = excluded_chat = 0
        first = last = None
        for record in records:
            try:
                stamp = parse_timestamp(record["time"])
            except (ValueError, KeyError, TypeError):
                continue
            if stamp.tzinfo is None or not start <= stamp <= end:
                continue
            raw = record.get("log", "")
            if not isinstance(raw, str) or ' /api/' not in raw:
                continue
            match = request.search(raw)
            if not match:
                no_timing += 1
                continue
            method, path, status, elapsed, upstream = match.groups()
            if path.startswith(("/api/chat", "/api/agent", "/api/ai/")):
                excluded_chat += 1
                continue
            elapsed, upstream = _duration(elapsed), _duration(upstream)
            if elapsed is None:
                no_timing += 1
                continue
            key = (method, safe_route(path.split("?", 1)[0]))
            for groups in (grouped, combined):
                group = groups[key]
                group["request"].append(elapsed)
                if upstream is not None:
                    group["upstream"].append(upstream)
                group["status"][status] += 1
            count += 1
            first = min(first, stamp) if first else stamp
            last = max(last, stamp) if last else stamp
        sources.append({"container": name, "requests": count,
                        "without_timing": no_timing, "excluded_chat": excluded_chat,
                        "first": first.isoformat() if first else None,
                        "last": last.isoformat() if last else None,
                        "endpoints": _endpoints(grouped)})
    return {"window_start": start.isoformat(), "window_end": end.isoformat(),
            "quantiles": "nearest-rank", "sources": sources,
            "endpoints": _endpoints(combined)}


def iter_log_records(log):
    for file in sorted(log.parent.glob(log.name + "*")):
        opener = gzip.open if file.suffix == ".gz" else open
        with opener(file, "rt", errors="replace") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield record


def _container_name(value):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", value):
        raise argparse.ArgumentTypeError("expected an explicit Docker container name or ID")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-container", action="append", required=True, type=_container_name,
                        help="OpenAPI source container; repeat for each selected app")
    parser.add_argument("--frontend-container", action="append", required=True, type=_container_name,
                        help="access-log source container; repeat for each selected frontend")
    args = parser.parse_args(argv)
    apps = list(dict.fromkeys(args.app_container))
    frontends = list(dict.fromkeys(args.frontend_container))
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=7)
    # Metadata includes nested routers and dynamically registered endpoints.
    route_source = r'''
import json,urllib.request
with urllib.request.urlopen('http://127.0.0.1:8000/openapi.json',timeout=10) as response:
 print(json.dumps(sorted(json.load(response)['paths'])))
'''
    templates = set()
    for name in apps:
        templates.update(json.loads(subprocess.check_output([
            "docker", "exec", name, "python", "-c", route_source,
        ], text=True)))
    # Read only the metadata needed for the explicitly selected sources.
    metadata = subprocess.check_output([
        "docker", "inspect", "--format", '{"Name": {{json .Name}}, "LogPath": {{json .LogPath}}}',
        *frontends,
    ], text=True)
    containers = [json.loads(line) for line in metadata.splitlines()]
    log_paths = {}
    for container in containers:
        log = pathlib.Path(container["LogPath"])
        if not container["LogPath"] or not log.is_absolute():
            raise ValueError("selected frontend has no absolute Docker log file")
        # A name and ID may resolve to the same container; count its logs once.
        log_paths[container["Name"].lstrip("/")] = log
    source_records = [(name, iter_log_records(log)) for name, log in log_paths.items()]
    print(json.dumps(aggregate_sources(source_records, compile_routes(templates), start, end),
                     ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
