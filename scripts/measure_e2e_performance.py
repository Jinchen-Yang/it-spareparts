#!/usr/bin/env python3
"""Read-only HTTP latency probe; token/response bodies are never written to reports."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import statistics
import sys
from datetime import datetime, timezone
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ENDPOINTS = {
    'parts_search': '/api/parts/search?q=SSD',
    'data_quality_issues': '/api/data-quality/issues',
    'sales_analytics_orders': '/api/sales/analytics/orders',
    'dashboard_sales': '/api/dashboard/sales',
    'dashboard_purchase_orders': '/api/dashboard/purchase-orders',
    'dashboard_part_ranking': '/api/dashboard/part-ranking',
}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', default='http://127.0.0.1:18601')
    p.add_argument('--token-file', type=Path, required=True)
    p.add_argument('--parallel', type=int, default=12)
    p.add_argument('--rounds', type=int, default=4)
    p.add_argument('--timeout', type=float, default=60)
    p.add_argument('--export', action='store_true', help='Also measure unfiltered XLSX separately')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--max-p95', type=float, help='Exit nonzero above this mixed-load p95')
    a = p.parse_args()
    if a.parallel < 1 or a.rounds < 1 or a.timeout <= 0:
        p.error('parallel, rounds and timeout must be positive')
    started_at = datetime.now(timezone.utc).isoformat()
    token = a.token_file.read_text().strip()

    def measure(item):
        name, path = item
        start = time.monotonic()
        size = 0
        status = None
        error = None
        try:
            req = Request(a.base.rstrip('/') + path, headers={'Authorization': 'Bearer ' + token})
            with urlopen(req, timeout=180 if name == 'orders_export_nofilter' else a.timeout) as r:
                status = r.status
                while chunk := r.read(1024 * 1024):
                    size += len(chunk)
        except HTTPError as e:
            status = e.code
            error = 'http_error'
        except (TimeoutError, URLError, OSError) as e:
            error = ('TimeoutError' if isinstance(e, TimeoutError)
                     or isinstance(getattr(e, 'reason', None), TimeoutError)
                     else type(e).__name__)
        return {'endpoint': name, 'seconds': round(time.monotonic()-start, 4),
                'status': status, 'error': error, 'bytes': size}

    def summary(rows):
        times = sorted(r['seconds'] for r in rows)
        return {'count': len(rows), 'p50': round(statistics.median(times),4),
                'p95': times[math.ceil(.95*len(times))-1], 'max': max(times),
                'http_504': sum(r['status']==504 for r in rows),
                'http_503': sum(r['status']==503 for r in rows),
                'client_timeouts': sum(r['error']=='TimeoutError' for r in rows),
                'errors': sum(r['error'] is not None for r in rows)}

    print('Measuring isolated requests (3 per endpoint)', file=sys.stderr, flush=True)
    isolated = [measure(item) for _ in range(3) for item in ENDPOINTS.items()]
    print(f'Measuring mixed load ({a.parallel} workers)', file=sys.stderr, flush=True)
    with ThreadPoolExecutor(max_workers=a.parallel) as executor:
        mixed = list(executor.map(measure, list(ENDPOINTS.items())*a.rounds))
    report = {'started_at': started_at, 'base': a.base, 'parallel': a.parallel, 'rounds': a.rounds,
              'read_timeout_seconds': a.timeout, 'paths': ENDPOINTS,
              'isolated': {name: summary([r for r in isolated if r['endpoint']==name]) for name in ENDPOINTS},
              'mixed': {'overall': summary(mixed), **{name: summary([r for r in mixed if r['endpoint']==name]) for name in ENDPOINTS}},
              'samples': {'isolated': isolated, 'mixed': mixed}}
    if a.export:
        report['export_read_timeout_seconds'] = 180
        print('Measuring unfiltered XLSX export', file=sys.stderr, flush=True)
        report['export'] = measure(('orders_export_nofilter', '/api/maintenance/orders/export'))
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('samples','paths')}, indent=2))
    if a.max_p95 and (report['mixed']['overall']['p95'] > a.max_p95 or report['mixed']['overall']['errors']):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
