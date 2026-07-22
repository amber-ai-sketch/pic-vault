#!/bin/bash
# init_storage.sh - Create top-level directory skeleton on the working disk
# Usage: ./scripts/init_storage.sh [--work /Volumes/Storage] [--with-example-themes]
#
# Idempotent: re-running is safe (mkdir -p is a no-op if exists).
# v2: default does NOT seed example themes (avoids accidental misclassification).
#     Use --with-example-themes to opt-in.

set -euo pipefail

# Defaults
WORK="${WORK:-/Volumes/Storage}"
WITH_EXAMPLE_THEMES=0

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --work)
            WORK="$2"
            shift 2
            ;;
        --with-example-themes)
            WITH_EXAMPLE_THEMES=1
            shift
            ;;
        -h|--help)
            cat << 'USAGE'
Usage: init_storage.sh [--work PATH] [--with-example-themes]

By default, creates an empty events.yaml. Pass --with-example-themes
to seed events.yaml from outputs/events.example.yaml.

USAGE
            exit 0
            ;;
        *)
            echo "Unknown arg: $1" >&2
            exit 1
            ;;
    esac
done

# Sandbox validation
if [ "${DUPEGURU_TEST:-}" = "1" ]; then
    : # skip sandbox in test mode
else
    case "$WORK" in
        /Volumes/Storage|/Volumes/Storage/*) ;;
        /Volumes/YM/MediaVault|/Volumes/YM/MediaVault/*) ;;
        /Volumes/WD4T/MediaVault|/Volumes/WD4T/MediaVault/*) ;;
        *)
            echo "ERROR: --work $WORK is not in path whitelist" >&2
            echo "  Allowed: /Volumes/Storage, /Volumes/WD4T/MediaVault, /Volumes/YM/MediaVault" >&2
            exit 1
            ;;
    esac
fi

# Verify path is writable
if [[ ! -d "$WORK" ]]; then
    echo "ERROR: $WORK does not exist or is not a directory" >&2
    exit 1
fi
if [[ ! -w "$WORK" ]]; then
    echo "ERROR: $WORK is not writable" >&2
    exit 1
fi

# Create directory skeleton
mkdir -p "$WORK/inbox"
mkdir -p "$WORK/by-date"
mkdir -p "$WORK/screenshots"
mkdir -p "$WORK/_favorite"
mkdir -p "$WORK/_vlogs"
mkdir -p "$WORK/_trash"
mkdir -p "$WORK/_meta/stars"
mkdir -p "$WORK/_meta/edl"
mkdir -p "$WORK/_meta/checksums"
mkdir -p "$WORK/_meta/thumbs"
mkdir -p "$WORK/_meta/scripts"
mkdir -p "$WORK/_meta/logs"

# Create events.yaml (empty by default, opt-in for example)
if [[ ! -f "$WORK/_meta/events.yaml" ]]; then
    if [[ "$WITH_EXAMPLE_THEMES" == "1" ]] && [[ -f "$(dirname "$(readlink -f "$0")")/../outputs/events.example.yaml" ]]; then
        cp "$(dirname "$(readlink -f "$0")")/../outputs/events.example.yaml" "$WORK/_meta/events.yaml"
        echo "✓ Seeded _meta/events.yaml from example (includes sample themes: 海南/夏令营/重要证件照)"
    else
        # Empty events.yaml with just header
        cat > "$WORK/_meta/events.yaml" << 'EVENTSEOF'
# Theme definitions for rename_organize.py
# Add themes with ./scripts/add_theme.py --interactive

themes: []
EVENTSEOF
        echo "✓ Created empty _meta/events.yaml (use --with-example-themes next time for samples)"
    fi
fi

echo "✓ Created directory skeleton in $WORK"
echo "  inbox/  by-date/  screenshots/  _favorite/  _vlogs/  _trash/  _meta/"
