#!/usr/bin/env python3
"""
rename_organize.py - Rename + organize photos/videos into by-date / screenshots / screenrecords.

Classification (v7):
  0. Known camera filename → by-date/ (normal)
  Images:
    1. Filename contains "screenshot" → screenshots/
    2. No EXIF GPS → screenshots/
  Videos:
    1. Filename contains "record" → screenrecords/
    2. Missing Make OR missing GPS → screenrecords/
  Else → by-date/<year>/<month>[_theme]/<photos|videos>/

Usage:
    ./rename_organize.py --work /Volumes/Storage --dry-run
    ./rename_organize.py --work /Volumes/Storage
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

# Sandbox
ALLOWED_WORK_PREFIXES = ('/Volumes/Storage', '/Volumes/YM/MediaVault', '/Users/ym/Downloads/pic-test')

# Source whitelist
SOURCE_WHITELIST = {
    'iphone', 'samsung', 'xiaomi', 'huawei', 'oppo', 'vivo', 'oneplus', 'google',
    'canon', 'canon-a', 'canon-b',
    'nikon', 'nikon-a', 'sony', 'sony-a', 'fuji', 'fuji-a',
    'ricoh-gr', 'ricoh-gr2',
    'dji', 'dji-nano', 'dji-action', 'dji-pocket', 'dji-osmo',
    'gopro',
    'insta360', 'insta360-x3', 'insta360-go', 'insta360-x4',
    'akaso', 'parrot', 'garmin', 'leica', 'panasonic',
}

EXIF_MAKE_MAP = {
    'Apple': 'iphone', 'SAMSUNG': 'samsung', 'samsung': 'samsung',
    'Xiaomi': 'xiaomi', 'xiaomi': 'xiaomi',
    'HUAWEI': 'huawei', 'huawei': 'huawei',
    'OPPO': 'oppo', 'oppo': 'oppo',
    'vivo': 'vivo', 'VIVO': 'vivo',
    'OnePlus': 'oneplus', 'oneplus': 'oneplus',
    'Google': 'google', 'google': 'google',
    'Canon': 'canon', 'canon': 'canon',
    'NIKON CORPORATION': 'nikon', 'NIKON': 'nikon', 'nikon': 'nikon',
    'SONY': 'sony', 'sony': 'sony',
    'FUJIFILM': 'fuji', 'fujifilm': 'fuji',
    'RICOH IMAGING COMPANY, LTD.': 'ricoh-gr',
    'DJI': 'dji', 'dji': 'dji',
    'GoPro': 'gopro', 'gopro': 'gopro',
    # Insta360 (added after vlog file misclassification)
    'Insta360': 'insta360', 'insta360': 'insta360',
    'INSTA360': 'insta360',
    # Other common action cameras / camcorders
    'AKASO': 'akaso', 'Akaso': 'akaso',
    'Parrot': 'parrot', 'PARROT': 'parrot',
    'Garmin': 'garmin', 'GARMIN': 'garmin',
    'Leica': 'leica', 'LEICA': 'leica',
    'Panasonic': 'panasonic', 'PANASONIC': 'panasonic',
}

# Keywords: screenshot (images) vs record substring (videos).
DEFAULT_SCREENSHOT_KEYWORDS = [
    'screenshot',
]
# v7: any filename containing "record" (screenrecord / recording / …)
DEFAULT_RECORDING_KEYWORDS = [
    'record',
]

# Known camera filename patterns (v5: real photos/videos, NOT screenshots)
# If filename matches one of these, it's a real camera file (not a screen recording),
# regardless of EXIF/QuickTime metadata. This fixes v3's misclassification of
# Xiaomi/Canon/DJI videos (which lack Make/Model metadata) as screenshots.
import re
CAMERA_FILENAME_PATTERNS = [
    # Xiaomi (cameras + phones)
    r'^VID[\d_-]',                # VID_yyyyMMdd_HHmmss[_xx_xx].mp4
    r'^VIDEO[\d_-]',              # VIDEO_yyyyMMdd_xxxxxxxxx.mp4
    r'^IMG[\d_-]',                # IMG_yyyyMMdd_HHmmss or IMG_xxxx (iPhone/Android)
    # Generic Android camera
    r'^\d{8}_\d{6}_\d{3}',      # yyyyMMdd_HHmmss_xxx (date-time-millisec)
    # DJI
    r'^DJI_\d{8}_',              # DJI_yyyyMMdd_HHmmss_xxxx
    r'^dji_mimo_',                # DJI Mimo app (Osmo Pocket etc.)
    # Canon
    r'^MVI_',                      # MVI_xxxx
    r'^IMG_\d{4}',                # iPhone default
    # Sony
    r'^DSC\d+',                  # DSCxxxx
    r'^C\d{6}',                  # C0010001 (Sony video)
    # GoPro
    r'^GOPR\d+',                 # GOPR0001
    r'^GH\d{4}',                  # GoPro Hero
    r'^GX\d{6}',                  # GoPro newer
    # Other
    r'^MVIMG_\d+',               # Android video
    r'^V\d{6}',                   # Panasonic video
    # Apps
    r'^xhs_live_photo_',          # Xiaohongshu live photo
    # WhatsApp (transferred media)
    r'^VID-\d{4}-WA\d+',        # WhatsApp video
    r'^AUD-\d{4}-WA\d+',       # WhatsApp audio
    r'^IMG-\d{4}-WA\d+',       # WhatsApp image
    r'^PTT-\d{4}-WA\d+',       # WhatsApp voice memo
]
CAMERA_FILENAME_REGEX = re.compile('|'.join(CAMERA_FILENAME_PATTERNS), re.IGNORECASE)


def is_camera_filename(path: Path) -> bool:
    """Return True if filename matches known camera/camcorder naming pattern.
    These are real photos/videos from cameras, not screen recordings."""
    return bool(CAMERA_FILENAME_REGEX.match(path.name))

VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.m4v', '.3gp', '.hevc', '.webm'}
LIVE_STILL_EXTS = {'.heic', '.jpg', '.jpeg'}
LIVE_MOTION_EXT = '.mov'
IMAGE_EXTS = {'.jpg', '.jpeg', '.heic', '.png', '.webp',
              '.raw', '.cr2', '.cr3', '.nef', '.arw', '.dng', '.orf', '.rw2'}

HASH_LENGTH = 4
CHUNK = 1024 * 1024


def validate_path(path_str: str, allowed_prefixes, kind: str) -> Path:
    import os
    if os.environ.get("DUPEGURU_TEST") == "1":
        return Path(path_str).expanduser().resolve()

    p = Path(path_str).expanduser().resolve()
    for prefix in allowed_prefixes:
        prefix_resolved = str(Path(prefix).resolve())
        if str(p) == prefix_resolved or str(p).startswith(prefix_resolved + '/'):
            return p
    raise ValueError(
        f"--{kind} {path_str} is not in path whitelist.\n"
        f"  Allowed: {', '.join(allowed_prefixes)}"
    )


def sha256_short(path: Path, length=HASH_LENGTH) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()[:length]


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTS


# === EXIF ===

def read_exif(path: Path) -> dict:
    """Read EXIF from image via PIL. Returns {tag_id: value}."""
    if not is_image(path):
        return {}
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img._getexif() or {}
    except Exception:
        return {}


def get_make_from_exif(exif: dict) -> str:
    if not exif:
        return ''
    make = exif.get(0x010F, b'')
    if isinstance(make, bytes):
        make = make.decode('utf-8', 'ignore')
    return make.strip()


def has_gps(exif: dict) -> bool:
    if not exif:
        return False
    gps = exif.get(0x8825)
    if not gps:
        return False
    # GPSLatitude = 2, GPSLongitude = 4
    return bool(gps.get(2) or gps.get(4))


def has_camera_make(exif: dict) -> bool:
    return bool(get_make_from_exif(exif))


def has_video_make(video_tags: dict) -> bool:
    if not video_tags:
        return False
    for k in ('make', 'manufacturer', 'com.apple.quicktime.make'):
        if str(video_tags.get(k, '')).strip():
            return True
    return False


def has_video_gps(video_tags: dict) -> bool:
    """Best-effort GPS/location tags from ffprobe. Missing → treat as no GPS."""
    if not video_tags:
        return False
    for k in (
        'location',
        'com.apple.quicktime.location.ISO6709',
        'com.apple.quicktime.location.name',
        'gps-coordinates',
        'location-eng',
    ):
        if str(video_tags.get(k, '')).strip():
            return True
    return False


# === Video metadata ===

def read_video_metadata(path: Path) -> dict:
    """ffprobe tags from video."""
    if not is_video(path):
        return {}
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json',
             '-show_entries', 'format_tags:stream_tags', str(path)],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout)
        tags = {}
        if 'format' in data and 'tags' in data['format']:
            tags.update(data['format']['tags'])
        for stream in data.get('streams', []):
            if 'tags' in stream:
                tags.update(stream['tags'])
        return tags
    except Exception:
        return {}


def get_video_creation_time(path: Path) -> Optional[str]:
    """Get video creation_time as YYYYMMDD_HHMMSS."""
    if not is_video(path):
        return None
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json',
             '-show_entries', 'format_tags=creation_time', str(path)],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        ct = data.get('format', {}).get('tags', {}).get('creation_time', '')
        m = re.match(r'(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})', ct)
        if m:
            y, mo, d, h, mi, s = m.groups()
            return f"{y}{mo}{d}_{h}{mi}{s}"
    except Exception:
        pass
    return None


# === Date extraction ===

def get_date_from_exif(exif: dict) -> Optional[str]:
    """Get DateTimeOriginal as YYYYMMDD_HHMMSS."""
    if not exif:
        return None
    dto = exif.get(0x9003, b'')
    if isinstance(dto, bytes):
        dto = dto.decode('utf-8', 'ignore')
    m = re.match(r'(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})', dto)
    if m:
        y, mo, d, h, mi, s = m.groups()
        return f"{y}{mo}{d}_{h}{mi}{s}"
    return None


def get_date_from_mtime(path: Path) -> str:
    mt = datetime.fromtimestamp(path.stat().st_mtime)
    return mt.strftime("%Y%m%d_%H%M%S")


def get_date(path: Path, exif: dict) -> str:
    if is_image(path):
        d = get_date_from_exif(exif)
        if d:
            return d
    if is_video(path):
        d = get_video_creation_time(path)
        if d:
            return d
    return get_date_from_mtime(path)


# === Source detection ===

def get_source(path: Path, exif: dict, video_tags: dict, cli_source: Optional[str]) -> Optional[str]:
    """Priority: CLI > .source sidecar > EXIF Make > None."""
    if cli_source:
        return cli_source if cli_source in SOURCE_WHITELIST else None

    sidecar = path.parent / '.source'
    if sidecar.is_file():
        try:
            src = sidecar.read_text().strip()
            if src in SOURCE_WHITELIST:
                return src
        except Exception:
            pass

    make = get_make_from_exif(exif)
    if make:
        src = EXIF_MAKE_MAP.get(make) or EXIF_MAKE_MAP.get(make.title())
        if src:
            return src
        # Auto-downgrade: unknown Make → use normalized Make as source
        # e.g. "Insta360" → "insta360", "AKASO" → "akaso"
        # The user can later add proper entries to EXIF_MAKE_MAP if desired
        return _normalize_source(make)

    if video_tags:
        for k in ('make', 'manufacturer', 'com.apple.quicktime.make'):
            make_v = video_tags.get(k, '').strip()
            if make_v:
                src = EXIF_MAKE_MAP.get(make_v) or EXIF_MAKE_MAP.get(make_v.title())
                if src:
                    return src
                # Auto-downgrade for video metadata too
                return _normalize_source(make_v)

    return None


def _normalize_source(name: str) -> str:
    """Normalize unknown Make/Model into a safe source segment.

    - Lowercase
    - Replace spaces, dots, slashes with dashes
    - Keep only [a-z0-9-]
    Examples:
      "Insta360"     -> "insta360"
      "AKASO TECH"   -> "akaso-tech"
      "GoPro, Inc."  -> "gopro-inc"
      "Evil/Corp"    -> "evil-corp"
    """
    import re as _re
    s = name.strip().lower()
    s = _re.sub(r'[/\\]+', '-', s)
    s = _re.sub(r'[\s._,]+', '-', s)
    s = _re.sub(r'[^a-z0-9\-]+', '', s)
    s = s.strip('-')
    return s or 'unknown'


# === Capture classification (v7) ===

def classify_capture(path: Path, exif: dict, video_tags: dict,
                      screenshot_keywords: list = None,
                      recording_keywords: list = None,
                      no_gps: bool = True) -> str:
    """v7 classification: 'recording' | 'screenshot' | 'normal'.

    Logic:
      0. Known camera filename → normal
      Images:
        1. Filename contains screenshot keyword → screenshot
        2. No EXIF GPS → screenshot
      Videos:
        1. Filename contains record keyword → recording
        2. Missing Make OR missing GPS → recording
      Else → normal

    ``no_gps`` is kept for CLI compatibility; aggressive GPS/Make rules are
    always on in v7 (the flag is ignored).
    """
    del no_gps  # always-on in v7
    screenshot_keywords = screenshot_keywords or DEFAULT_SCREENSHOT_KEYWORDS
    recording_keywords = recording_keywords or DEFAULT_RECORDING_KEYWORDS

    if is_camera_filename(path):
        return 'normal'

    name_lower = path.name.lower()
    name_normalized = name_lower.replace('_', ' ').replace('.', ' ').replace('-', ' ')

    if is_video(path):
        for kw in recording_keywords:
            if kw.lower() in name_normalized:
                return 'recording'
        has_make = has_video_make(video_tags)
        has_loc = has_video_gps(video_tags)
        if (not has_make) or (not has_loc):
            return 'recording'
        return 'normal'

    # Images (and other non-video media)
    for kw in screenshot_keywords:
        if kw.lower() in name_normalized:
            return 'screenshot'
    if not has_gps(exif):
        return 'screenshot'
    return 'normal'


# Backward compatibility shim
def is_screenshot(path: Path, exif: dict, video_tags: dict,
                  keywords: list, no_gps: bool) -> bool:
    """Legacy API: returns True if classified as recording or screenshot.
    Prefer classify_capture() for new code."""
    result = classify_capture(path, exif, video_tags,
                              DEFAULT_SCREENSHOT_KEYWORDS,
                              DEFAULT_RECORDING_KEYWORDS, no_gps)
    return result in ('screenshot', 'recording')


def star_bucket_for_rel(rel: str) -> str:
    """Map a work-relative path to its stars JSON bucket name."""
    parts = Path(rel).parts
    if not parts:
        return 'unknown'
    if parts[0] in ('screenshots', 'screenrecords', 'docs', 'things'):
        return parts[0]
    if parts[0] == 'by-date' and len(parts) >= 3:
        return parts[2]
    return parts[0]


def _exif_make_str(exif: dict) -> str:
    if not exif:
        return ''
    make = exif.get(0x010f, b'')
    if isinstance(make, bytes):
        make = make.decode('utf-8', 'ignore')
    return str(make or '').strip()


def is_live_still_eligible(path: Path, exif: dict = None) -> bool:
    """Still half of a Live Photo: camera filename or Apple/iPhone Make."""
    if path.suffix.lower() not in LIVE_STILL_EXTS:
        return False
    if is_camera_filename(path):
        return True
    if exif is None:
        exif = read_exif(path)
    make = _exif_make_str(exif).lower()
    return 'apple' in make or 'iphone' in make


def _pick_live_still(stills: list[Path]) -> Optional[Path]:
    """Prefer HEIC; require is_live_still_eligible."""
    stills = sorted(
        stills,
        key=lambda p: (0 if p.suffix.lower() == '.heic' else 1, p.name),
    )
    for still in stills:
        try:
            if is_live_still_eligible(still):
                return still
        except Exception:
            continue
    return None


def find_live_photo_pairs(files: list[Path]) -> dict:
    """Map still Path -> motion .mov Path for Live Photo pairs in inbox.

    1) Same-folder: parent + stem (case-insensitive).
    2) Cross-folder: among leftovers, unique stem with exactly one eligible
       still and one .mov anywhere under inbox (reunites stock splits).
       Ambiguous stems (2+ stills or 2+ movs) are skipped with a warning.
    """
    by_parent_stem = {}  # (parent, stem) -> {'stills','movs'}
    by_stem = {}  # stem -> {'stills','movs'}
    for f in files:
        if not f.is_file():
            continue
        stem = f.stem.lower()
        parent = str(f.parent.resolve())
        ext = f.suffix.lower()
        if ext not in LIVE_STILL_EXTS and ext != LIVE_MOTION_EXT:
            continue
        g1 = by_parent_stem.setdefault((parent, stem), {'stills': [], 'movs': []})
        g2 = by_stem.setdefault(stem, {'stills': [], 'movs': []})
        if ext == LIVE_MOTION_EXT:
            g1['movs'].append(f)
            g2['movs'].append(f)
        else:
            g1['stills'].append(f)
            g2['stills'].append(f)

    pairs = {}
    used = set()

    # Pass 1: same directory
    for g in by_parent_stem.values():
        if not g['movs'] or not g['stills']:
            continue
        still = _pick_live_still(g['stills'])
        if still is None:
            continue
        mov = sorted(g['movs'], key=lambda p: p.name)[0]
        if still in used or mov in used:
            continue
        pairs[still] = mov
        used.add(still)
        used.add(mov)

    # Pass 2: cross-folder unique stem (stock often splits HEIC/MOV into dirs)
    for stem, g in sorted(by_stem.items()):
        stills = [p for p in g['stills'] if p not in used]
        movs = [p for p in g['movs'] if p not in used]
        if not stills or not movs:
            continue
        eligible = []
        for s in stills:
            try:
                if is_live_still_eligible(s):
                    eligible.append(s)
            except Exception:
                continue
        if not eligible:
            continue
        if len(eligible) != 1 or len(movs) != 1:
            print(
                f"  [warn] ambiguous Live Photo stem {stem!r}: "
                f"{len(eligible)} still(s), {len(movs)} mov(s) — skip cross-folder pair",
                file=sys.stderr,
                flush=True,
            )
            continue
        still = _pick_live_still(eligible) or eligible[0]
        mov = movs[0]
        # Skip if already same-folder (handled); here parents differ or leftover
        if still.parent.resolve() == mov.parent.resolve():
            continue
        # Require close mtimes so unrelated batches with the same stem
        # (e.g. IMG_0001) are not paired across folders.
        try:
            dt = abs(still.stat().st_mtime - mov.stat().st_mtime)
        except OSError:
            continue
        if dt > 5.0:
            print(
                f"  [warn] cross-folder Live stem {stem!r}: "
                f"mtime delta {dt:.1f}s > 5s — skip pair",
                file=sys.stderr,
                flush=True,
            )
            continue
        pairs[still] = mov
        used.add(still)
        used.add(mov)

    return pairs


def _theme_bucket_parts(date: str, theme: Optional[dict]) -> tuple:
    """Return (year, month_dir_name) under by-date/.

    Theme buckets use theme.month (start month), not the file's capture month.
    """
    if theme:
        tm = str(theme.get('month') or '').strip()
        theme_name = (theme.get('name') or '').strip()
        if _THEME_MONTH_RE.match(tm):
            safe = theme_name.replace('/', '_').replace('\\', '_').replace('..', '_')
            if theme_name:
                return tm[:4], f'{tm}_{safe}'
            return tm[:4], tm
    if not date or len(date) < 6:
        return '', ''
    y, m = date[:4], date[4:6]
    return y, f'{y}-{m}'


def _month_dir_name(date: str, theme: Optional[dict]) -> str:
    _year, month_dir = _theme_bucket_parts(date, theme)
    return month_dir


def unique_live_pair_dests(dest_dir: Path, stem: str, still_ext: str) -> tuple:
    """Pick unused stem for still+mov pair (bumps _N if either exists)."""
    n = 0
    while True:
        s = stem if n == 0 else f"{stem}_{n}"
        still_p = dest_dir / f"{s}{still_ext}"
        mov_p = dest_dir / f"{s}{LIVE_MOTION_EXT}"
        if not still_p.exists() and not mov_p.exists():
            return still_p, mov_p
        n += 1


def live_companion_of(path: Path) -> Optional[Path]:
    """Same-dir Live Photo companion: still ↔ .mov with the same stem."""
    if not path.is_file():
        return None
    ext = path.suffix.lower()
    stem_l = path.stem.lower()
    parent = path.parent
    if ext == LIVE_MOTION_EXT:
        want = LIVE_STILL_EXTS
    elif ext in LIVE_STILL_EXTS:
        want = {LIVE_MOTION_EXT}
    else:
        return None
    try:
        candidates = list(parent.iterdir())
    except OSError:
        return None
    for p in candidates:
        if not p.is_file() or p.resolve() == path.resolve():
            continue
        if p.suffix.lower() in want and p.stem.lower() == stem_l:
            return p
    return None


def unique_pair_dests(dest_dir: Path, stem: str, ext1: str, ext2: str) -> tuple:
    """Pick unused shared stem for two companion files (bumps _N if either exists)."""
    n = 0
    while True:
        s = stem if n == 0 else f"{stem}_{n}"
        p1 = dest_dir / f"{s}{ext1}"
        p2 = dest_dir / f"{s}{ext2}"
        if not p1.exists() and not p2.exists():
            return p1, p2
        n += 1


def plan_live_pair(work: Path, still: Path, mov: Path,
                   events: list = None, cli_source: Optional[str] = None) -> tuple:
    """Plan destinations for a Live Photo pair (both under photos/).

    Returns (still_dest, mov_dest, stem_name_without_ext).
    Stem: <date>_<source>_live_<hash> or <date>_live_<hash>.
    Hash is from the still image bytes.
    """
    events = events if events is not None else []
    exif = read_exif(still)
    date = get_date(still, exif)
    source = get_source(still, exif, {}, cli_source)
    h = sha256_short(still)
    if source:
        stem = f"{date}_{source}_live_{h}"
    else:
        stem = f"{date}_live_{h}"

    theme = match_theme(still, date, source, events)
    year, month_dir = _theme_bucket_parts(date, theme)
    dest_dir = work / 'by-date' / year / month_dir / 'photos'
    still_ext = still.suffix.lower()
    return unique_live_pair_dests(dest_dir, stem, still_ext) + (stem,)


def compute_dest(work: Path, src: Path, capture_type: str,
                 events: list = None, cli_source: Optional[str] = None,
                 exif: dict = None, video_tags: dict = None) -> tuple:
    """Compute (dest_path, new_name, capture_type) for a file.

    Does not move. ``capture_type``: screenshot|recording|docs|things|normal.
    """
    events = events if events is not None else []
    if exif is None:
        exif = read_exif(src)
    if video_tags is None:
        video_tags = read_video_metadata(src) if is_video(src) else {}

    date = get_date(src, exif)
    source = get_source(src, exif, video_tags, cli_source)
    h = sha256_short(src)
    ext = src.suffix.lower()
    source_part = f"{source}_" if source else ""

    if capture_type == 'recording':
        dest_dir = work / 'screenrecords'
        new_name = f"screenrecorder_{date}_{source_part}{h}{ext}"
    elif capture_type == 'screenshot':
        dest_dir = work / 'screenshots'
        new_name = f"screenshot_{date}_{source_part}{h}{ext}"
    elif capture_type == 'docs':
        dest_dir = work / 'docs'
        new_name = f"doc_{date}_{source_part}{h}{ext}"
    elif capture_type == 'things':
        dest_dir = work / 'things'
        new_name = f"things_{date}_{source_part}{h}{ext}"
    else:
        theme = match_theme(src, date, source, events)
        year, month_dir_name = _theme_bucket_parts(date, theme)
        bucket_type = 'videos' if is_video(src) else 'photos'
        dest_dir = work / 'by-date' / year / month_dir_name / bucket_type
        new_name = f"{date}_{source_part}{h}{ext}"

    dest = get_unique_dest(dest_dir / new_name)
    return dest, new_name, capture_type


def plan_destination(work: Path, path: Path, force_type: Optional[str] = None,
                     events: list = None, cli_source: Optional[str] = None,
                     screenshot_keywords: list = None,
                     recording_keywords: list = None) -> tuple:
    """Plan dest for path. force_type: None | screenshot | recording | docs | things | normal.

    Returns (dest_path, capture_type, new_name).
    """
    exif = read_exif(path)
    video_tags = read_video_metadata(path) if is_video(path) else {}
    if force_type in ('screenshot', 'recording', 'docs', 'things', 'normal'):
        capture_type = force_type
    else:
        capture_type = classify_capture(
            path, exif, video_tags,
            screenshot_keywords or DEFAULT_SCREENSHOT_KEYWORDS,
            recording_keywords or DEFAULT_RECORDING_KEYWORDS,
            True,
        )
    dest, new_name, capture_type = compute_dest(
        work, path, capture_type, events=events, cli_source=cli_source,
        exif=exif, video_tags=video_tags,
    )
    return dest, capture_type, new_name


def reclassify_paths(work: Path, paths: list, action: str,
                     dry_run: bool = False, events: list = None) -> list:
    """Reclassify files. action: 'to_screen' | 'to_normal' | 'to_docs' | 'to_things'.

    to_screen: image → screenshot, video → recording (by suffix).
    to_normal: force normal (by-date naming).
    to_docs: force docs/ (manual document photos).
    to_things: force things/ (manual object photos).

    Live Photo pairs (same-dir still ↔ .mov) move together with a shared stem.
    For to_screen, the still leads so the companion .mov follows into screenshots/
    (not screenrecords/ alone).
    """
    if action not in ('to_screen', 'to_normal', 'to_docs', 'to_things'):
        raise ValueError(f'unknown action: {action}')
    events = events if events is not None else load_events(work, None)
    results = []
    work_res = work.resolve()

    # Normalize + dedupe input paths; track which will be handled as companions.
    normalized = []
    path_set = set()
    for rel in paths:
        rel = str(rel).lstrip('/')
        if rel in path_set:
            continue
        path_set.add(rel)
        normalized.append(rel)

    handled = set()  # resolved Paths already moved/skipped as part of a pair

    for rel in normalized:
        src = (work / rel).resolve()
        if src in handled:
            continue

        item = {'ok': False, 'src': rel, 'dest': None, 'capture_type': None}
        try:
            if not str(src).startswith(str(work_res) + os.sep) and src != work_res:
                item['error'] = 'path outside work'
                results.append(item)
                continue
            if not src.is_file():
                item['error'] = 'not a file'
                results.append(item)
                continue

            companion = live_companion_of(src)
            lead = src
            follower = companion
            # Prefer still as lead when classifying to_screen, or when both halves
            # were requested (avoid splitting mov → screenrecords alone).
            if companion is not None:
                src_is_mov = src.suffix.lower() == LIVE_MOTION_EXT
                comp_is_still = companion.suffix.lower() in LIVE_STILL_EXTS
                try:
                    comp_rel = str(companion.resolve().relative_to(work_res))
                except ValueError:
                    comp_rel = None
                both_requested = bool(comp_rel and comp_rel in path_set)
                if (src_is_mov and comp_is_still
                        and (action == 'to_screen' or both_requested)):
                    lead, follower = companion, src
                    item['src'] = str(lead.resolve().relative_to(work_res))

            if action == 'to_screen':
                force = 'recording' if is_video(lead) else 'screenshot'
            elif action == 'to_docs':
                force = 'docs'
            elif action == 'to_things':
                force = 'things'
            else:
                force = 'normal'

            dest, capture_type, new_name = plan_destination(
                work, lead, force_type=force, events=events,
            )
            item['capture_type'] = capture_type

            follower_dest = None
            follower_orig = None
            if follower is not None:
                follower_orig = follower.resolve()
                stem = Path(new_name).stem
                lead_dest, follower_dest = unique_pair_dests(
                    dest.parent, stem, lead.suffix.lower(), follower.suffix.lower(),
                )
                dest = lead_dest
                item['companion_src'] = str(follower_orig.relative_to(work_res))
                item['companion_dest'] = str(follower_dest.resolve().relative_to(work_res))

            item['dest'] = str(dest.resolve().relative_to(work_res))
            item['new_name'] = dest.name
            lead_orig = lead.resolve()

            if lead_orig == dest.resolve() and (
                    follower is None
                    or follower_orig == follower_dest.resolve()):
                item['ok'] = True
                item['skipped'] = True
                handled.add(lead_orig)
                if follower_orig is not None:
                    handled.add(follower_orig)
                results.append(item)
                continue

            if dry_run:
                item['ok'] = True
                handled.add(lead_orig)
                if follower_orig is not None:
                    handled.add(follower_orig)
                results.append(item)
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(lead), str(dest))
            if follower is not None and follower_dest is not None:
                try:
                    shutil.move(str(follower), str(follower_dest))
                except Exception:
                    try:
                        if dest.is_file() and not Path(lead_orig).exists():
                            shutil.move(str(dest), str(lead_orig))
                    except Exception as rb:
                        print(
                            f"  [live-pair] rollback failed: {rb}",
                            file=sys.stderr,
                            flush=True,
                        )
                    raise
                handled.add(follower_orig)
            handled.add(lead_orig)
            item['ok'] = True
        except Exception as e:
            item['error'] = str(e)
        results.append(item)

    return results


def resolve_archived_date(path: Path) -> Optional[str]:
    """YYYYMMDD from archived filename, else EXIF/mtime (first 8 of get_date)."""
    date, _ = parse_archived_name(path.name)
    if date and re.match(r'^\d{8}$', date):
        return date
    try:
        exif = read_exif(path) if is_image(path) else {}
    except Exception:
        exif = {}
    raw = get_date(path, exif)
    if raw and len(raw) >= 8 and raw[:8].isdigit():
        return raw[:8]
    return None


def return_to_default_month_paths(work: Path, paths: list,
                                  dry_run: bool = False) -> list:
    """Move by-date files into the default YYYY-MM month bucket (no theme).

    Destination: by-date/<Y>/<YYYY-MM>/{photos|videos}/ — ignores events.yaml
    theme matching (reverse of rebucket_themes for selected files).

    Keeps basename when possible. Live Photo pairs (same-dir still ↔ .mov)
    move together with a shared stem. Only accepts paths under by-date/.
    """
    results = []
    work_res = work.resolve()

    normalized = []
    path_set = set()
    for rel in paths:
        rel = str(rel).lstrip('/')
        if rel in path_set:
            continue
        path_set.add(rel)
        normalized.append(rel)

    handled = set()

    for rel in normalized:
        src = (work / rel).resolve()
        if src in handled:
            continue

        item = {'ok': False, 'src': rel, 'dest': None, 'capture_type': 'normal'}
        try:
            if not str(src).startswith(str(work_res) + os.sep) and src != work_res:
                item['error'] = 'path outside work'
                results.append(item)
                continue
            if not src.is_file():
                item['error'] = 'not a file'
                results.append(item)
                continue

            try:
                rel_check = str(src.relative_to(work_res))
            except ValueError:
                item['error'] = 'path outside work'
                results.append(item)
                continue
            parts = Path(rel_check).parts
            if len(parts) < 2 or parts[0] != 'by-date':
                item['error'] = 'not under by-date/'
                results.append(item)
                continue

            companion = live_companion_of(src)
            lead = src
            follower = companion
            if companion is not None:
                src_is_mov = src.suffix.lower() == LIVE_MOTION_EXT
                comp_is_still = companion.suffix.lower() in LIVE_STILL_EXTS
                try:
                    comp_rel = str(companion.resolve().relative_to(work_res))
                except ValueError:
                    comp_rel = None
                both_requested = bool(comp_rel and comp_rel in path_set)
                if src_is_mov and comp_is_still and both_requested:
                    lead, follower = companion, src
                    item['src'] = str(lead.resolve().relative_to(work_res))

            date = resolve_archived_date(lead)
            if not date:
                item['error'] = 'cannot resolve date'
                results.append(item)
                continue

            year = date[:4]
            month_str = f'{date[:4]}-{date[4:6]}'
            sub = lead.parent.name
            if sub not in ('photos', 'videos'):
                sub = 'videos' if is_video(lead) else 'photos'
            dest_dir = work / 'by-date' / year / month_str / sub

            follower_dest = None
            follower_orig = None
            lead_orig = lead.resolve()
            if follower is not None:
                follower_orig = follower.resolve()

            def _occupied(cand: Path, *self_paths: Path) -> bool:
                if not cand.exists():
                    return False
                try:
                    cres = cand.resolve()
                except OSError:
                    return True
                for sp in self_paths:
                    if sp is not None and cres == sp:
                        return False
                return True

            if follower is not None:
                d0 = dest_dir / lead.name
                d1 = dest_dir / follower.name
                if _occupied(d0, lead_orig, follower_orig) or _occupied(
                        d1, lead_orig, follower_orig):
                    lead_dest, follower_dest = unique_pair_dests(
                        dest_dir, lead.stem,
                        lead.suffix.lower(), follower.suffix.lower(),
                    )
                else:
                    lead_dest, follower_dest = d0, d1
                dest = lead_dest
                item['companion_src'] = str(follower_orig.relative_to(work_res))
                item['companion_dest'] = str(follower_dest.resolve().relative_to(work_res))
            else:
                cand = dest_dir / lead.name
                if _occupied(cand, lead_orig):
                    dest = get_unique_dest(cand)
                else:
                    dest = cand

            item['dest'] = str(dest.resolve().relative_to(work_res))
            item['new_name'] = dest.name

            if lead_orig == dest.resolve() and (
                    follower is None
                    or follower_orig == follower_dest.resolve()):
                item['ok'] = True
                item['skipped'] = True
                handled.add(lead_orig)
                if follower_orig is not None:
                    handled.add(follower_orig)
                results.append(item)
                continue

            if dry_run:
                item['ok'] = True
                handled.add(lead_orig)
                if follower_orig is not None:
                    handled.add(follower_orig)
                results.append(item)
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(lead), str(dest))
            if follower is not None and follower_dest is not None:
                try:
                    shutil.move(str(follower), str(follower_dest))
                except Exception:
                    try:
                        if dest.is_file() and not Path(lead_orig).exists():
                            shutil.move(str(dest), str(lead_orig))
                    except Exception as rb:
                        print(
                            f"  [live-pair] rollback failed: {rb}",
                            file=sys.stderr,
                            flush=True,
                        )
                    raise
                handled.add(follower_orig)
            handled.add(lead_orig)
            item['ok'] = True
        except Exception as e:
            item['error'] = str(e)
        results.append(item)

    return results


# === Theme matching ===

def parse_simple_yaml(text: str) -> dict:
    """Minimal YAML parser for events.yaml.

    Supports the schema we use:
      - top-level scalars
      - top-level list of scalars: [a, b, c]
      - top-level list of dicts:
          - name: foo
            month: 2024-07
            sources: [iphone, canon]
            date_range:
              start: 2024-07-10
              end:   2024-07-18
            files:
              - foo.jpg
              - bar.jpg
    """
    result = {}
    lines = text.split('\n')

    def get_indent(line):
        return len(line) - len(line.lstrip())

    def parse_value(s):
        s = s.strip()
        if not s:
            return None
        if s.startswith('[') and s.endswith(']'):
            inner = s[1:-1].strip()
            if not inner:
                return []
            parts = [p.strip().strip('"').strip("'") for p in inner.split(',')]
            return [p for p in parts if p]
        if s.lower() == 'true':
            return True
        if s.lower() == 'false':
            return False
        if s.startswith('"') and s.endswith('"'):
            return s[1:-1]
        if s.startswith("'") and s.endswith("'"):
            return s[1:-1]
        if s.isdigit():
            return int(s)
        return s.strip('"').strip("'")

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            i += 1
            continue
        indent = get_indent(line)

        # Top-level key (indent == 0)
        if indent == 0 and ':' in stripped:
            key, _, val = stripped.partition(':')
            key = key.strip()
            val = val.strip()
            if val == '':
                # Could be list of dicts OR nested mapping
                # Look ahead: if next non-empty line starts with '  - ' -> list of dicts
                j = i + 1
                while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith('#')):
                    j += 1
                if j < len(lines) and lines[j].lstrip().startswith('- '):
                    # List of dicts
                    items = []
                    cur_dict = None
                    while j < len(lines):
                        l = lines[j]
                        if not l.strip() or l.strip().startswith('#'):
                            j += 1
                            continue
                        l_indent = get_indent(l)
                        if l_indent == 0:
                            break  # back to top-level
                        l_stripped = l.strip()
                        if l.startswith('  - ') or l.startswith('- '):
                            # New dict item
                            if cur_dict is not None:
                                items.append(cur_dict)
                            rest = l_stripped[2:].strip() if l_stripped.startswith('- ') else l_stripped
                            cur_dict = {}
                            if ':' in rest:
                                k, _, v = rest.partition(':')
                                cur_dict[k.strip()] = parse_value(v)
                        elif l.startswith('    ') and cur_dict is not None:
                            # Field of current dict
                            if ':' in l_stripped:
                                k, _, v = l_stripped.partition(':')
                                k = k.strip()
                                v = v.strip()
                                if v == '' and k == 'date_range':
                                    # Nested start/end under date_range
                                    nested = {}
                                    j2 = j + 1
                                    while j2 < len(lines):
                                        nl = lines[j2]
                                        if not nl.strip() or nl.strip().startswith('#'):
                                            j2 += 1
                                            continue
                                        if get_indent(nl) <= l_indent:
                                            break
                                        ns = nl.strip()
                                        if ':' in ns:
                                            nk, _, nv = ns.partition(':')
                                            nested[nk.strip()] = parse_value(nv)
                                        j2 += 1
                                    cur_dict[k] = nested
                                    j = j2
                                    continue
                                if v == '' and k in ('sources', 'files'):
                                    # Block list: sources:\n      - iphone
                                    list_items = []
                                    j2 = j + 1
                                    while j2 < len(lines):
                                        nl = lines[j2]
                                        if not nl.strip() or nl.strip().startswith('#'):
                                            j2 += 1
                                            continue
                                        if get_indent(nl) <= l_indent:
                                            break
                                        ns = nl.strip()
                                        if ns.startswith('- '):
                                            list_items.append(parse_value(ns[2:].strip()))
                                        elif ns.startswith('-'):
                                            list_items.append(parse_value(ns[1:].strip()))
                                        else:
                                            break
                                        j2 += 1
                                    cur_dict[k] = list_items
                                    j = j2
                                    continue
                                cur_dict[k] = parse_value(v)
                        j += 1
                    if cur_dict is not None:
                        items.append(cur_dict)
                    result[key] = items
                    i = j
                    continue
                else:
                    # Nested mapping (not used in our schema but handle anyway)
                    nested = {}
                    j = i + 1
                    while j < len(lines):
                        l = lines[j]
                        if not l.strip() or l.strip().startswith('#'):
                            j += 1
                            continue
                        if get_indent(l) == 0:
                            break
                        if ':' in l:
                            k, _, v = l.strip().partition(':')
                            nested[k.strip()] = parse_value(v)
                        j += 1
                    result[key] = nested
                    i = j
                    continue
            else:
                result[key] = parse_value(val)
            i += 1
            continue
        else:
            i += 1

    return result


_THEME_MONTH_RE = re.compile(r'^\d{4}-\d{2}$')
_THEME_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_THEME_NAME_RE = re.compile(r'^[^/\\\0]+$')


def _as_iso_date(value) -> str:
    """Normalize YAML date / datetime / str to YYYY-MM-DD (or '')."""
    if value is None:
        return ''
    if hasattr(value, 'isoformat'):
        try:
            return str(value.isoformat())[:10]
        except Exception:
            pass
    s = str(value).strip()
    if len(s) >= 10 and s[4] == '-' and s[7] == '-':
        return s[:10]
    return s


def _require_iso_calendar_date(value, *, label: str = 'date') -> str:
    """Return YYYY-MM-DD calendar date, or '' if empty. Raise ValueError if invalid."""
    s = _as_iso_date(value)
    if not s:
        return ''
    if not _THEME_DATE_RE.match(s):
        raise ValueError(f'{label} must be YYYY-MM-DD (got {s!r})')
    try:
        date.fromisoformat(s)
    except ValueError as e:
        raise ValueError(f'{label} is not a valid calendar date (got {s!r})') from e
    return s


def _as_str_list(value) -> list:
    """Normalize sources/files to a list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        return [s] if s else []
    if isinstance(value, (list, tuple)):
        out = []
        for x in value:
            if x is None:
                continue
            s = str(x).strip()
            if s:
                out.append(s)
        return out
    s = str(value).strip()
    return [s] if s else []


