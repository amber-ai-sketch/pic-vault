#!/usr/bin/env python3
"""
web_browse.py - Local Flask-free HTTP server to browse by-date/ with thumbnails + star.

Uses Python's built-in http.server (no Flask dep). Single file, single port.

Usage:
    ./web_browse.py --work /Volumes/Storage --port 8765
    ./web_browse.py --work /Volumes/Storage --host 0.0.0.0 --port 8765  # LAN (explicit)
"""

import argparse
import email.utils
import html as html_lib
import json
import mimetypes
import os
import queue
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ALLOWED_WORK_PREFIXES = ('/Volumes/Storage', '/Volumes/YM/MediaVault', '/Users/ym/Downloads/pic-test')
ALLOWED_BACKUP_PREFIXES = ('/Volumes/WD4T/MediaVault', '/Volumes/YM/MediaVault')
THUMB_CACHE = '_meta/thumbs'
VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.m4v', '.3gp', '.hevc', '.webm'}
RUN_TIMEOUT_SEC = None  # 不超时；长任务实质不限时
# 仅当 RUN_TIMEOUT_SEC 为 None 时作为极长兜底；再设为 None 则完全无限
RUN_HARD_CAP_SEC = 7 * 24 * 3600
MAX_POST_BODY = 2 * 1024 * 1024  # 2 MiB
# Large galleries (screenshots etc.) paginate so HTML/DOM stay bounded.
GALLERY_PAGE_SIZE = 150
GALLERY_PAGE_SIZE_MAX = 500
MONTH_SEGMENT_RE = re.compile(r'^\d{4}-\d{2}(_[^/\\]+)?$')
# Star bucket names: alnum / . _ - / CJK (theme dirs like 2026-07_海南)
_STAR_BUCKET_SAFE_RE = re.compile(
    r'^[A-Za-z0-9._\-\u3400-\u9fff\uf900-\ufaff]+$'
)

# Sibling scripts discovered at import time (so absolute paths are baked in)
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
PICVAULT_BIN = PROJECT_ROOT / 'picvault'
DEDUPE_SCRIPT = SCRIPT_DIR / 'dedupe.py'
RENAME_SCRIPT = SCRIPT_DIR / 'rename_organize.py'
SYNC_SCRIPT = SCRIPT_DIR / 'sync_to_backup.sh'
INIT_SCRIPT = SCRIPT_DIR / 'init_storage.sh'
DASHBOARD_HTML = PROJECT_ROOT / 'outputs' / 'dashboard.html'
BACKUP_DEFAULT = '/Volumes/WD4T/MediaVault'


def dashboard_file_path() -> str:
    return str(DASHBOARD_HTML.resolve())


def dashboard_file_url() -> str:
    """file:// URL for outputs/dashboard.html (控制台)."""
    return DASHBOARD_HTML.resolve().as_uri()

EMPTY_EVENTS_YAML = """# 主题配置（rename_organize、Web、themes）
# 也可用：./scripts/add_theme.py --interactive
#
# 示例（复制下面块，去掉每行行首的「# 」后保存；文件夹会变成 by-date/2026/2026-07_海南/）：
#
# themes:
#   - name: 海南
#     # month 可省略：有 date_range.start 时自动 = 开始月；手写须与 start 同月
#     date_range:
#       start: 2026-07-10
#       end: 2026-07-18
#     sources:
#       - iphone
#       - canon
#   # 跨月区间：桶名固定用开始月（1 月拍的也进 2025-12_…）
#   # - name: 香港-深圳
#   #   date_range:
#   #     start: 2025-12-28
#   #     end: 2026-01-05
#   # 仅来源（当月只有一个主题时，命中 sources 的都进该桶；需写 month）：
#   - name: 夏令营
#     month: 2026-08
#     sources: [iphone]
#   # 或显式文件列表（优先级最高）：
#   # - name: 重要证件照
#   #   month: 2026-06
#   #   files:
#   #     - 20260601_100000_iphone_a3f2.heic
#
# 字段：name 必填；month（YYYY-MM）有 start 时可省略；date_range、sources、files 至少写一项
# date_range 可跨月；主题桶 = by-date/<开始年>/<开始月>_<名>/
# 保存只写配置，不搬文件。保存后复制试跑命令；确认无误再加 --yes。
# 单个主题只扫该主题；全量同步用 --all，风险更高。
# rename 只处理 inbox，不再自动全量同步主题

themes: []
"""

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import rename_organize as rename_mod  # noqa: E402

# Whitelist of commands runnable via /api/run. Each value is a zero-arg
# callable that returns argv as a list. Args are SERVER-SIDE CONSTANTS -- no
# user-supplied paths flow into subprocess. Populated in main() once --work
# is resolved.
RUN_COMMANDS = {}

# Single-user: at most one /api/run at a time
_RUN_LOCK = threading.Lock()
_ACTIVE_RUN = None  # dict meta or None
_ACTIVE_PROC = None  # subprocess.Popen or None
_CANCEL_REQUESTED = False

_RUN_ID_RE = re.compile(r'^[\w\-]+$')


def runs_dir(work: Path) -> Path:
    return work / '_meta' / 'logs' / 'runs'


def make_run_id(cmd_name: str) -> str:
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    safe = re.sub(r'[^\w\-]+', '_', str(cmd_name or 'run'))
    return f'{ts}-{safe}'


def persist_run_meta(work: Path, meta: dict) -> None:
    d = runs_dir(work)
    d.mkdir(parents=True, exist_ok=True)
    body = json.dumps(meta, indent=2, ensure_ascii=False) + '\n'
    (d / f"{meta['id']}.json").write_text(body, encoding='utf-8')
    (d / 'latest.json').write_text(body, encoding='utf-8')


def load_run_meta(work: Path, run_id: str):
    if not _RUN_ID_RE.match(run_id):
        return None
    path = runs_dir(work) / f'{run_id}.json'
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def load_latest_run_meta(work: Path):
    path = runs_dir(work) / 'latest.json'
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


_PIPELINE_APPLY_CMDS = ('dedupe_apply', 'rename_apply', 'sync_apply')


def pipeline_step_markers(work: Path) -> dict:
    """Latest successful Apply markers for dashboard steps 02/03/05.

    Scans persisted run logs under runs_dir (excluding latest.json). For each
    apply command, keeps the newest status==ok entry by finished_at then id.
    A successful one-shot ``pipeline`` run marks all three apply steps done
    (unless a newer individual apply exists for that step).
    Also flags running=true when _ACTIVE_RUN matches that command or pipeline.
    """
    markers = {
        name: {
            'done': False,
            'running': False,
            'finished_at': None,
            'id': None,
        }
        for name in _PIPELINE_APPLY_CMDS
    }
    d = runs_dir(work)
    best_pipeline = None  # (sort_key, meta)
    if d.is_dir():
        best = {}  # command_name -> (sort_key, meta)
        for path in d.glob('*.json'):
            if path.name == 'latest.json':
                continue
            try:
                meta = json.loads(path.read_text(encoding='utf-8'))
            except Exception:
                continue
            if not isinstance(meta, dict):
                continue
            if meta.get('status') != 'ok':
                continue
            cmd = meta.get('command_name')
            finished = meta.get('finished_at') or ''
            run_id = meta.get('id') or path.stem
            sort_key = (str(finished), str(run_id))
            if cmd == 'pipeline':
                if best_pipeline is None or sort_key > best_pipeline[0]:
                    best_pipeline = (sort_key, meta)
                continue
            if cmd not in markers:
                continue
            prev = best.get(cmd)
            if prev is None or sort_key > prev[0]:
                best[cmd] = (sort_key, meta)
        for cmd, (_, meta) in best.items():
            markers[cmd] = {
                'done': True,
                'running': False,
                'finished_at': meta.get('finished_at'),
                'id': meta.get('id'),
            }
        if best_pipeline is not None:
            pipe_key, pipe_meta = best_pipeline
            for cmd in markers:
                cur_finished = markers[cmd].get('finished_at') or ''
                cur_id = markers[cmd].get('id') or ''
                cur_key = (str(cur_finished), str(cur_id))
                if not markers[cmd]['done'] or pipe_key >= cur_key:
                    markers[cmd] = {
                        'done': True,
                        'running': False,
                        'finished_at': pipe_meta.get('finished_at'),
                        'id': pipe_meta.get('id'),
                    }

    with _RUN_LOCK:
        active = dict(_ACTIVE_RUN) if _ACTIVE_RUN else None
    if active and active.get('status') == 'running':
        cmd = active.get('command_name')
        if cmd in markers:
            markers[cmd]['running'] = True
        elif cmd == 'pipeline':
            for name in markers:
                markers[name]['running'] = True

    return markers


def effective_run_timeout_sec():
    """None => no timeout at all."""
    if RUN_TIMEOUT_SEC is not None:
        return RUN_TIMEOUT_SEC
    return RUN_HARD_CAP_SEC


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


def path_is_under(child: Path, root: Path) -> bool:
    """True if resolved child is root or a descendant of root."""
    try:
        child_r = child.resolve()
        root_r = root.resolve()
    except OSError:
        return False
    return child_r == root_r or str(child_r).startswith(str(root_r) + os.sep)


def safe_under_work(work: Path, rel: str):
    """Resolve work/rel; return Path if it stays under work, else None.

    Rejects empty, absolute, and any ``..`` path segments (same fence as /raw).
    """
    if not rel or not isinstance(rel, str):
        return None
    rel = rel.strip()
    if not rel:
        return None
    p = Path(rel)
    if p.is_absolute() or '..' in p.parts:
        return None
    if '\0' in rel:
        return None
    try:
        full = (work / rel).resolve()
    except OSError:
        return None
    if not path_is_under(full, work):
        return None
    return full


def is_safe_star_bucket(bucket: str) -> bool:
    """Reject path separators / traversal; allow alnum, ._- and CJK only."""
    if not bucket or not isinstance(bucket, str):
        return False
    if len(bucket) > 200:
        return False
    if '/' in bucket or '\\' in bucket or '..' in bucket:
        return False
    return bool(_STAR_BUCKET_SAFE_RE.fullmatch(bucket))


def resolve_stars_path(work: Path, bucket: str) -> Path:
    """Build ``_meta/stars/{bucket}.json`` and require it stays under stars dir."""
    if not is_safe_star_bucket(bucket):
        raise ValueError('invalid bucket')
    stars_dir = (work / '_meta' / 'stars').resolve()
    path = (stars_dir / f'{bucket}.json').resolve()
    if path.parent != stars_dir or not path_is_under(path, stars_dir):
        raise ValueError('invalid bucket')
    return path


def is_safe_month_segment(month: str) -> bool:
    """Month URL segment: YYYY-MM or YYYY-MM_<theme>; no separators / .."""
    if not month or not isinstance(month, str):
        return False
    if '/' in month or '\\' in month or '..' in month:
        return False
    return bool(MONTH_SEGMENT_RE.fullmatch(month))


def is_allowed_cors_origin(origin) -> bool:
    """Allow missing Origin, file:// (null), and localhost / 127.0.0.1 / ::1."""
    if origin is None or origin == '':
        return True
    if origin == 'null':
        return True
    try:
        parsed = urllib.parse.urlparse(origin)
    except Exception:
        return False
    if parsed.scheme not in ('http', 'https'):
        return False
    host = (parsed.hostname or '').lower()
    return host in ('localhost', '127.0.0.1', '::1')


def _is_jpeg_bytes(path: Path) -> bool:
    """True if path starts with JPEG SOI marker (ffd8ff)."""
    try:
        with open(path, 'rb') as f:
            return f.read(3) == b'\xff\xd8\xff'
    except OSError:
        return False


def gen_thumbnail(src: Path, dst: Path, size=320) -> bool:
    """Generate thumbnail: sips for images, ffmpeg frame extract for videos.

    Always write real JPEG bytes to dst (.jpg). Plain ``sips -Z`` keeps the
    source format (HEIC/PNG), so renaming to .jpg leaves browsers unable to
    decode the grid thumb — force ``-s format jpeg``.
    """
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix.lower() in VIDEO_EXTS:
            # Grab a frame near 1s (or start if shorter); scale to fit size.
            r = subprocess.run(
                [
                    'ffmpeg', '-hide_banner', '-loglevel', 'error',
                    '-ss', '1', '-i', str(src),
                    '-frames:v', '1', '-q:v', '3',
                    '-vf', f'scale={size}:{size}:force_original_aspect_ratio=decrease',
                    '-y', str(dst),
                ],
                capture_output=True, timeout=60,
            )
            if r.returncode != 0 or not dst.exists():
                # Retry from t=0 for very short clips
                r = subprocess.run(
                    [
                        'ffmpeg', '-hide_banner', '-loglevel', 'error',
                        '-i', str(src),
                        '-frames:v', '1', '-q:v', '3',
                        '-vf', f'scale={size}:{size}:force_original_aspect_ratio=decrease',
                        '-y', str(dst),
                    ],
                    capture_output=True, timeout=60,
                )
            return r.returncode == 0 and dst.exists() and _is_jpeg_bytes(dst)

        subprocess.run(
            [
                'sips', '-s', 'format', 'jpeg', '-Z', str(size),
                str(src), '--out', str(dst),
            ],
            capture_output=True, check=True, timeout=30,
        )
        # Older sips / odd paths may still write src.name beside dst.
        produced = dst.parent / src.name
        if produced.exists() and produced != dst:
            shutil.move(str(produced), str(dst))
        return dst.exists() and _is_jpeg_bytes(dst)
    except Exception as e:
        print(f"  [thumb] {src}: {e}", file=sys.stderr)
        return False


def thumb_for(file_path: Path, work: Path, thumb_root: Path) -> Path:
    """Get thumbnail path for a file (generates if missing).

    Hot path: existing valid JPEG thumb returns without mkdir. Parent dirs are
    created only inside gen_thumbnail when a new thumb is written.
    """
    # Resolve both sides so macOS /var → /private/var (and safe_under_work's
    # resolved path) still yields a stable rel under thumb_root.
    rel = file_path.resolve().relative_to(work.resolve())
    thumb_path = thumb_root / rel.with_suffix('.jpg')
    if thumb_path.exists():
        if _is_jpeg_bytes(thumb_path):
            return thumb_path
        # Stale cache: HEIC/PNG bytes saved as .jpg (pre-format-jpeg fix).
        try:
            thumb_path.unlink()
        except OSError:
            pass
    if gen_thumbnail(file_path, thumb_path):
        return thumb_path
    return None


_LIVE_STILL_EXTS = {'.heic', '.jpg', '.jpeg'}


def is_live_companion_mov(path: Path) -> bool:
    """True if path is a .mov sitting next to a same-stem still (Live Photo)."""
    if path.suffix.lower() != '.mov':
        return False
    if not is_user_media_file(path):
        return False
    stem = path.stem
    parent = path.parent
    for ext in _LIVE_STILL_EXTS:
        if (parent / f'{stem}{ext}').is_file():
            return True
    return False


def is_live_photo_still(path: Path) -> bool:
    """True if path is a still with a same-stem .mov companion (Live Photo)."""
    if path.suffix.lower() not in _LIVE_STILL_EXTS:
        return False
    return (path.parent / f'{path.stem}.mov').is_file()


def count_month_media(month_dir: Path) -> tuple[int, int, int]:
    """Count (photos, videos, lives) for one by-date month bucket.

    photos: user media under photos/, excluding Live companion .mov (gallery semantics).
    videos: user media under videos/.
    lives: Live Photo pairs (still with same-stem .mov); one unit each, not still+mov.
    Uses filesystem stem pairing only — no EXIF.
    """
    photo_count = 0
    live_count = 0
    photos_dir = month_dir / 'photos'
    if photos_dir.exists():
        for f in photos_dir.rglob('*'):
            if not is_user_media_file(f) or is_live_companion_mov(f):
                continue
            photo_count += 1
            if is_live_photo_still(f):
                live_count += 1
    video_count = 0
    videos_dir = month_dir / 'videos'
    if videos_dir.exists():
        video_count = sum(
            1 for f in videos_dir.rglob('*') if is_user_media_file(f)
        )
    return photo_count, video_count, live_count


def format_ledger_stats(photos: int, videos: int,
                        stars: int = 0, lives: int = 0) -> str:
    """Chinese ledger-stats line; omit zero star/Live Photo to keep rows readable."""
    parts = [f'照片 {photos}', f'视频 {videos}']
    if stars:
        parts.append(f'加星 {stars}')
    if lives:
        parts.append(f'实况 {lives}')
    return '｜'.join(parts)


def bucket_month_key(name: str) -> str:
    """YYYY-MM prefix of a by-date bucket folder name."""
    return name.split('_', 1)[0] if '_' in name else name


def bucket_display_name(name: str) -> str:
    """Short gallery/ledger title: theme name or YYYY-MM (not full folder)."""
    if '_' in name:
        return name.split('_', 1)[1]
    return name




# Short TTL caches for ThreadingHTTPServer (lock around dict mutations).
# Correct enough for a personal vault; dashboard polls every ~5s so 10–15s
# avoids full directory walks on every hit without feeling stale.
_CACHE_LOCK = threading.Lock()
_TOPBAR_CACHE_TTL = 15.0
_STATUS_COUNTS_TTL = 10.0
_TRASH_COUNT_TTL = 10.0
# work_key -> {'expires': float, 'sig': tuple, 'buckets': dict, 'star_n': int}
_topbar_cache: dict = {}
# work_key -> {'expires': float, 'sig': tuple, 'counts': dict}
_status_counts_cache: dict = {}
# work_key -> {'expires': float, 'sig': tuple, 'count': int}
_trash_count_cache: dict = {}

_TOPBAR_MTIME_ROOTS = (
    'by-date', 'screenshots', 'screenrecords', 'docs', 'things', '_meta/stars',
    '_meta/events.yaml',
)
_STATUS_MTIME_ROOTS = (
    'inbox', 'by-date', 'screenshots', 'screenrecords', 'docs', 'things',
    '_vlogs', '_trash', '_meta/stars',
)
_TRASH_MTIME_ROOTS = ('_trash',)


def _dir_mtime_sig(work: Path, roots: tuple) -> tuple:
    """Cheap invalidation hint from top-level dir mtimes (not a full tree walk)."""
    parts = []
    for name in roots:
        p = work.joinpath(*name.split('/'))
        try:
            parts.append(p.stat().st_mtime_ns if p.exists() else 0)
        except OSError:
            parts.append(0)
    return tuple(parts)


def clear_web_caches():
    """Drop in-memory topbar/status caches (tests / after bulk mutations)."""
    with _CACHE_LOCK:
        _topbar_cache.clear()
        _status_counts_cache.clear()
        _trash_count_cache.clear()


def count_configured_themes(work: Path) -> int:
    """Count valid themes from _meta/events.yaml for UI labels."""
    path = work / '_meta' / 'events.yaml'
    if not path.exists():
        return 0
    try:
        themes = rename_mod.parse_events_yaml_text(path.read_text(encoding='utf-8'))
        rename_mod.validate_events_themes(themes)
    except Exception:
        return 0
    return len(themes)


