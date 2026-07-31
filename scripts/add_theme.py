#!/usr/bin/env python3
"""
add_theme.py - Add, list, or remove theme definitions in events.yaml.

Usage:
    ./add_theme.py --work /Volumes/Storage --interactive
    ./add_theme.py --work /Volumes/Storage --name 海南 --month 2026-07 ...
    ./add_theme.py --work /Volumes/Storage --list
    ./add_theme.py --work /Volumes/Storage --remove 海南
"""

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

# Sandbox
ALLOWED_WORK_PREFIXES = ('/Volumes/Storage', '/Volumes/YM/MediaVault', '/Users/ym/Downloads/pic-test')


def validate_path(path_str: str, allowed_prefixes, kind: str) -> Path:
    p = Path(path_str).expanduser().resolve()
    if os.environ.get('DUPEGURU_TEST') == '1':
        return p
    for prefix in allowed_prefixes:
        prefix_resolved = str(Path(prefix).resolve())
        if str(p) == prefix_resolved or str(p).startswith(prefix_resolved + '/'):
            return p
    raise ValueError(
        f"--{kind} {path_str} is not in path whitelist.\n"
        f"  Allowed: {', '.join(allowed_prefixes)}"
    )


def _format_theme_yaml_block(theme: dict) -> str:
    """Format one theme as indented YAML list item lines."""
    lines = [f"  - name: {theme['name']}"]
    lines.append(f"    month: {theme['month']}")

    date_range = theme.get('date_range') if isinstance(theme.get('date_range'), dict) else None
    start = theme.get('date_range_start')
    end = theme.get('date_range_end')
    if date_range is not None:
        start = start or date_range.get('start')
        end = end or date_range.get('end')
    if start and end:
        lines.append(f"    date_range:")
        lines.append(f"      start: {start}")
        lines.append(f"      end:   {end}")

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


def _theme_period(theme: dict) -> str:
    dr = theme.get('date_range') if isinstance(theme.get('date_range'), dict) else None
    start = end = ''
    if dr is not None:
        start = str(dr.get('start') or '').strip()
        end = str(dr.get('end') or '').strip()
    if not start:
        start = str(theme.get('start') or '').strip()
    if not end:
        end = str(theme.get('end') or '').strip()
    if start and end:
        return f'{start} ~ {end}'
    return start or end or ''


def load_themes(work: Path) -> list:
    """Load and validate themes from events.yaml."""
    import rename_organize as ro

    path = work / '_meta' / 'events.yaml'
    if not path.exists():
        return []
    text = path.read_text(encoding='utf-8')
    themes = ro.parse_events_yaml_text(text)
    ro.validate_events_themes(themes)
    return themes


def render_themes_yaml(themes: list) -> str:
    """Render a canonical themes section."""
    if not themes:
        return 'themes: []\n'
    return 'themes:\n' + ''.join(_format_theme_yaml_block(theme) for theme in themes)


def split_events_yaml(text: str) -> tuple[str, str]:
    """Split events.yaml into prefix before themes: and suffix after it."""
    match = re.search(r'(?m)^themes:\s*(?:\[\s*\])?\s*$', text)
    if not match:
        raise ValueError('events.yaml missing themes section')

    lines = text.splitlines(keepends=True)
    header_line = text[:match.start()].count('\n')
    section_end = len(lines)
    for index in range(header_line + 1, len(lines)):
        line = lines[index]
        stripped = line.lstrip(' \t')
        if not stripped.strip() or stripped.startswith('#'):
            continue
        if len(line) - len(stripped) == 0:
            section_end = index
            break

    prefix = ''.join(lines[:header_line])
    suffix = ''.join(lines[section_end:])
    return prefix, suffix


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
    text = path.read_text(encoding='utf-8')
    block = _format_theme_yaml_block(theme)

    # Fresh init / empty flow list: themes: [] → themes:\n  - name: ...
    empty_flow = re.search(r'(?m)^themes:\s*\[\s*\]\s*$', text)
    if empty_flow:
        text = text[: empty_flow.start()] + 'themes:\n' + block + text[empty_flow.end() :]
        if not text.endswith('\n'):
            text += '\n'
        path.write_text(text, encoding='utf-8')
        return

    # No themes key yet — add header then the item
    if not re.search(r'(?m)^themes:\s*(?:$|\[)', text):
        if not text.endswith('\n'):
            text += '\n'
        text += 'themes:\n' + block
        path.write_text(text, encoding='utf-8')
        return

    # Existing block-style list (or bare `themes:`) — append item
    if not text.endswith('\n'):
        text += '\n'
    path.write_text(text + block, encoding='utf-8')


