#!/bin/bash
# onboard_migrate.sh - Stage SSD migration: copy _pre_migration_backup to working/inbox.
# User has already placed data at $BACKUP/_pre_migration_backup (manually).
# This script only does stage + init. NO snapshot (user-managed).

set -euo pipefail

WORK="${WORK:-/Volumes/YM/MediaVault/working}"
BACKUP_SRC="${BACKUP:-/Volumes/YM/MediaVault/_pre_migration_backup}"
DRY_RUN=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --work) WORK="$2"; shift 2 ;;
        --backup) BACKUP_SRC="$2"; shift 2 ;;
        --dry-run) DRY_RUN="--dry-run"; shift ;;
        -h|--help)
            cat << 'USAGE'
Usage: onboard_migrate.sh [--work PATH] [--backup PATH] [--dry-run]

Stages data from SSD backup dir to working/inbox.
User is responsible for placing original data at $BACKUP first.
USAGE
            exit 0 ;;
        *)
            echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

# Sandbox
case "$WORK" in
    /Volumes/YM/MediaVault/*) ;;
    *) echo "ERROR: --work $WORK must be under /Volumes/YM/MediaVault" >&2; exit 1 ;;
esac
case "$BACKUP_SRC" in
    /Volumes/YM/MediaVault/_pre_migration_backup|/Volumes/YM/MediaVault/_pre_migration_backup/*) ;;
    *) echo "ERROR: --backup $BACKUP_SRC must be /Volumes/YM/MediaVault/_pre_migration_backup" >&2; exit 1 ;;
esac

# Verify backup exists and is non-empty
if [[ ! -d "$BACKUP_SRC" ]]; then
    echo "ERROR: $BACKUP_SRC does not exist" >&2
    echo "  User must manually place original data at this path first." >&2
    exit 1
fi
if [[ -z "$(ls -A "$BACKUP_SRC" 2>/dev/null)" ]]; then
    echo "ERROR: $BACKUP_SRC is empty" >&2
    exit 1
fi

# Show what we're about to do
echo "→ Source (user-placed): $BACKUP_SRC"
echo "  Contents:"
ls -la "$BACKUP_SRC" | head -20
echo ""
echo "→ Target: $WORK"
echo ""

# Init working skeleton
mkdir -p "$WORK/inbox"
mkdir -p "$WORK/by-date"
mkdir -p "$WORK/screenshots"
mkdir -p "$WORK/_favorite"
mkdir -p "$WORK/_vlogs"
mkdir -p "$WORK/_trash"
mkdir -p "$WORK/_meta"/{stars,edl,checksums,thumbs,scripts,logs}

# Stage: copy backup -> working/inbox
echo "→ Staging $BACKUP_SRC -> $WORK/inbox/"
if [[ "$DRY_RUN" == "--dry-run" ]]; then
    echo "  [dry-run] would copy files"
    du -sh "$BACKUP_SRC" 2>/dev/null | head -1
else
    # Use cp -c (APFS clonefile) if available, fallback to cp -a
    if cp -c -r --version 2>&1 | head -1 | grep -q 'coreutils\|bsd'; then
        cp -c -r "$BACKUP_SRC/." "$WORK/inbox/" 2>/dev/null || cp -a "$BACKUP_SRC/." "$WORK/inbox/"
    else
        cp -a "$BACKUP_SRC/." "$WORK/inbox/"
    fi
    echo "✓ Staged"
fi

echo ""
echo "Next steps:"
echo "  ./dedupe.py --work $WORK"
echo "  ./rename_organize.py --work $WORK"
echo "  ./sync_to_backup.sh --work $WORK --backup /Volumes/WD4T/MediaVault"
