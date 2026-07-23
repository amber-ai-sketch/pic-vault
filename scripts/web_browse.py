#!/usr/bin/env python3
"""
web_browse.py - Local Flask-free HTTP server to browse by-date/ with thumbnails + star.

Uses Python's built-in http.server (no Flask dep). Single file, single port.

Usage:
    ./web_browse.py --work /Volumes/Storage --host 0.0.0.0 --port 8765
"""

import argparse
import html as html_lib
import json
import mimetypes
import os
import queue
import re
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
THUMB_CACHE = '_meta/thumbs'
VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.m4v', '.3gp', '.hevc', '.webm'}
RUN_TIMEOUT_SEC = None  # 不超时；长任务实质不限时
# 仅当 RUN_TIMEOUT_SEC 为 None 时作为极长兜底；再设为 None 则完全无限
RUN_HARD_CAP_SEC = 7 * 24 * 3600

# Sibling scripts discovered at import time (so absolute paths are baked in)
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
PICVAULT_BIN = PROJECT_ROOT / 'picvault'
DEDUPE_SCRIPT = SCRIPT_DIR / 'dedupe.py'
RENAME_SCRIPT = SCRIPT_DIR / 'rename_organize.py'
SYNC_SCRIPT = SCRIPT_DIR / 'sync_to_backup.sh'
INIT_SCRIPT = SCRIPT_DIR / 'init_storage.sh'
BACKUP_DEFAULT = '/Volumes/WD4T/MediaVault'

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
    """Latest successful Apply markers for dashboard steps 02/03/04.

    Scans persisted run logs under runs_dir (excluding latest.json). For each
    apply command, keeps the newest status==ok entry by finished_at then id.
    Also flags running=true when _ACTIVE_RUN matches that command.
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
            cmd = meta.get('command_name')
            if cmd not in markers:
                continue
            if meta.get('status') != 'ok':
                continue
            finished = meta.get('finished_at') or ''
            run_id = meta.get('id') or path.stem
            sort_key = (str(finished), str(run_id))
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

    with _RUN_LOCK:
        active = dict(_ACTIVE_RUN) if _ACTIVE_RUN else None
    if active and active.get('status') == 'running':
        cmd = active.get('command_name')
        if cmd in markers:
            markers[cmd]['running'] = True

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


def gen_thumbnail(src: Path, dst: Path, size=320) -> bool:
    """Generate thumbnail: sips for images, ffmpeg frame extract for videos."""
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
            return r.returncode == 0 and dst.exists()

        subprocess.run(
            ['sips', '-Z', str(size), str(src), '--out', str(dst.parent)],
            capture_output=True, check=True, timeout=30
        )
        # sips preserves extension; we want .jpg
        produced = dst.parent / src.name
        if produced.exists() and produced != dst:
            shutil.move(str(produced), str(dst))
        return dst.exists()
    except Exception as e:
        print(f"  [thumb] {src}: {e}", file=sys.stderr)
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
    """Scan by-date/, screenshots/, screenrecords/, docs/ for bucket info."""
    result = {
        'years': {},
        'screenshots_count': 0,
        'screenrecords_count': 0,
        'docs_count': 0,
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
    screenrecords = work / 'screenrecords'
    if screenrecords.exists():
        result['screenrecords_count'] = sum(
            1 for f in screenrecords.iterdir() if f.is_file()
        )
    docs = work / 'docs'
    if docs.exists():
        result['docs_count'] = sum(1 for f in docs.iterdir() if f.is_file())

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


def list_screenrecords(work: Path) -> list[Path]:
    screenrecords = work / 'screenrecords'
    if not screenrecords.exists():
        return []
    return sorted([f for f in screenrecords.iterdir() if f.is_file()],
                  key=lambda f: f.stat().st_mtime, reverse=True)


def list_docs(work: Path) -> list[Path]:
    docs = work / 'docs'
    if not docs.exists():
        return []
    return sorted([f for f in docs.iterdir() if f.is_file()],
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


# —— Browse UI (contact-sheet gallery) ————————————————————————————————
# Shares visual language with outputs/dashboard.html: Syne / Figtree /
# JetBrains Mono, cool slate-cyan archive palette. Signature: contact-sheet
# frames with cyan top rail + amber star notch.


def _esc(s) -> str:
    return html_lib.escape(str(s), quote=True)


PAGE_CSS = '''
:root {
  --paper: #C8D0D8;
  --ink: #15202B;
  --cyan: #178A9C;
  --amber: #C48A1A;
  --live: #1F7A4D;
  --rail: #E8EEF2;
  --muted: #5A6A78;
  --sheet: #B8C2CC;
  --frame: #DCE3E9;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  min-height: 100vh;
  font-family: "Figtree", sans-serif;
  color: var(--ink);
  line-height: 1.5;
  background-color: var(--paper);
  background-image:
    radial-gradient(rgba(21,32,43,0.045) 0.6px, transparent 0.6px),
    repeating-linear-gradient(-28deg, transparent 0, transparent 11px, rgba(21,32,43,0.028) 11px, rgba(21,32,43,0.028) 12px);
  background-size: 4px 4px, auto;
}
a { color: var(--cyan); text-decoration: none; }
a:hover { text-decoration: underline; text-underline-offset: 3px; }
:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }

.wrap { max-width: 1180px; margin: 0 auto; padding: 24px 20px 64px; }

.masthead {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 12px 24px;
  align-items: end;
  padding-bottom: 20px;
  border-bottom: 1px solid rgba(21,32,43,0.18);
  margin-bottom: 28px;
}
.brand {
  font-family: "Syne", sans-serif;
  font-weight: 800;
  font-size: clamp(1.9rem, 5vw, 2.8rem);
  line-height: 0.95;
  letter-spacing: -0.03em;
  margin: 0;
  color: var(--ink);
}
.brand a { color: inherit; text-decoration: none; }
.brand a:hover { color: var(--cyan); text-decoration: none; }
.tagline { margin: 8px 0 0; font-size: 0.92rem; color: var(--muted); max-width: 40em; }
.work-path {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.72rem;
  color: var(--muted);
  text-align: right;
  word-break: break-all;
  max-width: 28em;
}

.nav {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 4px;
  margin: -12px 0 28px;
  padding-bottom: 16px;
  border-bottom: 1px solid rgba(21,32,43,0.1);
}
.nav a, .nav span {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.72rem;
  letter-spacing: 0.04em;
  padding: 5px 10px;
  border: 1px solid transparent;
  border-radius: 2px;
  color: var(--muted);
  text-decoration: none;
}
.nav a:hover {
  color: var(--ink);
  border-color: rgba(21,32,43,0.2);
  background: var(--rail);
  text-decoration: none;
}
.nav a.here {
  color: var(--ink);
  border-color: rgba(23,138,156,0.45);
  background: rgba(23,138,156,0.1);
}
.nav .sep { color: rgba(21,32,43,0.25); padding: 5px 2px; user-select: none; }

.page-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px 20px;
  margin-bottom: 18px;
}
.page-title {
  font-family: "Syne", sans-serif;
  font-weight: 800;
  font-size: clamp(1.5rem, 3.5vw, 2.1rem);
  letter-spacing: -0.02em;
  margin: 0;
  line-height: 1.1;
}
.page-meta {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.75rem;
  color: var(--muted);
}
.section-label {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.72rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--muted);
  margin: 0 0 12px;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
}
.toolbar .count {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.75rem;
  color: var(--muted);
  margin-right: auto;
}
.chip {
  font-family: "Figtree", sans-serif;
  font-weight: 600;
  font-size: 0.8rem;
  padding: 5px 11px;
  border: 1px solid rgba(21,32,43,0.28);
  border-radius: 2px;
  background: var(--rail);
  color: var(--ink);
  cursor: pointer;
  transition: border-color .15s, background .15s, color .15s;
}
.chip:hover { border-color: var(--cyan); }
.chip.on {
  background: var(--cyan);
  border-color: var(--cyan);
  color: #fff;
}
.chip.on .n { color: rgba(255,255,255,0.85); }
.chip .n {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.72rem;
  color: var(--muted);
  margin-left: 4px;
}
.btn-reclass {
  font-family: "Figtree", sans-serif;
  font-weight: 600;
  font-size: 0.8rem;
  padding: 5px 11px;
  border: 1px solid rgba(21,32,43,0.28);
  border-radius: 2px;
  background: var(--paper);
  color: var(--ink);
  cursor: pointer;
}
.btn-reclass:hover { border-color: var(--cyan); color: var(--cyan); }
.btn-reclass:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.btn-trash {
  font-family: "Figtree", sans-serif;
  font-weight: 600;
  font-size: 0.8rem;
  padding: 5px 11px;
  border: 1px solid rgba(168,52,40,0.45);
  border-radius: 2px;
  background: rgba(168,52,40,0.08);
  color: #A83428;
  cursor: pointer;
}
.btn-trash:hover { background: rgba(168,52,40,0.16); }
.btn-trash:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.sel-count {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.72rem;
  color: var(--cyan);
  min-width: 4.5em;
}

/* Year / month ledger */
.ledger {
  border: 1px solid rgba(21,32,43,0.22);
  border-top: 3px solid var(--cyan);
  background: rgba(232,238,242,0.55);
}
.ledger-row {
  display: grid;
  grid-template-columns: minmax(5.5em, auto) 1fr auto;
  gap: 10px 18px;
  align-items: baseline;
  padding: 14px 16px;
  border-bottom: 1px solid rgba(21,32,43,0.12);
  text-decoration: none;
  color: inherit;
}
.ledger-row:last-child { border-bottom: none; }
.ledger-row:hover {
  background: rgba(23,138,156,0.08);
  text-decoration: none;
}
@media (prefers-reduced-motion: no-preference) {
  .ledger-row {
    animation: frameIn .4s ease forwards;
  }
  .ledger-row:nth-child(1) { animation-delay: .04s; }
  .ledger-row:nth-child(2) { animation-delay: .08s; }
  .ledger-row:nth-child(3) { animation-delay: .12s; }
  .ledger-row:nth-child(4) { animation-delay: .16s; }
  .ledger-row:nth-child(5) { animation-delay: .2s; }
  .ledger-row:nth-child(6) { animation-delay: .24s; }
  .ledger-row:nth-child(7) { animation-delay: .28s; }
  .ledger-row:nth-child(8) { animation-delay: .32s; }
}
.ledger-key {
  font-family: "Syne", sans-serif;
  font-weight: 800;
  font-size: 1.25rem;
  letter-spacing: -0.02em;
  color: var(--ink);
}
.ledger-sub {
  font-size: 0.9rem;
  color: var(--muted);
}
.ledger-sub .theme {
  color: var(--cyan);
  font-weight: 600;
}
.ledger-stats {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.72rem;
  color: var(--muted);
  text-align: right;
  white-space: nowrap;
}
.ledger-empty {
  padding: 28px 16px;
  color: var(--muted);
  font-size: 0.95rem;
}

/* Contact sheet */
.sheet {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(168px, 1fr));
  gap: 0;
  border: 1px solid rgba(21,32,43,0.22);
  border-top: 3px solid var(--cyan);
  background: var(--sheet);
}
.cell {
  position: relative;
  background: var(--frame);
  border-right: 1px solid rgba(21,32,43,0.14);
  border-bottom: 1px solid rgba(21,32,43,0.14);
}
.cell.hidden { display: none; }
.cell.starred { box-shadow: inset 0 0 0 2px rgba(196,138,26,0.55); }
.cell.selected { box-shadow: inset 0 0 0 2px rgba(23,138,156,0.7); }
.cell.starred.selected { box-shadow: inset 0 0 0 2px rgba(196,138,26,0.55), inset 0 0 0 4px rgba(23,138,156,0.55); }
.cell .pick {
  position: absolute;
  top: 8px;
  left: 8px;
  z-index: 3;
  width: 18px;
  height: 18px;
  margin: 0;
  accent-color: var(--cyan);
  cursor: pointer;
}
@media (prefers-reduced-motion: no-preference) {
  .cell { animation: frameIn .45s ease forwards; }
  .cell:nth-child(6n+1) { animation-delay: .03s; }
  .cell:nth-child(6n+2) { animation-delay: .06s; }
  .cell:nth-child(6n+3) { animation-delay: .09s; }
  .cell:nth-child(6n+4) { animation-delay: .12s; }
  .cell:nth-child(6n+5) { animation-delay: .15s; }
  .cell:nth-child(6n+6) { animation-delay: .18s; }
}

@keyframes frameIn {
  from { transform: translateY(8px); }
  to { transform: translateY(0); }
}

.cell .thumb {
  display: block;
  width: 100%;
  aspect-ratio: 1;
  object-fit: cover;
  background: #9AA6B2;
  cursor: zoom-in;
  vertical-align: middle;
}
.cell .thumb-miss {
  display: flex;
  align-items: center;
  justify-content: center;
  aspect-ratio: 1;
  background: #9AA6B2;
  color: var(--ink);
  font-family: "JetBrains Mono", monospace;
  font-size: 0.7rem;
  text-decoration: none;
}
.cell .edge {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  padding: 6px 8px 7px;
  background: rgba(232,238,242,0.85);
  border-top: 1px solid rgba(21,32,43,0.1);
}
.cell .idx {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.62rem;
  letter-spacing: 0.06em;
  color: var(--muted);
  flex-shrink: 0;
}
.cell .fname {
  font-family: "JetBrains Mono", monospace;
  font-size: 0.62rem;
  color: var(--ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.cell .badge {
  position: absolute;
  left: 8px;
  top: auto;
  bottom: 34px; /* above .edge filename strip */
  font-family: "JetBrains Mono", monospace;
  font-size: 0.62rem;
  letter-spacing: 0.04em;
  padding: 2px 5px;
  background: rgba(21,32,43,0.72);
  color: #fff;
  border-radius: 2px;
  pointer-events: none;
  z-index: 2;
}
.star {
  position: absolute;
  top: 6px;
  right: 6px;
  width: 32px;
  height: 32px;
  border: 1px solid rgba(21,32,43,0.35);
  border-radius: 2px;
  background: rgba(232,238,242,0.92);
  color: var(--muted);
  cursor: pointer;
  font-size: 15px;
  line-height: 1;
  display: grid;
  place-items: center;
  padding: 0;
  transition: background .15s, color .15s, border-color .15s, transform .15s;
  z-index: 2;
}
.star:hover {
  border-color: var(--amber);
  color: var(--amber);
  transform: scale(1.06);
}
.star.on {
  background: var(--amber);
  border-color: var(--amber);
  color: #1a1408;
}
.star.busy { opacity: 0.55; pointer-events: none; }
.star.pulse { animation: starPulse .35s ease; }
@keyframes starPulse {
  0% { transform: scale(1); }
  40% { transform: scale(1.18); }
  100% { transform: scale(1); }
}

/* Lightbox */
.lb {
  display: none;
  position: fixed;
  inset: 0;
  z-index: 100;
  background: rgba(21,32,43,0.88);
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.lb.open { display: flex; }
.lb img, .lb video {
  max-width: min(96vw, 1200px);
  max-height: 88vh;
  object-fit: contain;
  box-shadow: 0 12px 48px rgba(0,0,0,0.45);
}
.lb-bar {
  position: fixed;
  bottom: 18px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  gap: 8px;
  align-items: center;
  background: rgba(232,238,242,0.95);
  border: 1px solid rgba(21,32,43,0.25);
  border-radius: 2px;
  padding: 8px 12px;
  font-family: "JetBrains Mono", monospace;
  font-size: 0.72rem;
  color: var(--ink);
  max-width: 90vw;
}
.lb-bar .nm {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 48vw;
}
.lb-bar button {
  font-family: "Figtree", sans-serif;
  font-weight: 600;
  font-size: 0.8rem;
  padding: 4px 10px;
  border: 1px solid rgba(21,32,43,0.28);
  border-radius: 2px;
  background: var(--rail);
  color: var(--ink);
  cursor: pointer;
}
.lb-bar button:hover { border-color: var(--cyan); }
.lb-bar .star-lb.on {
  background: var(--amber);
  border-color: var(--amber);
}

.toast {
  position: fixed;
  bottom: 20px;
  right: 20px;
  background: var(--ink);
  color: var(--rail);
  font-size: 0.85rem;
  padding: 10px 14px;
  border-radius: 2px;
  opacity: 0;
  transform: translateY(8px);
  transition: opacity .2s, transform .2s;
  pointer-events: none;
  z-index: 200;
}
.toast.show { opacity: 1; transform: translateY(0); }

.pre-block {
  margin: 0;
  padding: 16px;
  background: rgba(232,238,242,0.7);
  border: 1px solid rgba(21,32,43,0.18);
  border-top: 3px solid var(--cyan);
  font-family: "JetBrains Mono", monospace;
  font-size: 0.78rem;
  overflow-x: auto;
  white-space: pre-wrap;
  color: var(--ink);
}

@media (max-width: 640px) {
  .masthead { grid-template-columns: 1fr; }
  .work-path { text-align: left; max-width: none; }
  .ledger-row { grid-template-columns: 1fr; gap: 4px; }
  .ledger-stats { text-align: left; white-space: normal; }
  .sheet { grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); }
}
@media (prefers-reduced-motion: reduce) {
  .star.pulse { animation: none; }
  html { scroll-behavior: auto; }
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
    if (!path || !bucket || btn.classList.contains('busy')) return;
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
      setStarred(btn, data.starred);
      btn.classList.add('pulse');
      setTimeout(function () { btn.classList.remove('pulse'); }, 350);
      syncStarCount();
      applyFilter();
      var lbStar = document.querySelector('.star-lb');
      if (lbStar && lbStar.getAttribute('data-path') === path) {
        setStarred(lbStar, data.starred);
      }
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

  function syncStarCount() {
    var n = document.querySelectorAll('.cell.starred').length;
    var el = document.getElementById('starCount');
    if (el) el.textContent = String(n);
    var chipN = document.querySelector('[data-filter="starred"] .n');
    if (chipN) chipN.textContent = String(n);
    var meta = document.getElementById('pageMetaStars');
    if (meta) meta.textContent = String(n);
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
      toast('请先勾选文件');
      return;
    }
    var label = ({
      to_screen: '移至截图录屏',
      to_normal: '移回普通分类',
      to_docs: '移至文档'
    })[action] || action;
    if (!confirm(label + '：' + paths.length + ' 个文件？\\n会按规则重命名并移动。')) return;
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
      toast('请先勾选文件');
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

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeLightbox();
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
    var raw = lb.querySelector('.open-raw');
    raw.href = src;
    var starBtn = lb.querySelector('.star-lb');
    starBtn.setAttribute('data-path', path);
    starBtn.setAttribute('data-bucket', bucket);
    var cellStar = document.querySelector('.star[data-path="' + CSS.escape(path) + '"]');
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


def page_shell(title: str, body: str, work: Path = None, crumbs: list = None) -> bytes:
    """Wrap page body in shared masthead / nav / assets."""
    crumbs = crumbs or [('首页', '/')]
    nav_parts = []
    for i, (label, href) in enumerate(crumbs):
        if i:
            nav_parts.append('<span class="sep">/</span>')
        if href and i < len(crumbs) - 1:
            nav_parts.append(f'<a href="{_esc(href)}">{_esc(label)}</a>')
        else:
            nav_parts.append(f'<a class="here" href="{_esc(href or "#")}">{_esc(label)}</a>')
    # Always expose Screenshots + Themes as secondary jumps
    nav_parts.append('<span class="sep">·</span>')
    nav_parts.append('<a href="/starred">加星</a>')
    nav_parts.append('<a href="/screenshots">截图</a>')
    nav_parts.append('<a href="/screenrecords">录屏</a>')
    nav_parts.append('<a href="/docs">文档</a>')
    nav_parts.append('<a href="/themes">主题</a>')

    work_html = ''
    if work is not None:
        work_html = f'<div class="work-path">{_esc(work)}</div>'

    doc = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} — PicVault</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&family=Syne:wght@700;800&display=swap" rel="stylesheet">
<style>{PAGE_CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="masthead">
    <div>
      <h1 class="brand"><a href="/">PicVault</a></h1>
      <p class="tagline">归档图库 · 浏览与加星</p>
    </div>
    {work_html}
  </header>
  <nav class="nav" aria-label="面包屑">{''.join(nav_parts)}</nav>
  {body}
</div>
<script>{PAGE_JS}</script>
</body>
</html>'''
    return doc.encode('utf-8')


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
    badge = '<span class="badge">VIDEO</span>' if is_video else ''
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
    """context: normal | screen | docs — hide the button for the current bucket."""
    actions = []
    if context != 'screen':
        actions.append(
            '<button type="button" class="btn-reclass" data-reclassify="to_screen" disabled>'
            '移至截图录屏</button>'
        )
    if context != 'normal':
        actions.append(
            '<button type="button" class="btn-reclass" data-reclassify="to_normal" disabled>'
            '移回普通分类</button>'
        )
    if context != 'docs':
        actions.append(
            '<button type="button" class="btn-reclass" data-reclassify="to_docs" disabled>'
            '移至文档</button>'
        )
    actions.append(
        '<button type="button" class="btn-trash" data-trash="1" disabled>'
        '移至回收站</button>'
    )
    return (
        f'<div class="toolbar">'
        f'<span class="count">{file_count} 个文件 · '
        f'<span id="pageMetaStars">{star_count}</span> 已加星</span>'
        f'<span class="sel-count" id="selCount"></span>'
        f'<button type="button" class="chip on" data-filter="all">全部</button>'
        f'<button type="button" class="chip" data-filter="starred">'
        f'仅加星<span class="n" id="starCount">{star_count}</span></button>'
        f'{"".join(actions)}'
        f'</div>'
    )


def render_home(work: Path) -> bytes:
    buckets = scan_buckets(work)
    years = sorted(buckets['years'].keys(), reverse=True)
    rows = []
    for year in years:
        months = buckets['years'][year]
        total_photos = sum(m['photos'] for m in months)
        total_videos = sum(m['videos'] for m in months)
        themed = sum(1 for m in months if m.get('is_themed'))
        rows.append(
            f'<a class="ledger-row" href="/y/{_esc(year)}">'
            f'<span class="ledger-key">{_esc(year)}</span>'
            f'<span class="ledger-sub">{len(months)} 个月'
            f'{" · " + str(themed) + " 个主题" if themed else ""}</span>'
            f'<span class="ledger-stats">{total_photos} 张 · {total_videos} 视频</span>'
            f'</a>'
        )
    if not rows:
        ledger = '<div class="ledger"><div class="ledger-empty">还没有归档。把照片放进 inbox/ 后跑 pipeline。</div></div>'
    else:
        ledger = f'<div class="ledger">{"".join(rows)}</div>'

    shot_link = ''
    links = []
    if buckets['screenshots_count'] > 0:
        links.append(
            f'<a href="/screenshots">截图库 → {buckets["screenshots_count"]} 个文件</a>'
        )
    if buckets.get('screenrecords_count', 0) > 0:
        links.append(
            f'<a href="/screenrecords">录屏库 → {buckets["screenrecords_count"]} 个文件</a>'
        )
    if buckets.get('docs_count', 0) > 0:
        links.append(
            f'<a href="/docs">文档库 → {buckets["docs_count"]} 个文件</a>'
        )
    starred_n = count_starred(work)
    # Prefer existing-file count for the home link
    starred_live = len(list_all_starred(work))
    if starred_live > 0 or starred_n > 0:
        links.insert(
            0,
            f'<a href="/starred">加星 → {starred_live} 个文件</a>',
        )
    if links:
        shot_link = (
            f'<p style="margin:20px 0 0;font-size:0.92rem;color:var(--muted)">'
            f'{" · ".join(links)}</p>'
        )

    body = (
        f'<div class="page-head">'
        f'<h2 class="page-title">年份索引</h2>'
        f'<p class="page-meta">按 by-date/ 浏览归档</p>'
        f'</div>'
        f'<p class="section-label">Years</p>'
        f'{ledger}{shot_link}'
    )
    return page_shell('归档浏览', body, work=work, crumbs=[('首页', '/')])


def render_year(work: Path, year: str) -> bytes:
    by_date = work / 'by-date' / year
    if not by_date.exists():
        body = (
            f'<div class="page-head"><h2 class="page-title">{_esc(year)}</h2></div>'
            f'<div class="ledger"><div class="ledger-empty">未找到该年份。</div></div>'
        )
        return page_shell(year, body, work=work, crumbs=[('首页', '/'), (year, f'/y/{year}')])

    months = [m for m in sorted(by_date.iterdir()) if m.is_dir()]
    rows = []
    for m in months:
        photo_count = sum(1 for _ in (m / 'photos').rglob('*') if _.is_file()) if (m / 'photos').exists() else 0
        video_count = sum(1 for _ in (m / 'videos').rglob('*') if _.is_file()) if (m / 'videos').exists() else 0
        is_themed = '_' in m.name
        theme = m.name.split('_', 1)[1] if is_themed else ''
        sub = (
            f'<span class="theme">{_esc(theme)}</span>'
            if theme else '默认桶'
        )
        href = f'/y/{year}/{urllib.parse.quote(m.name)}'
        rows.append(
            f'<a class="ledger-row" href="{_esc(href)}">'
            f'<span class="ledger-key">{_esc(m.name)}</span>'
            f'<span class="ledger-sub">{sub}</span>'
            f'<span class="ledger-stats">{photo_count} 张 · {video_count} 视频</span>'
            f'</a>'
        )

    ledger_inner = ''.join(rows) if rows else '<div class="ledger-empty">空年份</div>'
    body = (
        f'<div class="page-head">'
        f'<h2 class="page-title">{_esc(year)}</h2>'
        f'<p class="page-meta">{len(months)} 个桶</p>'
        f'</div>'
        f'<p class="section-label">Months</p>'
        f'<div class="ledger">{ledger_inner}</div>'
    )
    return page_shell(year, body, work=work, crumbs=[('首页', '/'), (year, f'/y/{year}')])


def render_bucket(work: Path, year: str, month: str, thumb_root: Path) -> bytes:
    bucket_name = month
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
    body = (
        f'<div class="page-head">'
        f'<h2 class="page-title">{_esc(month)}</h2>'
        f'<p class="page-meta">{_esc(year)}</p>'
        f'</div>'
        f'{_gallery_toolbar(len(files), len(stars), context="normal")}'
        f'{sheet}'
    )
    return page_shell(
        f'{year}/{month}',
        body,
        work=work,
        crumbs=[
            ('首页', '/'),
            (year, f'/y/{year}'),
            (month, f'/y/{year}/{urllib.parse.quote(month)}'),
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
        f'<p class="page-meta">screenshots/</p>'
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
        f'<p class="page-meta">screenrecords/</p>'
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
        f'<p class="page-meta">docs/ · 手机拍的证件/票据等，手动移入</p>'
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
        f'<p class="page-meta">汇总所有桶的 ★ · {len(items)} 个</p>'
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
            if not f.is_file():
                continue
            if f.name == '.DS_Store':
                continue
            if f.name.startswith('.') and f.name != '.source':
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
    'inbox', 'by-date', 'screenshots', '_favorite', '_vlogs', '_trash', '_meta',
)


def ensure_work_dirs(work: Path):
    """Create dirs that newer versions added (safe for older work disks)."""
    (work / 'screenrecords').mkdir(parents=True, exist_ok=True)
    (work / 'screenshots').mkdir(parents=True, exist_ok=True)
    (work / 'docs').mkdir(parents=True, exist_ok=True)


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
                path_q = urllib.parse.unquote(path_q)
                bucket = qs['bucket'][0] if isinstance(qs['bucket'], list) else qs['bucket']
                bucket = urllib.parse.unquote(str(bucket))
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
                self._run_streaming(argv, cmd_str, cmd_name)
                return
            elif path == '/api/reclassify':
                action = data.get('action') if isinstance(data, dict) else None
                paths = data.get('paths') if isinstance(data, dict) else None
                if action not in ('to_screen', 'to_normal', 'to_docs'):
                    self._send_json({
                        'ok': False,
                        'error': 'action must be to_screen|to_normal|to_docs',
                    })
                    return
                if not isinstance(paths, list) or not paths:
                    self._send_json({'ok': False, 'error': 'paths required'})
                    return
                if len(paths) > 500:
                    self._send_json({'ok': False, 'error': 'too many paths (max 500)'})
                    return
                results = rename_mod.reclassify_paths(self.work, paths, action, dry_run=False)
                for item in results:
                    if item.get('ok') and item.get('dest') and not item.get('skipped'):
                        migrate_star_path(self.work, item['src'], item['dest'])
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
            elif path == '/starred':
                body = render_starred(self.work, self.thumb_root)
                self._send(body, 'text/html')
            elif path == '/api/status':
                # JSON status endpoint for dashboard polling
                self._send_json({
                    'running': True,
                    'work': str(self.work),
                    'initialized': is_work_initialized(self.work),
                    'inbox': count_files_in(self.work / 'inbox'),
                    'by_date': count_files_in(self.work / 'by-date'),
                    'screenshots': count_files_in(self.work / 'screenshots'),
                    'screenrecords': count_files_in(self.work / 'screenrecords'),
                    'docs': count_files_in(self.work / 'docs'),
                    'vlogs': count_files_in(self.work / '_vlogs'),
                    'trash': count_files_in(self.work / '_trash'),
                    'starred': count_starred(self.work),
                    'last_sync': last_sync_time(self.work),
                    'pipeline': pipeline_step_markers(self.work),
                })
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
                path_q = urllib.parse.unquote(path_q)
                bucket = qs['bucket'][0] if isinstance(qs['bucket'], list) else qs['bucket']
                bucket = urllib.parse.unquote(str(bucket))
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

            chunk = ''
            next_offset = offset
            if log_path.is_file():
                size = log_path.stat().st_size
                if offset > size:
                    offset = size
                with open(log_path, 'rb') as f:
                    f.seek(offset)
                    data = f.read()
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
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
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
        global _ACTIVE_RUN
        meta['finished_at'] = datetime.now().isoformat(timespec='seconds')
        meta['status'] = status
        meta['rc'] = rc
        meta['error'] = error
        persist_run_meta(self.work, meta)
        with _RUN_LOCK:
            if _ACTIVE_RUN and _ACTIVE_RUN.get('id') == meta['id']:
                _ACTIVE_RUN = None

    def _run_streaming(self, argv, cmd_str: str, cmd_name: str):
        """Run argv, stream NDJSON, and persist to _meta/logs/runs/.

        Client disconnect does not kill the subprocess — output keeps going to
        the log file so the dashboard can resume via /api/runs/<id>/log.
        """
        global _ACTIVE_RUN

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
                    env={**os.environ, 'PYTHONUNBUFFERED': '1'},
                )
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
        full = (self.work / rel_path).resolve()
        work_res = self.work.resolve()
        if not (str(full).startswith(str(work_res) + os.sep) or full == work_res):
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
        """Serve thumbnail."""
        if not rel_path:
            self._send(b'missing', 'text/plain', 400); return
        thumb = thumb_for(self.work / rel_path, self.work, self.thumb_root)
        if not thumb or not thumb.exists():
            self._send(b'no thumb', 'text/plain', 404); return
        self._send_raw(str(thumb.relative_to(self.work)))

    def _render_themes(self) -> bytes:
        path = self.work / '_meta' / 'events.yaml'
        crumbs = [('首页', '/'), ('主题', '/themes')]
        if not path.exists():
            body = (
                '<div class="page-head"><h2 class="page-title">主题</h2></div>'
                '<div class="ledger"><div class="ledger-empty">'
                '未找到 events.yaml。</div></div>'
            )
            return page_shell('主题', body, work=self.work, crumbs=crumbs)
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
        content = '\n'.join(themes_text).strip() or '(空)'
        body = (
            '<div class="page-head">'
            '<h2 class="page-title">主题</h2>'
            '<p class="page-meta">_meta/events.yaml</p>'
            '</div>'
            f'<pre class="pre-block">{_esc(content)}</pre>'
        )
        return page_shell('主题', body, work=self.work, crumbs=crumbs)


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
    ensure_work_dirs(work)

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