def scan_buckets(work: Path) -> dict:
    """Scan by-date/, screenshots/, screenrecords/, docs/, things/ for bucket info."""
    result = {
        'years': {},
        'screenshots_count': 0,
        'screenrecords_count': 0,
        'docs_count': 0,
        'things_count': 0,
        'themes_count': count_configured_themes(work),
    }

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
                photo_count, video_count, live_count = count_month_media(month_dir)
                # Stars JSON is keyed by month bucket name (e.g. 2024-07_海南).
                star_count = len(load_stars(work, month_dir.name))
                is_themed = '_' in month_dir.name
                theme_name = month_dir.name.split('_', 1)[1] if is_themed else ''
                months.append({
                    'name': month_dir.name,
                    'is_themed': is_themed,
                    'theme': theme_name,
                    'photos': photo_count,
                    'videos': video_count,
                    'stars': star_count,
                    'lives': live_count,
                })
            result['years'][year] = months

    screenshots = work / 'screenshots'
    if screenshots.exists():
        result['screenshots_count'] = sum(
            1 for f in screenshots.iterdir() if is_user_media_file(f)
        )
    screenrecords = work / 'screenrecords'
    if screenrecords.exists():
        result['screenrecords_count'] = sum(
            1 for f in screenrecords.iterdir() if is_user_media_file(f)
        )
    docs = work / 'docs'
    if docs.exists():
        result['docs_count'] = sum(
            1 for f in docs.iterdir() if is_user_media_file(f)
        )
    things = work / 'things'
    if things.exists():
        result['things_count'] = sum(
            1 for f in things.iterdir() if is_user_media_file(f)
        )

    return result


def get_topbar_stats(work: Path) -> tuple:
    """Cached (star_n, buckets) for page_shell jumps; TTL + mtime of work roots."""
    key = str(work.resolve()) if work.exists() else str(work)
    now = time.monotonic()
    sig = _dir_mtime_sig(work, _TOPBAR_MTIME_ROOTS)
    with _CACHE_LOCK:
        hit = _topbar_cache.get(key)
        if hit and hit['expires'] > now and hit['sig'] == sig:
            return hit['star_n'], hit['buckets']
    star_n = len(list_all_starred(work))
    buckets = scan_buckets(work)
    with _CACHE_LOCK:
        _topbar_cache[key] = {
            'expires': now + _TOPBAR_CACHE_TTL,
            'sig': sig,
            'buckets': buckets,
            'star_n': star_n,
        }
    return star_n, buckets


def get_cached_scan_buckets(work: Path) -> dict:
    """scan_buckets via topbar cache so home + page_shell share one walk."""
    _, buckets = get_topbar_stats(work)
    return buckets


def get_status_counts(work: Path) -> dict:
    """Cached count_files_in bundle for GET /api/status.

    Dashboard setInterval(pollStatus, 5000) would otherwise full-walk inbox/
    by-date/… on every poll. First call still computes; later hits within
    _STATUS_COUNTS_TTL reuse the counts (invalidated by TTL or root mtimes).
    """
    key = str(work.resolve()) if work.exists() else str(work)
    now = time.monotonic()
    sig = _dir_mtime_sig(work, _STATUS_MTIME_ROOTS)
    with _CACHE_LOCK:
        hit = _status_counts_cache.get(key)
        if hit and hit['expires'] > now and hit['sig'] == sig:
            return dict(hit['counts'])
    counts = {
        'inbox': count_files_in(work / 'inbox'),
        'by_date': count_files_in(work / 'by-date'),
        'screenshots': count_files_in(work / 'screenshots'),
        'screenrecords': count_files_in(work / 'screenrecords'),
        'docs': count_files_in(work / 'docs'),
        'things': count_files_in(work / 'things'),
        'vlogs': count_files_in(work / '_vlogs'),
        'trash': count_files_in(work / '_trash'),
        'starred': count_starred(work),
    }
    with _CACHE_LOCK:
        _status_counts_cache[key] = {
            'expires': now + _STATUS_COUNTS_TTL,
            'sig': sig,
            'counts': counts,
        }
    return dict(counts)


def get_trash_count(work: Path) -> int:
    """Cached count for the home trash card without walking every folder."""
    key = str(work.resolve()) if work.exists() else str(work)
    now = time.monotonic()
    sig = _dir_mtime_sig(work, _TRASH_MTIME_ROOTS)
    with _CACHE_LOCK:
        hit = _trash_count_cache.get(key)
        if hit and hit['expires'] > now and hit['sig'] == sig:
            return int(hit['count'])
    count = count_files_in(work / '_trash')
    with _CACHE_LOCK:
        _trash_count_cache[key] = {
            'expires': now + _TRASH_COUNT_TTL,
            'sig': sig,
            'count': count,
        }
    return int(count)

def list_bucket(work: Path, year: str, month: str, theme: str = None) -> list[Path]:
    """List files in a specific month/theme bucket.

    Live Photo companion .mov files (same stem as a still in photos/) are
    omitted so the gallery shows one cell per Live Photo.
    """
    month_dir = work / 'by-date' / year / month
    if not month_dir.exists():
        return []
    files = []
    for bucket_type in ('photos', 'videos'):
        sub = month_dir / bucket_type
        if sub.exists():
            files.extend(f for f in sub.rglob('*') if is_user_media_file(f))
    files = [f for f in files if not is_live_companion_mov(f)]
    return sorted(files)


def list_screenshots(work: Path) -> list[Path]:
    screenshots = work / 'screenshots'
    if not screenshots.exists():
        return []
    return sorted([f for f in screenshots.iterdir() if is_user_media_file(f)],
                  key=lambda f: f.stat().st_mtime, reverse=True)


def list_screenrecords(work: Path) -> list[Path]:
    screenrecords = work / 'screenrecords'
    if not screenrecords.exists():
        return []
    return sorted([f for f in screenrecords.iterdir() if is_user_media_file(f)],
                  key=lambda f: f.stat().st_mtime, reverse=True)


def list_docs(work: Path) -> list[Path]:
    docs = work / 'docs'
    if not docs.exists():
        return []
    return sorted([f for f in docs.iterdir() if is_user_media_file(f)],
                  key=lambda f: f.stat().st_mtime, reverse=True)


def list_things(work: Path) -> list[Path]:
    things = work / 'things'
    if not things.exists():
        return []
    return sorted([f for f in things.iterdir() if is_user_media_file(f)],
                  key=lambda f: f.stat().st_mtime, reverse=True)


def list_all_starred(work: Path) -> list[tuple]:
    """All starred files that still exist: [(Path, bucket_name), ...] by mtime desc."""
    stars_dir = work / '_meta' / 'stars'
    if not stars_dir.exists():
        return []
    items = []
    seen = set()
    for jf in sorted(stars_dir.glob('*.json')):
        bucket = jf.stem
        for rel in load_stars(work, bucket):
            if rel in seen:
                continue
            full = work / rel
            if full.is_file():
                items.append((full, bucket, rel))
                seen.add(rel)
    items.sort(key=lambda t: t[0].stat().st_mtime, reverse=True)
    return items


def list_gallery_entries(work: Path, kind: str, year: str = None,
                         month: str = None) -> list[tuple]:
    """Gallery entries as [(Path, star_bucket), ...] for paginated render/API.

    ``kind``: bucket | screenshots | screenrecords | docs | things | starred
    """
    if kind == 'bucket':
        if not year or not month:
            return []
        return [(f, month) for f in list_bucket(work, year, month)]
    if kind == 'screenshots':
        return [(f, 'screenshots') for f in list_screenshots(work)]
    if kind == 'screenrecords':
        return [(f, 'screenrecords') for f in list_screenrecords(work)]
    if kind == 'docs':
        return [(f, 'docs') for f in list_docs(work)]
    if kind == 'things':
        return [(f, 'things') for f in list_things(work)]
    if kind == 'starred':
        return [(path, bucket) for path, bucket, _rel in list_all_starred(work)]
    return []


def gallery_stars_map(work: Path, kind: str, entries: list[tuple]) -> dict:
    """Star lookup for gallery cells (rel_path → True)."""
    if kind == 'starred':
        return {str(path.relative_to(work)): True for path, _bucket in entries}
    if not entries:
        return {}
    bucket = entries[0][1]
    return load_stars(work, bucket)


def clamp_gallery_limit(raw_limit) -> int:
    try:
        n = int(raw_limit)
    except (TypeError, ValueError):
        return GALLERY_PAGE_SIZE
    if n < 1:
        return GALLERY_PAGE_SIZE
    return min(n, GALLERY_PAGE_SIZE_MAX)


def clamp_gallery_offset(raw_offset, total: int) -> int:
    try:
        n = int(raw_offset)
    except (TypeError, ValueError):
        return 0
    if n < 0:
        return 0
    return min(n, max(total, 0))


def load_stars(work: Path, bucket: str) -> dict:
    """Load stars JSON, returning dict {rel_path: True}."""
    try:
        path = resolve_stars_path(work, bucket)
    except ValueError:
        return {}
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            entries = [k for k, v in data.items() if v]
        else:
            return {}
        stars = {}
        work_res = work.resolve()
        for rel in entries:
            if not isinstance(rel, str):
                continue
            full = safe_under_work(work, rel)
            if full is None or not full.is_file():
                continue
            try:
                rel_norm = str(full.resolve().relative_to(work_res))
            except (OSError, ValueError):
                continue
            stars[rel_norm] = True
        return stars
    except Exception:
        return {}


def save_stars(work: Path, bucket: str, stars: dict):
    path = resolve_stars_path(work, bucket)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: True for k in stars}, indent=2, ensure_ascii=False))
    clear_web_caches()


def migrate_star_path(work: Path, old_rel: str, new_rel: str):
    """If old_rel was starred, move the star entry to new_rel's bucket."""
    old_bucket = rename_mod.star_bucket_for_rel(old_rel)
    new_bucket = rename_mod.star_bucket_for_rel(new_rel)
    stars = load_stars(work, old_bucket)
    if old_rel not in stars:
        # Also search other star files in case bucket guess was wrong
        stars_dir = work / '_meta' / 'stars'
        if stars_dir.exists():
            for f in stars_dir.glob('*.json'):
                bucket = f.stem
                s = load_stars(work, bucket)
                if old_rel in s:
                    s.pop(old_rel, None)
                    save_stars(work, bucket, s)
                    ns = load_stars(work, new_bucket)
                    ns[new_rel] = True
                    save_stars(work, new_bucket, ns)
                    return
        return
    stars.pop(old_rel, None)
    save_stars(work, old_bucket, stars)
    ns = load_stars(work, new_bucket)
    ns[new_rel] = True
    save_stars(work, new_bucket, ns)


def remove_star_path(work: Path, rel: str):
    """Remove a path from whatever stars JSON it appears in."""
    bucket = rename_mod.star_bucket_for_rel(rel)
    stars = load_stars(work, bucket)
    if rel in stars:
        stars.pop(rel, None)
        save_stars(work, bucket, stars)
        return
    stars_dir = work / '_meta' / 'stars'
    if not stars_dir.exists():
        return
    for f in stars_dir.glob('*.json'):
        s = load_stars(work, f.stem)
        if rel in s:
            s.pop(rel, None)
            save_stars(work, f.stem, s)


def trash_paths(work: Path, paths: list) -> list:
    """Move selected files into _trash/<batch>/<original-rel>. Soft delete.

    Returns list of {ok, src, dest, error?}.
    """
    batch = datetime.now().strftime('%Y%m%d-%H%M%S')
    work_res = work.resolve()
    results = []
    for rel in paths:
        rel = str(rel).lstrip('/')
        item = {'ok': False, 'src': rel, 'dest': None}
        try:
            src = (work / rel).resolve()
            if not str(src).startswith(str(work_res) + os.sep) and src != work_res:
                item['error'] = 'path outside work'
                results.append(item)
                continue
            # Refuse deleting from _trash / _meta themselves
            top = Path(rel).parts[0] if Path(rel).parts else ''
            if top in ('_trash', '_meta'):
                item['error'] = f'cannot trash from {top}/'
                results.append(item)
                continue
            if not src.is_file():
                item['error'] = 'not a file'
                results.append(item)
                continue
            dest = work / '_trash' / batch / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                stem, ext = dest.stem, dest.suffix
                n = 1
                while True:
                    cand = dest.parent / f'{stem}_{n}{ext}'
                    if not cand.exists():
                        dest = cand
                        break
                    n += 1
            shutil.move(str(src), str(dest))
            remove_star_path(work, rel)
            item['ok'] = True
            item['dest'] = str(dest.relative_to(work))
        except Exception as e:
            item['error'] = str(e)
        results.append(item)
    if any(item.get('ok') for item in results):
        clear_web_caches()
    return results


# —— Browse UI (aligned with dashboard: pure white / black / soft gray) ——


def _esc(s) -> str:
    return html_lib.escape(str(s), quote=True)


