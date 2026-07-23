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


# —— Browse UI (contact-sheet gallery) ————————————————————————————————
# Shares visual language with outputs/dashboard.html: Syne / Figtree /
# JetBrains Mono, cool slate-cyan archive palette. Signature: contact-sheet
# frames with cyan top rail + amber star notch.

VIDEO_EXTS = {'.mp4', '.mov', '.webm', '.m4v'}


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
  top: 8px;
  font-family: "JetBrains Mono", monospace;
  font-size: 0.62rem;
  letter-spacing: 0.04em;
  padding: 2px 5px;
  background: rgba(21,32,43,0.72);
  color: #fff;
  border-radius: 2px;
  pointer-events: none;
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

  var filterMode = 'all';
  function applyFilter() {
    document.querySelectorAll('.cell').forEach(function (cell) {
      var show = filterMode === 'all' || cell.classList.contains('starred');
      cell.classList.toggle('hidden', !show);
    });
  }

  document.addEventListener('click', function (e) {
    var star = e.target.closest('.star');
    if (star) {
      e.preventDefault();
      e.stopPropagation();
      toggleStar(star);
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
    nav_parts.append('<a href="/screenshots">截图</a>')
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
    rel = str(f.relative_to(work))
    is_video = f.suffix.lower() in VIDEO_EXTS
    is_starred = rel in stars
    q = urllib.parse.quote(rel)
    raw_url = f'/raw?p={q}'
    thumb = thumb_for(f, work, thumb_root)
    if thumb and thumb.exists():
        media = (
            f'<img class="thumb" src="/thumb?p={q}" alt="{_esc(f.name)}" '
            f'loading="lazy" data-lightbox="{_esc(raw_url)}" '
            f'data-path="{_esc(rel)}" data-bucket="{_esc(bucket)}" '
            f'data-name="{_esc(f.name)}" data-video="{"1" if is_video else "0"}">'
        )
    elif is_video:
        media = (
            f'<video class="thumb" src="{_esc(raw_url)}" muted playsinline '
            f'data-lightbox="{_esc(raw_url)}" data-path="{_esc(rel)}" '
            f'data-bucket="{_esc(bucket)}" data-name="{_esc(f.name)}" data-video="1"></video>'
        )
    else:
        media = (
            f'<a class="thumb-miss" href="{_esc(raw_url)}" '
            f'data-lightbox="{_esc(raw_url)}" data-path="{_esc(rel)}" '
            f'data-bucket="{_esc(bucket)}" data-name="{_esc(f.name)}" data-video="0">查看</a>'
        )
    badge = '<span class="badge">VIDEO</span>' if is_video else ''
    star_cls = 'star on' if is_starred else 'star'
    star_char = '★' if is_starred else '☆'
    cell_cls = 'cell starred' if is_starred else 'cell'
    return (
        f'<article class="{cell_cls}">'
        f'{media}{badge}'
        f'<button type="button" class="{star_cls}" data-path="{_esc(rel)}" '
        f'data-bucket="{_esc(bucket)}" aria-pressed="{"true" if is_starred else "false"}" '
        f'title="{"取消加星" if is_starred else "加星"}">{star_char}</button>'
        f'<div class="edge"><span class="idx">{index:03d}</span>'
        f'<span class="fname" title="{_esc(f.name)}">{_esc(f.name)}</span></div>'
        f'</article>'
    )


def _gallery_toolbar(file_count: int, star_count: int) -> str:
    return (
        f'<div class="toolbar">'
        f'<span class="count">{file_count} 个文件 · '
        f'<span id="pageMetaStars">{star_count}</span> 已加星</span>'
        f'<button type="button" class="chip on" data-filter="all">全部</button>'
        f'<button type="button" class="chip" data-filter="starred">'
        f'仅加星<span class="n" id="starCount">{star_count}</span></button>'
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
    if buckets['screenshots_count'] > 0:
        shot_link = (
            f'<p style="margin:20px 0 0;font-size:0.92rem;color:var(--muted)">'
            f'<a href="/screenshots">截图库 → {buckets["screenshots_count"]} 个文件</a></p>'
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
        f'{_gallery_toolbar(len(files), len(stars))}'
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
        f'{_gallery_toolbar(len(files), len(stars))}'
        f'{sheet}'
    )
    return page_shell(
        '截图',
        body,
        work=work,
        crumbs=[('首页', '/'), ('截图', '/screenshots')],
    )


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


# Top-level dirs created by init_storage.sh (must all exist as directories).
_INIT_SKELETON_DIRS = (
    'inbox', 'by-date', 'screenshots', '_favorite', '_vlogs', '_trash', '_meta',
)


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
                    'initialized': is_work_initialized(self.work),
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