def _theme_range(theme: dict) -> tuple:
    """Return (start_iso, end_iso) for a theme, either nested or flat. Empty strings if none."""
    dr = theme.get('date_range') if isinstance(theme.get('date_range'), dict) else None
    if dr is not None:
        return _as_iso_date(dr.get('start')), _as_iso_date(dr.get('end'))
    return _as_iso_date(theme.get('start')), _as_iso_date(theme.get('end'))


def _iter_months_spanned(start_iso: str, end_iso: str):
    """Yield YYYY-MM from start month through end month inclusive."""
    if not start_iso or not end_iso or len(start_iso) < 7 or len(end_iso) < 7:
        return
    sm, em = start_iso[:7], end_iso[:7]
    if not _THEME_MONTH_RE.match(sm) or not _THEME_MONTH_RE.match(em):
        return
    y, m = int(sm[:4]), int(sm[5:7])
    ey, emonth = int(em[:4]), int(em[5:7])
    if (y, m) > (ey, emonth):
        return
    while (y, m) <= (ey, emonth):
        yield f'{y:04d}-{m:02d}'
        m += 1
        if m > 12:
            m = 1
            y += 1


def _normalize_theme_dict(t: dict) -> dict:
    """Copy theme with string dates and list sources/files (PyYAML-safe).

    If date_range.start is set, month becomes start's YYYY-MM (bucket = start month).
    """
    out = dict(t)
    if 'month' in out and out['month'] is not None:
        m = out['month']
        if hasattr(m, 'isoformat'):
            out['month'] = str(m.isoformat())[:7]
        else:
            out['month'] = str(m).strip()

    dr = out.get('date_range')
    if isinstance(dr, dict):
        out['date_range'] = {
            'start': _require_iso_calendar_date(dr.get('start'), label='date_range.start'),
            'end': _require_iso_calendar_date(dr.get('end'), label='date_range.end'),
        }
    if 'start' in out:
        out['start'] = _require_iso_calendar_date(out.get('start'), label='date_range.start')
    if 'end' in out:
        out['end'] = _require_iso_calendar_date(out.get('end'), label='date_range.end')
    if 'sources' in out:
        out['sources'] = _as_str_list(out.get('sources'))
    if 'files' in out:
        out['files'] = _as_str_list(out.get('files'))
    if 'name' in out and out['name'] is not None:
        out['name'] = str(out['name']).strip()

    start, _end = _theme_range(out)
    if start and len(start) >= 7:
        derived = start[:7]
        existing = str(out.get('month') or '').strip()
        if existing and _THEME_MONTH_RE.match(existing) and existing != derived:
            raise ValueError(
                f'month {existing!r} must equal date_range start month {derived!r}'
            )
        out['month'] = derived
    return out


