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
from datetime import datetime
from pathlib import Path
from typing import Optional

# Sandbox
ALLOWED_WORK_PREFIXES = ('/Volumes/Storage', '/Volumes/YM/MediaVault')

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
    - Replace spaces and dots with dashes
    - Strip trailing junk
    Examples:
      "Insta360"     -> "insta360"
      "AKASO TECH"   -> "akaso-tech"
      "GoPro, Inc."  -> "gopro-inc"
    """
    import re as _re
    s = name.strip().lower()
    s = _re.sub(r'[\s._,]+', '-', s)
    s = s.strip('-')
    return s or "unknown"


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
    if parts[0] in ('screenshots', 'screenrecords', 'docs'):
        return parts[0]
    if parts[0] == 'by-date' and len(parts) >= 3:
        return parts[2]
    return parts[0]


def compute_dest(work: Path, src: Path, capture_type: str,
                 events: list = None, cli_source: Optional[str] = None,
                 exif: dict = None, video_tags: dict = None) -> tuple:
    """Compute (dest_path, new_name, capture_type) for a file.

    Does not move. ``capture_type``: screenshot|recording|docs|normal.
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
    else:
        theme = match_theme(src, date, source, events)
        year = date[:4]
        month = date[:6]
        bucket_type = 'videos' if is_video(src) else 'photos'
        if theme:
            theme_name = theme.get('name', '').strip()
            if theme_name:
                month_dir_name = f"{month[:4]}-{month[4:]}_{theme_name}"
            else:
                month_dir_name = f"{month[:4]}-{month[4:]}"
        else:
            month_dir_name = f"{month[:4]}-{month[4:]}"
        dest_dir = work / 'by-date' / year / month_dir_name / bucket_type
        new_name = f"{date}_{source_part}{h}{ext}"

    dest = get_unique_dest(dest_dir / new_name)
    return dest, new_name, capture_type


def plan_destination(work: Path, path: Path, force_type: Optional[str] = None,
                     events: list = None, cli_source: Optional[str] = None,
                     screenshot_keywords: list = None,
                     recording_keywords: list = None) -> tuple:
    """Plan dest for path. force_type: None | screenshot | recording | docs | normal.

    Returns (dest_path, capture_type, new_name).
    """
    exif = read_exif(path)
    video_tags = read_video_metadata(path) if is_video(path) else {}
    if force_type in ('screenshot', 'recording', 'docs', 'normal'):
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
    """Reclassify files. action: 'to_screen' | 'to_normal' | 'to_docs'.

    to_screen: image → screenshot, video → recording (by suffix).
    to_normal: force normal (by-date naming).
    to_docs: force docs/ (manual document photos).
    """
    if action not in ('to_screen', 'to_normal', 'to_docs'):
        raise ValueError(f'unknown action: {action}')
    events = events if events is not None else load_events(work, None)
    results = []
    work_res = work.resolve()

    for rel in paths:
        rel = str(rel).lstrip('/')
        src = (work / rel).resolve()
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

            if action == 'to_screen':
                force = 'recording' if is_video(src) else 'screenshot'
            elif action == 'to_docs':
                force = 'docs'
            else:
                force = 'normal'

            dest, capture_type, new_name = plan_destination(
                work, src, force_type=force, events=events,
            )
            item['capture_type'] = capture_type
            item['dest'] = str(dest.relative_to(work))
            item['new_name'] = new_name

            if src.resolve() == dest.resolve():
                item['ok'] = True
                item['skipped'] = True
                results.append(item)
                continue

            if dry_run:
                item['ok'] = True
                results.append(item)
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
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
                                cur_dict[k.strip()] = parse_value(v)
                            elif l_stripped.startswith('- ') and cur_dict.get(k.strip()) is None:
                                # List field starting with '- item'
                                pass  # simplified; won't hit in our schema
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


def load_events(work: Path, events_path: Optional[Path]) -> list[dict]:
    path = events_path or (work / '_meta' / 'events.yaml')
    if not path.exists():
        return []
    try:
        text = path.read_text()
        # Try YAML first
        try:
            import yaml  # type: ignore
            data = yaml.safe_load(text) or {}
        except ImportError:
            data = parse_simple_yaml(text)
        themes = data.get('themes', [])
        if not isinstance(themes, list):
            return []
        # Normalize: convert list of dicts
        result = []
        for t in themes:
            if isinstance(t, dict):
                result.append(t)
        return result
    except Exception as e:
        print(f"[warn] could not load events.yaml: {e}", file=sys.stderr)
        return []


