#!/usr/bin/env python3
"""
web_browse.py - Local Flask-free HTTP server to browse by-date/ with thumbnails + star.

Uses Python's built-in http.server (no Flask dep). Single file, single port.

Usage:
    ./web_browse.py --work /Volumes/Storage --host 0.0.0.0 --port 8765
"""

import argparse
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ALLOWED_WORK_PREFIXES = ('/Volumes/Storage', '/Volumes/YM/MediaVault')
THUMB_CACHE = '_meta/thumbs'

# Sibling scripts discovered at import time (so absolute paths are baked in)
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
PICVAULT_BIN = PROJECT_ROOT / 'picvault'
DEDUPE_SCRIPT = SCRIPT_DIR / 'dedupe.py'
RENAME_SCRIPT = SCRIPT_DIR / 'rename_organize.py'
SYNC_SCRIPT = SCRIPT_DIR / 'sync_to_backup.sh'
INIT_SCRIPT = SCRIPT_DIR / 'init_storage.sh'
BACKUP_DEFAULT = '/Volumes/WD4T/MediaVault'

# Whitelist of commands runnable via /api/run. Each value is a zero-arg
# callable that returns argv as a list. Args are SERVER-SIDE CONSTANTS -- no
# user-supplied paths flow into subprocess. Populated in main() once --work
# is resolved.
RUN_COMMANDS = {}


def validate_path(path_str: str, allowed_prefixes, kind: str) -> Path:
    p = Path(path_str).expanduser().resolve()
    for prefix in allowed_prefixes:
        prefix_resolved = str(Path(prefix).resolve())
        if str(p) == prefix_resolved or str(p).startswith(prefix_resolved + '/'):
            return p
    raise ValueError(
        f"--{kind} {path_str} is not in path whitelist.\n"
        f"  Allowed: {', '.join(allowed_prefixes)}"
    )


def gen_thumbnail(src: Path, dst: Path, size=320) -> bool:
    """Generate thumbnail using macOS sips (built-in)."""
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ['sips', '-Z', str(size), str(src), '--out', str(dst.parent)],
            capture_output=True, check=True, timeout=30
        )
        # sips preserves extension; we want .jpg
        # Move/rename if needed
        produced = dst.parent / src.name
        if produced.exists() and produced != dst:
            shutil.move(str(produced), str(dst))
        return dst.exists()
    except Exception as e:
        print(f"  [sips] {src}: {e}", file=sys.stderr)
        return False


def thumb_for(file_path: Path, work: Path, thumb_root: Path) -> Path:
    """Get thumbnail path for a file (generates if missing)."""
    rel = file_path.relative_to(work)
    thumb_path = thumb_root / rel.with_suffix('.jpg')
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    if thumb_path.exists():
        return thumb_path
    if gen_thumbnail(file_path, thumb_path):
        return thumb_path
    return None


def scan_buckets(work: Path) -> dict:
    """Scan by-date/ and screenshots/ for bucket info."""
    result = {'years': {}, 'screenshots_count': 0}

    by_date = work / 'by-date'
    if by_date.exists():
        for year_dir in sorted(by_date.iterdir()):
            if not year_dir.is_dir():
                continue
            year = year_dir.name
            months = []
            for month_dir in sorted(year_dir.iterdir()):
                if not month_dir.is_dir():
                    continue
                # Count files
                photo_count = sum(1 for _ in (month_dir / 'photos').rglob('*')
                                  if _.is_file()) if (month_dir / 'photos').exists() else 0
                video_count = sum(1 for _ in (month_dir / 'videos').rglob('*')
                                  if _.is_file()) if (month_dir / 'videos').exists() else 0
                # Determine if themed
                is_themed = '_' in month_dir.name
                theme_name = month_dir.name.split('_', 1)[1] if is_themed else ''
                months.append({
                    'name': month_dir.name,
                    'is_themed': is_themed,
                    'theme': theme_name,
                    'photos': photo_count,
                    'videos': video_count,
                })
            result['years'][year] = months

    screenshots = work / 'screenshots'
    if screenshots.exists():
        result['screenshots_count'] = sum(1 for f in screenshots.iterdir() if f.is_file())

    return result


