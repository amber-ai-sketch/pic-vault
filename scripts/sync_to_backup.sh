#!/bin/bash
# sync_to_backup.sh - rsync working disk to backup disk.
#
# Default mode: append-only mirror (no --delete on 4T).
# Use --verify for size/mtime verification (no full checksum).
# Use --prune --confirm to remove orphans from backup (DANGEROUS).
#
# Usage:
#   ./sync_to_backup.sh
#   ./sync_to_backup.sh --dry-run
#   ./sync_to_backup.sh --verify
#   ./sync_to_backup.sh --prune --confirm

set -euo pipefail

WORK="${WORK:-/Volumes/Storage}"
BACKUP="${BACKUP:-/Volumes/WD4T/MediaVault}"
DRY_RUN=""
VERIFY=""
PRUNE=""
CONFIRM=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --work) WORK="$2"; shift 2 ;;
        --backup) BACKUP="$2"; shift 2 ;;
        --dry-run) DRY_RUN="-n"; shift ;;
        --verify) VERIFY="verify"; shift ;;
        --prune) PRUNE="prune"; shift ;;
        --confirm) CONFIRM="yes"; shift ;;
        -h|--help)
            cat << 'USAGE'
Usage: sync_to_backup.sh [--work PATH] [--backup PATH] [--dry-run] [--verify] [--prune --confirm]

Modes:
  (default)     Append-only mirror: only adds new files to backup, never deletes.
  --verify      After a real mirror, verify with size/mtime (skipped under --dry-run).
  --prune       DANGEROUS: also delete files from backup that are not on work disk.
                Requires --confirm to actually run.

Examples:
  sync_to_backup.sh                    # mirror work -> backup
  sync_to_backup.sh --dry-run          # preview what would be mirrored
  sync_to_backup.sh --verify           # mirror + verify
  sync_to_backup.sh --verify --dry-run # preview only; verify is skipped
  sync_to_backup.sh --prune --confirm  # mirror + delete orphans on backup
USAGE
            exit 0 ;;
        *)
            echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

# Canonicalize (resolve .. and symlinks) before whitelist checks.
# Align work roots with Python validators (dedupe/rename ALLOWED_WORK_PREFIXES).
_canonicalize() {
    python3 -c 'import os,sys; print(os.path.realpath(os.path.abspath(sys.argv[1])))' "$1"
}

_under_prefix() {
    local path="$1"
    local prefix="$2"
    [[ "$path" == "$prefix" || "$path" == "$prefix"/* ]]
}

if [ "${DUPEGURU_TEST:-}" = "1" ]; then
    : # skip sandbox in test mode
else
    WORK="$(_canonicalize "$WORK")"
    BACKUP="$(_canonicalize "$BACKUP")"

    WORK_OK=0
    for prefix in /Volumes/Storage /Volumes/YM/MediaVault; do
        prefix_r="$(_canonicalize "$prefix" 2>/dev/null || echo "$prefix")"
        if _under_prefix "$WORK" "$prefix_r"; then
            WORK_OK=1
            break
        fi
    done
    if [[ "$WORK_OK" -ne 1 ]]; then
        echo "ERROR: --work $WORK is not in path whitelist" >&2
        echo "  Allowed: /Volumes/Storage, /Volumes/YM/MediaVault" >&2
        exit 1
    fi

    BACKUP_OK=0
    for prefix in /Volumes/WD4T/MediaVault /Volumes/YM/MediaVault; do
        prefix_r="$(_canonicalize "$prefix" 2>/dev/null || echo "$prefix")"
        if _under_prefix "$BACKUP" "$prefix_r"; then
            BACKUP_OK=1
            break
        fi
    done
    if [[ "$BACKUP_OK" -ne 1 ]]; then
        echo "ERROR: --backup $BACKUP is not in path whitelist" >&2
        echo "  Allowed: /Volumes/WD4T/MediaVault, /Volumes/YM/MediaVault" >&2
        exit 1
    fi
fi

# Sanity checks
[[ -d "$WORK" ]] || { echo "ERROR: $WORK not found" >&2; exit 1; }
[[ -d "$BACKUP" ]] || { echo "ERROR: $BACKUP not found" >&2; exit 1; }

# Build rsync args
# Include only specific subdirs to avoid touching backup root's other content
RSYNC_ARGS=(
    -avh $DRY_RUN
    --include='by-date/***'
    --include='screenshots/***'
    --include='screenrecords/***'
    --include='docs/***'
    --include='things/***'
    --include='_favorite/***'
    --include='_vlogs/***'
    --exclude='*'
)

# Add --delete only if pruning
if [[ "$PRUNE" == "prune" ]]; then
    if [[ "$CONFIRM" != "yes" ]]; then
        echo "ERROR: --prune requires --confirm" >&2
        exit 1
    fi
    echo "⚠️  PRUNE MODE: will delete files on backup not present on work disk"
    echo "    BACKUP: $BACKUP"
    echo ""
    echo "Type YES to continue:"
    read -r reply
    if [[ "$reply" != "YES" ]]; then
        echo "Aborted"
        exit 1
    fi
    RSYNC_ARGS+=(--delete)
fi

# Mirror
echo "→ Mirroring $WORK -> $BACKUP"
echo "  Including: by-date/, screenshots/, screenrecords/, docs/, things/, _favorite/, _vlogs/"
rsync "${RSYNC_ARGS[@]}" "$WORK/" "$BACKUP/" || {
    echo "ERROR: rsync failed" >&2
    exit 1
}

# Optional verify (skip when --dry-run: nothing was written, size/mtime compare
# against an unchanged backup is meaningless)
if [[ "$VERIFY" == "verify" ]]; then
    if [[ -n "$DRY_RUN" ]]; then
        echo ""
        echo "→ Skipping verify under --dry-run (no files written; size/mtime compare is meaningless)."
        echo "  Dry-run preview above is the source of truth. Use Apply (without --dry-run) for real sync + verify."
    else
        echo ""
        echo "→ Verifying (size/mtime compare, no full checksum)..."
        # -n dry compare; no -c (checksum) — full checksum on multi-10GB libs is too slow.
        # Fail only on real file-level itemize lines, not rsync chatter / dir headers.
        VERIFY_LOG="${TMPDIR:-/tmp}/picvault-sync-verify-$$.log"
        rsync -avhn --itemize-changes \
              --include='by-date/***' --include='screenshots/***' \
              --include='screenrecords/***' --include='docs/***' \
              --include='things/***' \
              --include='_favorite/***' --include='_vlogs/***' --exclude='*' \
              "$WORK/" "$BACKUP/" > "$VERIFY_LOG" 2>&1 || true
        # Itemize 2nd char: f=file d=directory L=symlink D=device S=special.
        # Must include lowercase d so missing dirs (e.g. cd+++++++++) fail verify.
        if grep -E '^[<>ch.*][fdLDS]' "$VERIFY_LOG" >/dev/null 2>&1 || \
           grep -E '^\*deleting' "$VERIFY_LOG" >/dev/null 2>&1; then
            echo "⚠️  Differences detected:"
            grep -E '^[<>ch.*][fdLDS]|^\*deleting' "$VERIFY_LOG" | head -30
            echo "Full log: $VERIFY_LOG"
            exit 1
        else
            echo "✓ Verification passed: size/mtime match (no pending file/directory differences)"
            rm -f "$VERIFY_LOG"
        fi
    fi
fi

echo "✓ Done"