def parse_events_yaml_text(text: str) -> list[dict]:
    """Parse events.yaml text into theme dicts. Raises ValueError on parse errors."""
    try:
        try:
            import yaml  # type: ignore
            data = yaml.safe_load(text) or {}
        except ImportError:
            data = parse_simple_yaml(text)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f'YAML parse error: {e}') from e

    if not isinstance(data, dict):
        raise ValueError('events.yaml root must be a mapping')
    themes = data.get('themes', [])
    if themes is None:
        themes = []
    if not isinstance(themes, list):
        raise ValueError('themes must be a list')

    result: list[dict] = []
    for i, t in enumerate(themes):
        if not isinstance(t, dict):
            raise ValueError(f'themes[{i}] must be a mapping')
        try:
            result.append(_normalize_theme_dict(t))
        except ValueError as e:
            raise ValueError(f'themes[{i}]: {e}') from e
    return result


def validate_events_themes(themes: list) -> None:
    """Require each theme has safe name, YYYY-MM month, and a match rule. Raises ValueError."""
    seen_names: dict[str, int] = {}
    for i, t in enumerate(themes):
        if not isinstance(t, dict):
            raise ValueError(f'themes[{i}] must be a mapping')
        name = t.get('name')
        month = t.get('month')
        if name is None or not str(name).strip():
            raise ValueError(f'themes[{i}]: name required')
        name_s = str(name).strip()
        if (
            not _THEME_NAME_RE.match(name_s)
            or name_s in ('.', '..')
            or '..' in name_s
        ):
            raise ValueError(
                f'themes[{i}]: name must not contain /, \\, or .. '
                f'(got {name_s!r})'
            )
        if name_s in seen_names:
            raise ValueError(
                f'themes[{i}] ({name_s}): duplicate theme name '
                f'(also themes[{seen_names[name_s]}])'
            )
        seen_names[name_s] = i

        # Strict calendar dates for date_range (reject "not-a-date", 2026-02-30, etc.)
        try:
            dr = t.get('date_range') if isinstance(t.get('date_range'), dict) else None
            if dr is not None:
                start = _require_iso_calendar_date(dr.get('start'), label='date_range.start')
                end = _require_iso_calendar_date(dr.get('end'), label='date_range.end')
            else:
                start = _require_iso_calendar_date(t.get('start'), label='date_range.start')
                end = _require_iso_calendar_date(t.get('end'), label='date_range.end')
        except ValueError as e:
            raise ValueError(f'themes[{i}] ({name_s}): {e}') from e

        month_s = '' if month is None else str(month).strip()
        if start and len(start) >= 7:
            derived = start[:7]
            if month_s and month_s != derived:
                raise ValueError(
                    f'themes[{i}] ({name_s}): month {month_s!r} must equal '
                    f'date_range start month {derived!r}'
                )
            if not month_s:
                month_s = derived
        if not _THEME_MONTH_RE.match(month_s):
            raise ValueError(
                f'themes[{i}]: month must be YYYY-MM '
                f'(set month or provide date_range.start)'
            )

        sources = _as_str_list(t.get('sources'))
        files = _as_str_list(t.get('files'))
        if not start and not end and not sources and not files:
            raise ValueError(
                f'themes[{i}] ({name_s}): need date_range and/or sources and/or files '
                f'(name+month alone never matches any file)'
            )
        if (start and not end) or (end and not start):
            raise ValueError(
                f'themes[{i}] ({name_s}): date_range needs both start and end'
            )
        if start and end and start > end:
            raise ValueError(
                f'themes[{i}] ({name_s}): date_range start must be <= end'
            )