def list_bucket(work: Path, year: str, month: str, theme: str = None) -> list[Path]:
    """List files in a specific month/theme bucket."""
    month_dir = work / 'by-date' / year / month
    if not month_dir.exists():
        return []
    files = []
    for bucket_type in ('photos', 'videos'):
        sub = month_dir / bucket_type
        if sub.exists():
            files.extend(f for f in sub.rglob('*') if f.is_file())
    return sorted(files)


def list_screenshots(work: Path) -> list[Path]:
    screenshots = work / 'screenshots'
    if not screenshots.exists():
        return []
    return sorted([f for f in screenshots.iterdir() if f.is_file()],
                  key=lambda f: f.stat().st_mtime, reverse=True)


def load_stars(work: Path, bucket: str) -> dict:
    """Load stars JSON, returning dict {rel_path: True}."""
    path = work / '_meta' / 'stars' / f"{bucket}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if isinstance(data, list):
            return {k: True for k in data}
        return {k: bool(v) for k, v in data.items() if v}
    except Exception:
        return {}


def save_stars(work: Path, bucket: str, stars: dict):
    path = work / '_meta' / 'stars' / f"{bucket}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: True for k in stars}, indent=2, ensure_ascii=False))


HTML_HEAD = '''<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>PicVault</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
  .item { background: #f5f5f5; border-radius: 8px; overflow: hidden; position: relative; }
  .item img { width: 100%; height: 180px; object-fit: cover; display: block; }
  .item video { width: 100%; height: 180px; object-fit: cover; display: block; background: #000; }
  .item .name { padding: 5px; font-size: 11px; word-break: break-all; }
  .item .star { position: absolute; top: 5px; right: 5px; background: rgba(0,0,0,0.5); color: #fff; border: none; width: 30px; height: 30px; border-radius: 50%; cursor: pointer; font-size: 16px; }
  .item .star.on { background: gold; color: #333; }
  h1, h2 { color: #333; }
  a { color: #0066cc; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .nav { margin-bottom: 20px; padding: 10px; background: #f0f0f0; border-radius: 4px; }
</style>
<script>
// Star/unstar on click. Uses fetch() to POST /api/star with path+bucket.
document.addEventListener('click', async function(e) {
  var btn = e.target.closest('.star');
  if (!btn) return;
  e.preventDefault();
  var path = btn.getAttribute('data-path');
  var bucket = btn.getAttribute('data-bucket');
  if (!path || !bucket) return;
  try {
    var r = await fetch('/api/star', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: path, bucket: bucket, action: 'toggle'})
    });
    var data = await r.json();
    if (data.ok) {
      if (data.starred) { btn.classList.add('on'); btn.textContent = '★'; }
      else { btn.classList.remove('on'); btn.textContent = '☆'; }
    } else {
      alert('Star failed: ' + (data.error || 'unknown'));
    }
  } catch (err) {
    alert('Network error: ' + err);
  }
});
</script>
</head><body>
<div class="nav">
  <a href="/">Home</a> |
  <a href="/screenshots">Screenshots</a> |
  <a href="/themes">Themes</a>
</div>
'''

HTML_FOOT = '</body></html>'


def render_home(work: Path) -> bytes:
    buckets = scan_buckets(work)
    parts = [HTML_HEAD.encode(), f'<h1>PicVault</h1><p>Work: {work}</p>'.encode()]
    parts.append(f'<h2>Years</h2><div class="grid">'.encode())
    for year in sorted(buckets['years'].keys(), reverse=True):
        months = buckets['years'][year]
        total_photos = sum(m['photos'] for m in months)
        total_videos = sum(m['videos'] for m in months)
        parts.append(
            f'<div class="item"><div class="name"><a href="/y/{year}"><b>{year}</b></a><br>'
            f'{len(months)} months<br>{total_photos} photos<br>{total_videos} videos</div></div>'.encode()
        )
    parts.append(b'</div>')
    if buckets['screenshots_count'] > 0:
        parts.append(
            f'<p><a href="/screenshots">→ {buckets["screenshots_count"]} screenshots</a></p>'.encode()
        )
    parts.append(HTML_FOOT.encode())
    return b''.join(parts)


