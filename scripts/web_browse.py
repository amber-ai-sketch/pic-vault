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

ALLOWED_WORK_PREFIXES = ('/Volumes/Storage', '/Volumes/YM/MediaVault')
ALLOWED_BACKUP_PREFIXES = ('/Volumes/WD4T/MediaVault', '/Volumes/YM/MediaVault')
THUMB_CACHE = '_meta/thumbs'
VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.m4v', '.3gp', '.hevc', '.webm'}
RUN_TIMEOUT_SEC = None  # 不超时；长任务实质不限时
# 仅当 RUN_TIMEOUT_SEC 为 None 时作为极长兜底；再设为 None 则完全无限
RUN_HARD_CAP_SEC = 7 * 24 * 3600
MAX_POST_BODY = 2 * 1024 * 1024  # 2 MiB
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

EMPTY_EVENTS_YAML = """# 主题配置（rename_organize / Web /themes）
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
# 字段：name 必填；month(YYYY-MM) 有 start 时可省略；date_range / sources / files 至少其一
# date_range 可跨月；主题桶 = by-date/<开始年>/<开始月>_<名>/
# 保存后按主题同步：picvault theme rebucket --theme <名> （先 dry-run，再 --yes）
# （只扫该主题；可迁出到其它主题；全量用 --all）
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
    """Chinese ledger-stats line; omit zero star/Live to keep rows readable."""
    parts = [f'{photos} 张', f'{videos} 视频']
    if stars:
        parts.append(f'{stars} 加星')
    if lives:
        parts.append(f'{lives} Live')
    return ' · '.join(parts)


def bucket_month_key(name: str) -> str:
    """YYYY-MM prefix of a by-date bucket folder name."""
    return name.split('_', 1)[0] if '_' in name else name


def bucket_display_name(name: str) -> str:
    """Short gallery/ledger title: theme name or YYYY-MM (not full folder)."""
    if '_' in name:
        return name.split('_', 1)[1]
    return name


def peek_bucket_previews(work: Path, year: str, bucket: str, limit: int = 3) -> list[str]:
    """Cheap photo previews for ledger rows (relative paths). Prefer stills."""
    if limit <= 0:
        return []
    month_dir = work / 'by-date' / year / bucket
    photos = month_dir / 'photos'
    out: list[str] = []
    if photos.is_dir():
        try:
            entries = sorted(photos.iterdir(), key=lambda p: p.name)
        except OSError:
            entries = []
        for f in entries:
            if not is_user_media_file(f):
                continue
            if f.suffix.lower() in VIDEO_EXTS:
                continue
            out.append(str(f.relative_to(work)))
            if len(out) >= limit:
                return out
    if len(out) >= limit:
        return out[:limit]
    videos = month_dir / 'videos'
    if videos.is_dir():
        try:
            entries = sorted(videos.iterdir(), key=lambda p: p.name)
        except OSError:
            entries = []
        for f in entries:
            if is_user_media_file(f):
                out.append(str(f.relative_to(work)))
                if len(out) >= limit:
                    break
    return out[:limit]


def peek_year_previews(work: Path, year: str, months: list, limit: int = 3) -> list[str]:
    """Up to `limit` previews across a year's non-empty buckets (newest first)."""
    out: list[str] = []
    ordered = sorted(months, key=lambda m: m.get('name') or '', reverse=True)
    for m in ordered:
        if int(m.get('photos') or 0) + int(m.get('videos') or 0) <= 0:
            continue
        need = limit - len(out)
        if need <= 0:
            break
        out.extend(peek_bucket_previews(work, year, m['name'], limit=need))
    return out[:limit]


def ledger_previews_html(rels: list[str]) -> str:
    if not rels:
        return ''
    imgs = []
    for rel in rels:
        q = urllib.parse.quote(rel)
        imgs.append(
            f'<img src="/thumb?p={_esc(q)}" alt="" loading="lazy" decoding="async">'
        )
    return f'<span class="ledger-previews" aria-hidden="true">{"".join(imgs)}</span>'



# Short TTL caches for ThreadingHTTPServer (lock around dict mutations).
# Correct enough for a personal vault; dashboard polls every ~5s so 10–15s
# avoids full directory walks on every hit without feeling stale.
_CACHE_LOCK = threading.Lock()
_TOPBAR_CACHE_TTL = 15.0
_STATUS_COUNTS_TTL = 10.0
# work_key -> {'expires': float, 'sig': tuple, 'buckets': dict, 'star_n': int}
_topbar_cache: dict = {}
# work_key -> {'expires': float, 'sig': tuple, 'counts': dict}
_status_counts_cache: dict = {}

