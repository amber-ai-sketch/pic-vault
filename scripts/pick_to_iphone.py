#!/usr/bin/env python3
"""
pick_to_iphone.py - Build _favorite/<bucket>/ from stars/<bucket>.json + generate AppleScript.

Usage:
    ./pick_to_iphone.py --work /Volumes/Storage --bucket 2026-07_海南
    ./pick_to_iphone.py --work /Volumes/Storage --bucket 2026-08
    ./pick_to_iphone.py --work /Volumes/Storage --bucket screenshots
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ALLOWED_WORK_PREFIXES = ('/Volumes/Storage',)


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


def load_stars(work: Path, bucket: str) -> list[str]:
    """Load starred file paths from _meta/stars/<bucket>.json."""
    path = work / '_meta' / 'stars' / f"{bucket}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [k for k, v in data.items() if v]
        return []
    except Exception as e:
        print(f"[warn] could not load {path}: {e}", file=sys.stderr)
        return []


def bucket_is_themed(bucket: str) -> bool:
    """Has '_' separator → themed bucket (e.g., 2026-07_海南)."""
    return '_' in bucket


def resolve_star_path(work: Path, star_entry: str) -> Path:
    """Resolve a star entry to a real file path under work."""
    p = Path(star_entry)
    if p.is_absolute():
        return p
    return work / p


def generate_applescript(bucket: str, is_themed: bool) -> str:
    """Generate AppleScript that imports into Photos and marks favorites."""
    folder_path = f"/Volumes/Storage/_favorite/{bucket}" if is_themed else "/Volumes/Storage/_favorite"
    if is_themed:
        album_name = bucket
    else:
        # Non-themed: album name = "Picks" for months, "Screenshots" for screenshots
        if bucket == 'screenshots':
            album_name = "Screenshots"
        else:
            album_name = "Picks"

    # Escape bucket for AppleScript string
    bucket_escaped = bucket.replace('"', '\\"')
    album_escaped = album_name.replace('"', '\\"')
    folder_escaped = folder_path.replace('"', '\\"')

    return f"""on run
    set bucketName to "{bucket_escaped}"
    set folderPath to "{folder_escaped}"
    set albumName to "{album_escaped}"

    tell application "Photos"
        activate
        set importFolder to (POSIX file folderPath) as alias

        if not (exists album albumName) then
            make new album named albumName
        end if

        import {{importFolder}} into (album albumName) skip checking duplicates yes
        delay 5

        set theItems to media items of album albumName
        repeat with anItem in theItems
            set favorite of anItem to true
        end repeat
    end tell
end run
"""


def main():
    parser = argparse.ArgumentParser(description='Build _favorite/ from stars + generate AppleScript')
    parser.add_argument('--work', default='/Volumes/Storage')
    parser.add_argument('--bucket', required=True,
                        help='Bucket name (e.g., 2026-07_海南, 2026-08, screenshots)')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    try:
        work = validate_path(args.work, ALLOWED_WORK_PREFIXES, 'work')
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    is_themed = bucket_is_themed(args.bucket)
    stars = load_stars(work, args.bucket)

    if not stars:
        print(f"No stars found in {work}/_meta/stars/{args.bucket}.json")
        print("Star files first via web UI, then re-run.")
        sys.exit(1)

    print(f"→ Read {work}/_meta/stars/{args.bucket}.json: {len(stars)} files")

    # Determine target
    if is_themed:
        target_dir = work / '_favorite' / args.bucket
    else:
        target_dir = work / '_favorite'  # flat root
    target_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    missing = []
    for star in stars:
        src = resolve_star_path(work, star)
        if not src.exists():
            missing.append(star)
            continue
        dest = target_dir / src.name
        if dest.exists():
            # Same name; check content hash
            if src.read_bytes() == dest.read_bytes():
                continue  # already copied
            # Different content; append hash to name
            import hashlib
            h = hashlib.sha256(src.read_bytes()).hexdigest()[:4]
            stem = src.stem
            ext = src.suffix
            dest = target_dir / f"{stem}_{h}{ext}"

        if args.dry_run:
            print(f"  [dry-run] {src.name} -> {target_dir.relative_to(work)}")
        else:
            shutil.copy2(src, dest)
            copied += 1

    # Generate AppleScript
    scpt_dir = work / '_meta' / 'scripts'
    scpt_dir.mkdir(parents=True, exist_ok=True)
    scpt_path = scpt_dir / f"favorite-{args.bucket}.scpt"

    if args.dry_run:
        print(f"  [dry-run] would write {scpt_path}")
    else:
        scpt_path.write_text(generate_applescript(args.bucket, is_themed))

    prefix = '[dry-run] Would' if args.dry_run else '✓ Did'
    print(f"\n{prefix} copy {copied} files to {target_dir}")
    if missing:
        print(f"⚠️  {len(missing)} starred files not found (skipped):")
        for m in missing[:5]:
            print(f"    {m}")
        if len(missing) > 5:
            print(f"    ... and {len(missing) - 5} more")
    print(f"\nNext step:")
    print(f"  osascript {scpt_path}")


if __name__ == '__main__':
    main()
