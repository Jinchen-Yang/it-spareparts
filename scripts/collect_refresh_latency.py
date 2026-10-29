"""Read Docker access logs on the production host; emit aggregate metrics only.

Run over SSH stdin. Reads the local OpenAPI route metadata once, without calling
business endpoints, changing configuration, or exporting log lines, query
strings, identifiers or SQL parameters.
Unknown paths are grouped under a fixed placeholder, never printed verbatim.
"""
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
    ordered = sorted(values)
    return {
        "n": len(values),
        "p50_s": ordered[math.ceil(len(values) * 0.50) - 1],
        "p95_s": ordered[math.ceil(len(values) * 0.95) - 1],
        "max_s": ordered[-1],
        "total_s": round(sum(values), 3),
    }


def main():
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=7)
    # Existing OpenAPI metadata includes nested router prefixes and dynamically
    # registered endpoints. Static decorator scanning silently misses those.
    route_source = r'''
import json,urllib.request
with urllib.request.urlopen('http://127.0.0.1:8000/openapi.json',timeout=10) as response:
 print(json.dumps(sorted(json.load(response)['paths'])))
'''
    templates = json.loads(subprocess.check_output([
        "docker", "exec", "it-spareparts-app-r142", "python", "-c", route_source,
    ], text=True))
    routes = []
    for template in sorted(templates, key=lambda t: ("{" in t, -len(t))):
        pattern = "".join("[^/]+" if part.startswith("{") else re.escape(part)
                          for part in re.split(r"(\{[^}]+\})", template))
        routes.append((re.compile(pattern), template))
    safe_route = functools.lru_cache(maxsize=8192)(lambda path: normalize_path(path, routes))
    names = subprocess.check_output([
        "docker", "ps", "-a", "--format", "{{.Names}}",
    ], text=True).splitlines()
    names = [n for n in names if n.startswith("it-spareparts-frontend-")]
    containers = json.loads(subprocess.check_output(["docker", "inspect", *names]))
    request = re.compile(r'"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (\S+) HTTP/[^\"]+".*?status=(\d+).*?request_time=([\d.]+) upstream_time=([^\s]+)')
    grouped = collections.defaultdict(lambda: {"request": [], "upstream": [], "status": collections.Counter()})
    sources = []
    for container in containers:
        log = pathlib.Path(container["LogPath"])
        count = no_timing = excluded_chat = 0
        first = last = None
        for file in sorted(log.parent.glob(log.name + "*")):
            opener = gzip.open if file.suffix == ".gz" else open
            with opener(file, "rt", errors="replace") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                        # Docker uses nanoseconds; Python 3.10 accepts at most
                        # microseconds. Truncate explicitly, never drop 99.9%
                        # of requests just because their timestamp has 9 digits.
                        stamp = parse_timestamp(record["time"])
                    except (ValueError, KeyError):
                        continue
                    if not start <= stamp <= end:
                        continue
                    raw = record.get("log", "")
                    if ' /api/' not in raw:
                        continue
                    match = request.search(raw)
                    if not match:
                        no_timing += 1
                        continue
                    method, path, status, elapsed, upstream = match.groups()
                    if path.startswith(("/api/chat", "/api/agent", "/api/ai/")):
                        excluded_chat += 1
                        continue
                    route = safe_route(path.split("?", 1)[0])
                    key = (method, route)
                    group = grouped[key]
                    group["request"].append(float(elapsed))
                    if re.fullmatch(r"\d+(?:\.\d+)?", upstream):
                        group["upstream"].append(float(upstream))
                    group["status"][status] += 1
                    count += 1
                    first = min(first, stamp) if first else stamp
                    last = max(last, stamp) if last else stamp
        sources.append({"container": container["Name"].lstrip("/"), "requests": count,
                        "without_timing": no_timing, "excluded_chat": excluded_chat,
                        "first": first.isoformat() if first else None,
                        "last": last.isoformat() if last else None})
    endpoints = []
    for (method, path), group in grouped.items():
        endpoints.append({"method": method, "route": path,
                          **metrics(group["request"]), "status": dict(group["status"]),
                          "upstream": metrics(group["upstream"]) if group["upstream"] else None})
    endpoints.sort(key=lambda row: row["p95_s"], reverse=True)
    print(json.dumps({"window_start": start.isoformat(), "window_end": end.isoformat(),
                      "quantiles": "nearest-rank", "sources": sources,
                      "endpoints": endpoints}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