def warn_overlapping_themes(themes: list) -> None:
    """Warn on stderr when any themes have overlapping date_ranges."""
    ranged = []
    for t in themes or []:
        if not isinstance(t, dict):
            continue
        name = str(t.get('name') or '').strip() or '?'
        start, end = _theme_range(t)
        if not start or not end:
            continue
        ranged.append((name, start, end))
    for i in range(len(ranged)):
        n1, s1, e1 = ranged[i]
        for j in range(i + 1, len(ranged)):
            n2, s2, e2 = ranged[j]
            if s1 <= e2 and s2 <= e1:
                print(
                    f"  [warn] overlapping themes: "
                    f"{n1!r} ({s1}–{e1}) vs {n2!r} ({s2}–{e2}); "
                    f"earlier entry in events.yaml wins",
                    file=sys.stderr,
                )


def load_events(work: Path, events_path: Optional[Path]) -> list[dict]:
    """Load and validate themes. Raises ValueError if events.yaml is invalid."""
    path = events_path or (work / '_meta' / 'events.yaml')
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as e:
        raise ValueError(f'cannot read events.yaml: {e}') from e
    themes = parse_events_yaml_text(text)
    validate_events_themes(themes)
    warn_overlapping_themes(themes)
    return themes


def match_theme(file_path: Path, date: str, source: Optional[str], events: list) -> Optional[dict]:
    if not events or not date:
        return None
    m = re.match(r'(\d{4})(\d{2})\d{2}', date)
    if not m:
        return None
    year, month = m.groups()
    month_str = f"{year}-{month}"

    file_date = date[:8]  # YYYYMMDD
    file_date_iso = f"{file_date[:4]}-{file_date[4:6]}-{file_date[6:8]}"

    # Relativize path for files: matching (prefer path under work-like roots)
    try:
        rel_path = str(file_path).replace('\\', '/')
        parts = Path(rel_path).parts
        if 'by-date' in parts:
            idx = parts.index('by-date')
            rel_path = '/'.join(parts[idx:])
        else:
            rel_path = file_path.name
    except Exception:
        rel_path = str(file_path)
    base_name = Path(rel_path).name

    for theme in events:
        # Highest: explicit files (basename or full relative path; no substring)
        explicit = _as_str_list(theme.get('files'))
        if explicit:
            for pat in explicit:
                p = pat.strip()
                while p.startswith('./'):
                    p = p[2:]
                p = p.lstrip('/')
                if not p:
                    continue
                if p == base_name or p == rel_path or rel_path.endswith('/' + p):
                    return theme

        start, end = _theme_range(theme)
        # date_range may span months; do not require file month == theme.month
        if start and end:
            if start <= file_date_iso <= end:
                sources = _as_str_list(theme.get('sources'))
                if not sources or (source is not None and source in sources):
                    return theme
            continue

        # Only sources (no date_range): still limited to theme.month
        if str(theme.get('month') or '').strip() != month_str:
            continue
        sources = _as_str_list(theme.get('sources'))
        if sources and source and source in sources:
            month_themes = [
                t for t in events
                if str(t.get('month') or '').strip() == month_str
            ]
            if len(month_themes) == 1:
                return theme

    return None


