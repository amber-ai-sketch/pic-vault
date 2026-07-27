#!/usr/bin/env python3
"""
add_theme.py - Interactively add a theme definition to events.yaml.

Usage:
    ./add_theme.py --work /Volumes/Storage --interactive
    ./add_theme.py --work /Volumes/Storage --name 海南 --month 2026-07 ...
"""

import argparse
import re
import sys
from pathlib import Path

# Sandbox
ALLOWED_WORK_PREFIXES = ('/Volumes/Storage', '/Volumes/YM/MediaVault', '/Users/ym/Downloads/pic-test')


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


def _format_theme_yaml_block(theme: dict) -> str:
    """Format one theme as indented YAML list item lines."""
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

    return "\n".join(lines) + "\n"


def _candidate_theme_dict(theme: dict) -> dict:
    """Map add_theme fields into rename_organize theme shape for normalize/validate."""
    out = {
        'name': theme.get('name'),
        'month': theme.get('month'),
        'sources': list(theme.get('sources') or []),
    }
    if theme.get('files'):
        out['files'] = list(theme['files'])
    start = theme.get('date_range_start') or theme.get('start')
    end = theme.get('date_range_end') or theme.get('end')
    if start or end:
        out['date_range'] = {'start': start or '', 'end': end or ''}
    return out


def prepare_theme_for_append(work: Path, theme: dict) -> dict:
    """Normalize + validate theme (and uniqueness vs existing). Mutates month from start."""
    import rename_organize as ro

    existing: list = []
    path = work / '_meta' / 'events.yaml'
    if path.exists():
        existing = ro.parse_events_yaml_text(path.read_text(encoding='utf-8'))

    candidate = _candidate_theme_dict(theme)
    month = str(candidate.get('month') or '').strip()
    if month and not re.match(r'^\d{4}-\d{2}$', month):
        raise ValueError(f'month must be YYYY-MM (got {month!r})')

    norm = ro._normalize_theme_dict(candidate)
    # Keep add_theme write shape in sync with derived month
    theme = dict(theme)
    theme['month'] = norm.get('month') or theme.get('month')
    if theme.get('date_range_start') and theme.get('date_range_end'):
        theme['date_range_start'] = norm.get('date_range', {}).get('start') or theme['date_range_start']
        theme['date_range_end'] = norm.get('date_range', {}).get('end') or theme['date_range_end']
    ro.validate_events_themes(existing + [norm])
    return theme


def append_theme_to_events(work: Path, theme: dict, *, validate: bool = True):
    """Append a single theme entry to events.yaml.

    Handles fresh init files with ``themes: []`` by rewriting to a block-style
    list so the result stays valid YAML (plain append after ``[]`` is invalid).
    """
    path = work / '_meta' / 'events.yaml'
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create header if file doesn't exist
        path.write_text(
            "# 主题配置（rename_organize / Web /themes）\n"
            "# 也可用：./scripts/add_theme.py --interactive\n"
            "#\n"
            "# 示例（去掉行首 # 后保存；→ by-date/2026/2026-07_海南/）：\n"
            "#\n"
            "# themes:\n"
            "#   - name: 海南\n"
            "#     month: 2026-07\n"
            "#     date_range:\n"
            "#       start: 2026-07-10\n"
            "#       end: 2026-07-18\n"
            "#     sources:\n"
            "#       - iphone\n"
            "#       - canon\n"
            "#\n"
            "# 字段：name、month(YYYY-MM) 必填；date_range / sources / files 至少其一\n"
            "# 保存后：picvault theme rebucket --theme <名> [--yes]\n"
            "\n"
            "themes: []\n"
        )

    if validate:
        theme = prepare_theme_for_append(work, theme)
    text = path.read_text()
    block = _format_theme_yaml_block(theme)

    # Fresh init / empty flow list: themes: [] → themes:\n  - name: ...
    empty_flow = re.search(r'(?m)^themes:\s*\[\s*\]\s*$', text)
    if empty_flow:
        text = text[: empty_flow.start()] + 'themes:\n' + block + text[empty_flow.end() :]
        if not text.endswith('\n'):
            text += '\n'
        path.write_text(text)
        return

    # No themes key yet — add header then the item
    if not re.search(r'(?m)^themes:\s*(?:$|\[)', text):
        if not text.endswith('\n'):
            text += '\n'
        text += 'themes:\n' + block
        path.write_text(text)
        return

    # Existing block-style list (or bare `themes:`) — append item
    if not text.endswith('\n'):
        text += '\n'
    path.write_text(text + block)


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

    month = prompt("Month (YYYY-MM, blank if date_range start will derive it)", "")
    if month and not re.match(r'^\d{4}-\d{2}$', month):
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
    if end and not re.match(r'^\d{4}-\d{2}-\d{2}$', end):
        print("ERROR: dates must be YYYY-MM-DD format")
        sys.exit(1)
    if not month and start:
        month = start[:7]
    if not month:
        print("ERROR: month or date_range start is required")
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
        if not args.name:
            print("ERROR: --interactive or --name required", file=sys.stderr)
            sys.exit(1)
        if args.month and not re.match(r'^\d{4}-\d{2}$', args.month):
            print(f"ERROR: --month must be YYYY-MM (got {args.month!r})", file=sys.stderr)
            sys.exit(1)
        if (args.start and not args.end) or (args.end and not args.start):
            print("ERROR: both --start and --end must be provided together", file=sys.stderr)
            sys.exit(1)
        month = args.month
        if not month and args.start:
            month = args.start[:7] if len(args.start) >= 7 else None
        if not month:
            print("ERROR: --month or --start required", file=sys.stderr)
            sys.exit(1)
        theme = {
            'name': args.name,
            'month': month,
            'sources': [s.strip() for s in (args.sources or '').split(',') if s.strip()],
        }
        if args.start and args.end:
            theme['date_range_start'] = args.start
            theme['date_range_end'] = args.end

    if not theme.get('date_range_start') and not theme.get('sources') and not theme.get('files'):
        print(
            "ERROR: need date_range (--start/--end) and/or --sources "
            "(name+month alone never matches files)",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        theme = prepare_theme_for_append(work, theme)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\n✓ Appending theme:")
    print(f"  name: {theme['name']}")
    print(f"  month: {theme['month']}")
    if theme.get('date_range_start'):
        print(f"  date_range: {theme['date_range_start']} ~ {theme['date_range_end']}")
    if theme.get('sources'):
        print(f"  sources: {theme['sources']}")

    append_theme_to_events(work, theme, validate=False)
    print(f"\n✓ Appended to {work}/_meta/events.yaml")
    print(f"\nNext step: picvault theme rebucket --theme {theme['name']} [--yes]")


if __name__ == '__main__':
    main()