def render_year(work: Path, year: str) -> bytes:
    months = list_bucket.__self__ if False else None  # placeholder
    by_date = work / 'by-date' / year
    if not by_date.exists():
        return (HTML_HEAD + f'<h1>{year}</h1><p>not found</p>' + HTML_FOOT).encode()
    months = []
    for m in sorted(by_date.iterdir()):
        if not m.is_dir():
            continue
        months.append(m)
    parts = [HTML_HEAD.encode(), f'<h1>{year}</h1><div class="grid">'.encode()]
    for m in months:
        photo_count = sum(1 for _ in (m/'photos').rglob('*') if _.is_file()) if (m/'photos').exists() else 0
        video_count = sum(1 for _ in (m/'videos').rglob('*') if _.is_file()) if (m/'videos').exists() else 0
        is_themed = '_' in m.name
        theme = m.name.split('_', 1)[1] if is_themed else ''
        parts.append(
            f'<div class="item"><div class="name"><a href="/y/{year}/{urllib.parse.quote(m.name)}"><b>{m.name}</b></a><br>'
            f'{"theme: "+theme if theme else "default bucket"}<br>{photo_count} photos<br>{video_count} videos</div></div>'.encode()
        )
    parts.append(b'</div>' + HTML_FOOT.encode())
    return b''.join(parts)


def render_bucket(work: Path, year: str, month: str, thumb_root: Path) -> bytes:
    bucket_name = month
    files = list_bucket(work, year, month)
    stars = load_stars(work, bucket_name)
    parts = [HTML_HEAD.encode(), f'<h1>{year}/{month}</h1>'.encode(),
             f'<p>{len(files)} files, {len(stars)} starred</p>'.encode(),
             b'<div class="grid">']
    for f in files:
        rel = str(f.relative_to(work))
        thumb = thumb_for(f, work, thumb_root)
        is_video = f.suffix.lower() in {'.mp4', '.mov', '.webm', '.m4v'}
        is_starred = rel in stars
        if thumb and thumb.exists():
            media = f'<a href="/raw?p={urllib.parse.quote(rel)}"><img src="/thumb?p={urllib.parse.quote(rel)}"></a>'
        elif is_video:
            media = f'<video src="/raw?p={urllib.parse.quote(rel)}" muted></video>'
        else:
            media = f'<a href="/raw?p={urllib.parse.quote(rel)}">view</a>'
        parts.append(
            f'<div class="item">{media}'
            f'<button class="star {"on" if is_starred else ""}" data-path="{urllib.parse.quote(rel)}" data-bucket="{bucket_name}">{"★" if is_starred else "☆"}</button>'
            f'<div class="name">{f.name}</div></div>'.encode()
        )
    parts.append(b'</div>' + HTML_FOOT.encode())
    return b''.join(parts)


def render_screenshots(work: Path, thumb_root: Path) -> bytes:
    files = list_screenshots(work)
    bucket_name = 'screenshots'
    stars = load_stars(work, bucket_name)
    parts = [HTML_HEAD.encode(), '<h1>Screenshots</h1>'.encode(),
             f'<p>{len(files)} files</p>'.encode(), b'<div class="grid">']
    for f in files:
        rel = str(f.relative_to(work))
        thumb = thumb_for(f, work, thumb_root)
        is_starred = rel in stars
        is_video = f.suffix.lower() in {'.mp4', '.mov', '.webm', '.m4v'}
        if thumb and thumb.exists():
            media = f'<a href="/raw?p={urllib.parse.quote(rel)}"><img src="/thumb?p={urllib.parse.quote(rel)}"></a>'
        elif is_video:
            media = f'<video src="/raw?p={urllib.parse.quote(rel)}" muted></video>'
        else:
            media = f'<a href="/raw?p={urllib.parse.quote(rel)}">view</a>'
        parts.append(
            f'<div class="item">{media}'
            f'<button class="star {"on" if is_starred else ""}" data-path="{urllib.parse.quote(rel)}" data-bucket="{bucket_name}">{"★" if is_starred else "☆"}</button>'
            f'<div class="name">{f.name}</div></div>'.encode()
        )
    parts.append(b'</div>' + HTML_FOOT.encode())
    return b''.join(parts)


# macOS / Spotlight / version-control noise to skip at directory level
_HIDDEN_DIR_NOISE = frozenset({
    '.DS_Store',  # not a dir, but include for safety
    '.Spotlight-V100', '.Trashes', '.fseventsd',
    '.git', '.idea', '.vscode', '.svn', '.hg',
    '__pycache__',
})