# === File processing ===

def scan_inbox(work: Path) -> list[Path]:
    inbox = work / 'inbox'
    if not inbox.exists():
        return []
    files = []
    for f in inbox.rglob('*'):
        if f.is_file():
            # Skip .DS_Store, .source sidecars, and other dotfiles
            if f.name.startswith('.'):
                continue
            files.append(f)
    return files


def scan_by_date_default_months(work: Path, events: list,
                                theme_names: Optional[list] = None) -> list[Path]:
    """List files in by-date/<Y>/<YYYY-MM>/{photos,videos}/ for months themes cover.

    For themes with date_range, every YYYY-MM spanned by the range is scanned
    (so cross-month files can be pulled into the start-month theme bucket).
    Without date_range, only theme.month is used.
    Does not include theme side buckets (YYYY-MM_name).
    If theme_names is set, only months belonging to those theme names are scanned.
    """
    files = []
    scoped = events or []
    if theme_names is not None:
        want = {str(n).strip() for n in theme_names if str(n).strip()}
        scoped = [t for t in scoped if str(t.get('name') or '').strip() in want]
    months: set = set()
    for t in scoped:
        start, end = _theme_range(t)
        if start and end:
            months.update(_iter_months_spanned(start, end))
        elif t.get('month'):
            months.add(str(t.get('month')).strip())
    for month_str in sorted(m for m in months if m):
        for d in _default_month_dirs(work, month_str):
            try:
                for f in d.iterdir():
                    if not f.is_file():
                        continue
                    if f.name.startswith('.'):
                        continue
                    files.append(f)
            except OSError as e:
                print(f'  [warn] cannot list {d}: {e}', file=sys.stderr)
    return files