def match_theme(file_path: Path, date: str, source: Optional[str], events: list) -> Optional[dict]:
    if not events or not date:
        return None
    m = re.match(r'(\d{4})(\d{2})\d{2}', date)
    if not m:
        return None
    year, month = m.groups()
    month_str = f"{year}-{month}"

    # Try to extract day for date_range matching
    file_date = date[:8]  # YYYYMMDD
    file_date_iso = f"{file_date[:4]}-{file_date[4:6]}-{file_date[6:8]}"

    # Relativize path for files: matching
    try:
        rel_path = str(file_path.relative_to(file_path.parents[len(file_path.parents) - 2]))
    except Exception:
        rel_path = str(file_path)

    for theme in events:
        if theme.get('month') != month_str:
            continue

        # Highest: explicit files
        explicit = theme.get('files') or []
        if explicit is None:
            explicit = []
        if isinstance(explicit, list) and explicit and any(rel_path.endswith(f) or f in rel_path for f in explicit):
            return theme

        # Date range + source (supports both nested and flat formats)
        dr = theme.get('date_range')
        if dr is None:
            # Flat format (parser limitation): start/end at top level
            start = theme.get('start', '')
            end = theme.get('end', '')
        elif isinstance(dr, dict):
            start = dr.get('start', '')
            end = dr.get('end', '')
        else:
            start = end = ''
        if start and end and start <= file_date_iso <= end:
            sources = theme.get('sources') or []
            if isinstance(sources, list) and (not sources or source in sources):
                return theme

        # Only sources (when single theme in month AND no date_range)
        if not start and not end:
            sources = theme.get('sources') or []
            if isinstance(sources, list) and source and source in sources:
                month_themes = [t for t in events if t.get('month') == month_str]
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
            # Skip .DS_Store and other system files
            if f.name.startswith('.') and f.name != '.source':
                continue
            files.append(f)
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


def main():
    parser = argparse.ArgumentParser(
        description='Rename + organize into by-date / screenshots / screenrecords')
    parser.add_argument('--work', default='/Volumes/Storage',
                        help='Working disk root (default: /Volumes/Storage)')
    parser.add_argument('--apply-events', default=None,
                        help='Path to events.yaml (default: <work>/_meta/events.yaml)')
    parser.add_argument('--source', default=None,
                        help='Default source for files without EXIF (e.g., iphone, canon)')
    parser.add_argument('--no-gps-rule', dest='no_gps_rule', action='store_true',
                        default=False,
                        help='Deprecated (v7): aggressive GPS/Make rules are always on')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    try:
        work = validate_path(args.work, ALLOWED_WORK_PREFIXES, 'work')
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    events_path = Path(args.apply_events) if args.apply_events else None
    events = load_events(work, events_path)
    print(f"→ Loaded {len(events)} themes from events.yaml")

    files = scan_inbox(work)
    print(f"→ Found {len(files)} files in {work}/inbox")

    if not files:
        print("✓ Nothing to process")
        return

    # Ensure destination roots exist on apply
    if not args.dry_run:
        (work / 'screenshots').mkdir(parents=True, exist_ok=True)
        (work / 'screenrecords').mkdir(parents=True, exist_ok=True)
        (work / 'docs').mkdir(parents=True, exist_ok=True)
        (work / 'by-date').mkdir(parents=True, exist_ok=True)

    stats = {'moved': 0, 'screenshots': 0, 'recordings': 0, 'photos': 0, 'videos': 0}
    total = len(files)
    start_time = time.time()
    last_report = start_time

    for i, f in enumerate(files, 1):
        try:
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
    print(f"\n{prefix} process {len(files)} files:")
    print(f"  screenshots: {stats['screenshots']}")
    print(f"  recordings:  {stats['recordings']}")
    print(f"  photos:      {stats['photos']}")
    print(f"  videos:      {stats['videos']}")


if __name__ == '__main__':
    main()
