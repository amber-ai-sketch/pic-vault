#!/usr/bin/env python3
"""Negative path-sandbox tests for PicVault scripts (no real disks required)."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = PROJECT_ROOT / 'scripts'
PICVAULT = PROJECT_ROOT / 'picvault'

sys.path.insert(0, str(SCRIPTS))

passed = 0
failed = 0


def check(name, cond, detail=''):
    global passed, failed
    if cond:
        print(f'  ✓ {name}')
        passed += 1
    else:
        print(f'  ✗ FAIL: {name}' + (f' — {detail}' if detail else ''))
        failed += 1


def run(cmd, env=None):
    # When env is passed, use it as the full environment (callers strip
    # PICVAULT_TEST / DUPEGURU_TEST). Do not copy+update — that would keep
    # sandbox bypass flags from the parent process.
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return r.returncode, r.stdout, r.stderr


def test_dedupe_batch_escape():
    print('\n1. dedupe.py --batch path escape')
    import dedupe as d

    for bad in ('../evil', 'a/b', 'a\\b', '..', 'foo/../bar'):
        try:
            d.validate_batch_name(bad)
            check(f'reject batch {bad!r}', False, 'should raise')
        except ValueError:
            check(f'reject batch {bad!r}', True)

    check('accept plain batch', d.validate_batch_name('2026-07-20-A') == '2026-07-20-A')

    env = {**os.environ, 'DUPEGURU_TEST': '1'}
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        (work / 'inbox').mkdir(parents=True)
        rc, out, err = run([
            'python3', str(SCRIPTS / 'dedupe.py'),
            '--work', str(work),
            '--batch', '../outside',
        ], env=env)
        check('CLI rejects --batch ../outside', rc != 0 and ('batch' in err.lower() or '..' in err))


def test_pick_absolute_star_rejected():
    print('\n2. pick_to_iphone.py absolute star rejected')
    import pick_to_iphone as pick

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        work.mkdir()
        try:
            pick.resolve_star_path(work, '/tmp/evil.jpg')
            check('reject absolute star', False, 'should raise')
        except ValueError as e:
            check('reject absolute star', 'absolute' in str(e).lower() or 'work-relative' in str(e).lower())

        try:
            pick.resolve_star_path(work, '../outside.jpg')
            check('reject .. star', False, 'should raise')
        except ValueError:
            check('reject .. star', True)

        rel = 'by-date/2026/2026-07/photos/a.jpg'
        (work / rel).parent.mkdir(parents=True)
        (work / rel).write_bytes(b'ok')
        resolved = pick.resolve_star_path(work, rel)
        check('accept relative under work', resolved == (work / rel).resolve())

        # AppleScript uses "" escape and actual work subset path
        scpt = pick.generate_applescript('2026-08', 'Picks', str(work / '_favorite' / '2026-08'))
        check('AS escape uses ""', '""' in pick.as_escape('say "hi"') or pick.as_escape('a"b') == 'a""b')
        check('AS folderPath uses work subset', str(work / '_favorite' / '2026-08') in scpt)
        check('AS no hardcoded /Volumes/Storage/_favorite alone',
              '/Volumes/Storage/_favorite"' not in scpt.replace('/Volumes/Storage/_favorite/2026-08', ''))


def test_make_vlog_path_escape():
    print('\n3. make_vlog.py EDL path / transition / in-out')
    import make_vlog as mv

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        vid = work / 'by-date' / '2026' / '2026-07_海南' / 'videos'
        vid.mkdir(parents=True)
        clip = vid / 'a.mp4'
        clip.write_bytes(b'x')

        ok = mv.validate_clip(work, {
            'path': str(clip.relative_to(work)),
            'in': 0,
            'out': 1.5,
        }, 0)
        check('accept relative clip', ok['path'] == clip.resolve() and ok['out'] == 1.5)

        try:
            mv.validate_clip(work, {'path': '/etc/passwd', 'in': 0, 'out': 1}, 0)
            check('reject absolute clip', False, 'should raise')
        except ValueError:
            check('reject absolute clip', True)

        try:
            mv.validate_clip(work, {'path': '../../../etc/passwd', 'in': 0, 'out': 1}, 0)
            check('reject .. clip', False, 'should raise')
        except ValueError:
            check('reject .. clip', True)

        try:
            mv.validate_clip(work, {'path': str(clip.relative_to(work)), 'in': 'x', 'out': 1}, 0)
            check('reject non-float in', False, 'should raise')
        except ValueError:
            check('reject non-float in', True)

        try:
            mv.validate_transition('crossfade-1s;evil')
            check('reject bad transition', False, 'should raise')
        except ValueError:
            check('reject bad transition', True)

        check('allow concat', mv.validate_transition('concat') == 'concat')

        # CLI: escape path in EDL under DUPEGURU_TEST
        edl_dir = work / '_meta' / 'edl'
        edl_dir.mkdir(parents=True)
        (edl_dir / 't.json').write_text(json.dumps({
            'clips': [{'path': '../../evil.mp4', 'in': 0, 'out': 1}],
        }))
        env = {**os.environ, 'DUPEGURU_TEST': '1'}
        rc, out, err = run([
            'python3', str(SCRIPTS / 'make_vlog.py'),
            '--work', str(work),
            '--theme', 't',
            '--style', 'concat',
        ], env=env)
        check('CLI rejects escaping EDL path', rc != 0 and ('escape' in err.lower() or '..' in err or 'work-relative' in err.lower()))


def test_picvault_check_path_dotdot():
    print('\n4. picvault check_path rejects .. tricks')
    escape = '/Volumes/Storage/../../Users'
    env = {k: v for k, v in os.environ.items() if k not in ('WORK', 'PICVAULT_WORK', 'PICVAULT_TEST')}
    env['PICVAULT_SANDBOX_BYPASS'] = '0'
    rc, out, err = run([str(PICVAULT), '--work', escape, 'status'], env=env)
    combined = (out + err).lower()
    check('rejects Storage/../../Users', rc != 0 and ('whitelist' in combined or 'not in path' in combined))

    # Misplaced --work must not be ignored
    env2 = {**env, 'PICVAULT_TEST': '1'}
    rc2, out2, err2 = run([str(PICVAULT), 'status', '--work', '/Volumes/Storage'], env=env2)
    check('rejects --work after command', rc2 != 0 and '--work must come before' in (out2 + err2))

    text = PICVAULT.read_text(encoding='utf-8')
    check('sanitize_bucket defined', 'sanitize_bucket()' in text)
    check('pipeline sync uses --verify', 'cmd_sync --verify --yes' in text)
    check('web host defaults to 127.0.0.1', '--host 127.0.0.1' in text)

    rc3, out3, err3 = run([
        str(PICVAULT), 'star', '../evil', 'a.jpg',
    ], env=env2)
    check('star rejects ../ bucket', rc3 != 0 and ('invalid bucket' in (out3 + err3).lower() or '..' in (out3 + err3)))


def test_sync_work_sandbox():
    print('\n5. sync_to_backup.sh --work sandbox')
    rc, out, err = run([
        'bash', str(SCRIPTS / 'sync_to_backup.sh'),
        '--work', '/Users/foo',
        '--backup', '/Volumes/WD4T/MediaVault',
        '--dry-run',
    ], env={k: v for k, v in os.environ.items() if k != 'DUPEGURU_TEST'})
    check('rejects --work /Users/foo', rc != 0 and 'whitelist' in (out + err).lower())

    # .. escape via Storage prefix
    rc2, out2, err2 = run([
        'bash', str(SCRIPTS / 'sync_to_backup.sh'),
        '--work', '/Volumes/Storage/../../Users',
        '--backup', '/Volumes/WD4T/MediaVault',
        '--dry-run',
    ], env={k: v for k, v in os.environ.items() if k != 'DUPEGURU_TEST'})
    check('rejects work .. escape', rc2 != 0 and 'whitelist' in (out2 + err2).lower())


def main():
    print('Path sandbox negative tests')
    test_dedupe_batch_escape()
    test_pick_absolute_star_rejected()
    test_make_vlog_path_escape()
    test_picvault_check_path_dotdot()
    test_sync_work_sandbox()
    print(f'\n{passed} passed, {failed} failed')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