PAGE_CSS = '''
:root {
  --paper: #FFFFFC;
  --mist: #F4F2EC;
  --ink: #11100E;
  --muted: #66625B;
  --line: #DDD9CF;
  --live: #236F4C;
  --warn: #886318;
  --hazard: #8E2F28;
  --bg: var(--paper);
  --soft: var(--mist);
  --sans: "PingFang SC", "Hiragino Sans GB", -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
  --text-xs: 0.75rem;
  --text-sm: 0.875rem;
  --text-md: 1rem;
}
* { box-sizing: border-box; }
html {
  max-width: 100%;
  overflow-x: hidden;
  overflow-x: clip;
  scroll-behavior: smooth;
}
  body {
  margin: 0;
  min-height: 100vh;
  font-family: var(--sans);
  color: var(--ink);
  line-height: 1.5;
  background: var(--paper);
  max-width: 100%;
  overflow-x: hidden;
  overflow-x: clip;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--ink); text-decoration: none; }
a:hover { text-decoration: underline; text-underline-offset: 3px; }
:focus-visible { outline: 1px solid var(--ink); outline-offset: 3px; }

.wrap { width: min(100%, 1120px); margin: 0 auto; padding: 28px clamp(22px, 2.6vw, 32px) 72px; }

.brand-mark {
  font-family: var(--sans);
  font-style: normal;
  font-weight: 500;
  font-size: 1.05rem;
  letter-spacing: -0.04em;
  color: var(--ink);
  text-decoration: none;
  flex-shrink: 0;
  line-height: 1;
}
.brand-mark:hover { text-decoration: none; opacity: 0.7; }

.topbar {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 12px 24px;
  align-items: center;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--line);
  margin-bottom: 28px;
}
.topbar-center {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: center;
  text-align: center;
}
.crumbs {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: 4px 2px;
  min-width: 0;
}
.crumbs a, .crumbs span {
  font-size: var(--text-xs);
  color: var(--muted);
  text-decoration: none;
  padding: 2px 4px;
}
.crumbs a:hover { color: var(--ink); text-decoration: none; background: var(--mist); }
.crumbs a.here { color: var(--ink); font-weight: 500; }
.crumbs .sep { color: var(--muted); user-select: none; padding: 2px 0; }

.jumps {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 18px;
  align-items: center;
  justify-content: flex-end;
}
.jumps a, .jumps-more > summary {
  font-size: var(--text-xs);
  color: var(--muted);
  text-decoration: none;
  letter-spacing: 0.01em;
}
.jumps a:hover { color: var(--ink); text-decoration: none; }
.jumps a .n {
  font-family: var(--mono);
  font-size: var(--text-xs);
  color: var(--muted);
}
.jumps a#consoleLink,
.jumps-more > summary { font-weight: 500; color: var(--ink); }
.jumps-more {
  position: relative;
}
.jumps-more > summary {
  list-style: none;
  cursor: pointer;
  padding: 2px 0;
  user-select: none;
}
.jumps-more > summary::-webkit-details-marker { display: none; }
.jumps-more > summary:hover { color: var(--ink); }
.jumps-more-panel {
  position: absolute;
  right: 0;
  top: calc(100% + 8px);
  z-index: 30;
  min-width: 13rem;
  padding: 10px 0;
  background: var(--paper);
  border: 1px solid var(--line);
  box-shadow: 0 8px 24px rgba(0,0,0,0.06);
  display: flex;
  flex-direction: column;
  gap: 0;
}
.jumps-more-panel a {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 9px 14px;
  color: var(--muted);
  text-decoration: none;
  white-space: nowrap;
}
.jumps-more-panel .jump-name { color: var(--ink); font-weight: 500; }
.jumps-more-panel .n { font-family: var(--mono); font-size: var(--text-xs); color: var(--muted); }
.jumps-more-panel a:hover {
  background: var(--mist);
  color: var(--ink);
  text-decoration: none;
}

.page-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px 24px;
  margin-bottom: 22px;
}
.page-title {
  font-family: var(--sans);
  font-weight: 500;
  font-size: clamp(1.55rem, 2.8vw, 2rem);
  letter-spacing: -0.045em;
  margin: 0;
  line-height: 1.1;
  color: var(--ink);
}
.page-lede {
  margin: 8px 0 0;
  font-size: var(--text-sm);
  color: var(--muted);
  max-width: 36em;
}
.page-meta {
  font-family: var(--sans);
  font-size: var(--text-sm);
  color: var(--muted);
  letter-spacing: 0;
  max-width: 40em;
}
.section-label {
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--muted);
  margin: 0 0 14px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}

.home-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 14px;
}
.home-card {
  min-height: 150px;
  border: 1px solid var(--line);
  background: var(--paper);
  color: var(--ink);
  text-decoration: none;
  padding: 18px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  transition: border-color .14s ease, transform .14s ease, box-shadow .14s ease;
}
.home-card[href]:hover {
  border-color: var(--ink);
  transform: translateY(-1px);
  box-shadow: 0 8px 22px rgba(0,0,0,0.05);
  text-decoration: none;
}
.home-card-primary {
  grid-column: span 2;
  background: var(--ink);
  color: #fff;
}
.home-card-disabled {
  background: var(--mist);
  color: var(--muted);
}
.home-card-top {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
}
.home-card-name {
  font-size: var(--text-sm);
  font-weight: 600;
  letter-spacing: -0.02em;
}
.home-card-count {
  font-family: var(--mono);
  font-size: var(--text-xs);
  color: var(--muted);
  white-space: nowrap;
}
.home-card-primary .home-card-count { color: rgba(255,255,255,0.72); }
.home-card-desc {
  margin: auto 0 0;
  font-size: var(--text-sm);
  line-height: 1.55;
  color: var(--muted);
}
.home-card-primary .home-card-desc { color: rgba(255,255,255,0.76); }
.home-card-meta {
  font-family: var(--mono);
  font-size: var(--text-xs);
  color: var(--muted);
  line-height: 1.55;
}
.home-card-primary .home-card-meta { color: rgba(255,255,255,0.72); }
.home-card-go {
  align-self: flex-end;
  font-size: 1.4rem;
  line-height: 1;
  color: inherit;
}
@media (max-width: 700px) {
  .home-card-primary { grid-column: span 1; }
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 18px;
  padding: 10px 0 12px;
  background: var(--paper);
  border: none;
  border-bottom: 1px solid var(--line);
  border-radius: 0;
  position: sticky;
  top: 0;
  z-index: 10;
}
.toolbar .count {
  font-family: var(--sans);
  font-size: var(--text-xs);
  color: var(--muted);
  margin-right: auto;
  letter-spacing: 0;
}
.toolbar-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
}
.toolbar-organize {
  display: none;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  width: 100%;
  padding-top: 10px;
  margin-top: 6px;
  border-top: 1px solid var(--line);
}
body.select-mode .toolbar-organize { display: flex; }
#selectModeBtn.on {
  color: var(--ink);
  box-shadow: inset 0 -2px 0 var(--ink);
}
.chip {
  font-family: var(--sans);
  font-weight: 500;
  font-size: var(--text-xs);
  padding: 5px 10px 7px;
  border: none;
  border-radius: 0;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  transition: color .12s, box-shadow .12s;
  box-shadow: inset 0 -2px 0 transparent;
}
.chip:hover { color: var(--ink); background: transparent; }
.chip.on {
  background: transparent;
  color: var(--ink);
  box-shadow: inset 0 -2px 0 var(--ink);
}
.chip.on .n { color: var(--muted); }
.chip .n {
  font-family: var(--mono);
  font-size: var(--text-xs);
  color: var(--muted);
  margin-left: 4px;
}
.btn-reclass,
.btn-bulk-star {
  font-family: var(--sans);
  font-weight: 500;
  font-size: var(--text-xs);
  padding: 5px 12px;
  border: none;
  border-radius: 0;
  background: transparent;
  color: var(--ink);
  cursor: pointer;
  transition: background .12s;
}
.btn-reclass:hover,
.btn-bulk-star:hover { background: var(--paper); }
.btn-reclass:disabled,
.btn-bulk-star:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-trash {
  font-family: var(--sans);
  font-weight: 500;
  font-size: var(--text-xs);
  padding: 5px 12px;
  border: 1px solid var(--hazard);
  border-radius: 0;
  background: transparent;
  color: var(--hazard);
  cursor: pointer;
  transition: background .12s, color .12s;
}
.btn-trash:hover { background: var(--hazard); color: #fff; }
.btn-trash:disabled { opacity: 0.4; cursor: not-allowed; }
.sel-count {
  font-family: var(--mono);
  font-size: var(--text-xs);
  color: var(--ink);
  min-width: 4.5em;
}
.toolbar .filter-tip {
  font-family: var(--sans);
  font-size: var(--text-xs);
  color: var(--muted);
  margin-left: 6px;
  letter-spacing: 0.02em;
}
.toolbar .filter-tip::before { content: '说明：'; }
.toolbar .review-tip {
  font-size: var(--text-xs);
  color: var(--muted);
  width: 100%;
  margin: 2px 0 0;
}
.toolbar .review-tip kbd {
  font-family: var(--mono);
  font-size: 0.7rem;
  border: 1px solid var(--line);
  background: var(--mist);
  padding: 1px 5px;
}
.gallery-more {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  margin: 28px 0 48px;
}
.gallery-more-tip {
  font-family: var(--sans);
  font-size: var(--text-xs);
  color: var(--muted);
  margin: 0;
  letter-spacing: 0.02em;
}
.btn-more {
  font-family: var(--sans);
  font-weight: 500;
  font-size: var(--text-sm);
  padding: 10px 28px;
  border: 1px solid var(--line);
  border-radius: 0;
  background: transparent;
  color: var(--ink);
  cursor: pointer;
  transition: background .12s, border-color .12s;
}
.btn-more:hover { background: var(--paper); border-color: var(--ink); }
.btn-more.busy { opacity: 0.45; pointer-events: none; }

.ledger {
  border-top: 1px solid var(--line);
  background: transparent;
  overflow: hidden;
}
.ledger-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 4px 18px;
  align-items: start;
  padding: 18px 4px;
  border-bottom: 1px solid var(--line);
  text-decoration: none;
  color: inherit;
  transition: background .12s;
}
.ledger-row > * { min-width: 0; }
.ledger-row:hover {
  background: var(--mist);
  text-decoration: none;
}
.ledger-row.is-empty {
  opacity: 0.55;
}
.ledger-row.is-empty:hover {
  opacity: 0.85;
}
.ledger-row:hover .ledger-key { font-weight: 400; letter-spacing: -0.03em; }
.ledger-row > a.ledger-key {
  text-decoration: none;
  color: inherit;
}
.ledger-key {
  font-family: var(--sans);
  font-weight: 500;
  font-size: 1.15rem;
  letter-spacing: -0.035em;
  color: var(--ink);
  transition: letter-spacing .12s ease;
  overflow-wrap: anywhere;
}
.ledger-sub {
  grid-column: 1;
  font-size: var(--text-sm);
  color: var(--muted);
  overflow-wrap: anywhere;
}
.ledger-sub .theme { color: var(--ink); font-weight: 500; }
.ledger-stats {
  grid-column: 1;
  font-family: var(--sans);
  font-size: var(--text-xs);
  color: var(--muted);
  text-align: left;
  white-space: normal;
  letter-spacing: 0.02em;
}
.ledger-actions {
  grid-column: 2;
  grid-row: 1 / span 3;
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  justify-self: end;
  align-self: center;
}
.ledger-sync {
  font-family: var(--sans);
  font-size: var(--text-xs);
  color: var(--muted);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 0;
  padding: 4px 8px;
  cursor: pointer;
  line-height: 1.2;
  opacity: 0.85;
  border-color: var(--line);
  transition: opacity .12s ease, color .12s ease, border-color .12s ease;
}
.ledger-row:hover .ledger-sync,
.ledger-sync:focus-visible {
  opacity: 1;
  border-color: #C9C5B8;
  color: var(--ink);
}
.ledger-sync:hover,
.ledger-sync:focus-visible {
  color: var(--ink);
  border-color: #C9C5B8;
}
@media (hover: none) {
  .ledger-sync { opacity: 0.85; border-color: var(--line); }
}
.ledger-go {
  grid-column: 2;
  grid-row: 1 / span 3;
  font-family: var(--sans);
  font-size: 1.15rem;
  color: var(--muted);
  opacity: 0.75;
  line-height: 1;
  transition: color .12s ease, transform .12s ease, opacity .12s ease;
  justify-self: end;
  align-self: center;
}
.ledger-row:hover .ledger-go { color: var(--ink); opacity: 1; transform: translateX(2px); }
.ledger-empty {
  padding: 48px 8px;
  color: var(--muted);
  font-size: var(--text-sm);
  text-align: left;
  max-width: 28em;
}
.empty-card {
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
  padding: 30px 4px 34px;
  max-width: 42rem;
}
.empty-kicker {
  margin: 0 0 8px;
  font-family: var(--mono);
  font-size: var(--text-xs);
  color: var(--muted);
  letter-spacing: 0.04em;
}
.empty-title {
  margin: 0 0 8px;
  font-size: 1.05rem;
  font-weight: 500;
  letter-spacing: -0.03em;
  color: var(--ink);
}
.empty-text {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--muted);
  line-height: 1.7;
}
.empty-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 18px;
}
.empty-actions a,
.empty-actions button {
  font-family: var(--sans);
  font-weight: 500;
  font-size: var(--text-xs);
  padding: 7px 12px;
  border: 1px solid var(--line);
  background: transparent;
  color: var(--ink);
  text-decoration: none;
  cursor: pointer;
}
.empty-actions a.primary {
  background: var(--ink);
  color: #fff;
  border-color: var(--ink);
}
.empty-actions a:hover,
.empty-actions button:hover { border-color: var(--ink); text-decoration: none; }

.sheet {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 16px 16px;
}
.cell {
  position: relative;
  background: transparent;
  border: none;
  border-radius: 0;
  overflow: visible;
}
.cell:hover .thumb,
.cell:hover .thumb-miss {
  outline: 1px solid var(--ink);
  outline-offset: 0;
}
.cell.hidden { display: none; }
.cell.starred .thumb,
.cell.starred .thumb-miss {
  outline: 1px solid var(--ink);
  outline-offset: 0;
}
.cell.selected .thumb,
.cell.selected .thumb-miss {
  outline: 2px solid var(--ink);
  outline-offset: 0;
}
.cell .pick {
  position: absolute;
  top: 10px;
  left: 10px;
  z-index: 3;
  width: 16px;
  height: 16px;
  margin: 0;
  accent-color: var(--ink);
  cursor: pointer;
  opacity: 0.9;
  display: none;
}
body.select-mode .cell .pick { display: block; }
body.select-mode .cell .star { opacity: 0.55; }
body.select-mode .cell:hover .star,
body.select-mode .star.on { opacity: 1; }
.cell .thumb {
  display: block;
  width: 100%;
  aspect-ratio: 1;
  object-fit: cover;
  background: var(--mist);
  cursor: zoom-in;
  vertical-align: middle;
}
.cell .thumb-miss {
  display: flex;
  align-items: center;
  justify-content: center;
  aspect-ratio: 1;
  background: var(--mist);
  color: var(--muted);
  font-family: var(--mono);
  font-size: var(--text-xs);
  text-decoration: none;
}
.cell .edge {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 2px 0;
  background: transparent;
  border-top: none;
}
.cell .idx {
  font-family: var(--mono);
  font-size: var(--text-xs);
  color: var(--muted);
  flex-shrink: 0;
  letter-spacing: 0.04em;
}
.cell .fname {
  font-family: var(--sans);
  font-size: var(--text-xs);
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
  opacity: 0.72;
  transition: opacity .12s ease;
}
.cell:hover .fname,
.cell:focus-within .fname,
body.select-mode .cell .fname {
  opacity: 1;
}
.cell .badge {
  position: absolute;
  left: 10px;
  top: auto;
  bottom: 36px;
  font-family: var(--mono);
  font-size: var(--text-xs);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  padding: 2px 5px;
  background: rgba(20,20,20,0.72);
  color: #fff;
  pointer-events: none;
  z-index: 2;
}
.star {
  position: absolute;
  top: 8px;
  right: 8px;
  width: 28px;
  height: 28px;
  border: none;
  border-radius: 0;
  background: rgba(20,20,20,0.28);
  color: rgba(255,255,255,0.88);
  text-shadow: 0 1px 2px rgba(0,0,0,0.35);
  cursor: pointer;
  font-size: 15px;
  line-height: 1;
  display: grid;
  place-items: center;
  padding: 0;
  transition: color .12s, background .12s, opacity .12s, transform .12s;
  z-index: 2;
  opacity: 0.76;
}
.cell:hover .star,
.star.on,
.star:focus-visible { opacity: 1; }
.star:hover { color: #fff; background: rgba(20,20,20,0.35); }
.star.on {
  background: var(--ink);
  color: #fff;
  text-shadow: none;
  opacity: 1;
}
.star.busy { opacity: 0.55; pointer-events: none; }
.star.pulse { animation: starPulse .35s ease; }
@keyframes starPulse {
  0% { transform: scale(1); }
  40% { transform: scale(1.12); }
  100% { transform: scale(1); }
}

.lb {
  display: flex;
  position: fixed;
  inset: 0;
  z-index: 100;
  background: #0a0a0a;
  align-items: center;
  justify-content: center;
  padding: 0;
  opacity: 0;
  visibility: hidden;
  pointer-events: none;
  transition: opacity 0.35s ease, visibility 0.35s ease;
}
.lb.open {
  opacity: 1;
  visibility: visible;
  pointer-events: auto;
}
.lb img, .lb video {
  max-width: 100vw;
  max-height: 100vh;
  object-fit: contain;
}
.lb-bar {
  position: fixed;
  bottom: 20px;
  left: 50%;
  transform: translateX(-50%);
  display: grid;
  grid-template-columns: minmax(12rem, 1fr) auto auto auto;
  gap: 10px 14px;
  align-items: center;
  background: rgba(255,255,255,0.92);
  border: none;
  padding: 12px 14px;
  font-family: var(--sans);
  font-size: var(--text-xs);
  color: var(--ink);
  width: min(980px, calc(100vw - 40px));
}
.lb-meta { min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.lb-bar .nm {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
  color: var(--ink);
}
.lb-bar .pos {
  font-family: var(--mono);
  font-size: var(--text-xs);
  color: var(--muted);
  flex-shrink: 0;
  letter-spacing: 0.02em;
}
.lb-group {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding-left: 12px;
  border-left: 1px solid var(--line);
}
.lb-group:first-of-type { border-left: none; padding-left: 0; }
.lb-pick-wrap {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-family: var(--sans);
  font-size: var(--text-xs);
  cursor: pointer;
  user-select: none;
}
.lb-pick {
  width: 15px;
  height: 15px;
  margin: 0;
  accent-color: var(--ink);
}
.lb-bar button,
.lb-bar a {
  font-family: var(--sans);
  font-weight: 500;
  font-size: var(--text-xs);
  padding: 4px 10px;
  border: none;
  background: transparent;
  color: var(--ink);
  cursor: pointer;
  text-decoration: none;
}
.lb-bar button:hover,
.lb-bar a:hover { background: var(--mist); }
.lb-bar .star-lb { opacity: 1; color: var(--muted); text-shadow: none; position: static; width: auto; height: auto; }
.lb-bar .star-lb.on { background: var(--ink); color: #fff; }
.lb-bar .lb-trash { color: #8f1d1d; }
.lb-bar .lb-trash.busy { opacity: 0.55; pointer-events: none; }
@media (max-width: 900px) {
  .lb-bar { grid-template-columns: 1fr; align-items: stretch; }
  .lb-group { border-left: none; padding-left: 0; flex-wrap: wrap; }
}

.toast {
  position: fixed;
  bottom: 20px;
  right: 20px;
  background: var(--ink);
  color: #fff;
  font-size: var(--text-sm);
  padding: 10px 14px;
  opacity: 0;
  transform: translateY(8px);
  transition: opacity .2s, transform .2s;
  pointer-events: none;
  z-index: 200;
}
.toast.show { opacity: 1; transform: translateY(0); }

.pre-block {
  margin: 0;
  padding: 18px;
  background: var(--mist);
  border: none;
  font-family: var(--mono);
  font-size: var(--text-xs);
  overflow-x: auto;
  white-space: pre-wrap;
  color: var(--ink);
}

.events-editor {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 8px;
}
.events-fold {
  margin-top: 28px;
  border-top: 1px solid var(--line);
  padding-top: 14px;
}
.events-fold > summary {
  list-style: none;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--muted);
  letter-spacing: 0.04em;
  user-select: none;
  padding: 4px 0;
}
.events-fold > summary::-webkit-details-marker { display: none; }
.events-fold > summary::before {
  content: '›';
  font-size: var(--text-sm);
  line-height: 1;
  transition: transform 0.12s ease;
}
.events-fold[open] > summary::before { transform: rotate(90deg); }
.events-fold > summary:hover { color: var(--ink); }
.events-fold .events-editor { margin-top: 12px; }
.events-editor textarea {
  width: 100%;
  min-height: 280px;
  max-height: 55vh;
  box-sizing: border-box;
  padding: 16px 18px;
  border: 1px solid var(--line);
  background: var(--mist);
  color: var(--ink);
  font-family: var(--mono);
  font-size: var(--text-xs);
  line-height: 1.45;
  resize: vertical;
}
.events-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.events-toolbar button {
  font: inherit;
  font-size: var(--text-sm);
  padding: 8px 14px;
  border: 1px solid var(--line);
  background: var(--paper);
  color: var(--ink);
  cursor: pointer;
}
.events-toolbar button.primary {
  background: var(--ink);
  color: var(--paper);
  border-color: var(--ink);
}
.events-toolbar button:disabled {
  opacity: 0.5;
  cursor: wait;
}
.events-hint {
  margin: 0 0 14px;
  font-size: var(--text-xs);
  color: var(--muted);
  line-height: 1.45;
  max-width: 40em;
}
.events-hint.err { color: var(--hazard); }
.events-fold-note {
  margin: 0 0 10px;
  font-size: var(--text-xs);
  color: var(--muted);
  line-height: 1.45;
  max-width: 42em;
}
.events-status {
  font-size: var(--text-xs);
  min-height: 1.2em;
  color: var(--muted);
}
.events-status.ok { color: var(--live); }
.events-status.err { color: var(--hazard); }
@media (prefers-reduced-motion: reduce) {
  .events-fold > summary::before { transition: none; }
}

.page-in {
  animation: pageIn 0.45s ease both;
}
@keyframes pageIn {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: none; }
}

@media (max-width: 640px) {
  .wrap { padding: 20px 16px 56px; }
  .topbar {
    grid-template-columns: 1fr;
    justify-items: start;
  }
  .topbar-center { align-items: flex-start; text-align: left; }
  .crumbs { justify-content: flex-start; }
  .jumps { justify-content: flex-start; }
  .ledger-row { grid-template-columns: 1fr auto; gap: 4px 10px; }
  .ledger-sub { grid-column: 1 / -1; }
  .ledger-stats { text-align: left; white-space: normal; }
  .ledger-go { grid-row: 1; grid-column: 2; }
  .sheet { grid-template-columns: repeat(auto-fill, minmax(148px, 1fr)); gap: 14px 14px; }
  .star { opacity: 0.92; }
}
@media (prefers-reduced-motion: reduce) {
  .star.pulse { animation: none; }
  .page-in { animation: none; }
  html { scroll-behavior: auto; }
  .chip, .btn-reclass, .btn-bulk-star, .btn-trash, .btn-more, .ledger-row, .cell, .star, .lb-bar button, .ledger-go, .ledger-key, .ledger-sync { transition: none; }
}

'''

