#!/usr/bin/env python3
"""
add_theme.py - Interactively add a theme definition to events.yaml.

Usage:
    ./add_theme.py --work /Volumes/Storage --interactive
    ./add_theme.py --work /Volumes/Storage --name 海南 --month 2026-07 ...
"""

import argparse
import sys
from pathlib import Path

# Sandbox
ALLOWED_WORK_PREFIXES = ('/Volumes/Storage',)


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


def load_existing_themes(work: Path) -> list:
    """Load existing themes from events.yaml."""
    path = work / '_meta' / 'events.yaml'
    if not path.exists():
        return []
    try:
        text = path.read_text()
        # Try YAML first
        themes = []
        in_themes = False
        for line in text.split('\n'):
            if line.startswith('themes:'):
                in_themes = True
                continue
            if not in_themes:
                continue
            stripped = line.strip()
            if stripped.startswith('#') or not stripped:
                continue
            if not line.startswith('  '):  # not indented under themes:
                in_themes = False
                continue
            # Crude extraction - count a new theme when we see "- name:"
            if stripped.startswith('- name:'):
                themes.append({'_pending': True, 'name': stripped.split(':', 1)[1].strip()})
            elif themes and '_pending' in themes[-1] and ':' in stripped:
                k, _, v = stripped.partition(':')
                themes[-1][k.strip()] = v.strip()
                if k.strip() == 'files' or k.strip() == 'sources':
                    themes[-1]['_multi'] = True
        return themes
    except Exception as e:
        print(f"[warn] could not parse events.yaml: {e}", file=sys.stderr)
        return []


def append_theme_to_events(work: Path, theme: dict):
    """Append a single theme entry to events.yaml."""
    path = work / '_meta' / 'events.yaml'
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create header if file doesn't exist
        path.write_text("# Theme definitions for rename_organize.py\n# Each theme: name, month, optional date_range/sources/files\n\nthemes:\n")

    text = path.read_text()
    if not text.rstrip().endswith('themes:'):
        # No themes: header yet
        if not text.endswith('\n'):
            text += '\n'
        text += 'themes:\n'

    # Build YAML block
    lines = [f"  - name: {theme['name']}"]
    lines.append(f"    month: {theme['month']}")

    if theme.get('date_range_start') and theme.get('date_range_end'):
        lines.append(f"    date_range:")
        lines.append(f"      start: {theme['date_range_start']}")
        lines.append(f"      end:   {theme['date_range_end']}")

    if theme.get('sources'):
        sources_str = ", ".join(theme['sources'])
        lines.append(f"    sources: [{sources_str}]")

    if theme.get('files'):
        lines.append(f"    files:")
        for f in theme['files']:
            lines.append(f"      - {f}")

    block = "\n".join(lines) + "\n"

    with open(path, 'a') as f:
        f.write(block)


def prompt(question: str, default: str = '') -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{question}{suffix}: ").strip()
    return val if val else default


def interactive_add(work: Path) -> dict:
    """Prompt for theme details."""
    print("Add new theme (press Enter to skip optional fields):\n")
    name = prompt("Theme name (e.g., 海南)", "")
    if not name:
        print("ERROR: theme name is required")
        sys.exit(1)

    month = prompt("Month (YYYY-MM)", "")
    if not re.match(r'^\d{4}-\d{2}$', month):
        print("ERROR: month must be YYYY-MM format")
        sys.exit(1)

    start = prompt("Date range start (YYYY-MM-DD, empty to skip)", "")
    end = prompt("Date range end   (YYYY-MM-DD, empty to skip)", "")
    if (start and not end) or (end and not start):
        print("ERROR: both start and end must be provided together")
        sys.exit(1)
    if start and not re.match(r'^\d{4}-\d{2}-\d{2}$', start):
        print("ERROR: dates must be YYYY-MM-DD format")
        sys.exit(1)

    sources_str = prompt("Source devices (comma-separated, empty=any)", "")
    sources = [s.strip() for s in sources_str.split(',') if s.strip()] if sources_str else []

    print()
    theme = {
        'name': name,
        'month': month,
        'sources': sources,
    }
    if start and end:
        theme['date_range_start'] = start
        theme['date_range_end'] = end

    return theme


import re

def main():
    parser = argparse.ArgumentParser(description='Add theme to events.yaml')
    parser.add_argument('--work', default='/Volumes/Storage')
    parser.add_argument('--interactive', action='store_true')
    parser.add_argument('--name', default=None)
    parser.add_argument('--month', default=None)
    parser.add_argument('--start', default=None)
    parser.add_argument('--end', default=None)
    parser.add_argument('--sources', default=None, help='Comma-separated')
    args = parser.parse_args()

    try:
        work = validate_path(args.work, ALLOWED_WORK_PREFIXES, 'work')
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.interactive:
        theme = interactive_add(work)
    else:
        if not args.name or not args.month:
            print("ERROR: --interactive or --name + --month required")
            sys.exit(1)
        theme = {
            'name': args.name,
            'month': args.month,
            'sources': [s.strip() for s in (args.sources or '').split(',') if s.strip()],
        }
        if args.start and args.end:
            theme['date_range_start'] = args.start
            theme['date_range_end'] = args.end

    print(f"\n✓ Appending theme:")
    print(f"  name: {theme['name']}")
    print(f"  month: {theme['month']}")
    if theme.get('date_range_start'):
        print(f"  date_range: {theme['date_range_start']} ~ {theme['date_range_end']}")
    if theme.get('sources'):
        print(f"  sources: {theme['sources']}")

    append_theme_to_events(work, theme)
    print(f"\n✓ Appended to {work}/_meta/events.yaml")
    print(f"\nNext step: ./rename_organize.py  (apply the new theme)")


if __name__ == '__main__':
    main()