def get_unique_dest(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem = dest.stem
    ext = dest.suffix
    counter = 1
    while True:
        candidate = dest.parent / f"{stem}_{counter}{ext}"
        if not candidate.exists():
            return candidate
        counter += 1


def process_file(work: Path, f: Path, events: list, cli_source: Optional[str],
                 screenshot_keywords: list, recording_keywords: list,
                 no_gps: bool, dry_run: bool, stats: dict):
    dest, capture_type, _new_name = plan_destination(
        work, f, force_type=None, events=events, cli_source=cli_source,
        screenshot_keywords=screenshot_keywords,
        recording_keywords=recording_keywords,
    )
    # no_gps ignored in v7; kept in signature for call-site compatibility
    del no_gps

    if dry_run:
        print(f"  [dry-run] {f.relative_to(work)} -> {dest.relative_to(work)}")
    else:
        _ensure_dest_under_work(work, dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(dest))
        stats['moved'] += 1

    if capture_type == 'recording':
        stats['recordings'] += 1
    elif capture_type == 'screenshot':
        stats['screenshots'] += 1
    elif is_video(f):
        stats['videos'] += 1
    else:
        stats['photos'] += 1


def process_live_pair(work: Path, still: Path, mov: Path, events: list,
                      cli_source: Optional[str], dry_run: bool, stats: dict):
    """Move/rename a Live Photo pair into by-date/.../photos/ with shared stem."""
    still_dest, mov_dest, _stem = plan_live_pair(
        work, still, mov, events=events, cli_source=cli_source,
    )
    cross = still.parent.resolve() != mov.parent.resolve()
    tag = '[live-pair-cross]' if cross else '[live-pair]'
    if dry_run:
        print(
            f"  [dry-run] {tag} {still.relative_to(work)} -> "
            f"{still_dest.relative_to(work)}"
        )
        print(
            f"  [dry-run] {tag} {mov.relative_to(work)} -> "
            f"{mov_dest.relative_to(work)}"
        )
    else:
        _ensure_dest_under_work(work, still_dest)
        _ensure_dest_under_work(work, mov_dest)
        still_orig = still.resolve()
        still_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(still), str(still_dest))
        try:
            shutil.move(str(mov), str(mov_dest))
        except Exception:
            # Best-effort rollback so we never leave a half pair at dest.
            try:
                if still_dest.is_file() and not still_orig.exists():
                    shutil.move(str(still_dest), str(still_orig))
            except Exception as rb:
                print(
                    f"  [live-pair] rollback failed: {rb}",
                    file=sys.stderr,
                    flush=True,
                )
            raise
        stats['moved'] += 2

    stats['photos'] += 1  # still counts as photo
    stats['live_pairs'] += 1


# === Rebucket default by-date months → theme buckets (no inbox) ===

ARCHIVED_NAME_RE = re.compile(
    r'^(?P<date>\d{8})_(?P<time>\d{6})'
    r'(?:_(?P<source>[a-zA-Z0-9-]+))?'
    r'(?:_live)?'
    r'_(?P<hash>[0-9a-fA-F]{4,})'
    r'(?P<ext>\.[^.]+)$',
    re.IGNORECASE,
)


def parse_archived_name(name: str) -> tuple:
    """Parse archived filename → (date_yyyymmdd, source_or_None) or (None, None)."""
    m = ARCHIVED_NAME_RE.match(name)
    if not m:
        return None, None
    source = m.group('source')
    if source and source.lower() == 'live':
        # Rare: ..._live_<hash> with no camera source
        source = None
    return m.group('date'), source


def _load_stars_json(work: Path, bucket: str) -> dict:
    path = work / '_meta' / 'stars' / f'{bucket}.json'
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, list):
            return {k: True for k in data}
        return {k: bool(v) for k, v in data.items() if v}
    except Exception:
        return {}


def _save_stars_json(work: Path, bucket: str, stars: dict) -> None:
    path = work / '_meta' / 'stars' / f'{bucket}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({k: True for k in stars}, indent=2, ensure_ascii=False) + '\n',
        encoding='utf-8',
    )