_TOPBAR_MTIME_ROOTS = (
    'by-date', 'screenshots', 'screenrecords', 'docs', 'things', '_meta/stars',
)
_STATUS_MTIME_ROOTS = (
    'inbox', 'by-date', 'screenshots', 'screenrecords', 'docs', 'things',
    '_vlogs', '_trash', '_meta/stars',
)


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


def scan_buckets(work: Path) -> dict:
    """Scan by-date/, screenshots/, screenrecords/, docs/, things/ for bucket info."""
    result = {
        'years': {},
        'screenshots_count': 0,
        'screenrecords_count': 0,
        'docs_count': 0,
        'things_count': 0,
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
            return {k: True for k in data}
        return {k: bool(v) for k, v in data.items() if v}
    except Exception:
        return {}


def save_stars(work: Path, bucket: str, stars: dict):
    path = resolve_stars_path(work, bucket)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: True for k in stars}, indent=2, ensure_ascii=False))


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
    return results


# —— Browse UI (aligned with dashboard: pure white / black / soft gray) ——


def _esc(s) -> str:
    return html_lib.escape(str(s), quote=True)


PAGE_CSS = '''
:root {
  --paper: #FFFFFF;
  --mist: #F4F4F4;
  --ink: #111111;
  --muted: #6B6B6B;
  --line: #E6E6E6;
  --live: #1F7A4D;
  --warn: #8A6A1F;
  --hazard: #9B2C2C;
  --bg: var(--paper);
  --soft: var(--mist);
  --sans: "Geist Sans", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  min-height: 100vh;
  font-family: var(--sans);
  color: var(--ink);
  line-height: 1.5;
  background: var(--paper);
  -webkit-font-smoothing: antialiased;
}
a { color: var(--ink); text-decoration: none; }
a:hover { text-decoration: underline; text-underline-offset: 3px; }
:focus-visible { outline: 1px solid var(--ink); outline-offset: 3px; }

.wrap { max-width: 1120px; margin: 0 auto; padding: 28px 28px 72px; }

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
  font-size: 0.82rem;
  color: var(--muted);
  text-decoration: none;
  padding: 2px 4px;
}
.crumbs a:hover { color: var(--ink); text-decoration: none; background: var(--mist); }
.crumbs a.here { color: var(--ink); font-weight: 500; }
.crumbs .sep { color: var(--line); user-select: none; padding: 2px 0; }

.jumps {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 14px;
  align-items: center;
  justify-content: flex-end;
}
.jumps a, .jumps-more > summary {
  font-size: 0.78rem;
  color: var(--muted);
  text-decoration: none;
  letter-spacing: 0.01em;
}
.jumps a:hover { color: var(--ink); text-decoration: none; }
.jumps a .n {
  font-family: var(--mono);
  font-size: 0.7rem;
  color: var(--muted);
}
.jumps a#consoleLink { font-weight: 500; color: var(--ink); }
.jumps-more {
  position: relative;
}
.jumps-more > summary {
  list-style: none;
  cursor: pointer;
  font-weight: 500;
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
  min-width: 10.5rem;
  padding: 8px 0;
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
  padding: 8px 14px;
  color: var(--muted);
  text-decoration: none;
  white-space: nowrap;
}
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
  font-size: 0.92rem;
  color: var(--muted);
  max-width: 36em;
}
.page-meta {
  font-family: var(--mono);
  font-size: 0.7rem;
  color: var(--muted);
  letter-spacing: 0.02em;
}
.section-label {
  font-size: 0.72rem;
  font-weight: 500;
  color: var(--muted);
  margin: 0 0 14px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
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
  font-family: var(--mono);
  font-size: 0.7rem;
  color: var(--muted);
  margin-right: auto;
  letter-spacing: 0.02em;
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
  gap: 4px;
  align-items: center;
  width: 100%;
  padding-top: 8px;
  margin-top: 4px;
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
  font-size: 0.78rem;
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
  font-size: 0.68rem;
  color: var(--muted);
  margin-left: 4px;
}
.btn-reclass {
  font-family: var(--sans);
  font-weight: 500;
  font-size: 0.78rem;
  padding: 5px 12px;
  border: none;
  border-radius: 0;
  background: transparent;
  color: var(--ink);
  cursor: pointer;
  transition: background .12s;
}
.btn-reclass:hover { background: var(--paper); }
.btn-reclass:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-trash {
  font-family: var(--sans);
  font-weight: 500;
  font-size: 0.78rem;
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
  font-size: 0.7rem;
  color: var(--ink);
  min-width: 4.5em;
}

.ledger {
  border-top: 1px solid var(--line);
  background: transparent;
  overflow: hidden;
}
.ledger-row {
  display: grid;
  grid-template-columns: minmax(5.5em, auto) 1fr auto auto;
  gap: 10px 18px;
  align-items: baseline;
  padding: 18px 4px;
  border-bottom: 1px solid var(--line);
  text-decoration: none;
  color: inherit;
  transition: background .12s;
}
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
}
.ledger-sub { font-size: 0.9rem; color: var(--muted); }
.ledger-sub .theme { color: var(--ink); font-weight: 500; }
.ledger-stats {
  font-family: var(--mono);
  font-size: 0.72rem;
  color: #444444;
  text-align: right;
  white-space: nowrap;
  letter-spacing: 0.02em;
}
.ledger-actions {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  justify-self: end;
}
.ledger-sync {
  font-family: var(--sans);
  font-size: 0.72rem;
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
  border-color: #c8c5be;
  color: var(--ink);
}
.ledger-sync:hover,
.ledger-sync:focus-visible {
  color: var(--ink);
  border-color: #c8c5be;
}
@media (hover: none) {
  .ledger-sync { opacity: 0.85; border-color: var(--line); }
}
.ledger-key-wrap {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.ledger-previews {
  display: inline-flex;
  gap: 3px;
  flex-shrink: 0;
}
.ledger-previews img {
  width: 32px;
  height: 32px;
  object-fit: cover;
  background: var(--mist);
  display: block;
}
.ledger-go {
  font-family: var(--sans);
  font-size: 1.15rem;
  color: var(--muted);
  opacity: 0.75;
  line-height: 1;
  transition: color .12s ease, transform .12s ease, opacity .12s ease;
  justify-self: end;
}
.ledger-row:hover .ledger-go { color: var(--ink); opacity: 1; transform: translateX(2px); }
.ledger-empty {
  padding: 48px 8px;
  color: var(--muted);
  font-size: 0.95rem;
  text-align: left;
  max-width: 28em;
}

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
  font-size: 0.7rem;
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
  font-size: 0.62rem;
  color: var(--muted);
  flex-shrink: 0;
  letter-spacing: 0.04em;
}
.cell .fname {
  font-family: var(--sans);
  font-size: 0.72rem;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
  opacity: 0;
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
  font-size: 0.58rem;
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
  background: rgba(20,20,20,0.18);
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
  opacity: 0.55;
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
  display: flex;
  gap: 8px;
  align-items: center;
  background: rgba(255,255,255,0.92);
  border: none;
  padding: 10px 14px;
  font-family: var(--mono);
  font-size: 0.7rem;
  color: var(--ink);
  max-width: 90vw;
}
.lb-bar .nm {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 42vw;
}
.lb-bar .pos {
  font-family: var(--mono);
  font-size: 0.68rem;
  color: var(--muted);
  flex-shrink: 0;
  letter-spacing: 0.02em;
}
.lb-bar button,
.lb-bar a {
  font-family: var(--sans);
  font-weight: 500;
  font-size: 0.78rem;
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

.toast {
  position: fixed;
  bottom: 20px;
  right: 20px;
  background: var(--ink);
  color: #fff;
  font-size: 0.85rem;
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
  font-size: 0.78rem;
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
  font-size: 0.78rem;
  font-weight: 500;
  color: var(--muted);
  letter-spacing: 0.04em;
  user-select: none;
  padding: 4px 0;
}
.events-fold > summary::-webkit-details-marker { display: none; }
.events-fold > summary::before {
  content: '›';
  font-size: 0.95rem;
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
  font-size: 0.78rem;
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
  font-size: 0.85rem;
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
  font-size: 0.82rem;
  color: var(--muted);
  line-height: 1.45;
  max-width: 40em;
}
.events-hint.err { color: var(--hazard); }
.events-fold-note {
  margin: 0 0 10px;
  font-size: 0.78rem;
  color: var(--muted);
  line-height: 1.45;
  max-width: 42em;
}
.events-status {
  font-size: 0.82rem;
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
  .chip, .btn-reclass, .btn-trash, .ledger-row, .cell, .star, .lb-bar button, .ledger-go, .ledger-key, .ledger-sync { transition: none; }
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
      syncStarCount();
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
      btn.textContent = on ? '完成选择' : '选择';
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
    if (el) el.textContent = n ? ('已选 ' + n) : '';
    document.querySelectorAll('.btn-reclass, .btn-trash').forEach(function (b) {
      b.disabled = n === 0;
    });
  }

  function selectedPaths() {
    return Array.prototype.map.call(
      document.querySelectorAll('.cell.selected .pick'),
      function (cb) { return cb.getAttribute('data-path'); }
    ).filter(Boolean);
  }

  async function reclassify(action) {
    var paths = selectedPaths();
    if (!paths.length) {
      toast('请先点「选择」，再勾选文件');
      return;
    }
    var label = ({
      to_screen: '移至截图录屏',
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
      toast('请先点「选择」，再勾选文件');
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

  var filterMode = 'all';
  function applyFilter() {
    document.querySelectorAll('.cell').forEach(function (cell) {
      var show = filterMode === 'all' || cell.classList.contains('starred');
      cell.classList.toggle('hidden', !show);
    });
  }

  document.addEventListener('change', function (e) {
    var pick = e.target.closest('.pick');
    if (!pick) return;
    var cell = pick.closest('.cell');
    if (cell) cell.classList.toggle('selected', pick.checked);
    syncSelCount();
  });

  document.addEventListener('click', function (e) {
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
    var re = e.target.closest('[data-reclassify]');
    if (re) {
      e.preventDefault();
      reclassify(re.getAttribute('data-reclassify'));
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

  document.addEventListener('keydown', function (e) {
    if (isTypingTarget(e.target)) return;
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
        '<span class="pos"></span>' +
        '<span class="nm"></span>' +
        '<button type="button" class="star star-lb" title="加星">☆</button>' +
        '<a class="open-raw" href="#" target="_blank" rel="noopener">原图</a>' +
        '<button type="button" id="lbClose">关闭</button></div>';
      document.body.appendChild(lb);
    }
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
      posEl.textContent = items.length ? ((pos + 1) + ' / ' + items.length) : '';
    }
    var raw = lb.querySelector('.open-raw');
    raw.href = src;
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
            crumb_parts.append('<span class="sep">/</span>')
        if href and i < len(crumbs) - 1:
            crumb_parts.append(f'<a href="{_esc(href)}">{_esc(label)}</a>')
        else:
            crumb_parts.append(f'<a class="here" href="{_esc(href or "#")}">{_esc(label)}</a>')

    # Counts for top jumps (avoid repeating a footer link dump on home)
    shots_n = records_n = docs_n = things_n = 0
    if work is not None:
        if buckets is None or star_n is None:
            cached_star, cached_buckets = get_topbar_stats(work)
            if star_n is None:
                star_n = cached_star
            if buckets is None:
                buckets = cached_buckets
        star_n = int(star_n or 0)
        shots_n = int(buckets.get('screenshots_count') or 0)
        records_n = int(buckets.get('screenrecords_count') or 0)
        docs_n = int(buckets.get('docs_count') or 0)
        things_n = int(buckets.get('things_count') or 0)
    else:
        star_n = int(star_n or 0)

    def _jump(href: str, label: str, n: int = None) -> str:
        if n is None:
            return f'<a href="{_esc(href)}">{_esc(label)}</a>'
        return (
            f'<a href="{_esc(href)}">{_esc(label)}'
            f'<span class="n"> {n}</span></a>'
        )

    more_links = [
        _jump('/screenshots', '截图', shots_n),
        _jump('/screenrecords', '录屏', records_n),
        _jump('/docs', '文档', docs_n),
        _jump('/things', '物品', things_n),
        _jump('/themes', '主题'),
    ]
    jump_parts = [
        '<a href="#" id="consoleLink">控制台</a>',
        _jump('/starred', '加星', star_n),
        '<details class="jumps-more">'
        '<summary>更多</summary>'
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
<title>{_esc(title)} — PicVault</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/geist-sans@5.2.5/400.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/geist-sans@5.2.5/500.css">
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
        f'<a href="/">回归档</a></div></div>'
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
        badge = '<span class="badge">Live</span>'
    elif is_video:
        badge = '<span class="badge">VIDEO</span>'
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


def _gallery_toolbar(file_count: int, star_count: int, context: str = 'normal') -> str:
    """context: normal | theme | screen | docs | things — hide the button for the current bucket."""
    actions = []
    if context == 'theme':
        actions.append(
            '<button type="button" class="btn-reclass" data-reclassify="to_default_month" disabled>'
            '放回默认月桶</button>'
        )
    if context != 'screen':
        actions.append(
            '<button type="button" class="btn-reclass" data-reclassify="to_screen" disabled>'
            '移至截图录屏</button>'
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
    return (
        f'<div class="toolbar">'
        f'<span class="count">{file_count} 个文件 · '
        f'<span id="pageMetaStars">{star_count}</span> 已加星</span>'
        f'<div class="toolbar-filters">'
        f'<button type="button" class="chip on" data-filter="all">全部</button>'
        f'<button type="button" class="chip" data-filter="starred">'
        f'仅加星<span class="n" id="starCount">{star_count}</span></button>'
        f'<button type="button" class="chip" id="selectModeBtn" data-select-toggle '
        f'aria-pressed="false">选择</button>'
        f'</div>'
        f'<div class="toolbar-organize" aria-label="整理">'
        f'<span class="sel-count" id="selCount"></span>'
        f'{"".join(actions)}'
        f'</div>'
        f'</div>'
    )


def render_home(work: Path) -> bytes:
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
            sub = f'{n_default} 个月 · {n_themed} 个主题'
        elif n_themed:
            sub = f'{n_themed} 个主题'
        elif n_default:
            sub = f'{n_default} 个月'
        else:
            sub = '空'
        stats = format_ledger_stats(
            total_photos, total_videos, total_stars, total_lives
        )
        previews = ledger_previews_html(peek_year_previews(work, year, months, limit=3))
        rows.append(
            f'<a class="ledger-row" href="/y/{_esc(year)}">'
            f'<span class="ledger-key-wrap">'
            f'<span class="ledger-key">{_esc(year)}</span>{previews}</span>'
            f'<span class="ledger-sub">{sub}</span>'
            f'<span class="ledger-stats">{stats}</span>'
            f'<span class="ledger-go" aria-hidden="true">›</span>'
            f'</a>'
        )
    if not rows:
        ledger = (
            '<div class="ledger"><div class="ledger-empty">'
            '还没有归档。把照片放进收件箱后，到控制台跑流水线。'
            '</div></div>'
        )
    else:
        ledger = f'<div class="ledger">{"".join(rows)}</div>'

    body = (
        f'<div class="page-head">'
        f'<div>'
        f'<h2 class="page-title">归档</h2>'
        f'</div>'
        f'</div>'
        f'{ledger}'
    )
    return page_shell(
        '归档', body, work=work, crumbs=[], buckets=buckets, star_n=star_n,
    )


def render_year(work: Path, year: str) -> bytes:
    by_date = work / 'by-date' / year
    if not by_date.exists():
        body = (
            f'<div class="page-head"><h2 class="page-title">{_esc(year)}</h2></div>'
            f'<div class="ledger"><div class="ledger-empty">未找到该年份。</div></div>'
        )
        return page_shell(year, body, work=work, crumbs=[('首页', '/'), (year, f'/y/{year}')])

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
        previews = ''
        if not empty:
            previews = ledger_previews_html(
                peek_bucket_previews(work, year, m.name, limit=3)
            )
        row = (
            f'<a class="{row_cls}" href="{_esc(href)}">'
            f'<span class="ledger-key-wrap">'
            f'<span class="ledger-key">{_esc(key)}</span>{previews}</span>'
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
    ledger_inner = ''.join(rows) if rows else '<div class="ledger-empty">空年份</div>'
    body = (
        f'<div class="page-head">'
        f'<h2 class="page-title">{_esc(year)}</h2>'
        f'<p class="page-meta">{len(months)} 个入口</p>'
        f'</div>'
        f'<div class="ledger">{ledger_inner}</div>'
    )
    return page_shell(year, body, work=work, crumbs=[('首页', '/'), (year, f'/y/{year}')])


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
                return f'{int(m.group(1))}/{int(m.group(2))}'
            return d
        parts.append(f'{short(start)}–{short(end)}')
    elif start or end:
        parts.append(start or end)
    return ' · '.join(parts) if parts else '—'


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
    files = list_bucket(work, year, month)
    stars = load_stars(work, bucket_name)
    cells = [
        _media_cell(f, work, thumb_root, bucket_name, stars, i + 1)
        for i, f in enumerate(files)
    ]
    sheet = (
        f'<div class="sheet" id="sheet">{"".join(cells)}</div>'
        if cells else
        '<div class="ledger"><div class="ledger-empty">这个桶里还没有文件。</div></div>'
    )
    meta = year
    if is_themed:
        theme = theme_for_bucket(work, month)
        if theme is not None:
            period = theme_ledger_sub(theme)
            if period and period != '—':
                meta = f'周期 {period}'
    body = (
        f'<div class="page-head">'
        f'<h2 class="page-title">{_esc(display)}</h2>'
        f'<p class="page-meta">{_esc(meta)}</p>'
        f'</div>'
        f'{_gallery_toolbar(len(files), len(stars), context=gallery_ctx)}'
        f'{sheet}'
    )
    return page_shell(
        f'{year}/{display}',
        body,
        work=work,
        crumbs=[
            ('首页', '/'),
            (year, f'/y/{year}'),
            (display, f'/y/{year}/{urllib.parse.quote(month)}'),
        ],
    )


def render_screenshots(work: Path, thumb_root: Path) -> bytes:
    files = list_screenshots(work)
    bucket_name = 'screenshots'
    stars = load_stars(work, bucket_name)
    cells = [
        _media_cell(f, work, thumb_root, bucket_name, stars, i + 1)
        for i, f in enumerate(files)
    ]
    sheet = (
        f'<div class="sheet" id="sheet">{"".join(cells)}</div>'
        if cells else
        '<div class="ledger"><div class="ledger-empty">没有截图。</div></div>'
    )
    body = (
        f'<div class="page-head">'
        f'<h2 class="page-title">截图</h2>'
        f'<p class="page-meta">手机截图</p>'
        f'</div>'
        f'{_gallery_toolbar(len(files), len(stars), context="screen")}'
        f'{sheet}'
    )
    return page_shell(
        '截图',
        body,
        work=work,
        crumbs=[('首页', '/'), ('截图', '/screenshots')],
    )


def render_screenrecords(work: Path, thumb_root: Path) -> bytes:
    files = list_screenrecords(work)
    bucket_name = 'screenrecords'
    stars = load_stars(work, bucket_name)
    cells = [
        _media_cell(f, work, thumb_root, bucket_name, stars, i + 1)
        for i, f in enumerate(files)
    ]
    sheet = (
        f'<div class="sheet" id="sheet">{"".join(cells)}</div>'
        if cells else
        '<div class="ledger"><div class="ledger-empty">没有录屏。</div></div>'
    )
    body = (
        f'<div class="page-head">'
        f'<h2 class="page-title">录屏</h2>'
        f'<p class="page-meta">屏幕录制</p>'
        f'</div>'
        f'{_gallery_toolbar(len(files), len(stars), context="screen")}'
        f'{sheet}'
    )
    return page_shell(
        '录屏',
        body,
        work=work,
        crumbs=[('首页', '/'), ('录屏', '/screenrecords')],
    )


def render_docs(work: Path, thumb_root: Path) -> bytes:
    files = list_docs(work)
    bucket_name = 'docs'
    stars = load_stars(work, bucket_name)
    cells = [
        _media_cell(f, work, thumb_root, bucket_name, stars, i + 1)
        for i, f in enumerate(files)
    ]
    sheet = (
        f'<div class="sheet" id="sheet">{"".join(cells)}</div>'
        if cells else
        '<div class="ledger"><div class="ledger-empty">没有文档照片。</div></div>'
    )
    body = (
        f'<div class="page-head">'
        f'<h2 class="page-title">文档</h2>'
        f'<p class="page-meta">证件、票据等，手动移入</p>'
        f'</div>'
        f'{_gallery_toolbar(len(files), len(stars), context="docs")}'
        f'{sheet}'
    )
    return page_shell(
        '文档',
        body,
        work=work,
        crumbs=[('首页', '/'), ('文档', '/docs')],
    )


def render_things(work: Path, thumb_root: Path) -> bytes:
    files = list_things(work)
    bucket_name = 'things'
    stars = load_stars(work, bucket_name)
    cells = [
        _media_cell(f, work, thumb_root, bucket_name, stars, i + 1)
        for i, f in enumerate(files)
    ]
    sheet = (
        f'<div class="sheet" id="sheet">{"".join(cells)}</div>'
        if cells else
        '<div class="ledger"><div class="ledger-empty">没有物品照片。</div></div>'
    )
    body = (
        f'<div class="page-head">'
        f'<h2 class="page-title">物品</h2>'
        f'<p class="page-meta">物品照片与视频，手动移入</p>'
        f'</div>'
        f'{_gallery_toolbar(len(files), len(stars), context="things")}'
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
    items = list_all_starred(work)
    stars_map = {rel: True for _, _, rel in items}
    cells = [
        _media_cell(path, work, thumb_root, bucket, stars_map, i + 1)
        for i, (path, bucket, _rel) in enumerate(items)
    ]
    sheet = (
        f'<div class="sheet" id="sheet">{"".join(cells)}</div>'
        if cells else
        '<div class="ledger"><div class="ledger-empty">'
        '还没有加星。在各库里点 ★，或点「仅加星」筛选。</div></div>'
    )
    body = (
        f'<div class="page-head">'
        f'<h2 class="page-title">加星</h2>'
        f'<p class="page-meta">{len(items)} 张已加星</p>'
        f'</div>'
        f'{_gallery_toolbar(len(items), len(items), context="starred")}'
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
    """True for countable media/docs files (excludes .DS_Store and most dotfiles)."""
    if not path.is_file():
        return False
    if path.name == '.DS_Store':
        return False
    if path.name.startswith('.') and path.name != '.source':
        return False
    return True


def count_files_in(dir_path: Path) -> int:
    """Count user-visible files under dir_path (aligned with rename_organize.scan_inbox).

    Skipped:
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
            bits.append(' · '.join(str(s) for s in sources if str(s).strip()))
        files = theme.get('files')
        if isinstance(files, list) and files:
            bits.append(f'{len(files)} 个文件')
        return ' · '.join(b for b in bits if b) or ''

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
                f'title="同步此主题：吸入/吐出本主题；不再匹配时可改派到其它主题。不改动其它主题桶里原有文件。">'
                f'复制同步命令</button>'
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
            ledger = (
                '<div class="ledger"><div class="ledger-empty">'
                '配置无法解析，请展开下方编辑配置修正。'
                '</div></div>'
            )
            hint = (
                f'<p class="events-hint err">配置有误：{_esc(parse_err)}</p>'
            )
            fold_note = ''
            fold_open = ' open'
        elif not rows:
            ledger = (
                '<div class="ledger"><div class="ledger-empty">'
                '还没有主题。展开下方编辑配置，按示例添加。'
                '</div></div>'
            )
            hint = ''
            fold_note = (
                '<p class="events-fold-note">保存只写配置。改完后对该主题点'
                '「复制同步命令」才会搬家（先 dry-run）。</p>'
            )
            fold_open = ''
        else:
            ledger = f'<div class="ledger">{"".join(rows)}</div>'
            hint = ''
            fold_note = (
                '<p class="events-fold-note">保存 ≠ 搬家。改配置 → 保存 → '
                '该行「复制同步命令」（先 dry-run）。同步只扫本主题。</p>'
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
            '<button type="button" id="eventsCopySyncAll">复制同步全部</button>'
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
            '  if(!confirm("全量同步会按配置收敛每一个主题桶，可能覆盖手工调整。复制的是 dry-run 命令；确认后再加 --yes。仍要复制？"))return;\n'
            '  copyCmd(syncAllCmd,"已复制同步全部（dry-run）；确认后加 --yes");\n'
            '});\n'
            'document.querySelectorAll(".ledger-sync").forEach(function(btn){\n'
            '  btn.addEventListener("click",function(e){\n'
            '    e.preventDefault();e.stopPropagation();\n'
            '    var cmd=btn.getAttribute("data-sync-cmd")||"";\n'
            '    if(!cmd){setStatus("无法生成同步命令","err");return;}\n'
            '    copyCmd(cmd,"已复制该主题同步命令（dry-run）；确认后加 --yes 或去掉 --dry-run");\n'
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
            '    setStatus("已保存 "+data.themes+" 个主题。点该行「复制同步命令」才会搬家。","ok");\n'
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
        'web_start':    lambda _b: [pb, 'web', 'start'],
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
