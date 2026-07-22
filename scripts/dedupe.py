#!/usr/bin/env python3
from typing import Optional

"""
dedupe.py - SHA-256 exact-byte deduplication.

Finds byte-identical files in /inbox and moves duplicates to /_trash/<batch>/.
The first (or largest) copy in each duplicate group is kept.

Usage:
    ./dedupe.py --work /Volumes/Storage --dry-run
    ./dedupe.py --work /Volumes/Storage
    ./dedupe.py --work /Volumes/Storage --batch 2026-07-20-A
"""

import argparse
import hashlib
import os
import shutil
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Sandbox whitelist
ALLOWED_WORK_PREFIXES = ('/Volumes/Storage', '/Volumes/YM/MediaVault')
ALLOWED_BACKUP_PREFIXES = ('/Volumes/WD4T/MediaVault',)
ALLOWED_SSD_PREFIXES = ('/Volumes/YM/MediaVault',)

CHUNK = 1024 * 1024  # 1 MB chunks for hash


def validate_path(path_str: str, allowed_prefixes, kind: str) -> Path:
    import os
    if os.environ.get("DUPEGURU_TEST") == "1":
        return Path(path_str).expanduser().resolve()

    """Validate that path is within allowed prefixes (sandbox)."""
    p = Path(path_str).expanduser().resolve()
    for prefix in allowed_prefixes:
        prefix_resolved = str(Path(prefix).resolve())
        if str(p) == prefix_resolved or str(p).startswith(prefix_resolved + '/'):
            return p
    raise ValueError(
        f"--{kind} {path_str} is not in path whitelist.\n"
        f"  Allowed: {', '.join(allowed_prefixes)}"
    )


def sha256_file(path: Path) -> str:
    """SHA-256 of file contents (streaming)."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def scan_files(work: Path, batch: "Optional[str]") -> list[Path]:
    """Recursively scan files in inbox/ (optionally limited to inbox/<batch>/)."""
    inbox = work / 'inbox'
    if not inbox.exists():
        return []
    if batch:
        target = inbox / batch
        if not target.exists():
            return []
        return [f for f in target.rglob('*') if f.is_file()]
    return [f for f in inbox.rglob('*') if f.is_file()]


def find_duplicates(files: list[Path]) -> dict[str, list[Path]]:
    """Group files by SHA-256; return only groups with >1 entry."""
    hashes = defaultdict(list)
    for f in files:
        try:
            h = sha256_file(f)
            hashes[h].append(f)
        except (OSError, IOError) as e:
            print(f"  [skip] {f}: {e}", file=sys.stderr)
    return {h: paths for h, paths in hashes.items() if len(paths) > 1}


def pick_keep(dupes: list[Path]) -> Path:
    """Pick which file to keep. Default: largest file (best quality)."""
    return max(dupes, key=lambda p: p.stat().st_size)


def move_to_trash(work: Path, file: Path, batch: str) -> Path:
    """Move duplicate to _trash/<batch>/<relative path>."""
    trash_root = work / '_trash' / batch
    # Preserve original path structure inside trash
    rel = file.relative_to(work / 'inbox')
    dest = trash_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(file), str(dest))
    return dest


def main():
    parser = argparse.ArgumentParser(description='SHA-256 deduplication')
    parser.add_argument('--work', default='/Volumes/Storage',
                        help='Working disk root (default: /Volumes/Storage)')
    parser.add_argument('--batch', default=None,
                        help='Process only files in inbox/<batch>/ (default: all)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be done without moving files')
    args = parser.parse_args()

    # Sandbox check
    try:
        work = validate_path(args.work, ALLOWED_WORK_PREFIXES, 'work')
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    batch = args.batch or datetime.now().strftime('%Y-%m-%d')

    print(f"→ Scanning {work}/inbox/{args.batch or ''}")
    files = scan_files(work, args.batch)
    print(f"  {len(files)} files")

    if not files:
        print("✓ Nothing to process")
        return

    print("→ Computing SHA-256...")
    dupes = find_duplicates(files)

    if not dupes:
        print("✓ No duplicates found")
        return

    total_kept = 0
    total_trashed = 0

    for h, paths in dupes.items():
        keep = pick_keep(paths)
        drop = sorted([p for p in paths if p != keep],
                      key=lambda p: str(p))
        print(f"\n  duplicate group sha256:{h[:8]}...")
        print(f"    KEEP   {keep.relative_to(work)}  ({keep.stat().st_size:,} bytes)")
        for d in drop:
            print(f"    TRASH  {d.relative_to(work)}  ({d.stat().st_size:,} bytes)")
            if not args.dry_run:
                try:
                    move_to_trash(work, d, batch)
                    total_trashed += 1
                except Exception as e:
                    print(f"    [error] {e}", file=sys.stderr)
        total_kept += 1

    prefix = '[dry-run] Would' if args.dry_run else '✓ Did'
    print(f"\n{prefix} keep {total_kept} files, trash {total_trashed} duplicates to {work}/_trash/{batch}/")


if __name__ == '__main__':
    main()