def remove_theme_from_events(work: Path, name: str) -> int:
    """Remove a theme by name and rewrite events.yaml."""
    path = work / '_meta' / 'events.yaml'
    if not path.exists():
        raise FileNotFoundError(f'{path} does not exist')

    themes = load_themes(work)
    remaining = [theme for theme in themes if str(theme.get('name') or '').strip() != name]
    if len(remaining) == len(themes):
        raise ValueError(f'theme {name!r} not found')

    text = path.read_text(encoding='utf-8')
    prefix, suffix = split_events_yaml(text)
    new_text = prefix + render_themes_yaml(remaining) + suffix
    if not new_text.endswith('\n'):
        new_text += '\n'

    bak = path.with_name('events.yaml.bak')
    shutil.copy2(path, bak)
    path.write_text(new_text, encoding='utf-8')
    return len(remaining)


def list_themes(work: Path) -> list:
    """Print themes in a readable summary format."""
    path = work / '_meta' / 'events.yaml'
    if not path.exists():
        print('No events.yaml', file=sys.stderr)
        return []

    themes = load_themes(work)
    print()
    print('Themes')
    print('─────────────────────────────────────────')
    if not themes:
        print('  (none)')
        print()
        return themes

    for theme in themes:
        name = str(theme.get('name') or '?').strip() or '?'
        month = str(theme.get('month') or '?').strip() or '?'
        period = _theme_period(theme)
        sources = ', '.join(str(source) for source in (theme.get('sources') or []) if str(source).strip())
        files = theme.get('files') or []
        details = [month]
        if period:
            details.append(period)
        if sources:
            details.append(f'sources={sources}')
        if files:
            details.append(f'files={len(files)}')
        print(f"  {name:20s}  {'  '.join(details)}")
    print()
    return themes


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

    files_str = prompt("Files (comma-separated basenames or paths, empty=skip)", "")
    files = [s.strip() for s in files_str.split(',') if s.strip()] if files_str else []

    print()
    theme = {
        'name': name,
        'month': month,
        'sources': sources,
    }
    if files:
        theme['files'] = files
    if start and end:
        theme['date_range_start'] = start
        theme['date_range_end'] = end

    return theme


def main():
    parser = argparse.ArgumentParser(description='Add theme to events.yaml')
    parser.add_argument('--work', default='/Volumes/Storage')
    parser.add_argument('--interactive', action='store_true')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--remove', default=None)
    parser.add_argument('--name', default=None)
    parser.add_argument('--month', default=None)
    parser.add_argument('--start', default=None)
    parser.add_argument('--end', default=None)
    parser.add_argument('--sources', default=None, help='Comma-separated')
    parser.add_argument('--files', default=None, help='Comma-separated')
    args = parser.parse_args()

    try:
        work = validate_path(args.work, ALLOWED_WORK_PREFIXES, 'work')
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.list:
        try:
            list_themes(work)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.remove:
        try:
            remaining = remove_theme_from_events(work, args.remove)
        except (FileNotFoundError, ValueError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"\n✓ Removed theme: {args.remove}")
        print(f"  backup: {work}/_meta/events.yaml.bak")
        print(f"  remaining themes: {remaining}")
        print(f"\nNext step: picvault theme rebucket --theme {args.remove} [--yes]")
        print("  or: picvault theme rebucket --all [--yes]")
        return

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
        files = [s.strip() for s in (args.files or '').split(',') if s.strip()]
        if files:
            theme['files'] = files
        if args.start and args.end:
            theme['date_range_start'] = args.start
            theme['date_range_end'] = args.end

    if not theme.get('date_range_start') and not theme.get('sources') and not theme.get('files'):
        print(
            "ERROR: need date_range (--start/--end) and/or --sources "
            "and/or --files (name+month alone never matches files)",
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