def migrate_star_path(work: Path, old_rel: str, new_rel: str) -> None:
    """Move a star entry from old_rel's bucket to new_rel's bucket if starred."""
    old_bucket = star_bucket_for_rel(old_rel)
    new_bucket = star_bucket_for_rel(new_rel)
    stars = _load_stars_json(work, old_bucket)
    if old_rel not in stars:
        stars_dir = work / '_meta' / 'stars'
        if not stars_dir.is_dir():
            return
        found = False
        for f in stars_dir.glob('*.json'):
            bucket = f.stem
            s = _load_stars_json(work, bucket)
            if old_rel in s:
                s.pop(old_rel, None)
                _save_stars_json(work, bucket, s)
                ns = _load_stars_json(work, new_bucket)
                ns[new_rel] = True
                _save_stars_json(work, new_bucket, ns)
                found = True
                break
        if not found:
            return
        return
    stars.pop(old_rel, None)
    _save_stars_json(work, old_bucket, stars)
    ns = _load_stars_json(work, new_bucket)
    ns[new_rel] = True
    _save_stars_json(work, new_bucket, ns)


def _default_month_dirs(work: Path, month_str: str) -> list:
    """Return existing by-date/<year>/<YYYY-MM>/{photos,videos} dirs (default bucket only)."""
    if not re.match(r'^\d{4}-\d{2}$', month_str):
        return []
    year = month_str[:4]
    base = work / 'by-date' / year / month_str
    out = []
    for sub in ('photos', 'videos'):
        d = base / sub
        if d.is_dir():
            out.append(d)
    return out


_THEME_BUCKET_RE = re.compile(r'^(\d{4}-\d{2})_(.+)$')


def iter_theme_bucket_dirs(work: Path):
    """Yield (bucket_dir, month_str, theme_name) for by-date theme side buckets."""
    by_date = work / 'by-date'
    if not by_date.is_dir():
        return
    try:
        year_dirs = sorted(by_date.iterdir())
    except OSError:
        return
    for year_dir in year_dirs:
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        try:
            buckets = sorted(year_dir.iterdir())
        except OSError:
            continue
        for bucket in buckets:
            if not bucket.is_dir():
                continue
            m = _THEME_BUCKET_RE.match(bucket.name)
            if not m:
                continue
            yield bucket, m.group(1), m.group(2)


def scan_by_date_theme_buckets(work: Path,
                               theme_names: Optional[list] = None) -> list[Path]:
    """List files in by-date theme side buckets YYYY-MM_<name>/{photos,videos}/.

    If theme_names is set, only buckets whose theme suffix is in that set
    (scoped sync — does not touch other theme buckets or unrelated orphans).
    """
    files = []
    want = None
    if theme_names is not None:
        want = {str(n).strip() for n in theme_names if str(n).strip()}
    for bucket, _month, name in iter_theme_bucket_dirs(work):
        if want is not None and name not in want:
            continue
        for sub in ('photos', 'videos'):
            d = bucket / sub
            if not d.is_dir():
                continue
            try:
                for f in d.iterdir():
                    if not f.is_file():
                        continue
                    if f.name.startswith('.'):
                        continue
                    files.append(f)
            except OSError as e:
                print(f'  [warn] cannot list {d}: {e}', file=sys.stderr)
    return files


def _ensure_dest_under_work(work: Path, dest: Path) -> Path:
    """Resolve dest and refuse paths outside work (path-traversal guard)."""
    work_res = work.resolve()
    dest_res = dest.resolve()
    if dest_res != work_res and not str(dest_res).startswith(str(work_res) + os.sep):
        raise ValueError(f'destination escapes work: {dest}')
    return dest_res


def _empty_reconcile_stats() -> dict:
    return {
        'scanned': 0,
        'moved': 0,
        'into_theme': 0,
        'to_default': 0,
        'reassign': 0,
        'skipped': 0,
        'parse_fail': 0,
        'errors': 0,
        'by_theme': {},
    }


def _resolve_live_lead(f: Path) -> tuple:
    """Return (lead, follower, date, source) or (None, None, None, None) on parse fail."""
    date, source = parse_archived_name(f.name)
    if not date:
        return None, None, None, None
    companion = live_companion_of(f)
    lead = f
    follower = companion
    if companion is not None:
        if f.suffix.lower() == LIVE_MOTION_EXT and companion.suffix.lower() in LIVE_STILL_EXTS:
            lead, follower = companion, f
            date, source = parse_archived_name(lead.name)
            if not date:
                return None, None, None, None
    return lead, follower, date, source


def _dest_dir_for_date(work: Path, date: str, theme: Optional[dict], sub: str) -> Path:
    year, month_dir = _theme_bucket_parts(date, theme)
    if sub not in ('photos', 'videos'):
        sub = 'videos' if sub == 'videos' else 'photos'
    return work / 'by-date' / year / month_dir / sub