def count_files_in(dir_path: Path) -> int:
    """Count files under dir_path, skipping macOS noise and hidden-dir subtrees.

    Skipped:
      - any file named exactly .DS_Store
      - any path whose components include a hidden-dir-noise name
        (e.g. .git/, .Spotlight-V100/, .Trashes/)

    NOT skipped:
      - individual files whose name happens to start with '.'
        (e.g. Android '.trashed-<ts>-IMG_xxx.jpg', '.Screenshot_xxx.jpg').
        Old logic was too aggressive and reported 0 for such inboxes.
    """
    if not dir_path.exists():
        return 0
    try:
        n = 0
        rel_base = dir_path.resolve()
        for f in dir_path.rglob('*'):
            if not f.is_file():
                continue
            if f.name == '.DS_Store':
                continue
            try:
                parts = set(f.resolve().relative_to(rel_base).parts)
            except ValueError:
                # Path outside rel_base (symlink escape) -- skip conservatively
                continue
            if parts & _HIDDEN_DIR_NOISE:
                continue
            n += 1
        return n
    except Exception:
        return 0


def count_starred(work: Path) -> int:
    """Count total starred items across all buckets."""
    stars_dir = work / '_meta' / 'stars'
    if not stars_dir.exists():
        return 0
    total = 0
    for f in stars_dir.glob('*.json'):
        try:
            data = json.loads(f.read_text())
            if isinstance(data, dict):
                total += sum(1 for v in data.values() if v)
            elif isinstance(data, list):
                total += len(data)
        except Exception:
            pass
    return total