PAGE_JS = '''
(function () {
  var toastEl = null;
  function toast(msg) {
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.className = 'toast';
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = msg;
    toastEl.classList.add('show');
    clearTimeout(toastEl._t);
    toastEl._t = setTimeout(function () { toastEl.classList.remove('show'); }, 1800);
  }

  function gallerySheet() {
    return document.getElementById('sheet');
  }

  function galleryStillPaging() {
    var sheet = gallerySheet();
    return !!(sheet && sheet.getAttribute('data-has-more') === '1');
  }

  function setStarTotal(n) {
    var sheet = gallerySheet();
    if (sheet) sheet.setAttribute('data-star-total', String(Math.max(0, n)));
    var el = document.getElementById('starCount');
    if (el) el.textContent = String(Math.max(0, n));
    var chipN = document.querySelector('[data-filter="starred"] .n');
    if (chipN) chipN.textContent = String(Math.max(0, n));
    var meta = document.getElementById('pageMetaStars');
    if (meta) meta.textContent = String(Math.max(0, n));
  }

  async function toggleStar(btn) {
    var path = btn.getAttribute('data-path');
    var bucket = btn.getAttribute('data-bucket');
    if (!path || !bucket) {
      toast('加星失败：缺少路径');
      return;
    }
    if (btn.classList.contains('busy')) return;
    btn.classList.add('busy');
    try {
      var r = await fetch('/api/star', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: path, bucket: bucket, action: 'toggle' })
      });
      var data = await r.json();
      if (!data.ok) {
        toast('加星失败：' + (data.error || 'unknown'));
        return;
      }
      // Sync gallery cell + lightbox bar for the same path (lightbox is outside .cell).
      applyStarState(path, data.starred);
      btn.classList.add('pulse');
      setTimeout(function () { btn.classList.remove('pulse'); }, 350);
      var sheet = gallerySheet();
      if (sheet && sheet.getAttribute('data-star-total') != null) {
        var cur = parseInt(sheet.getAttribute('data-star-total') || '0', 10) || 0;
        setStarTotal(cur + (data.starred ? 1 : -1));
      } else {
        syncStarCount();
      }
      applyFilter();
    } catch (err) {
      toast('网络错误：' + err);
    } finally {
      btn.classList.remove('busy');
    }
  }

  function setStarred(btn, on) {
    btn.classList.toggle('on', !!on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.textContent = on ? '★' : '☆';
    btn.title = on ? '取消加星' : '加星';
    var cell = btn.closest('.cell');
    if (cell) cell.classList.toggle('starred', !!on);
  }

  function applyStarState(path, on) {
    if (!path) return;
    var sel = '.star[data-path="' + CSS.escape(path) + '"]';
    document.querySelectorAll(sel).forEach(function (b) { setStarred(b, on); });
  }

  function syncStarCount() {
    // Paginated galleries keep server star totals on data-star-total (DOM is partial).
    var sheet = gallerySheet();
    if (sheet && sheet.getAttribute('data-star-total') != null) {
      var total = parseInt(sheet.getAttribute('data-star-total') || '0', 10) || 0;
      setStarTotal(total);
      return;
    }
    var n = document.querySelectorAll('.cell.starred').length;
    var el = document.getElementById('starCount');
    if (el) el.textContent = String(n);
    var chipN = document.querySelector('[data-filter="starred"] .n');
    if (chipN) chipN.textContent = String(n);
    var meta = document.getElementById('pageMetaStars');
    if (meta) meta.textContent = String(n);
  }

  function setSelectMode(on) {
    document.body.classList.toggle('select-mode', !!on);
    var btn = document.getElementById('selectModeBtn');
    if (btn) {
      btn.classList.toggle('on', !!on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      btn.textContent = on ? '完成选择' : '批量选择';
    }
    if (!on) {
      document.querySelectorAll('.cell.selected').forEach(function (cell) {
        cell.classList.remove('selected');
        var pick = cell.querySelector('.pick');
        if (pick) pick.checked = false;
      });
    }
    syncSelCount();
  }

  function syncSelCount() {
    var n = document.querySelectorAll('.cell.selected').length;
    var el = document.getElementById('selCount');
    if (el) {
      el.textContent = n ? ('已选 ' + n + ' 个') : (
        document.body.classList.contains('select-mode') ? '已选 0 个，先勾选缩略图' : ''
      );
    }
    document.querySelectorAll('.btn-reclass, .btn-bulk-star, .btn-trash').forEach(function (b) {
      b.disabled = n === 0;
    });
  }

  function selectedPaths() {
    return Array.prototype.map.call(
      document.querySelectorAll('.cell.selected .pick'),
      function (cb) { return cb.getAttribute('data-path'); }
    ).filter(Boolean);
  }

  function cellForPath(path) {
    if (!path) return null;
    var thumb = document.querySelector(
      '.cell [data-lightbox][data-path="' + CSS.escape(path) + '"]'
    );
    return thumb ? thumb.closest('.cell') : null;
  }

  function setPathPicked(path, on) {
    var cell = cellForPath(path);
    if (!cell) return false;
    var pick = cell.querySelector('.pick');
    if (!pick) return false;
    pick.checked = !!on;
    cell.classList.toggle('selected', !!on);
    if (on && !document.body.classList.contains('select-mode')) {
      setSelectMode(true);
    } else {
      syncSelCount();
    }
    syncLightboxPick(path);
    return true;
  }

  function currentLightboxPath() {
    if (!lb || !lb.classList.contains('open')) return '';
    return lb.getAttribute('data-current-path') || '';
  }

  var galleryShortcutPaths = {
    '/by-date': true,
    '/starred': true,
    '/screenshots': true,
    '/screenrecords': true,
    '/docs': true,
    '/things': true,
    '/themes': true
  };

  function normalizedSameOriginPath(href) {
    try {
      var url = new URL(href, window.location.href);
      if (url.origin !== window.location.origin) return '';
      return url.pathname.replace(/\\/+$/, '') || '/';
    } catch (err) {
      return '';
    }
  }

  function shouldReplaceGalleryShortcutNav(link, e) {
    if (!link || e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return false;
    if (e.button != null && e.button !== 0) return false;
    var currentPath = normalizedSameOriginPath(window.location.href);
    var targetPath = normalizedSameOriginPath(link.href || link.getAttribute('href') || '');
    return !!(galleryShortcutPaths[currentPath] && galleryShortcutPaths[targetPath] && currentPath !== targetPath);
  }

  function syncLightboxPick(path) {
    if (!lb) return;
    var cb = lb.querySelector('.lb-pick');
    if (!cb) return;
    var cell = cellForPath(path || currentLightboxPath());
    var pick = cell ? cell.querySelector('.pick') : null;
    cb.checked = !!(pick && pick.checked);
  }

  async function starSelected() {
    var cells = Array.prototype.slice.call(document.querySelectorAll('.cell.selected'));
    if (!cells.length) {
      toast('请先点「批量选择」，再勾选文件');
      return;
    }
    var targets = cells.map(function (cell) {
      var star = cell.querySelector('.star[data-path][data-bucket]');
      return star ? {
        path: star.getAttribute('data-path'),
        bucket: star.getAttribute('data-bucket'),
        already: star.classList.contains('on')
      } : null;
    }).filter(function (x) { return x && x.path && x.bucket; });
    var toAdd = targets.filter(function (x) { return !x.already; });
    if (!targets.length) {
      toast('没有可加星的文件');
      return;
    }
    if (!toAdd.length) {
      toast('已选文件都已加星');
      return;
    }
    try {
      var added = 0;
      for (var i = 0; i < toAdd.length; i++) {
        var item = toAdd[i];
        var r = await fetch('/api/star', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: item.path, bucket: item.bucket, action: 'on' })
        });
        var data = await r.json();
        if (!data.ok) {
          toast('加星失败：' + (data.error || item.path));
          return;
        }
        if (data.starred) {
          applyStarState(item.path, true);
          added += 1;
        }
      }
      var sheet = gallerySheet();
      if (sheet && sheet.getAttribute('data-star-total') != null) {
        var cur = parseInt(sheet.getAttribute('data-star-total') || '0', 10) || 0;
        setStarTotal(cur + added);
      } else {
        syncStarCount();
      }
      applyFilter();
      toast('已加星：' + added + ' 个');
    } catch (err) {
      toast('网络错误：' + err);
    }
  }

  async function reclassify(action) {
    var paths = selectedPaths();
    if (!paths.length) {
      toast('请先点「批量选择」，再勾选文件');
      return;
    }
    var label = ({
      to_screen: '移至截图与录屏',
      to_normal: '移回普通分类',
      to_docs: '移至文档',
      to_things: '移至物品',
      to_default_month: '放回默认月桶'
    })[action] || action;
    var hint = action === 'to_default_month'
      ? '按文件名日期移到 by-date/YYYY-MM/（默认月桶，不进主题）。'
      : '会按规则重命名并移动。';
    if (!confirm(label + '：' + paths.length + ' 个文件？\\n' + hint)) return;
    try {
      var r = await fetch('/api/reclassify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: action, paths: paths })
      });
      var data = await r.json();
      if (!data.ok) {
        toast('失败：' + (data.error || 'unknown'));
        return;
      }
      var moved = (data.results || []).filter(function (x) { return x.ok && !x.skipped; }).length;
      toast('完成：' + moved + ' 个已移动');
      setTimeout(function () { location.reload(); }, 500);
    } catch (err) {
      toast('网络错误：' + err);
    }
  }

  async function trashSelected() {
    var paths = selectedPaths();
    if (!paths.length) {
      toast('请先点「批量选择」，再勾选文件');
      return;
    }
    if (!confirm('移至回收站：' + paths.length + ' 个文件？\\n\\n• 会移到 _trash/（可找回，不是永久删除）\\n• 不会同步到备份盘')) return;
    try {
      var r = await fetch('/api/trash', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths: paths })
      });
      var data = await r.json();
      if (!data.ok) {
        toast('失败：' + (data.error || 'unknown'));
        return;
      }
      toast('已移入回收站：' + (data.moved || 0) + ' 个');
      setTimeout(function () { location.reload(); }, 500);
    } catch (err) {
      toast('网络错误：' + err);
    }
  }

  async function trashLightboxCurrent(btn) {
    var path = currentLightboxPath();
    if (!path) {
      toast('删除失败：缺少路径');
      return;
    }
    if (btn && btn.classList.contains('busy')) return;
    var cell = cellForPath(path);
    var name = '';
    if (cell) {
      var thumb = cell.querySelector('[data-lightbox]');
      name = thumb ? (thumb.getAttribute('data-name') || '') : '';
    }
    if (!confirm('删除当前文件？\\n' + (name || path) + '\\n\\n会移到 _trash/（可找回，不是永久删除）。')) return;
    var items = visibleLightboxThumbs();
    var idx = 0;
    for (var i = 0; i < items.length; i++) {
      if ((items[i].getAttribute('data-path') || '') === path) {
        idx = i;
        break;
      }
    }
    if (btn) btn.classList.add('busy');
    try {
      var r = await fetch('/api/trash', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths: [path] })
      });
      var data = await r.json();
      if (!data.ok) {
        toast('删除失败：' + (data.error || 'unknown'));
        return;
      }
      if (cell) {
        if (cell.classList.contains('starred')) {
          var sheet = gallerySheet();
          var cur = sheet && sheet.getAttribute('data-star-total') != null
            ? (parseInt(sheet.getAttribute('data-star-total') || '0', 10) || 0)
            : document.querySelectorAll('.cell.starred').length;
          setStarTotal(cur - 1);
        }
        cell.remove();
      }
      syncSelCount();
      toast('已移入回收站：1 个');
      var nextItems = visibleLightboxThumbs();
      if (!nextItems.length) {
        closeLightbox();
      } else {
        openLightbox(nextItems[Math.min(idx, nextItems.length - 1)]);
      }
    } catch (err) {
      toast('网络错误：' + err);
    } finally {
      if (btn) btn.classList.remove('busy');
    }
  }

  var filterMode = 'all';
  var galleryIO = null;
  function galleryAutoLoadAllowed() {
    // 「仅加星」会把未加星格子 display:none，#galleryMore 常留在视口内，
    // 若仍自动续载会把整库 DOM 拉回，抵消分页收益。手动「加载更多」仍可用。
    return filterMode === 'all';
  }
  function applyFilter() {
    document.querySelectorAll('.cell').forEach(function (cell) {
      var show = filterMode === 'all' || cell.classList.contains('starred');
      cell.classList.toggle('hidden', !show);
    });
  }

  var galleryLoading = false;
  async function loadMoreGallery() {
    var sheet = gallerySheet();
    if (!sheet || sheet.getAttribute('data-has-more') !== '1' || galleryLoading) return;
    var btn = document.getElementById('loadMoreBtn');
    galleryLoading = true;
    if (btn) btn.classList.add('busy');
    try {
      var kind = sheet.getAttribute('data-gallery-kind') || '';
      var offset = parseInt(sheet.getAttribute('data-offset') || '0', 10) || 0;
      var limit = parseInt(sheet.getAttribute('data-page-size') || '150', 10) || 150;
      var qs = new URLSearchParams();
      qs.set('kind', kind);
      qs.set('offset', String(offset));
      qs.set('limit', String(limit));
      var year = sheet.getAttribute('data-year');
      var month = sheet.getAttribute('data-month');
      if (year) qs.set('year', year);
      if (month) qs.set('month', month);
      var r = await fetch('/api/gallery?' + qs.toString());
      var data = await r.json();
      if (!data.ok) {
        toast('加载失败：' + (data.error || 'unknown'));
        return;
      }
      if (data.html) sheet.insertAdjacentHTML('beforeend', data.html);
      var next = data.next_offset != null ? data.next_offset : (offset + (data.count || 0));
      sheet.setAttribute('data-offset', String(next));
      sheet.setAttribute('data-has-more', data.has_more ? '1' : '0');
      if (data.star_count != null) {
        sheet.setAttribute('data-star-total', String(data.star_count));
        setStarTotal(data.star_count);
      }
      var tip = document.querySelector('.gallery-more-tip');
      var total = data.total != null ? data.total : (parseInt(sheet.getAttribute('data-total') || '0', 10) || 0);
      if (tip) {
        tip.textContent = data.has_more
          ? ('已显示 ' + next + ' 个｜共 ' + total + ' 个｜筛选只看已加载内容')
          : ('已全部加载 ' + total + ' 个');
      }
      if (!data.has_more) {
        if (btn) btn.hidden = true;
        if (galleryIO) {
          galleryIO.disconnect();
          galleryIO = null;
        }
      }
      applyFilter();
    } catch (err) {
      toast('网络错误：' + err);
    } finally {
      galleryLoading = false;
      if (btn) btn.classList.remove('busy');
    }
  }

  function setupGalleryPaging() {
    var more = document.getElementById('galleryMore');
    if (!more || !galleryStillPaging()) return;
    if ('IntersectionObserver' in window) {
      if (galleryIO) galleryIO.disconnect();
      galleryIO = new IntersectionObserver(function (entries) {
        if (!galleryAutoLoadAllowed()) return;
        if (entries.some(function (e) { return e.isIntersecting; })) {
          loadMoreGallery();
        }
      }, { rootMargin: '240px 0px' });
      galleryIO.observe(more);
    }
  }

  document.addEventListener('change', function (e) {
    var lbPick = e.target.closest('.lb-pick');
    if (lbPick) {
      setPathPicked(currentLightboxPath(), lbPick.checked);
      return;
    }
    var pick = e.target.closest('.pick');
    if (!pick) return;
    var cell = pick.closest('.cell');
    if (cell) cell.classList.toggle('selected', pick.checked);
    syncSelCount();
  });

  document.addEventListener('click', function (e) {
    document.querySelectorAll('.jumps-more[open]').forEach(function (menu) {
      if (!menu.contains(e.target)) menu.open = false;
    });
    var jumpLink = e.target.closest('.jumps-more-panel a[href]');
    if (shouldReplaceGalleryShortcutNav(jumpLink, e)) {
      e.preventDefault();
      window.location.replace(jumpLink.href);
      return;
    }
    var star = e.target.closest('.star');
    if (star) {
      e.preventDefault();
      e.stopPropagation();
      toggleStar(star);
      return;
    }
    if (e.target.closest('[data-select-toggle]')) {
      e.preventDefault();
      setSelectMode(!document.body.classList.contains('select-mode'));
      return;
    }
    if (e.target.closest('.pick')) {
      e.stopPropagation();
      return;
    }
    var lbTrash = e.target.closest('.lb-trash');
    if (lbTrash) {
      e.preventDefault();
      e.stopPropagation();
      trashLightboxCurrent(lbTrash);
      return;
    }
    var loadMore = e.target.closest('[data-load-more]');
    if (loadMore) {
      e.preventDefault();
      loadMoreGallery();
      return;
    }
    var re = e.target.closest('[data-reclassify]');
    if (re) {
      e.preventDefault();
      reclassify(re.getAttribute('data-reclassify'));
      return;
    }
    var bulkStar = e.target.closest('[data-bulk-star]');
    if (bulkStar) {
      e.preventDefault();
      starSelected();
      return;
    }
    var trashBtn = e.target.closest('[data-trash]');
    if (trashBtn) {
      e.preventDefault();
      trashSelected();
      return;
    }
    var chip = e.target.closest('[data-filter]');
    if (chip) {
      filterMode = chip.getAttribute('data-filter');
      document.querySelectorAll('[data-filter]').forEach(function (c) {
        c.classList.toggle('on', c === chip);
      });
      applyFilter();
      return;
    }
    var thumb = e.target.closest('[data-lightbox]');
    if (thumb) {
      e.preventDefault();
      openLightbox(thumb);
      return;
    }
    if (e.target.id === 'lbClose' || e.target.classList.contains('lb')) {
      closeLightbox();
    }
  });

  function isTypingTarget(el) {
    if (!el || el === document || el === document.body) return false;
    var tag = (el.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;
    if (el.isContentEditable) return true;
    return false;
  }

  function visibleLightboxThumbs() {
    // Gallery order; skip filter-hidden cells (Live companion MOVs are already omitted server-side).
    return Array.prototype.slice.call(
      document.querySelectorAll('.cell:not(.hidden) [data-lightbox]')
    );
  }

  function stepLightbox(dir) {
    if (!lb || !lb.classList.contains('open')) return;
    var items = visibleLightboxThumbs();
    if (!items.length) return;
    var curPath = '';
    var starBtn = lb.querySelector('.star-lb');
    if (starBtn) curPath = starBtn.getAttribute('data-path') || '';
    var idx = -1;
    for (var i = 0; i < items.length; i++) {
      if ((items[i].getAttribute('data-path') || '') === curPath) {
        idx = i;
        break;
      }
    }
    if (idx < 0) idx = 0;
    else idx = (idx + dir + items.length) % items.length; // wrap around
    openLightbox(items[idx]);
  }

  function pickCurrentAndAdvance() {
    var path = currentLightboxPath();
    if (!path) return;
    if (setPathPicked(path, true)) {
      toast('已勾选，下一张');
      stepLightbox(1);
    }
  }

  document.addEventListener('keydown', function (e) {
    var onLightboxPick = e.target && e.target.closest && e.target.closest('.lb-pick');
    if (isTypingTarget(e.target) && !onLightboxPick) return;
    if (e.key === 'Escape') {
      closeLightbox();
      return;
    }
    if (!lb || !lb.classList.contains('open')) return;
    if (e.key === 'ArrowLeft') {
      e.preventDefault();
      stepLightbox(-1);
    } else if (e.key === 'ArrowRight') {
      e.preventDefault();
      stepLightbox(1);
    } else if (e.code === 'Space' || e.key === ' ') {
      e.preventDefault();
      pickCurrentAndAdvance();
    }
  });

  var lb = null;
  function openLightbox(el) {
    var src = el.getAttribute('data-lightbox');
    var path = el.getAttribute('data-path') || '';
    var bucket = el.getAttribute('data-bucket') || '';
    var name = el.getAttribute('data-name') || '';
    var isVideo = el.getAttribute('data-video') === '1';
    if (!lb) {
      lb = document.createElement('div');
      lb.className = 'lb';
      lb.innerHTML = '<div class="lb-media"></div><div class="lb-bar">' +
        '<div class="lb-meta"><span class="pos"></span><span class="nm"></span></div>' +
        '<div class="lb-group"><label class="lb-pick-wrap"><input type="checkbox" class="lb-pick">勾选</label>' +
        '<button type="button" class="star star-lb" title="加入加星">☆ 加星</button></div>' +
        '<div class="lb-group"><a class="open-raw" href="#" target="_blank" rel="noopener">查看原图</a></div>' +
        '<div class="lb-group">' +
        '<button type="button" class="lb-trash">移至回收站</button>' +
        '<button type="button" id="lbClose">关闭</button></div></div>';
      document.body.appendChild(lb);
    }
    lb.setAttribute('data-current-path', path);
    var media = lb.querySelector('.lb-media');
    media.innerHTML = '';
    if (isVideo) {
      var v = document.createElement('video');
      v.src = src;
      v.controls = true;
      v.autoplay = true;
      media.appendChild(v);
    } else {
      var img = document.createElement('img');
      img.src = src;
      img.alt = name;
      media.appendChild(img);
    }
    lb.querySelector('.nm').textContent = name;
    var items = visibleLightboxThumbs();
    var pos = 0;
    for (var i = 0; i < items.length; i++) {
      if ((items[i].getAttribute('data-path') || '') === path) {
        pos = i;
        break;
      }
    }
    var posEl = lb.querySelector('.pos');
    if (posEl) {
      posEl.textContent = items.length ? ('第 ' + (pos + 1) + ' 项｜共 ' + items.length + ' 项') : '';
    }
    var raw = lb.querySelector('.open-raw');
    raw.href = src;
    syncLightboxPick(path);
    var starBtn = lb.querySelector('.star-lb');
    starBtn.setAttribute('data-path', path);
    starBtn.setAttribute('data-bucket', bucket);
    // Prefer gallery cell star (exclude .star-lb) so reopening reflects persisted UI state.
    var cellStar = document.querySelector(
      '.cell .star[data-path="' + CSS.escape(path) + '"]'
    );
    setStarred(starBtn, cellStar ? cellStar.classList.contains('on') : false);
    lb.classList.add('open');
  }
  function closeLightbox() {
    if (!lb) return;
    lb.classList.remove('open');
    var media = lb.querySelector('.lb-media');
    if (media) media.innerHTML = '';
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', setupGalleryPaging);
  } else {
    setupGalleryPaging();
  }
})();
'''