def _move_archived_group(work: Path, work_res: Path, lead: Path, follower: Optional[Path],
                         dest_dir: Path, dry_run: bool, verbose: bool,
                         handled: set, stats: dict, kind: str,
                         theme_key: Optional[str] = None) -> bool:
    """Move lead[+follower] into dest_dir. kind is into_theme|to_default|reassign.

    Returns True on success (including dry-run / already-there), False on error.
    """
    srcs = [lead] if follower is None else [lead, follower]
    if len(srcs) == 2:
        d0 = dest_dir / srcs[0].name
        d1 = dest_dir / srcs[1].name
        occupied = False
        for cand, src in ((d0, srcs[0]), (d1, srcs[1])):
            if not cand.exists():
                continue
            try:
                if cand.resolve() != src.resolve():
                    occupied = True
                    break
            except OSError:
                occupied = True
                break
        if occupied:
            dests = list(unique_pair_dests(
                dest_dir, srcs[0].stem,
                srcs[0].suffix.lower(), srcs[1].suffix.lower(),
            ))
        else:
            dests = [d0, d1]
    else:
        cand = dest_dir / srcs[0].name
        try:
            same = cand.exists() and cand.resolve() == srcs[0].resolve()
        except OSError:
            same = False
        dests = [cand if same or not cand.exists() else get_unique_dest(cand)]

    moves = []
    for src, dest in zip(srcs, dests):
        try:
            old_rel = str(src.resolve().relative_to(work_res))
        except ValueError:
            old_rel = str(src.relative_to(work))
        moves.append((src, dest, old_rel))

    # Already at destination
    try:
        if all(src.resolve() == dest.resolve() for src, dest, _ in moves):
            for src, _dest, _old in moves:
                handled.add(src.resolve())
            return True
    except OSError:
        pass

    def _count_one() -> None:
        stats['moved'] += 1
        stats[kind] = stats.get(kind, 0) + 1
        if theme_key:
            stats['by_theme'].setdefault(theme_key, 0)
            stats['by_theme'][theme_key] += 1

    if dry_run:
        for src, dest, _old in moves:
            _ensure_dest_under_work(work, dest)
            if verbose:
                print(f'  [dry-run] [{kind}] {src.relative_to(work)} -> {dest.relative_to(work)}')
            handled.add(src.resolve())
            _count_one()
        return True

    dest_dir_res = _ensure_dest_under_work(work, dest_dir)
    for _src, dest, _old in moves:
        _ensure_dest_under_work(work, dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    del dest_dir_res
    done = []
    try:
        for src, dest, old_rel in moves:
            if src.resolve() == dest.resolve():
                handled.add(src.resolve())
                continue
            shutil.move(str(src), str(dest))
            done.append((src, dest, old_rel))
            handled.add(src.resolve())
            try:
                new_rel = str(dest.resolve().relative_to(work_res))
            except ValueError:
                new_rel = str(dest.relative_to(work))
            migrate_star_path(work, old_rel, new_rel)
            if verbose:
                print(f'  [{kind}] {src.relative_to(work)} -> {dest.relative_to(work)}')
            _count_one()
        return True
    except Exception as e:
        for src, dest, _old in reversed(done):
            try:
                if dest.is_file() and not Path(src).exists():
                    shutil.move(str(dest), str(src))
            except Exception as rb:
                print(f'  [reconcile] rollback failed: {rb}', file=sys.stderr)
        print(f'  [error] {lead.relative_to(work)}: {e}', file=sys.stderr)
        stats['errors'] += 1
        return False


def rebucket_themes(work: Path, events: list, dry_run: bool = True,
                    verbose: bool = False, themes: Optional[list] = None) -> dict:
    """Bidirectional theme sync (alias of reconcile_themes)."""
    return reconcile_themes(
        work, events, dry_run=dry_run, verbose=verbose, themes=themes,
    )


def reconcile_themes(work: Path, events: list, dry_run: bool = True,
                     verbose: bool = False, themes: Optional[list] = None) -> dict:
    """Two-way theme sync on by-date (does NOT scan inbox).

    Phase A — into_theme: default YYYY-MM → matching theme bucket.
    Phase B — to_default / reassign: theme side buckets rematched against
    current events.yaml (orphan / renamed / shrunk ranges demote or move).

    themes: if set, only sync those theme names (default-month months for those
    themes + their YYYY-MM_<name> buckets). Other theme buckets are untouched.
    themes=None means full sync (all themes + all theme buckets including orphans).
    """
    stats = _empty_reconcile_stats()
    events = events or []
    work_res = work.resolve()
    handled: set = set()
    scope = None
    if themes is not None:
        scope = {str(n).strip() for n in themes if str(n).strip()}
        if not scope:
            return stats

    # --- Phase A: default month → theme ---
    if events:
        for f in scan_by_date_default_months(work, events, theme_names=themes):
            try:
                fres = f.resolve()
            except OSError:
                stats['errors'] += 1
                continue
            if fres in handled:
                continue

            stats['scanned'] += 1
            lead, follower, date, source = _resolve_live_lead(f)
            if lead is None:
                stats['parse_fail'] += 1
                stats['skipped'] += 1
                continue

            theme = match_theme(lead, date, source, events)
            if not theme:
                stats['skipped'] += 1
                continue
            theme_name = (theme.get('name') or '').strip()
            if not theme_name:
                stats['skipped'] += 1
                continue
            if scope is not None and theme_name not in scope:
                # Matched another theme — leave for that theme's sync
                stats['skipped'] += 1
                continue

            sub = lead.parent.name
            if sub not in ('photos', 'videos'):
                sub = 'videos' if is_video(lead) else 'photos'
            dest_dir = _dest_dir_for_date(work, date, theme, sub)
            _move_archived_group(
                work, work_res, lead, follower, dest_dir,
                dry_run, verbose, handled, stats, 'into_theme',
                theme_key=theme_name,
            )

    # --- Phase B: theme buckets → stay / reassign / default ---
    for f in scan_by_date_theme_buckets(work, theme_names=themes):
        try:
            fres = f.resolve()
        except OSError:
            stats['errors'] += 1
            continue
        if fres in handled:
            continue

        stats['scanned'] += 1
        lead, follower, date, source = _resolve_live_lead(f)
        if lead is None:
            stats['parse_fail'] += 1
            stats['skipped'] += 1
            continue

        theme = match_theme(lead, date, source, events) if events else None
        matched_name = (theme.get('name') or '').strip() if theme else ''

        sub = lead.parent.name
        if sub not in ('photos', 'videos'):
            sub = 'videos' if is_video(lead) else 'photos'

        if matched_name:
            # Full bucket identity: YYYY-MM_name (dest_dir), not name suffix alone.
            # Same theme name but wrong start-month bucket → reassign.
            dest_dir = _dest_dir_for_date(work, date, theme, sub)
            try:
                already = lead.parent.resolve() == dest_dir.resolve()
            except OSError:
                already = False
            if already:
                stats['skipped'] += 1
                handled.add(lead.resolve())
                if follower is not None:
                    try:
                        handled.add(follower.resolve())
                    except OSError:
                        pass
                continue
            kind = 'reassign'
            theme_key = matched_name
        else:
            dest_dir = _dest_dir_for_date(work, date, None, sub)
            kind = 'to_default'
            theme_key = None

        _move_archived_group(
            work, work_res, lead, follower, dest_dir,
            dry_run, verbose, handled, stats, kind,
            theme_key=theme_key,
        )

    return stats


def _print_rebucket_summary(stats: dict, dry_run: bool) -> None:
    prefix = '[dry-run] Would sync' if dry_run else '✓ Synced'
    moved = stats.get('moved', 0)
    print(
        f"{prefix} {moved} by-date file(s) "
        f"(into_theme={stats.get('into_theme', 0)}, "
        f"to_default={stats.get('to_default', 0)}, "
        f"reassign={stats.get('reassign', 0)}; "
        f"scanned {stats.get('scanned', 0)}, skipped {stats.get('skipped', 0)}, "
        f"parse_fail {stats.get('parse_fail', 0)}, errors {stats.get('errors', 0)})"
    )
    for name, n in sorted((stats.get('by_theme') or {}).items()):
        print(f'  → {name}: {n}')


def main():
    parser = argparse.ArgumentParser(
        description='Rename + organize inbox into by-date / screenshots / screenrecords. '
                    'Theme sync is separate: --rebucket-themes --theme NAME or --all')
    parser.add_argument('--work', default='/Volumes/Storage',
                        help='Working disk root (default: /Volumes/Storage)')
    parser.add_argument('--apply-events', default=None,
                        help='Path to events.yaml (default: <work>/_meta/events.yaml)')
    parser.add_argument('--source', default=None,
                        help='Default source for files without EXIF (e.g., iphone, canon)')
    parser.add_argument('--no-gps-rule', dest='no_gps_rule', action='store_true',
                        default=False,
                        help='Deprecated (v7): aggressive GPS/Make rules are always on')
    parser.add_argument('--rebucket-themes', action='store_true',
                        help='Only sync by-date themes both ways (skip inbox). '
                             'Requires --theme NAME and/or --all')
    parser.add_argument('--theme', action='append', default=[],
                        help='Theme name to sync (repeatable). Used with --rebucket-themes')
    parser.add_argument('--all', dest='all_themes', action='store_true',
                        help='With --rebucket-themes: sync all themes (may overwrite '
                             'manual moves in every theme bucket)')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    try:
        work = validate_path(args.work, ALLOWED_WORK_PREFIXES, 'work')
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    events_path = Path(args.apply_events) if args.apply_events else None
    try:
        events = load_events(work, events_path)
    except ValueError as e:
        print(f"ERROR: events.yaml: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"→ Loaded {len(events)} themes from events.yaml")

    if args.rebucket_themes:
        theme_names = [t.strip() for t in (args.theme or []) if str(t).strip()]
        if not theme_names and not args.all_themes:
            print(
                'ERROR: --rebucket-themes requires --theme NAME and/or --all\n'
                '  Example (one theme):  --rebucket-themes --theme 香港-深圳\n'
                '  Example (all themes): --rebucket-themes --all\n'
                '  Full sync can overwrite manual moves in every theme bucket.',
                file=sys.stderr,
            )
            sys.exit(2)
        mode = 'dry-run' if args.dry_run else 'apply'
        if args.all_themes and not theme_names:
            scope_label = 'all themes'
            scope_arg = None
        elif args.all_themes and theme_names:
            # --all wins as full sync; ignore redundant --theme
            scope_label = 'all themes'
            scope_arg = None
        else:
            scope_label = 'themes: ' + ', '.join(theme_names)
            scope_arg = theme_names
        print(f'→ Theme sync ({mode}): {scope_label}')
        stats = reconcile_themes(
            work, events, dry_run=args.dry_run, verbose=True, themes=scope_arg,
        )
        _print_rebucket_summary(stats, args.dry_run)
        return

    files = scan_inbox(work)
    print(f"→ Scanning inbox/ (theme sync is separate: --rebucket-themes --theme …)")
    print(f"→ inbox: {len(files)} file(s)")

    if not files:
        print('→ inbox empty — nothing to do')
        print('→ Tip: sync one theme with --rebucket-themes --theme <name> '
              '(or --all for every theme)')
        return

    # Ensure destination roots exist on apply
    if not args.dry_run:
        (work / 'screenshots').mkdir(parents=True, exist_ok=True)
        (work / 'screenrecords').mkdir(parents=True, exist_ok=True)
        (work / 'docs').mkdir(parents=True, exist_ok=True)
        (work / 'things').mkdir(parents=True, exist_ok=True)
        (work / 'by-date').mkdir(parents=True, exist_ok=True)

    live_pairs = find_live_photo_pairs(files)
    paired_paths = set(live_pairs.keys()) | set(live_pairs.values())
    if live_pairs:
        print(f"→ Detected {len(live_pairs)} Live Photo pair(s)")

    stats = {
        'moved': 0, 'screenshots': 0, 'recordings': 0,
        'photos': 0, 'videos': 0, 'live_pairs': 0,
    }
    total = len(files)
    start_time = time.time()
    last_report = start_time

    for i, f in enumerate(files, 1):
        try:
            if f in live_pairs:
                process_live_pair(
                    work, f, live_pairs[f], events, args.source,
                    args.dry_run, stats,
                )
            elif f in paired_paths:
                # Companion .mov handled with its still
                pass
            else:
                process_file(
                    work, f, events, args.source,
                    DEFAULT_SCREENSHOT_KEYWORDS, DEFAULT_RECORDING_KEYWORDS,
                    args.no_gps_rule,
                    args.dry_run, stats
                )
        except Exception as e:
            print(f"  [error] {f.relative_to(work)}: {e}", file=sys.stderr)

        # Progress report every 25 files or every 10 seconds
        now = time.time()
        if i % 25 == 0 or (now - last_report) > 10:
            elapsed = now - start_time
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate if rate > 0 else 0
            pct = 100 * i / total
            print(
                f"  [{i}/{total} {pct:5.1f}%] {elapsed:6.1f}s elapsed, "
                f"~{eta:5.0f}s remaining, {rate:5.1f} files/s    ",
                end='\r', file=sys.stderr, flush=True
            )
            last_report = now
    # Final newline
    print(file=sys.stderr)

    prefix = '[dry-run] Would' if args.dry_run else '✓ Did'
    print(f"\n{prefix} process {len(files)} inbox files:")
    print(f"  screenshots: {stats['screenshots']}")
    print(f"  recordings:  {stats['recordings']}")
    print(f"  photos:      {stats['photos']}")
    print(f"  videos:      {stats['videos']}")
    print(f"  live_pairs:  {stats['live_pairs']}")
    print('→ Tip: sync one theme with --rebucket-themes --theme <name> '
          '(or --all for every theme)')


if __name__ == '__main__':
    main()
