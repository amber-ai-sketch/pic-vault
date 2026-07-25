#!/usr/bin/env python3
"""
dashboard_boot.py — localhost-only helper so file:// dashboard can start Web UI
when :8765 is not running.

Binds 127.0.0.1:8764 (not the LAN). Dashboard POSTs /api/web/start with the same
WORK the copy-command uses; this process runs `<repo>/picvault web start`.

Usage:
    python3 scripts/dashboard_boot.py
    # or: picvault boot start
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
PICVAULT_BIN = PROJECT_ROOT / 'picvault'

# Keep in sync with web_browse.ALLOWED_WORK_PREFIXES (Web itself rejects others).
ALLOWED_WORK_PREFIXES = ('/Volumes/Storage', '/Volumes/YM/MediaVault')

BOOT_HOST = '127.0.0.1'
BOOT_PORT = 8764

_START_LOCK = threading.Lock()


def validate_work(path_str: str) -> Path:
    if os.environ.get('PICVAULT_TEST') == '1':
        return Path(path_str).expanduser().resolve()
    p = Path(path_str).expanduser().resolve()
    for prefix in ALLOWED_WORK_PREFIXES:
        prefix_resolved = str(Path(prefix).resolve()) if Path(prefix).exists() else prefix
        # When volume is unmounted, resolve() may fail or differ; also accept raw prefix.
        candidates = {prefix, prefix_resolved}
        for cand in candidates:
            if str(p) == cand or str(p).startswith(str(cand).rstrip('/') + '/'):
                return p
    raise ValueError(
        f'work {path_str} is not in path whitelist.\n'
        f'  Allowed: {", ".join(ALLOWED_WORK_PREFIXES)}'
    )


def start_web(work: Path, port: int = 8765) -> dict:
    """Run `picvault web start` with WORK set. Returns a JSON-serializable result."""
    if not PICVAULT_BIN.is_file():
        return {'ok': False, 'error': f'picvault not found: {PICVAULT_BIN}'}
    env = os.environ.copy()
    env['WORK'] = str(work)
    try:
        proc = subprocess.run(
            [str(PICVAULT_BIN), 'web', 'start', '--port', str(port)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(PROJECT_ROOT),
        )
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'web start timed out'}
    except OSError as e:
        return {'ok': False, 'error': str(e)}

    out = ((proc.stdout or '') + (proc.stderr or '')).strip()
    if proc.returncode != 0:
        return {
            'ok': False,
            'error': out or f'web start exited {proc.returncode}',
            'rc': proc.returncode,
        }
    return {
        'ok': True,
        'work': str(work),
        'port': port,
        'message': out or 'Web UI started',
        'url': f'http://localhost:{port}/',
    }


class BootHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write('[boot] ' + (fmt % args) + '\n')

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._cors()
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/api/health', '/'):
            self._json(200, {
                'ok': True,
                'service': 'picvault-dashboard-boot',
                'project_root': str(PROJECT_ROOT),
            })
            return
        self._json(404, {'ok': False, 'error': 'not found'})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != '/api/web/start':
            self._json(404, {'ok': False, 'error': 'not found'})
            return

        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length > 0 else b'{}'
        try:
            data = json.loads(raw.decode('utf-8') or '{}')
        except json.JSONDecodeError:
            self._json(400, {'ok': False, 'error': 'invalid JSON'})
            return

        work_raw = (data.get('work') or '').strip()
        if not work_raw:
            self._json(400, {'ok': False, 'error': 'missing work'})
            return

        try:
            port = int(data.get('port') or 8765)
        except (TypeError, ValueError):
            self._json(400, {'ok': False, 'error': 'invalid port'})
            return
        if port < 1 or port > 65535:
            self._json(400, {'ok': False, 'error': 'invalid port'})
            return

        try:
            work = validate_work(work_raw)
        except ValueError as e:
            self._json(400, {'ok': False, 'error': str(e)})
            return

        if not _START_LOCK.acquire(blocking=False):
            self._json(409, {'ok': False, 'error': 'start already in progress'})
            return
        try:
            result = start_web(work, port=port)
        finally:
            _START_LOCK.release()

        self._json(200 if result.get('ok') else 500, result)


def main():
    parser = argparse.ArgumentParser(description='PicVault dashboard boot helper')
    parser.add_argument('--host', default=BOOT_HOST)
    parser.add_argument('--port', type=int, default=BOOT_PORT)
    args = parser.parse_args()

    # Refuse non-loopback binds — this helper can spawn processes.
    if args.host not in ('127.0.0.1', 'localhost', '::1'):
        print('ERROR: boot helper must bind loopback only', file=sys.stderr)
        sys.exit(1)

    server = ThreadingHTTPServer((args.host, args.port), BootHandler)
    print(f'✓ PicVault boot helper at http://{args.host}:{args.port}/')
    print(f'  Project: {PROJECT_ROOT}')
    print(f'  POST /api/web/start  {{"work":"/Volumes/...", "port":8765}}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nShutting down boot helper...')
        server.shutdown()


if __name__ == '__main__':
    main()