def last_sync_time(work: Path) -> str:
    """Get timestamp of most recent sync, or 'never'."""
    logs = work / '_meta' / 'logs'
    if not logs.exists():
        return 'never'
    sync_logs = sorted(logs.glob('sync-*.log'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not sync_logs:
        return 'never'
    import datetime
    mtime = sync_logs[0].stat().st_mtime
    return datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')


class Handler(BaseHTTPRequestHandler):
    work: Path = None
    thumb_root: Path = None

    def log_message(self, fmt, *args):
        pass  # quiet

    def do_OPTIONS(self):
        # CORS preflight from file:// dashboard
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        """Handle star/unstar JSON requests."""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        # Read body
        try:
            length = int(self.headers.get('Content-Length', 0))
            body_bytes = self.rfile.read(length) if length else b''
            data = json.loads(body_bytes.decode('utf-8')) if body_bytes else {}
        except Exception as e:
            self._send_json({'ok': False, 'error': f'bad json: {e}'})
            return
        qs = {**urllib.parse.parse_qs(parsed.query), **data}

        try:
            if path == '/api/star' and 'path' in qs and 'bucket' in qs:
                path_q = qs['path'][0] if isinstance(qs['path'], list) else qs['path']
                bucket = qs['bucket'][0] if isinstance(qs['bucket'], list) else qs['bucket']
                # Security: path must be under work
                full = (self.work / path_q).resolve()
                if not str(full).startswith(str(self.work.resolve())):
                    self._send_json({'ok': False, 'error': 'path outside work'})
                    return
                action = qs.get('action', ['toggle'])[0] if isinstance(qs.get('action'), list) else qs.get('action', 'toggle')
                stars = load_stars(self.work, bucket)
                if action == 'on':
                    stars[path_q] = True
                elif action == 'off':
                    stars.pop(path_q, None)
                else:
                    stars[path_q] = not stars.get(path_q, False)
                    if not stars[path_q]:
                        stars.pop(path_q, None)
                save_stars(self.work, bucket, stars)
                self._send_json({'ok': True, 'starred': path_q in stars})
                return
            elif path == '/api/run':
                # Body: {"command": "<whitelisted-name>"}
                # Args are server-side constants; never user-supplied.
                cmd_name = data.get('command') if isinstance(data, dict) else None
                if cmd_name not in RUN_COMMANDS:
                    self._send_json({
                        'ok': False,
                        'error': f'unknown command: {cmd_name!r}',
                        'allowed': sorted(RUN_COMMANDS.keys()),
                    })
                    return
                argv = RUN_COMMANDS[cmd_name]()
                cmd_str = ' '.join(argv)
                try:
                    proc = subprocess.run(
                        argv, capture_output=True, text=True, timeout=300
                    )
                    self._send_json({
                        'ok': proc.returncode == 0,
                        'rc': proc.returncode,
                        'command': cmd_str,
                        'stdout': proc.stdout,
                        'stderr': proc.stderr,
                    })
                except subprocess.TimeoutExpired:
                    self._send_json({'ok': False, 'command': cmd_str, 'error': 'timeout (300s)'})
                except FileNotFoundError as e:
                    self._send_json({'ok': False, 'command': cmd_str, 'error': f'not found: {e}'})
                except Exception as e:
                    self._send_json({'ok': False, 'command': cmd_str, 'error': str(e)})
                return
            else:
                self._send_json({'ok': False, 'error': 'unknown endpoint'})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        try:
            if path == '/' or path == '/index.html':
                body = render_home(self.work)
                self._send(body, 'text/html')
            elif path == '/screenshots':
                body = render_screenshots(self.work, self.thumb_root)
                self._send(body, 'text/html')
            elif path == '/api/status':
                # JSON status endpoint for dashboard polling
                self._send_json({
                    'running': True,
                    'work': str(self.work),
                    'inbox': count_files_in(self.work / 'inbox'),
                    'by_date': count_files_in(self.work / 'by-date'),
                    'screenshots': count_files_in(self.work / 'screenshots'),
                    'vlogs': count_files_in(self.work / '_vlogs'),
                    'trash': count_files_in(self.work / '_trash'),
                    'starred': count_starred(self.work),
                    'last_sync': last_sync_time(self.work),
                })
            elif path == '/themes':
                body = self._render_themes()
                self._send(body, 'text/html')
            elif re.match(r'^/y/\d{4}$', path):
                year = path.split('/')[2]
                body = render_year(self.work, year)
                self._send(body, 'text/html')
            elif re.match(r'^/y/\d{4}/.+$', path):
                parts = path.split('/')
                year = parts[2]
                month = urllib.parse.unquote(parts[3])
                body = render_bucket(self.work, year, month, self.thumb_root)
                self._send(body, 'text/html')
            elif path == '/raw':
                p = qs.get('p', [''])[0]
                self._send_raw(p)
            elif path == '/thumb':
                p = qs.get('p', [''])[0]
                self._send_thumb(p)
            elif path == '/api/star' and 'path' in qs and 'bucket' in qs:
                path_q = qs['path'][0] if isinstance(qs['path'], list) else qs['path']
                bucket = qs['bucket'][0] if isinstance(qs['bucket'], list) else qs['bucket']
                # Security: path must be under work
                full = (self.work / path_q).resolve()
                if not str(full).startswith(str(self.work.resolve())):
                    self._send_json({'ok': False, 'error': 'path outside work'})
                    return
                action = qs.get('action', ['toggle'])[0]
                stars = load_stars(self.work, bucket)
                if action == 'on':
                    stars[path_q] = True
                elif action == 'off':
                    stars.pop(path_q, None)
                else:
                    stars[path_q] = not stars.get(path_q, False)
                    if not stars[path_q]:
                        stars.pop(path_q, None)
                save_stars(self.work, bucket, stars)
                self._send_json({'ok': True, 'starred': path_q in stars})
            else:
                self._send(b'404 Not Found', 'text/plain', 404)
        except Exception as e:
            self._send(f'ERROR: {e}'.encode(), 'text/plain', 500)

    def _send(self, body: bytes, content_type='text/html', status=200):
        self.send_response(status)
        self.send_header('Content-Type', f'{content_type}; charset=utf-8')
        # Allow dashboard.html opened via file:// to call /api/* freely.
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj):
        body = json.dumps(obj).encode()
        self._send(body, 'application/json')

    def _send_raw(self, rel_path: str):
        """Serve file content. Path sandbox: must be under work."""
        if not rel_path:
            self._send(b'missing path', 'text/plain', 400); return
        full = (self.work / rel_path).resolve()
        if not str(full).startswith(str(self.work)):
            self._send(b'forbidden', 'text/plain', 403); return
        if not full.exists() or not full.is_file():
            self._send(b'not found', 'text/plain', 404); return
        ctype, _ = mimetypes.guess_type(str(full))
        self.send_response(200)
        self.send_header('Content-Type', ctype or 'application/octet-stream')
        self.send_header('Content-Length', str(full.stat().st_size))
        # Range support for video
        range_header = self.headers.get('Range')
        if range_header:
            m = re.match(r'bytes=(\d+)-(\d*)', range_header)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else full.stat().st_size - 1
                self.send_response(206)
                self.send_header('Content-Range', f'bytes {start}-{end}/{full.stat().st_size}')
                self.send_header('Content-Length', str(end - start + 1))
                self.end_headers()
                with open(full, 'rb') as f:
                    f.seek(start)
                    self.wfile.write(f.read(end - start + 1))
                return
        self.end_headers()
        with open(full, 'rb') as f:
            shutil.copyfileobj(f, self.wfile)

    def _send_thumb(self, rel_path: str):
        """Serve thumbnail."""
        if not rel_path:
            self._send(b'missing', 'text/plain', 400); return
        thumb = thumb_for(self.work / rel_path, self.work, self.thumb_root)
        if not thumb or not thumb.exists():
            self._send(b'no thumb', 'text/plain', 404); return
        self._send_raw(str(thumb.relative_to(self.work)))

    def _render_themes(self) -> bytes:
        path = self.work / '_meta' / 'events.yaml'
        if not path.exists():
            return (HTML_HEAD + '<h1>Themes</h1><p>No events.yaml found</p>' + HTML_FOOT).encode()
        text = path.read_text()
        # Crude themes section extract
        in_themes = False
        themes_text = []
        for line in text.split('\n'):
            if line.startswith('themes:'):
                in_themes = True
                continue
            if in_themes:
                if line and not line.startswith(' ') and not line.startswith('#'):
                    break
                themes_text.append(line)
        body = HTML_HEAD + '<h1>Themes</h1><pre>' + '\n'.join(themes_text) + '</pre>' + HTML_FOOT
        return body.encode()


def main():
    parser = argparse.ArgumentParser(description='PicVault web browser')
    parser.add_argument('--work', default='/Volumes/Storage')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()

    try:
        work = validate_path(args.work, ALLOWED_WORK_PREFIXES, 'work')
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    thumb_root = work / THUMB_CACHE
    thumb_root.mkdir(parents=True, exist_ok=True)

    Handler.work = work
    Handler.thumb_root = thumb_root

    # Build argv factories. Args are baked-in; no user input flows in.
    w = str(work)
    b = BACKUP_DEFAULT
    pb = str(PICVAULT_BIN)

    def _vlog(extra, dry=True):
        # helper for dry/apply variants
        pass  # placeholder; see explicit entries below

    RUN_COMMANDS.update({
        # --- read-only / status ---
        'status':       lambda: [pb, 'status'],
        'doctor':       lambda: [pb, 'doctor'],

        # --- init / web lifecycle ---
        'init':         lambda: ['bash', str(INIT_SCRIPT), '--work', w],
        'web_start':    lambda: [pb, 'web', 'start'],
        'web_stop':     lambda: [pb, 'web', 'stop'],

        # --- dedupe (dry-run by default; apply moves files) ---
        'dedupe_dry':   lambda: ['python3', str(DEDUPE_SCRIPT), '--work', w, '--dry-run'],
        'dedupe_apply': lambda: ['python3', str(DEDUPE_SCRIPT), '--work', w],

        # --- rename + organize ---
        'rename_dry':   lambda: ['python3', str(RENAME_SCRIPT), '--work', w, '--dry-run'],
        'rename_apply': lambda: ['python3', str(RENAME_SCRIPT), '--work', w],

        # --- sync to backup (rsync; apply really mirrors) ---
        'sync_verify':  lambda: ['bash', str(SYNC_SCRIPT), '--work', w, '--backup', b, '--verify', '--dry-run'],
        'sync_apply':   lambda: ['bash', str(SYNC_SCRIPT), '--work', w, '--backup', b, '--verify'],
    })

    # Try to get hostname for display
    # mDNS .local hostname is already returned by gethostname(); only append if missing
    hostname = socket.gethostname()
    if not hostname.endswith('.local'):
        hostname = hostname + '.local'
    url = f"http://{hostname}:{args.port}/"

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    # OSC 8 hyperlink: makes the URL clickable in iTerm2, Terminal.app 12.5+, WezTerm, etc.
    # Falls back to plain text in older terminals.
    OSC8_START = '\033]8;;'
    OSC8_END = '\033]8;;\033\\'
    print(f"✓ PicVault web at {OSC8_START}{url}{OSC8_END}{url}\033[0m")
    print(f"  Work: {work}")
    print(f"  Thumbnails: {thumb_root}")
    print(f"  Press Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == '__main__':
    main()