def page_shell(title: str, body: str, work: Path = None, crumbs: list = None,
               buckets: dict = None, star_n: int = None) -> bytes:
    """Wrap page body in shared topbar / assets.

    When callers already have scan/star results (e.g. render_home), pass
    ``buckets`` / ``star_n`` to avoid a second walk. Otherwise uses the short
    TTL topbar cache (see get_topbar_stats).
    """
    if crumbs is None:
        crumbs = [('首页', '/')]
    crumb_parts = []
    for i, (label, href) in enumerate(crumbs):
        if i:
            crumb_parts.append('<span class="sep">›</span>')
        if href and i < len(crumbs) - 1:
            crumb_parts.append(f'<a href="{_esc(href)}">{_esc(label)}</a>')
        else:
            crumb_parts.append(f'<a class="here" href="{_esc(href or "#")}">{_esc(label)}</a>')

    # Counts for top jumps (avoid repeating a footer link dump on home)
    by_date_n = shots_n = records_n = docs_n = things_n = 0
    if work is not None:
        if buckets is None or star_n is None:
            cached_star, cached_buckets = get_topbar_stats(work)
            if star_n is None:
                star_n = cached_star
            if buckets is None:
                buckets = cached_buckets
        star_n = int(star_n or 0)
        totals = _by_date_totals(buckets)
        by_date_n = totals['photos'] + totals['videos']
        shots_n = int(buckets.get('screenshots_count') or 0)
        records_n = int(buckets.get('screenrecords_count') or 0)
        docs_n = int(buckets.get('docs_count') or 0)
        things_n = int(buckets.get('things_count') or 0)
        themes_n = int(buckets.get('themes_count') or 0)
    else:
        star_n = int(star_n or 0)
        themes_n = 0

    def _jump(href: str, label: str, n: int = None) -> str:
        if n is None:
            return f'<a href="{_esc(href)}">{_esc(label)}</a>'
        return (
            f'<a href="{_esc(href)}"><span class="jump-name">{_esc(label)}</span>'
            f'<span class="n">{int(n)} 项</span></a>'
        )

    more_links = [
        _jump('/by-date', '按日期', by_date_n),
        _jump('/starred', '加星', star_n),
        _jump('/screenshots', '截图', shots_n),
        _jump('/screenrecords', '录屏', records_n),
        _jump('/docs', '文档', docs_n),
        _jump('/things', '物品', things_n),
        _jump('/themes', '主题', themes_n),
    ]
    jump_parts = [
        '<a href="#" id="consoleLink">控制台</a>',
        '<details class="jumps-more">'
        '<summary>图库</summary>'
        f'<div class="jumps-more-panel">{"".join(more_links)}</div>'
        '</details>',
    ]

    crumbs_html = ''
    if crumb_parts:
        crumbs_html = (
            f'<nav class="crumbs" aria-label="面包屑">{"".join(crumb_parts)}</nav>'
        )

    # Server-known dashboard address (never claim browse origin is the console).
    dash_fallback_js = json.dumps(dashboard_file_url(), ensure_ascii=False)
    console_js = f'''
(function () {{
  var a = document.getElementById('consoleLink');
  if (!a) return;
  var dash = null;
  try {{ dash = localStorage.getItem('picvault.dashboard.url'); }} catch (e) {{}}
  var fallbackDash = {dash_fallback_js};
  function showConsoleHint() {{
    var url = (dash && String(dash).trim()) || fallbackDash || '';
    var tip = '请打开控制台（outputs/dashboard.html）。从控制台点「打开浏览」进入本页后即可记住返回路径。';
    if (!url) {{
      alert(tip);
      return;
    }}
    var done = function (copied) {{
      var msg = tip + '\\n\\n控制台地址：\\n' + url;
      if (copied) msg += '\\n\\n（已复制到剪贴板）';
      // prompt 内输入框可选中，便于手动复制；clipboard 成功时再带提示。
      if (window.prompt) {{
        window.prompt(msg + (copied ? '' : '\\n\\n可全选下方地址复制：'), url);
      }} else {{
        alert(msg);
      }}
    }};
    if (navigator.clipboard && navigator.clipboard.writeText) {{
      navigator.clipboard.writeText(url).then(function () {{ done(true); }}, function () {{ done(false); }});
    }} else {{
      done(false);
    }}
  }}
  if (dash) {{
    a.href = dash;
  }} else {{
    a.addEventListener('click', function (e) {{
      e.preventDefault();
      showConsoleHint();
    }});
  }}
}})();
'''

    doc = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}｜PicVault</title>
