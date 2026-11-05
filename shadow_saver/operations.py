"""Process health and bounded, privacy-preserving operational telemetry."""
import hmac
import json
import logging
import re
import time
from collections import defaultdict
from threading import Lock

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

_LOG = logging.getLogger(__name__)


def install_operations(app, db, config):
    """Install probes and read-only administration; never include request data."""
    _LOG.setLevel(logging.INFO)
    if not _LOG.hasHandlers():
        _LOG.addHandler(logging.StreamHandler())
    started = time.monotonic()
    token = config.get('OPS_STATUS_TOKEN') or ''
    revision = config.get('SOURCE_COMMIT') or ''
    revision = revision if re.fullmatch(r'[0-9a-f]{40}', revision) else None
    metrics = defaultdict(lambda: {'requests': 0, 'errors': 0, 'elapsed_ms_total': 0.0, 'elapsed_ms_max': 0.0})
    lock = Lock()

    def database_ready():
        try:
            with db() as connection:
                connection.execute('SELECT 1').fetchone()
            return True
        except Exception:
            return False

    @app.get('/healthz', include_in_schema=False)
    def health():
        return {'status': 'alive'}

    @app.get('/readyz', include_in_schema=False)
    def ready():
        ok = database_ready()
        return JSONResponse({'status': 'ready' if ok else 'unavailable'}, status_code=200 if ok else 503)

    @app.get('/ops/status', include_in_schema=False)
    def status(request: Request):
        if not token:
            raise HTTPException(404, 'Not found')
        authorization = request.headers.get('authorization', '')
        if not hmac.compare_digest(authorization.encode(), ('Bearer '+token).encode()):
            raise HTTPException(403, 'Forbidden')
        with lock:
            routes = [{'route': route, **values} for route, values in sorted(metrics.items())]
        return {'uptime_seconds': round(time.monotonic()-started, 3), 'source_commit': revision,
                'database': 'ready' if database_ready() else 'unavailable', 'routes': routes}

    # The outer application middleware catches failures before framework exception
    # reporting can serialize exception messages or a request URL.
    @app.middleware('http')
    async def telemetry(request, call_next):
        before = time.monotonic()
        category = None
        try:
            response = await call_next(request)
        except Exception:
            category = 'internal'
            response = JSONResponse({'detail': 'Internal server error'}, status_code=500)
        elapsed = round((time.monotonic()-before)*1000, 3)
        route = getattr(request.scope.get('route'), 'path', None) or '<unmatched>'
        code = response.status_code
        if category is None and code >= 400:
            category = 'server' if code >= 500 else 'client'
        with lock:
            entry = metrics[route]
            entry['requests'] += 1
            entry['errors'] += int(code >= 400)
            entry['elapsed_ms_total'] = round(entry['elapsed_ms_total']+elapsed, 3)
            entry['elapsed_ms_max'] = max(entry['elapsed_ms_max'], elapsed)
        _LOG.info(json.dumps({'event': 'http_request', 'route': route, 'status': code,
                              'elapsed_ms': elapsed, 'error_category': category}, separators=(',', ':')))
        return response