<style>{PAGE_CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="topbar">
    <a class="brand-mark" href="/">PicVault</a>
    <div class="topbar-center">
      {crumbs_html}
    </div>
    <nav class="jumps" aria-label="快捷入口">{''.join(jump_parts)}</nav>
  </header>
  <div class="page-in">
  {body}
  </div>
</div>
<script>{PAGE_JS}</script>
<script>{console_js}</script>
</body>
</html>'''
    return doc.encode('utf-8')


def html_error_page(title: str, message: str) -> bytes:
    """Minimal White Cube error page (not plain text)."""
    body = (
        f'<div class="page-head"><h2 class="page-title">{_esc(title)}</h2></div>'
        f'<div class="ledger"><div class="ledger-empty">{_esc(message)} '
        f'<a href="/">回到图库</a></div></div>'
    )
    return page_shell(title, body, work=None, crumbs=[('首页', '/'), (title, '#')])


def _media_cell(f: Path, work: Path, thumb_root: Path, bucket: str,
                stars: dict, index: int) -> str:
    """One gallery cell. Thumbs are lazy: HTML always points at /thumb; generation
    happens in GET /thumb so month pages return without blocking on sips."""
    rel = str(f.relative_to(work))
    is_video = f.suffix.lower() in VIDEO_EXTS
    is_starred = rel in stars
    q = urllib.parse.quote(rel)
    raw_url = f'/raw?p={q}'
    thumb_url = f'/thumb?p={q}'
    # Grid always uses <img> + /thumb (videos: ffmpeg frame). Click opens lightbox;
    # do not embed <video src=/raw> in the sheet (breaks preview / hammers Range).
    media = (
        f'<img class="thumb" src="{_esc(thumb_url)}" alt="{_esc(f.name)}" '
        f'loading="lazy" data-lightbox="{_esc(raw_url)}" '
        f'data-path="{_esc(rel)}" data-bucket="{_esc(bucket)}" '
        f'data-name="{_esc(f.name)}" data-video="{"1" if is_video else "0"}">'
    )
    if is_live_photo_still(f):
        badge = '<span class="badge">实况</span>'
    elif is_video:
        badge = '<span class="badge">视频</span>'
    else:
        badge = ''
    star_cls = 'star on' if is_starred else 'star'
    star_char = '★' if is_starred else '☆'
    cell_cls = 'cell starred' if is_starred else 'cell'
    return (
        f'<article class="{cell_cls}">'
        f'<input type="checkbox" class="pick" data-path="{_esc(rel)}" '
        f'aria-label="选择 {_esc(f.name)}">'
        f'{media}{badge}'
        f'<button type="button" class="{star_cls}" data-path="{_esc(rel)}" '
        f'data-bucket="{_esc(bucket)}" aria-pressed="{"true" if is_starred else "false"}" '
        f'title="{"取消加星" if is_starred else "加星"}">{star_char}</button>'
        f'<div class="edge"><span class="idx">{index:03d}</span>'
        f'<span class="fname" title="{_esc(f.name)}">{_esc(f.name)}</span></div>'
        f'</article>'
    )


def _gallery_toolbar(file_count: int, star_count: int, context: str = 'normal',
                     paginated: bool = False) -> str:
    """context: normal | theme | screen | docs | things — hide the button for the current bucket."""
    actions = []
    if context != 'starred':
        actions.append(
            '<button type="button" class="btn-bulk-star" data-bulk-star="1" disabled>'
            '加星</button>'
        )
    if context == 'theme':
        actions.append(
            '<button type="button" class="btn-reclass" data-reclassify="to_default_month" disabled>'
            '放回默认月桶</button>'
        )
    if context != 'screen':
        actions.append(
            '<button type="button" class="btn-reclass" data-reclassify="to_screen" disabled>'
            '移至截图与录屏</button>'
        )
    if context not in ('normal', 'theme'):
        actions.append(
            '<button type="button" class="btn-reclass" data-reclassify="to_normal" disabled>'
            '移回普通分类</button>'
        )
    if context != 'docs':
        actions.append(
            '<button type="button" class="btn-reclass" data-reclassify="to_docs" disabled>'
            '移至文档</button>'
        )
    if context != 'things':
        actions.append(
            '<button type="button" class="btn-reclass" data-reclassify="to_things" disabled>'
            '移至物品</button>'
        )
    actions.append(
        '<button type="button" class="btn-trash" data-trash="1" disabled>'
        '移至回收站</button>'
    )
    filter_tip = (
        '<span class="filter-tip">先加载更多，再筛选；否则只看已显示的缩略图。</span>'
        if paginated else ''
    )
    star_filter = ''
    if context != 'starred':
        star_filter = (
            '<button type="button" class="chip" data-filter="starred">'
            f'仅加星<span class="n" id="starCount">{star_count}</span></button>'
        )
    review_tip = (
        '<p class="review-tip">打开预览后按 <kbd>空格</kbd> 勾选当前并进入下一张；'
        '加星、原图和删除仍可单独操作。</p>'
        if file_count else ''
    )
    return (
        f'<div class="toolbar">'
        f'<span class="count">文件 {file_count}｜'
        f'加星 <span id="pageMetaStars">{star_count}</span></span>'
        f'<div class="toolbar-filters">'
        f'<button type="button" class="chip on" data-filter="all">全部</button>'
        f'{star_filter}'
        f'<button type="button" class="chip" id="selectModeBtn" data-select-toggle '
        f'aria-pressed="false">批量选择</button>'
        f'{filter_tip}'
        f'</div>'
        f'<div class="toolbar-organize" aria-label="整理">'
        f'<span class="sel-count" id="selCount"></span>'
        f'{"".join(actions)}'
        f'</div>'
        f'{review_tip}'
        f'</div>'
    )


def _empty_state(title: str, text: str, actions: list[tuple[str, str, bool]] = None,
                 kicker: str = '当前为空') -> str:
    actions = actions or []
    action_html = ''
    if actions:
        links = []
        for label, href, primary in actions:
            cls = ' class="primary"' if primary else ''
            links.append(f'<a{cls} href="{_esc(href)}">{_esc(label)}</a>')
        action_html = f'<div class="empty-actions">{"".join(links)}</div>'
    return (
        '<div class="empty-card">'
        f'<p class="empty-kicker">{_esc(kicker)}</p>'
        f'<h3 class="empty-title">{_esc(title)}</h3>'
        f'<p class="empty-text">{_esc(text)}</p>'
        f'{action_html}'
        '</div>'
    )


def _gallery_cells_html(entries: list[tuple], work: Path, thumb_root: Path,
                        stars: dict, start_index: int = 1) -> str:
    """Render consecutive gallery cells; ``entries`` is [(Path, bucket), ...]."""
    parts = []
    for i, (path, bucket) in enumerate(entries):
        parts.append(
            _media_cell(path, work, thumb_root, bucket, stars, start_index + i)
        )
    return ''.join(parts)


def _gallery_sheet_html(
    entries: list[tuple],
    work: Path,
    thumb_root: Path,
    stars: dict,
    kind: str,
    year: str = None,
    month: str = None,
    empty_message: str = '这个桶里还没有文件。',
    empty_text: str = None,
    empty_actions: list[tuple[str, str, bool]] = None,
) -> str:
    """First page of a gallery sheet + optional「加载更多」footer."""
    total = len(entries)
    if total == 0:
        return _empty_state(
            empty_message,
            empty_text or '这里不会自动生成内容；完成对应步骤或从图库手动移入后会出现。',
            empty_actions or [('回到图库', '/', False), ('打开按日期', '/by-date', True)],
        )
    page = entries[:GALLERY_PAGE_SIZE]
    loaded = len(page)
    has_more = total > loaded
    cells = _gallery_cells_html(page, work, thumb_root, stars, start_index=1)
    attrs = [
        f'data-gallery-kind="{_esc(kind)}"',
        f'data-offset="{loaded}"',
        f'data-total="{total}"',
        f'data-page-size="{GALLERY_PAGE_SIZE}"',
        f'data-has-more="{"1" if has_more else "0"}"',
        f'data-star-total="{len(stars)}"',
    ]
    if year:
        attrs.append(f'data-year="{_esc(year)}"')
    if month:
        attrs.append(f'data-month="{_esc(month)}"')
    more = ''
    if has_more:
        more = (
            '<div class="gallery-more" id="galleryMore">'
            f'<p class="gallery-more-tip">已显示 {loaded} 个｜共 {total} 个'
            '｜筛选只看已加载内容</p>'
            '<button type="button" class="btn-more" id="loadMoreBtn" data-load-more>'
            '加载更多</button>'
            '</div>'
        )
    return (
        f'<div class="sheet" id="sheet" {" ".join(attrs)}>{cells}</div>'
        f'{more}'
    )


def build_gallery_page_payload(
    work: Path,
    thumb_root: Path,
    kind: str,
    offset: int = 0,
    limit: int = None,
    year: str = None,
    month: str = None,
) -> dict:
    """JSON payload for GET /api/gallery (HTML cell fragments for one page)."""
    if kind not in (
        'bucket', 'screenshots', 'screenrecords', 'docs', 'things', 'starred',
    ):
        return {'ok': False, 'error': 'unknown kind'}
    if kind == 'bucket':
        if not year or not re.fullmatch(r'\d{4}', str(year)):
            return {'ok': False, 'error': 'year required (YYYY)'}
        if not month or not is_safe_month_segment(month):
            return {'ok': False, 'error': 'invalid month'}
        year_dir = (work / 'by-date' / year).resolve()
        month_dir = (work / 'by-date' / year / month).resolve()
        if not path_is_under(month_dir, year_dir):
            return {'ok': False, 'error': 'path not allowed'}
    entries = list_gallery_entries(work, kind, year=year, month=month)
    total = len(entries)
    limit = clamp_gallery_limit(limit if limit is not None else GALLERY_PAGE_SIZE)
    offset = clamp_gallery_offset(offset, total)
    page = entries[offset:offset + limit]
    stars = gallery_stars_map(work, kind, entries)
    html = _gallery_cells_html(
        page, work, thumb_root, stars, start_index=offset + 1,
    )
    next_offset = offset + len(page)
    return {
        'ok': True,
        'kind': kind,
        'total': total,
        'offset': offset,
        'limit': limit,
        'count': len(page),
        'next_offset': next_offset,
        'has_more': next_offset < total,
        'html': html,
        'star_count': len(stars),
    }


def _by_date_totals(buckets: dict) -> dict:
    years = buckets.get('years') or {}
    months = [m for year_months in years.values() for m in year_months]
    return {
        'years': len(years),
        'months': sum(1 for m in months if not m.get('is_themed')),
        'themes': sum(1 for m in months if m.get('is_themed')),
        'photos': sum(int(m.get('photos') or 0) for m in months),
        'videos': sum(int(m.get('videos') or 0) for m in months),
        'stars': sum(int(m.get('stars') or 0) for m in months),
        'lives': sum(int(m.get('lives') or 0) for m in months),
    }


def _home_card(label: str, count: int, desc: str, href: str = None,
               meta: str = '', primary: bool = False) -> str:
    cls = 'home-card home-card-primary' if primary else 'home-card'
    if href:
        tag = 'a'
        attrs = f' href="{_esc(href)}"'
        go = '<span class="home-card-go" aria-hidden="true">›</span>'
    else:
        tag = 'div'
        cls += ' home-card-disabled'
        attrs = ' aria-disabled="true"'
        go = ''
    meta_html = f'<div class="home-card-meta">{_esc(meta)}</div>' if meta else ''
    return (
        f'<{tag} class="{cls}"{attrs}>'
        f'<div class="home-card-top">'
        f'<span class="home-card-name">{_esc(label)}</span>'
        f'<span class="home-card-count">{int(count)} 项</span>'
        f'</div>'
        f'<p class="home-card-desc">{_esc(desc)}</p>'
        f'{meta_html}'
        f'{go}'
        f'</{tag}>'
    )


def _by_date_home_meta(totals: dict) -> str:
    parts = []
    if int(totals.get('years') or 0):
        parts.append(f"{int(totals.get('years') or 0)} 年")
    if int(totals.get('months') or 0):
        parts.append(f"{int(totals.get('months') or 0)} 个月")
    if int(totals.get('themes') or 0):
        parts.append(f"{int(totals.get('themes') or 0)} 个主题")
    return '｜'.join(parts)


def render_home(work: Path) -> bytes:
    star_n, buckets = get_topbar_stats(work)
    totals = _by_date_totals(buckets)
    by_date_n = totals['photos'] + totals['videos']
    trash_n = get_trash_count(work)
    by_date_meta = _by_date_home_meta(totals)
    has_main_gallery = by_date_n > 0
    cards = [
        _home_card('按日期', by_date_n, '按拍摄时间浏览', '/by-date', by_date_meta, primary=has_main_gallery),
        _home_card('加星', star_n, '所有已加星的照片和视频', '/starred'),
        _home_card('截图', buckets.get('screenshots_count') or 0, '截图集中清理和复核', '/screenshots'),
        _home_card('录屏', buckets.get('screenrecords_count') or 0, '屏幕录制视频集中回看', '/screenrecords'),
        _home_card('文档', buckets.get('docs_count') or 0, '证件、票据和纸面信息', '/docs'),
        _home_card('物品', buckets.get('things_count') or 0, '设备、包装和物件记录', '/things'),
        _home_card('主题', buckets.get('themes_count') or 0, '旅行和事件的主题桶配置', '/themes'),
        _home_card('回收站', trash_n, '软删除暂存，不参与备份。'),
    ]
    body = (
        '<div class="page-head">'
        '<div>'
        '<h2 class="page-title">图库</h2>'
        '<p class="page-lede">选择一个入口继续浏览、加星或整理。</p>'
        '</div>'
        '</div>'
        f'<section class="home-grid" aria-label="图库文件夹">{"".join(cards)}</section>'
    )
    return page_shell(
        '图库', body, work=work, crumbs=[], buckets=buckets, star_n=star_n,
    )


def render_by_date_home(work: Path) -> bytes:
    # One cached scan for ledger + topbar (page_shell reuses buckets/star_n).
    star_n, buckets = get_topbar_stats(work)
    years = sorted(buckets['years'].keys(), reverse=True)
    rows = []
    for year in years:
        months = buckets['years'][year]
        total_photos = sum(m['photos'] for m in months)
        total_videos = sum(m['videos'] for m in months)
        total_stars = sum(m.get('stars', 0) for m in months)
        total_lives = sum(m.get('lives', 0) for m in months)
        n_default = sum(1 for m in months if not m.get('is_themed'))
        n_themed = sum(1 for m in months if m.get('is_themed'))
        if n_default and n_themed:
            sub = f'{n_default} 个月｜{n_themed} 个主题'
        elif n_themed:
            sub = f'{n_themed} 个主题'
        elif n_default:
            sub = f'{n_default} 个月'
        else:
            sub = '空'
        stats = format_ledger_stats(
            total_photos, total_videos, total_stars, total_lives
        )
        rows.append(
            f'<a class="ledger-row" href="/y/{_esc(year)}">'
            f'<span class="ledger-key">{_esc(year)}</span>'
            f'<span class="ledger-sub">{sub}</span>'
            f'<span class="ledger-stats">{stats}</span>'
            f'<span class="ledger-go" aria-hidden="true">›</span>'
            f'</a>'
        )
    if not rows:
        ledger = _empty_state(
            '还没有按拍摄日期归档的照片或视频',
            '把素材放进 inbox 后，回到控制台执行“重命名并归档”；完成后会按拍摄时间出现在这里。',
            [('回到图库', '/', False), ('打开控制台', dashboard_file_url(), True)],
        )
    else:
        ledger = f'<div class="ledger">{"".join(rows)}</div>'

    body = (
        f'<div class="page-head">'
        f'<div>'
        f'<h2 class="page-title">按日期</h2>'
        f'<p class="page-lede">先选年份，再查看月份或主题</p>'
        f'</div>'
        f'</div>'
        f'{ledger}'
    )
    return page_shell(
        '按日期', body, work=work,
        crumbs=[('首页', '/'), ('按日期', '/by-date')],
        buckets=buckets, star_n=star_n,
    )


def render_year(work: Path, year: str) -> bytes:
    by_date = work / 'by-date' / year
    if not by_date.exists():
        body = (
            f'<div class="page-head"><h2 class="page-title">{_esc(year)}</h2></div>'
            f'{_empty_state("未找到该年份", "这个年份目录不存在，可能还没有归档，或目录已经被移动。", [("回到按日期", "/by-date", True)])}'
        )
        return page_shell(
            year, body, work=work,
            crumbs=[('首页', '/'), ('按日期', '/by-date'), (year, f'/y/{year}')],
        )

    months = [m for m in sorted(by_date.iterdir()) if m.is_dir()]
    filled_rows = []
    empty_rows = []
    for m in months:
        photo_count, video_count, live_count = count_month_media(m)
        star_count = len(load_stars(work, m.name))
        is_themed = '_' in m.name
        display = bucket_display_name(m.name)
        month_key = bucket_month_key(m.name)
        if is_themed:
            sub = month_key
            key = display
        else:
            sub = ''
            key = month_key
        href = f'/y/{year}/{urllib.parse.quote(m.name)}'
        stats = format_ledger_stats(
            photo_count, video_count, star_count, live_count
        )
        empty = photo_count == 0 and video_count == 0
        row_cls = 'ledger-row is-empty' if empty else 'ledger-row'
        sub_html = f'<span class="ledger-sub">{_esc(sub)}</span>' if sub else '<span class="ledger-sub"></span>'
        row = (
            f'<a class="{row_cls}" href="{_esc(href)}">'
            f'<span class="ledger-key">{_esc(key)}</span>'
            f'{sub_html}'
            f'<span class="ledger-stats">{stats}</span>'
            f'<span class="ledger-go" aria-hidden="true">›</span>'
            f'</a>'
        )
        if empty:
            empty_rows.append(row)
        else:
            filled_rows.append(row)

    rows = filled_rows + empty_rows
    if rows:
        ledger = f'<div class="ledger">{"".join(rows)}</div>'
    else:
        ledger = _empty_state(
            '这个年份还没有文件',
            '月份目录存在，但还没有可浏览的照片或视频。整理归档后再回来查看。',
            [('回到按日期', '/by-date', True)],
        )
    body = (
        f'<div class="page-head">'
        f'<h2 class="page-title">{_esc(year)}</h2>'
        f'<p class="page-meta">{len(months)} 个入口</p>'
        f'</div>'
        f'{ledger}'
    )
    return page_shell(
        year, body, work=work,
        crumbs=[('首页', '/'), ('按日期', '/by-date'), (year, f'/y/{year}')],
    )


def theme_ledger_sub(theme: dict) -> str:
    """Format theme month + date_range for /themes ledger and theme bucket headers."""
    month = str(theme.get('month') or '').strip()
    parts = [month] if month else []
    dr = theme.get('date_range')
    start = end = ''
    if isinstance(dr, dict):
        start = str(dr.get('start') or '').strip()
        end = str(dr.get('end') or '').strip()
    # parse_simple_yaml may flatten date_range into theme-level start/end
    if not start:
        start = str(theme.get('start') or '').strip()
    if not end:
        end = str(theme.get('end') or '').strip()
    if start and end:
        def short(d: str) -> str:
            m = re.match(r'^\d{4}-(\d{2})-(\d{2})$', d)
            if m:
                return f'{int(m.group(1))}月{int(m.group(2))}日'
            return d
        parts.append(f'{short(start)}到{short(end)}')
    elif start or end:
        parts.append(start or end)
    return '｜'.join(parts) if parts else '未设置周期'


def theme_for_bucket(work: Path, bucket: str):
    """Return events.yaml theme matching YYYY-MM_<name> bucket, or None."""
    if '_' not in bucket:
        return None
    try:
        themes = rename_mod.load_events(work, None)
    except ValueError:
        return None
    for t in themes:
        month = str(t.get('month') or '').strip()
        name = str(t.get('name') or '').strip()
        if month and name and f'{month}_{name}' == bucket:
            return t
    return None


def render_bucket(work: Path, year: str, month: str, thumb_root: Path) -> bytes:
    bucket_name = month
    is_themed = '_' in month
    display = bucket_display_name(month)
    gallery_ctx = 'theme' if is_themed else 'normal'
    entries = list_gallery_entries(work, 'bucket', year=year, month=month)
    stars = gallery_stars_map(work, 'bucket', entries)
    paginated = len(entries) > GALLERY_PAGE_SIZE
    sheet = _gallery_sheet_html(
        entries, work, thumb_root, stars, kind='bucket',
        year=year, month=month,
        empty_message='这个入口还没有文件',
        empty_text='归档后，文件会按拍摄时间进入默认月桶；主题桶需要先配置主题，再执行主题同步。',
        empty_actions=[('回到按日期', '/by-date', False), ('打开主题配置', '/themes', True)],
    )
    meta = year
    if is_themed:
        theme = theme_for_bucket(work, month)
        if theme is not None:
            period = theme_ledger_sub(theme)
            if period and period != '未设置周期':
                meta = f'周期：{period}'
    body = (
        f'<div class="page-head">'
        f'<h2 class="page-title">{_esc(display)}</h2>'
        f'<p class="page-meta">{_esc(meta)}</p>'
        f'</div>'
        f'{_gallery_toolbar(len(entries), len(stars), context=gallery_ctx, paginated=paginated)}'
        f'{sheet}'
    )
    return page_shell(
        f'{year}｜{display}',
        body,
        work=work,
        crumbs=[
            ('首页', '/'),
            ('按日期', '/by-date'),
            (year, f'/y/{year}'),
            (display, f'/y/{year}/{urllib.parse.quote(month)}'),
        ],
    )


def render_screenshots(work: Path, thumb_root: Path) -> bytes:
    entries = list_gallery_entries(work, 'screenshots')
    stars = gallery_stars_map(work, 'screenshots', entries)
    paginated = len(entries) > GALLERY_PAGE_SIZE
    sheet = _gallery_sheet_html(
        entries, work, thumb_root, stars, kind='screenshots',
        empty_message='还没有截图',
        empty_text='截图会在整理归档时从 inbox 分出来。归档完成后，这里适合集中清理和快速复核。',
        empty_actions=[('回到图库', '/', False), ('打开按日期', '/by-date', True)],
    )
    body = (
        f'<div class="page-head">'
        f'<h2 class="page-title">截图</h2>'
        f'<p class="page-meta">截图单独分出，适合快速清理和复核。</p>'
        f'</div>'
        f'{_gallery_toolbar(len(entries), len(stars), context="screen", paginated=paginated)}'
        f'{sheet}'
    )
    return page_shell(
        '截图',
        body,
        work=work,
        crumbs=[('首页', '/'), ('截图', '/screenshots')],
    )


def render_screenrecords(work: Path, thumb_root: Path) -> bytes:
    entries = list_gallery_entries(work, 'screenrecords')
    stars = gallery_stars_map(work, 'screenrecords', entries)
    paginated = len(entries) > GALLERY_PAGE_SIZE
    sheet = _gallery_sheet_html(
        entries, work, thumb_root, stars, kind='screenrecords',
        empty_message='还没有录屏',
        empty_text='屏幕录制会在整理归档时进入这里。之后可以在预览里播放、勾选或移至回收站。',
        empty_actions=[('回到图库', '/', False), ('打开截图', '/screenshots', True)],
    )
    body = (
        f'<div class="page-head">'
        f'<h2 class="page-title">录屏</h2>'
        f'<p class="page-meta">录屏集中在这里，方便回看和清理。</p>'
        f'</div>'
        f'{_gallery_toolbar(len(entries), len(stars), context="screen", paginated=paginated)}'
        f'{sheet}'
    )
    return page_shell(
        '录屏',
        body,
        work=work,
        crumbs=[('首页', '/'), ('录屏', '/screenrecords')],
    )


def render_docs(work: Path, thumb_root: Path) -> bytes:
    entries = list_gallery_entries(work, 'docs')
    stars = gallery_stars_map(work, 'docs', entries)
    paginated = len(entries) > GALLERY_PAGE_SIZE
    sheet = _gallery_sheet_html(
        entries, work, thumb_root, stars, kind='docs',
        empty_message='还没有文档照片',
        empty_text='证件、票据和纸面信息需要从任意图库批量选择后手动移入。移入后方便集中查看。',
        empty_actions=[('回到图库', '/', False), ('去截图页选择', '/screenshots', True)],
    )
    body = (
        f'<div class="page-head">'
        f'<h2 class="page-title">文档</h2>'
        f'<p class="page-meta">证件、票据和纸面信息，从图库手动移入。</p>'
        f'</div>'
        f'{_gallery_toolbar(len(entries), len(stars), context="docs", paginated=paginated)}'
        f'{sheet}'
    )
    return page_shell(
        '文档',
        body,
        work=work,
        crumbs=[('首页', '/'), ('文档', '/docs')],
    )


def render_things(work: Path, thumb_root: Path) -> bytes:
    entries = list_gallery_entries(work, 'things')
    stars = gallery_stars_map(work, 'things', entries)
    paginated = len(entries) > GALLERY_PAGE_SIZE
    sheet = _gallery_sheet_html(
        entries, work, thumb_root, stars, kind='things',
        empty_message='还没有物品照片',
        empty_text='设备、包装和物件记录需要从任意图库批量选择后手动移入。适合保存型号、标签和外观。',
        empty_actions=[('回到图库', '/', False), ('去截图页选择', '/screenshots', True)],
    )
    body = (
        f'<div class="page-head">'
        f'<h2 class="page-title">物品</h2>'
        f'<p class="page-meta">设备、包装和物件记录，从图库手动移入。</p>'
        f'</div>'
        f'{_gallery_toolbar(len(entries), len(stars), context="things", paginated=paginated)}'
        f'{sheet}'
    )
    return page_shell(
        '物品',
        body,
        work=work,
        crumbs=[('首页', '/'), ('物品', '/things')],
    )


def render_starred(work: Path, thumb_root: Path) -> bytes:
    """Unified gallery of every starred file across buckets."""
    entries = list_gallery_entries(work, 'starred')
    stars = gallery_stars_map(work, 'starred', entries)
    paginated = len(entries) > GALLERY_PAGE_SIZE
    sheet = _gallery_sheet_html(
        entries, work, thumb_root, stars, kind='starred',
        empty_message='还没有加星内容',
        empty_text='回到任意图库，点缩略图右上角的星标；预览时也可以用底部的加星按钮。',
        empty_actions=[('回到图库', '/', False), ('去截图页加星', '/screenshots', True)],
    )
    body = (
        f'<div class="page-head">'
        f'<h2 class="page-title">加星</h2>'
        f'<p class="page-meta">已加星 {len(entries)} 个，汇总所有桶里的精选。</p>'
        f'</div>'
        f'{_gallery_toolbar(len(entries), len(entries), context="starred", paginated=paginated)}'
        f'{sheet}'
    )
    return page_shell(
        '加星',
        body,
        work=work,
        crumbs=[('首页', '/'), ('加星', '/starred')],
    )


# macOS / Spotlight / Android recycle / version-control noise to skip at directory level
_HIDDEN_DIR_NOISE = frozenset({
    '.DS_Store',  # not a dir, but include for safety
    '.Spotlight-V100', '.Trashes', '.fseventsd',
    '.globalTrash',  # Android / gallery recycle bin
    '.git', '.idea', '.vscode', '.svn', '.hg',
    '__pycache__',
})


def is_user_media_file(path: Path) -> bool:
    """True for countable photo/video files (excludes .DS_Store and dotfiles)."""
    if not path.is_file():
        return False
    if path.name == '.DS_Store':
        return False
    if path.name.startswith('.') and path.name != '.source':
        return False
    if not rename_mod.is_media_file(path):
        return False
    return True


def count_files_in(dir_path: Path) -> int:
    """Count user-visible files under dir_path (aligned with rename_organize.scan_inbox).

    Skipped:
      - files that are not recognized photos/videos
      - any file named exactly .DS_Store
      - any file whose name starts with '.' except '.source' (sidecar);
        Android '.trashed-*' / similar recycle noise is excluded from contact-sheet counts
      - any path whose components include a hidden-dir-noise name
        (e.g. .git/, .Spotlight-V100/, .Trashes/, .globalTrash/)
    """
    if not dir_path.exists():
        return 0
    try:
        n = 0
        rel_base = dir_path.resolve()
        for f in dir_path.rglob('*'):
            if not is_user_media_file(f):
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
    return sum(len(load_stars(work, f.stem)) for f in stars_dir.glob('*.json'))


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


# Top-level dirs created by init_storage.sh (must all exist as directories).
_INIT_SKELETON_DIRS = (
    'inbox', 'by-date', 'screenshots', 'screenrecords', 'docs', 'things',
    '_favorite', '_vlogs', '_trash', '_meta',
)


def ensure_work_dirs(work: Path):
    """Create dirs that newer versions added (safe for older work disks)."""
    (work / 'screenrecords').mkdir(parents=True, exist_ok=True)
    (work / 'screenshots').mkdir(parents=True, exist_ok=True)
    (work / 'docs').mkdir(parents=True, exist_ok=True)
    (work / 'things').mkdir(parents=True, exist_ok=True)


def is_work_initialized(work: Path) -> bool:
    """True if work disk has the init_storage.sh skeleton + _meta/events.yaml file."""
    for name in _INIT_SKELETON_DIRS:
        p = work / name
        if not p.is_dir():
            return False
    return (work / '_meta' / 'events.yaml').is_file()


class Handler(BaseHTTPRequestHandler):
    work: Path = None
    thumb_root: Path = None
    bind_host: str = '127.0.0.1'
    bind_port: int = 8765

    def log_message(self, fmt, *args):
        pass  # quiet

    def _cors_origin_header(self):
        """Return an allowed Origin to echo, or None if none / not allowed."""
        origin = self.headers.get('Origin')
        if origin is None or origin == '':
            return None
        if is_allowed_cors_origin(origin):
            return origin
        return False  # present but disallowed

    def _write_cors_headers(self):
        """Attach CORS headers when Origin is allowed. Returns False if Origin forbidden."""
        echoed = self._cors_origin_header()
        if echoed is False:
            return False
        if echoed is not None:
            self.send_header('Access-Control-Allow-Origin', echoed)
            self.send_header('Vary', 'Origin')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        return True

    def _reject_cors(self):
        body = b'{"ok": false, "error": "origin not allowed"}'
        self.send_response(403)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # CORS preflight (file:// / localhost dashboard → API)
        origin = self.headers.get('Origin')
        if origin is not None and origin != '' and not is_allowed_cors_origin(origin):
            self.send_response(403)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        self.send_response(204)
        if origin:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        """Handle star/unstar JSON requests."""
        # Mutating POSTs: reject cross-site Origins (never reflect *).
        if self.headers.get('Origin') is not None and not is_allowed_cors_origin(
            self.headers.get('Origin')
        ):
            self._reject_cors()
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        # Read body (capped)
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
        except (TypeError, ValueError):
            self._send_json({'ok': False, 'error': 'bad Content-Length'})
            return
        if length < 0 or length > MAX_POST_BODY:
            # Drain a bounded amount so the client is less likely to hang.
            if length > 0:
                try:
                    self.rfile.read(min(length, MAX_POST_BODY + 65536))
                except Exception:
                    pass
            self._send_json({'ok': False, 'error': 'body too large'}, status=413)
            return
        try:
            body_bytes = self.rfile.read(length) if length else b''
            data = json.loads(body_bytes.decode('utf-8')) if body_bytes else {}
        except Exception as e:
            self._send_json({'ok': False, 'error': f'bad json: {e}'})
            return
        qs = {**urllib.parse.parse_qs(parsed.query), **data}

        try:
            if path == '/api/star':
                self._handle_star_api(qs)
                return
            elif path == '/api/run':
                # Body: {"command": "<whitelisted-name>", "backup"?: "<whitelisted path>"}
                # Command names are whitelisted; backup (if any) is sandbox-validated.
                cmd_name = data.get('command') if isinstance(data, dict) else None
                if cmd_name not in RUN_COMMANDS:
                    self._send_json({
                        'ok': False,
                        'error': f'unknown command: {cmd_name!r}',
                        'allowed': sorted(RUN_COMMANDS.keys()),
                    })
                    return
                backup = BACKUP_DEFAULT
                raw_backup = data.get('backup') if isinstance(data, dict) else None
                if raw_backup:
                    try:
                        backup = str(validate_path(str(raw_backup), ALLOWED_BACKUP_PREFIXES, 'backup'))
                    except ValueError as e:
                        self._send_json({'ok': False, 'error': str(e)})
                        return
                argv = RUN_COMMANDS[cmd_name](backup)
                cmd_str = ' '.join(argv)
                self._run_streaming(argv, cmd_str, cmd_name, backup=backup)
                return
            elif path == '/api/runs/cancel':
                self._cancel_active_run()
                return
            elif path == '/api/reclassify':
                action = data.get('action') if isinstance(data, dict) else None
                paths = data.get('paths') if isinstance(data, dict) else None
                if action not in (
                    'to_screen', 'to_normal', 'to_docs', 'to_things', 'to_default_month',
                ):
                    self._send_json({
                        'ok': False,
                        'error': (
                            'action must be to_screen|to_normal|to_docs|'
                            'to_things|to_default_month'
                        ),
                    })
                    return
                if not isinstance(paths, list) or not paths:
                    self._send_json({'ok': False, 'error': 'paths required'})
                    return
                if len(paths) > 500:
                    self._send_json({'ok': False, 'error': 'too many paths (max 500)'})
                    return
                if action == 'to_default_month':
                    results = rename_mod.return_to_default_month_paths(
                        self.work, paths, dry_run=False,
                    )
                else:
                    results = rename_mod.reclassify_paths(
                        self.work, paths, action, dry_run=False,
                    )
                for item in results:
                    if item.get('ok') and item.get('dest') and not item.get('skipped'):
                        migrate_star_path(self.work, item['src'], item['dest'])
                        if item.get('companion_src') and item.get('companion_dest'):
                            migrate_star_path(
                                self.work, item['companion_src'], item['companion_dest'],
                            )
                ok_n = sum(1 for r in results if r.get('ok'))
                if ok_n:
                    clear_web_caches()
                self._send_json({
                    'ok': ok_n == len(results),
                    'results': results,
                    'moved': sum(1 for r in results if r.get('ok') and not r.get('skipped')),
                })
                return
            elif path == '/api/trash':
                paths = data.get('paths') if isinstance(data, dict) else None
                if not isinstance(paths, list) or not paths:
                    self._send_json({'ok': False, 'error': 'paths required'})
                    return
                if len(paths) > 500:
                    self._send_json({'ok': False, 'error': 'too many paths (max 500)'})
                    return
                results = trash_paths(self.work, paths)
                ok_n = sum(1 for r in results if r.get('ok'))
                self._send_json({
                    'ok': ok_n == len(results),
                    'results': results,
                    'moved': ok_n,
                })
                return
            elif path == '/api/events':
                self._handle_events_api(data if isinstance(data, dict) else {})
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
            elif path == '/by-date':
                body = render_by_date_home(self.work)
                self._send(body, 'text/html')
            elif path == '/screenshots':
                body = render_screenshots(self.work, self.thumb_root)
                self._send(body, 'text/html')
            elif path == '/screenrecords':
                body = render_screenrecords(self.work, self.thumb_root)
                self._send(body, 'text/html')
            elif path == '/docs':
                body = render_docs(self.work, self.thumb_root)
                self._send(body, 'text/html')
            elif path == '/things':
                body = render_things(self.work, self.thumb_root)
                self._send(body, 'text/html')
            elif path == '/starred':
                body = render_starred(self.work, self.thumb_root)
                self._send(body, 'text/html')
            elif path == '/api/status':
                # JSON status endpoint for dashboard polling.
                # File counts use get_status_counts (short TTL) so pollStatus
                # every 5s does not full-walk the vault on every request.
                try:
                    code_mtime = datetime.fromtimestamp(
                        Path(__file__).stat().st_mtime
                    ).isoformat(timespec='seconds')
                except OSError:
                    code_mtime = None
                bind_host = self.bind_host or '127.0.0.1'
                bind_port = int(self.bind_port or 8765)
                lan_hint = None
                if bind_host in ('127.0.0.1', 'localhost', '::1'):
                    browse_url = f'http://127.0.0.1:{bind_port}/'
                elif bind_host in ('0.0.0.0', '::', '[::]'):
                    # Wildcard bind: loopback always works; LAN via this host's name.
                    hn = socket.gethostname()
                    if not hn.endswith('.local'):
                        hn = hn + '.local'
                    browse_url = f'http://127.0.0.1:{bind_port}/'
                    lan_hint = f'http://{hn}:{bind_port}/'
                else:
                    browse_url = f'http://{bind_host}:{bind_port}/'
                counts = get_status_counts(self.work)
                status = {
                    'running': True,
                    'work': str(self.work),
                    'initialized': is_work_initialized(self.work),
                    'code_mtime': code_mtime,
                    'host': bind_host,
                    'port': bind_port,
                    'url': browse_url,
                    'dashboard_path': dashboard_file_path(),
                    'dashboard_url': dashboard_file_url(),
                    'inbox': counts['inbox'],
                    'by_date': counts['by_date'],
                    'screenshots': counts['screenshots'],
                    'screenrecords': counts['screenrecords'],
                    'docs': counts['docs'],
                    'things': counts['things'],
                    'vlogs': counts['vlogs'],
                    'trash': counts['trash'],
                    'starred': counts['starred'],
                    'last_sync': last_sync_time(self.work),
                    'pipeline': pipeline_step_markers(self.work),
                }
                if lan_hint:
                    status['lan_hint'] = lan_hint
                self._send_json(status)
            elif path == '/api/runs/active':
                with _RUN_LOCK:
                    active = dict(_ACTIVE_RUN) if _ACTIVE_RUN else None
                self._send_json({'active': active})
            elif path == '/api/runs/latest':
                with _RUN_LOCK:
                    if _ACTIVE_RUN:
                        self._send_json({'run': dict(_ACTIVE_RUN)})
                        return
                latest = load_latest_run_meta(self.work)
                # Orphaned "running" after server restart → surface as error
                if latest and latest.get('status') == 'running':
                    latest = dict(latest)
                    latest['status'] = 'error'
                    latest['error'] = latest.get('error') or 'aborted (server restart)'
                self._send_json({'run': latest})
            elif path.startswith('/api/runs/'):
                self._handle_runs_get(path, qs)
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
                if not is_safe_month_segment(month):
                    self._send(html_error_page('无效路径', '月份名称不合法。'), 'text/html', 400)
                    return
                year_dir = (self.work / 'by-date' / year).resolve()
                month_dir = (self.work / 'by-date' / year / month).resolve()
                if not path_is_under(month_dir, year_dir):
                    self._send(html_error_page('无法打开', '路径不允许访问。'), 'text/html', 403)
                    return
                body = render_bucket(self.work, year, month, self.thumb_root)
                self._send(body, 'text/html')
            elif path == '/raw':
                p = qs.get('p', [''])[0]
                self._send_raw(p)
            elif path == '/thumb':
                p = qs.get('p', [''])[0]
                self._send_thumb(p)
            elif path == '/api/star':
                self._handle_star_api(qs)
            elif path == '/api/gallery':
                kind = (qs.get('kind') or [''])[0]
                year = (qs.get('year') or [''])[0] or None
                month = (qs.get('month') or [''])[0] or None
                if month:
                    month = urllib.parse.unquote(month)
                offset = (qs.get('offset') or ['0'])[0]
                limit = (qs.get('limit') or [str(GALLERY_PAGE_SIZE)])[0]
                payload = build_gallery_page_payload(
                    self.work,
                    self.thumb_root,
                    kind,
                    offset=offset,
                    limit=limit,
                    year=year,
                    month=month,
                )
                status = 200 if payload.get('ok') else 400
                self._send_json(payload, status=status)
            else:
                self._send(html_error_page('未找到', '没有这个页面。'), 'text/html', 404)
        except Exception as e:
            self._send(html_error_page('出错了', str(e)), 'text/html', 500)

    def _send(self, body: bytes, content_type='text/html', status=200):
        self.send_response(status)
        self.send_header('Content-Type', f'{content_type}; charset=utf-8')
        # Same-origin / localhost / file:// only — never reflect * for API responses.
        if not self._write_cors_headers():
            # Origin present but disallowed: still send body for non-JS clients,
            # but omit ACAO so browsers block cross-site reads.
            pass
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self._send(body, 'application/json', status=status)

    def _handle_star_api(self, qs: dict):
        """Toggle/on/off star for a work-relative path; persist under _meta/stars/."""
        raw_path = qs.get('path')
        raw_bucket = qs.get('bucket')
        if raw_path is None or raw_bucket is None:
            self._send_json({'ok': False, 'error': 'path and bucket required'})
            return
        path_q = raw_path[0] if isinstance(raw_path, list) else raw_path
        path_q = urllib.parse.unquote(str(path_q)).strip()
        bucket = raw_bucket[0] if isinstance(raw_bucket, list) else raw_bucket
        bucket = urllib.parse.unquote(str(bucket)).strip()
        if not path_q or not bucket:
            self._send_json({'ok': False, 'error': 'path and bucket required'})
            return
        if not is_safe_star_bucket(bucket):
            self._send_json({'ok': False, 'error': 'invalid bucket'})
            return
        try:
            resolve_stars_path(self.work, bucket)
        except ValueError:
            self._send_json({'ok': False, 'error': 'invalid bucket'})
            return
        # Media path must resolve to an existing file under work.
        full = safe_under_work(self.work, path_q)
        if full is None:
            self._send_json({'ok': False, 'error': 'path outside work'})
            return
        if not full.is_file():
            self._send_json({'ok': False, 'error': 'path not found'})
            return
        action = qs.get('action', ['toggle'])
        if isinstance(action, list):
            action = action[0] if action else 'toggle'
        action = str(action or 'toggle')
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

    def _handle_events_api(self, data: dict):
        """POST /api/events — validate and atomically write _meta/events.yaml."""
        yaml_text = data.get('yaml')
        if not isinstance(yaml_text, str):
            self._send_json({'ok': False, 'error': 'yaml string required'})
            return
        # Normalize newlines; reject null bytes
        if '\x00' in yaml_text:
            self._send_json({'ok': False, 'error': 'invalid yaml content'})
            return
        if not yaml_text.endswith('\n'):
            yaml_text = yaml_text + '\n'
        try:
            themes = rename_mod.parse_events_yaml_text(yaml_text)
            rename_mod.validate_events_themes(themes)
        except ValueError as e:
            self._send_json({'ok': False, 'error': str(e)})
            return

        meta = self.work / '_meta'
        meta.mkdir(parents=True, exist_ok=True)
        dest = meta / 'events.yaml'
        bak = meta / 'events.yaml.bak'
        tmp = meta / 'events.yaml.tmp'
        try:
            if dest.exists():
                shutil.copy2(dest, bak)
            tmp.write_text(yaml_text, encoding='utf-8')
            os.replace(tmp, dest)
            clear_web_caches()
        except Exception as e:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            self._send_json({'ok': False, 'error': f'write failed: {e}'})
            return
        self._send_json({
            'ok': True,
            'themes': len(themes),
            'path': '_meta/events.yaml',
            'bak': '_meta/events.yaml.bak' if bak.exists() else None,
        })

    def _handle_runs_get(self, path: str, qs: dict):
        """GET /api/runs/<id> or /api/runs/<id>/log?offset=N (active/latest handled above)."""
        parts = path.strip('/').split('/')
        # ['api', 'runs', '<id>'] or ['api', 'runs', '<id>', 'log']
        if len(parts) < 3:
            self._send(b'404 Not Found', 'text/plain', 404)
            return
        run_id = parts[2]
        if not _RUN_ID_RE.match(run_id):
            self._send(b'404 Not Found', 'text/plain', 404)
            return

        if len(parts) == 3:
            meta = load_run_meta(self.work, run_id)
            if meta is None:
                self._send(b'404 Not Found', 'text/plain', 404)
                return
            self._send_json(meta)
            return

        if len(parts) == 4 and parts[3] == 'log':
            meta = load_run_meta(self.work, run_id)
            if meta is None:
                self._send(b'404 Not Found', 'text/plain', 404)
                return
            try:
                offset = int((qs.get('offset') or ['0'])[0] or 0)
            except ValueError:
                offset = 0
            if offset < 0:
                offset = 0

            log_rel = meta.get('log') or f'_meta/logs/runs/{run_id}.log'
            log_path = (self.work / log_rel).resolve()
            work_res = self.work.resolve()
            if not str(log_path).startswith(str(work_res) + os.sep) and log_path != work_res:
                self._send(b'forbidden', 'text/plain', 403)
                return

            # Cap each read so huge run logs (dedupe etc.) cannot freeze clients.
            LOG_CHUNK_MAX = 64 * 1024
            chunk = ''
            next_offset = offset
            if log_path.is_file():
                size = log_path.stat().st_size
                if offset > size:
                    offset = size
                with open(log_path, 'rb') as f:
                    f.seek(offset)
                    data = f.read(LOG_CHUNK_MAX)
                next_offset = offset + len(data)
                chunk = data.decode('utf-8', errors='replace')

            # Prefer live status from _ACTIVE_RUN when this is the active one.
            # If meta still says running but nothing is active (server restart),
            # treat as aborted so clients can eof.
            status = meta.get('status') or 'error'
            with _RUN_LOCK:
                if _ACTIVE_RUN and _ACTIVE_RUN.get('id') == run_id:
                    status = _ACTIVE_RUN.get('status') or status
                elif status == 'running':
                    status = 'error'

            if status == 'running':
                eof = False
            elif log_path.is_file():
                eof = next_offset >= log_path.stat().st_size
            else:
                eof = True

            self._send_json({
                'id': run_id,
                'offset': offset,
                'next_offset': next_offset,
                'chunk': chunk,
                'eof': eof,
                'status': status,
            })
            return

        self._send(b'404 Not Found', 'text/plain', 404)

    def _begin_ndjson(self):
        """Start an NDJSON streaming response (no Content-Length)."""
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self._write_cors_headers()
        self.send_header('Connection', 'close')
        self.end_headers()

    def _emit_ndjson(self, obj):
        """Write one NDJSON event. Returns False if the client is gone."""
        try:
            line = (json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8')
            self.wfile.write(line)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return False

    def _finish_run_meta(self, meta: dict, status: str, rc=None, error=None):
        global _ACTIVE_RUN, _ACTIVE_PROC, _CANCEL_REQUESTED
        meta['finished_at'] = datetime.now().isoformat(timespec='seconds')
        meta['status'] = status
        meta['rc'] = rc
        meta['error'] = error
        persist_run_meta(self.work, meta)
        with _RUN_LOCK:
            if _ACTIVE_RUN and _ACTIVE_RUN.get('id') == meta['id']:
                _ACTIVE_RUN = None
            _ACTIVE_PROC = None
            _CANCEL_REQUESTED = False

    def _cancel_active_run(self):
        """POST /api/runs/cancel — terminate the active /api/run subprocess."""
        global _CANCEL_REQUESTED
        with _RUN_LOCK:
            meta = dict(_ACTIVE_RUN) if _ACTIVE_RUN else None
            proc = _ACTIVE_PROC
            running = bool(meta and meta.get('status') == 'running')
            if running:
                _CANCEL_REQUESTED = True
        if not running:
            self._send_json({'ok': False, 'error': '没有运行中的任务'})
            return
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._send_json({
            'ok': True,
            'cancelled': True,
            'run_id': meta.get('id') if meta else None,
            'command_name': meta.get('command_name') if meta else None,
        })

    def _run_streaming(self, argv, cmd_str: str, cmd_name: str, backup: str = None):
        """Run argv, stream NDJSON, and persist to _meta/logs/runs/.

        Client disconnect does not kill the subprocess — output keeps going to
        the log file so the dashboard can resume via /api/runs/<id>/log.
        Use POST /api/runs/cancel to terminate an active run.
        """
        global _ACTIVE_RUN, _ACTIVE_PROC, _CANCEL_REQUESTED

        if backup is None:
            backup = BACKUP_DEFAULT

        with _RUN_LOCK:
            if _ACTIVE_RUN is not None and _ACTIVE_RUN.get('status') == 'running':
                self._send_json({
                    'ok': False,
                    'error': '已有任务在运行',
                    'active': dict(_ACTIVE_RUN),
                })
                return
            run_id = make_run_id(cmd_name)
            log_rel = f'_meta/logs/runs/{run_id}.log'
            meta = {
                'id': run_id,
                'command_name': cmd_name,
                'command': cmd_str,
                'started_at': datetime.now().isoformat(timespec='seconds'),
                'finished_at': None,
                'status': 'running',
                'rc': None,
                'error': None,
                'log': log_rel,
            }
            runs_dir(self.work).mkdir(parents=True, exist_ok=True)
            persist_run_meta(self.work, meta)
            _ACTIVE_RUN = dict(meta)
            _CANCEL_REQUESTED = False
            _ACTIVE_PROC = None

        log_path = self.work / log_rel
        log_fp = None
        started = False
        proc = None
        client_ok = True
        finalized = False

        def append_log(stream: str, text: str):
            if log_fp is None:
                return
            try:
                log_fp.write(f'[{stream}] {text}\n')
                log_fp.flush()
            except Exception:
                pass

        def emit(obj):
            nonlocal client_ok
            if not client_ok:
                return
            if not self._emit_ndjson(obj):
                client_ok = False

        try:
            try:
                proc = subprocess.Popen(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    # WORK/BACKUP must match this server / dashboard so CLI helpers
                    # (web stop, pipeline sync) hit the same disks (env beats config).
                    env={
                        **os.environ,
                        'PYTHONUNBUFFERED': '1',
                        'WORK': str(self.work),
                        'BACKUP': str(backup),
                    },
                )
                with _RUN_LOCK:
                    _ACTIVE_PROC = proc
            except FileNotFoundError as e:
                err = f'not found: {e}'
                self._finish_run_meta(meta, 'error', rc=None, error=err)
                finalized = True
                self._send_json({'ok': False, 'command': cmd_str, 'error': err, 'run_id': run_id})
                return
            except Exception as e:
                err = str(e)
                self._finish_run_meta(meta, 'error', rc=None, error=err)
                finalized = True
                self._send_json({'ok': False, 'command': cmd_str, 'error': err, 'run_id': run_id})
                return

            log_fp = open(log_path, 'a', encoding='utf-8')

            try:
                self._begin_ndjson()
                started = True
                emit({
                    'type': 'start',
                    'run_id': run_id,
                    'command': cmd_str,
                })

                q = queue.Queue()

                def _reader(stream, event_type):
                    try:
                        for line in stream:
                            q.put((event_type, line.rstrip('\n')))
                    except Exception:
                        pass
                    finally:
                        q.put((None, event_type))

                t_out = threading.Thread(target=_reader, args=(proc.stdout, 'stdout'), daemon=True)
                t_err = threading.Thread(target=_reader, args=(proc.stderr, 'stderr'), daemon=True)
                t_out.start()
                t_err.start()

                timeout_sec = effective_run_timeout_sec()
                deadline = (time.monotonic() + timeout_sec) if timeout_sec is not None else None
                done = {'stdout': False, 'stderr': False}
                timed_out = False

                while not (done['stdout'] and done['stderr']):
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            timed_out = True
                            break
                        wait = min(0.5, remaining)
                    else:
                        wait = 0.5
                    try:
                        kind, payload = q.get(timeout=wait)
                    except queue.Empty:
                        if proc.poll() is not None and not t_out.is_alive() and not t_err.is_alive():
                            while True:
                                try:
                                    kind, payload = q.get_nowait()
                                except queue.Empty:
                                    break
                                if kind is None:
                                    done[payload] = True
                                else:
                                    append_log(kind, payload)
                                    emit({'type': kind, 'line': payload})
                            break
                        continue
                    if kind is None:
                        done[payload] = True
                    else:
                        append_log(kind, payload)
                        emit({'type': kind, 'line': payload})

                if timed_out:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
                    while True:
                        try:
                            kind, payload = q.get_nowait()
                        except queue.Empty:
                            break
                        if kind is not None:
                            append_log(kind, payload)
                            emit({'type': kind, 'line': payload})
                    err = f'timeout ({timeout_sec}s)'
                    emit({
                        'type': 'error',
                        'error': err,
                        'command': cmd_str,
                        'run_id': run_id,
                    })
                    self._finish_run_meta(meta, 'error', rc=None, error=err)
                    finalized = True
                else:
                    rc = proc.wait()
                    t_out.join(timeout=2)
                    t_err.join(timeout=2)
                    while True:
                        try:
                            kind, payload = q.get_nowait()
                        except queue.Empty:
                            break
                        if kind is not None:
                            append_log(kind, payload)
                            emit({'type': kind, 'line': payload})
                    with _RUN_LOCK:
                        cancelled = _CANCEL_REQUESTED
                    if cancelled:
                        err = '已打断'
                        append_log('stderr', err)
                        emit({
                            'type': 'error',
                            'error': err,
                            'command': cmd_str,
                            'run_id': run_id,
                        })
                        self._finish_run_meta(meta, 'cancelled', rc=rc, error=err)
                    else:
                        emit({
                            'type': 'end',
                            'ok': rc == 0,
                            'rc': rc,
                            'run_id': run_id,
                        })
                        if rc == 0:
                            self._finish_run_meta(meta, 'ok', rc=rc, error=None)
                        else:
                            self._finish_run_meta(
                                meta, 'error', rc=rc, error=f'exit {rc}'
                            )
                    finalized = True
            except Exception as e:
                if started:
                    emit({
                        'type': 'error',
                        'error': str(e),
                        'command': cmd_str,
                        'run_id': run_id,
                    })
                else:
                    self._send_json({
                        'ok': False,
                        'command': cmd_str,
                        'error': str(e),
                        'run_id': run_id,
                    })
                if proc is not None and proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                self._finish_run_meta(meta, 'error', rc=None, error=str(e))
                finalized = True
        finally:
            if log_fp is not None:
                try:
                    log_fp.close()
                except Exception:
                    pass
            if not finalized:
                # Unexpected exit without finalize — mark error & clear active
                with _RUN_LOCK:
                    if _ACTIVE_RUN and _ACTIVE_RUN.get('id') == run_id:
                        meta['finished_at'] = datetime.now().isoformat(timespec='seconds')
                        meta['status'] = 'error'
                        meta['error'] = meta.get('error') or 'aborted'
                        persist_run_meta(self.work, meta)
                        _ACTIVE_RUN = None
                        if proc is not None and proc.poll() is None:
                            try:
                                proc.kill()
                            except Exception:
                                pass
            else:
                with _RUN_LOCK:
                    if _ACTIVE_RUN and _ACTIVE_RUN.get('id') == run_id:
                        _ACTIVE_RUN = None

    def _send_raw(self, rel_path: str):
        """Serve original media; supports HTTP Range for video seeking."""
        if not rel_path:
            self._send(b'missing path', 'text/plain', 400); return
        full = safe_under_work(self.work, rel_path)
        if full is None:
            self._send(b'forbidden', 'text/plain', 403); return
        if not full.exists() or not full.is_file():
            self._send(b'not found', 'text/plain', 404); return
        ctype, _ = mimetypes.guess_type(str(full))
        ctype = ctype or 'application/octet-stream'
        size = full.stat().st_size
        range_header = self.headers.get('Range')
        if range_header:
            m = re.match(r'bytes=(\d+)-(\d*)', range_header)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
                end = min(end, size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.end_headers()
                    return
                length = end - start + 1
                self.send_response(206)
                self.send_header('Content-Type', ctype)
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                self.send_header('Content-Length', str(length))
                self.end_headers()
                with open(full, 'rb') as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(size))
        self.end_headers()
        with open(full, 'rb') as f:
            shutil.copyfileobj(f, self.wfile)

    def _send_thumb(self, rel_path: str):
        """Serve thumbnail (same work fence as /raw; output under thumb_root)."""
        if not rel_path:
            self._send(b'missing', 'text/plain', 400); return
        full = safe_under_work(self.work, rel_path)
        if full is None:
            self._send(b'forbidden', 'text/plain', 403); return
        if not full.is_file():
            self._send(b'not found', 'text/plain', 404); return
        thumb = thumb_for(full, self.work, self.thumb_root)
        if not thumb or not thumb.exists():
            self._send(b'no thumb', 'text/plain', 404); return
        thumb_res = thumb.resolve()
        thumb_root_res = self.thumb_root.resolve()
        if not path_is_under(thumb_res, thumb_root_res):
            self._send(b'forbidden', 'text/plain', 403); return
        try:
            st = thumb_res.stat()
        except OSError:
            self._send(b'no thumb', 'text/plain', 404); return
        # ETag from thumb mtime+size so browsers can revalidate without re-download.
        etag = f'W/"{st.st_mtime_ns:x}-{st.st_size:x}"'
        cache_ctrl = 'private, max-age=86400'
        inm = self.headers.get('If-None-Match')
        if inm and etag in {t.strip() for t in inm.split(',')}:
            self.send_response(304)
            self.send_header('ETag', etag)
            self.send_header('Cache-Control', cache_ctrl)
            self.send_header(
                'Last-Modified',
                email.utils.formatdate(st.st_mtime, usegmt=True),
            )
            self.end_headers()
            return
        try:
            data = thumb_res.read_bytes()
        except OSError:
            self._send(b'no thumb', 'text/plain', 404); return
        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Cache-Control', cache_ctrl)
        self.send_header('ETag', etag)
        self.send_header(
            'Last-Modified',
            email.utils.formatdate(st.st_mtime, usegmt=True),
        )
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def _theme_bucket_href(month: str, name: str) -> str:
        """Browse link for a theme side-bucket only (never default YYYY-MM/).

        Theme dirs are created by sync/rebucket. Until then the URL still points
        at YYYY-MM_<name>; render_bucket shows an empty gallery if missing.
        """
        month_s = str(month or '').strip()
        name_s = str(name or '').strip()
        if not re.match(r'^\d{4}-\d{2}$', month_s) or not name_s:
            return '/themes'
        year = month_s[:4]
        themed = f'{month_s}_{name_s}'
        return f'/y/{year}/{urllib.parse.quote(themed)}'

    @staticmethod
    def _theme_ledger_sub(theme: dict) -> str:
        return theme_ledger_sub(theme)

    @staticmethod
    def _theme_ledger_stats(theme: dict) -> str:
        bits = []
        sources = theme.get('sources')
        if isinstance(sources, list) and sources:
            bits.append('｜'.join(str(s) for s in sources if str(s).strip()))
        files = theme.get('files')
        if isinstance(files, list) and files:
            bits.append(f'{len(files)} 个文件')
        return '｜'.join(b for b in bits if b) or ''

    def _render_themes(self) -> bytes:
        path = self.work / '_meta' / 'events.yaml'
        crumbs = [('首页', '/'), ('主题', '/themes')]
        exists = path.exists()
        text = path.read_text(encoding='utf-8') if exists else EMPTY_EVENTS_YAML
        sync_all_cmd = (
            f"WORK={shlex.quote(str(self.work))} "
            f"{shlex.quote(str(PICVAULT_BIN))} theme rebucket --all"
        )
        if not PICVAULT_BIN.is_file():
            sync_all_cmd = (
                f"WORK={shlex.quote(str(self.work))} "
                f"python3 {shlex.quote(str(RENAME_SCRIPT))} "
                f"--rebucket-themes --all --dry-run"
            )

        def _theme_sync_cmd(theme_name: str) -> str:
            if PICVAULT_BIN.is_file():
                return (
                    f"WORK={shlex.quote(str(self.work))} "
                    f"{shlex.quote(str(PICVAULT_BIN))} theme rebucket "
                    f"--theme {shlex.quote(theme_name)}"
                )
            return (
                f"WORK={shlex.quote(str(self.work))} "
                f"python3 {shlex.quote(str(RENAME_SCRIPT))} "
                f"--rebucket-themes --theme {shlex.quote(theme_name)} --dry-run"
            )

        parse_err = None
        themes: list = []
        try:
            themes = rename_mod.parse_events_yaml_text(text)
            rename_mod.validate_events_themes(themes)
        except ValueError as e:
            parse_err = str(e)
            themes = []

        rows = []
        for t in themes:
            name = str(t.get('name') or '').strip() or '（未命名）'
            month = str(t.get('month') or '').strip()
            href = self._theme_bucket_href(month, name)
            sub = self._theme_ledger_sub(t)
            stats = self._theme_ledger_stats(t)
            sync_one = _theme_sync_cmd(name) if str(t.get('name') or '').strip() else ''
            sync_btn = (
                f'<button type="button" class="ledger-sync" data-sync-cmd="{_esc(sync_one)}" '
                f'aria-label="复制此主题试跑命令；不会搬文件，确认后再加 --yes。">'
                f'复制试跑命令</button>'
                if sync_one else ''
            )
            rows.append(
                f'<div class="ledger-row">'
                f'<a class="ledger-key" href="{_esc(href)}">{_esc(name)}</a>'
                f'<span class="ledger-sub">{_esc(sub)}</span>'
                f'<span class="ledger-stats">{_esc(stats)}</span>'
                f'<span class="ledger-actions">{sync_btn}'
                f'<a class="ledger-go" href="{_esc(href)}" aria-hidden="true">›</a>'
                f'</span>'
                f'</div>'
            )

        if parse_err:
            ledger = _empty_state(
                '主题配置无法解析',
                '展开下方编辑配置，按提示修正 YAML 后再保存。保存只写配置，不会搬文件。',
                [],
                kicker='配置有误',
            )
            hint = (
                f'<p class="events-hint err">配置有误：{_esc(parse_err)}</p>'
            )
            fold_note = ''
            fold_open = ' open'
        elif not rows:
            ledger = _empty_state(
                '还没有主题',
                '展开下方编辑配置，按示例添加旅行或事件。保存只写配置；真正搬文件前，先复制试跑命令看清计划。',
                [],
            )
            hint = ''
            fold_note = (
                '<p class="events-fold-note">保存只写配置，不搬文件。保存后回到主题行，先复制试跑命令；确认无误后再在终端加 --yes。</p>'
            )
            fold_open = ''
        else:
            ledger = f'<div class="ledger">{"".join(rows)}</div>'
            hint = ''
            fold_note = (
                '<p class="events-fold-note">保存只写配置。真正搬文件前，先复制对应主题的试跑命令；看清计划后再加 --yes。</p>'
            )
            fold_open = ''

        if not exists and not parse_err:
            hint = (
                '<p class="events-hint">尚未创建配置文件；保存时会写入配置。</p>'
            )

        body = (
            '<div class="page-head">'
            '<div>'
            '<h2 class="page-title">主题</h2>'
            '<p class="page-lede">按月份命名旅行与事件。</p>'
            '</div>'
            '</div>'
            f'{hint}'
            f'{ledger}'
            f'<details class="events-fold"{fold_open}>'
            '<summary>编辑配置</summary>'
            '<div class="events-editor">'
            f'{fold_note}'
            '<div class="events-toolbar">'
            '<button type="button" class="primary" id="eventsSave">保存</button>'
            '<button type="button" id="eventsReload">重新加载</button>'
            '<button type="button" id="eventsCopySyncAll">复制全部试跑命令</button>'
            '<span class="events-status" id="eventsStatus"></span>'
            '</div>'
            f'<textarea id="eventsYaml" spellcheck="false">{_esc(text)}</textarea>'
            '</div>'
            '</details>'
            '<script>(function(){\n'
            'var ta=document.getElementById("eventsYaml");\n'
            'var st=document.getElementById("eventsStatus");\n'
            'var saveBtn=document.getElementById("eventsSave");\n'
            'var fold=document.querySelector(".events-fold");\n'
            'var initial=ta.value;\n'
            'var syncAllCmd=' + json.dumps(sync_all_cmd) + ';\n'
            'function setStatus(msg, cls){st.textContent=msg||"";st.className="events-status"+(cls?" "+cls:"");}\n'
            'function dirty(){return ta.value!==initial;}\n'
            'function copyCmd(cmd, okMsg){\n'
            '  navigator.clipboard.writeText(cmd).then(function(){setStatus(okMsg,"ok");},\n'
            '    function(){setStatus("复制失败：请手动复制终端命令","err");});\n'
            '}\n'
            'ta.addEventListener("input",function(){if(dirty()&&fold&&!fold.open)fold.open=true;});\n'
            'window.addEventListener("beforeunload",function(e){if(!dirty())return;e.preventDefault();e.returnValue="";});\n'
            'document.getElementById("eventsReload").addEventListener("click",function(){\n'
            '  if(dirty()&&!confirm("丢弃未保存的修改？"))return;\n'
            '  location.reload();\n'
            '});\n'
            'document.getElementById("eventsCopySyncAll").addEventListener("click",function(){\n'
            '  if(!confirm("全量同步会检查每一个主题桶，可能覆盖手工调整。复制的是试跑命令，不会搬文件；确认后再加 --yes。仍要复制？"))return;\n'
            '  copyCmd(syncAllCmd,"已复制全部试跑命令；确认后再加 --yes");\n'
            '});\n'
            'document.querySelectorAll(".ledger-sync").forEach(function(btn){\n'
            '  btn.addEventListener("click",function(e){\n'
            '    e.preventDefault();e.stopPropagation();\n'
            '    var cmd=btn.getAttribute("data-sync-cmd")||"";\n'
            '    if(!cmd){setStatus("无法生成同步命令","err");return;}\n'
            '    copyCmd(cmd,"已复制该主题试跑命令；确认后再加 --yes");\n'
            '  });\n'
            '});\n'
            'saveBtn.addEventListener("click",async function(){\n'
            '  saveBtn.disabled=true;setStatus("保存中…","");\n'
            '  try{\n'
            '    var r=await fetch("/api/events",{method:"POST",headers:{"Content-Type":"application/json"},\n'
            '      body:JSON.stringify({yaml:ta.value})});\n'
            '    var data=await r.json();\n'
            '    if(!data.ok){\n'
            '      setStatus(data.error||"保存失败","err");\n'
            '      if(fold)fold.open=true;\n'
            '      return;\n'
            '    }\n'
            '    initial=ta.value;\n'
            '    setStatus("已保存 "+data.themes+" 个主题。先复制试跑命令；确认后再搬文件。","ok");\n'
            '    setTimeout(function(){location.reload();},600);\n'
            '  }catch(e){setStatus(String(e),"err");if(fold)fold.open=true;}\n'
            '  finally{saveBtn.disabled=false;}\n'
            '});\n'
            '})();</script>'
        )
        return page_shell('主题', body, work=self.work, crumbs=crumbs)


def main():
    parser = argparse.ArgumentParser(description='PicVault web browser')
    parser.add_argument('--work', default='/Volumes/Storage')
    parser.add_argument(
        '--host', default='127.0.0.1',
        help='Bind address (default 127.0.0.1; pass 0.0.0.0 for LAN)',
    )
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()

    try:
        work = validate_path(args.work, ALLOWED_WORK_PREFIXES, 'work')
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.host in ('0.0.0.0', '::', '[::]'):
        print(
            f"WARNING: binding to {args.host} exposes the gallery on all "
            f"interfaces. Prefer --host 127.0.0.1 unless you need LAN access.",
            file=sys.stderr,
        )

    thumb_root = work / THUMB_CACHE
    thumb_root.mkdir(parents=True, exist_ok=True)
    ensure_work_dirs(work)

    Handler.work = work
    Handler.thumb_root = thumb_root
    Handler.bind_host = args.host
    Handler.bind_port = args.port

    # Build argv factories: each takes a sandbox-validated backup path.
    w = str(work)
    pb = str(PICVAULT_BIN)

    RUN_COMMANDS.update({
        # --- read-only / status ---
        'status':       lambda _b: [pb, 'status'],
        'doctor':       lambda _b: [pb, 'doctor'],

        # --- init / web lifecycle ---
        'init':         lambda _b: ['bash', str(INIT_SCRIPT), '--work', w],
        'web_stop':     lambda _b: [pb, 'web', 'stop'],

        # --- dedupe (dry-run by default; apply moves files) ---
        'dedupe_dry':   lambda _b: ['python3', str(DEDUPE_SCRIPT), '--work', w, '--dry-run'],
        'dedupe_apply': lambda _b: ['python3', str(DEDUPE_SCRIPT), '--work', w],

        # --- rename + organize ---
        'rename_dry':   lambda _b: ['python3', str(RENAME_SCRIPT), '--work', w, '--dry-run'],
        'rename_apply': lambda _b: ['python3', str(RENAME_SCRIPT), '--work', w],

        # --- sync to backup (rsync; apply really mirrors) ---
        'sync_verify':  lambda b: ['bash', str(SYNC_SCRIPT), '--work', w, '--backup', b, '--verify', '--dry-run'],
        'sync_apply':   lambda b: ['bash', str(SYNC_SCRIPT), '--work', w, '--backup', b, '--verify'],

        # --- one-shot pipeline (picvault uses WORK/BACKUP from env) ---
        'pipeline':     lambda _b: [pb, 'pipeline', '--yes'],
    })

    # Try to get hostname for display
    # mDNS .local hostname is already returned by gethostname(); only append if missing
    hostname = socket.gethostname()
    if not hostname.endswith('.local'):
        hostname = hostname + '.local'
    if args.host in ('127.0.0.1', 'localhost', '::1'):
        url = f"http://127.0.0.1:{args.port}/"
    else:
        url = f"http://{hostname}:{args.port}/"

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    # OSC 8 hyperlink: makes the URL clickable in iTerm2, Terminal.app 12.5+, WezTerm, etc.
    # Falls back to plain text in older terminals.
    OSC8_START = '\033]8;;'
    OSC8_END = '\033]8;;\033\\'
    try:
        code_mtime = datetime.fromtimestamp(
            Path(__file__).stat().st_mtime
        ).isoformat(timespec='seconds')
    except OSError:
        code_mtime = '?'
    print(f"✓ PicVault web at {OSC8_START}{url}{OSC8_END}{url}\033[0m")
    print(f"  Work: {work}")
    print(f"  Thumbnails: {thumb_root}")
    print(f"  Code: {Path(__file__).resolve()} (mtime {code_mtime})")
    print(f"  Press Ctrl-C to stop")
    print(f"  Note: edit web_browse.py → restart this process (no auto-reload)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == '__main__':
    main()
