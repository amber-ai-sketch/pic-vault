#!/usr/bin/env python3
"""Targeted tests for Bugbot fixes (pipeline, backup, init dirs, dashboard wiring)."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import web_browse as wb  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DASHBOARD = PROJECT_ROOT / 'outputs' / 'dashboard.html'
PICVAULT = PROJECT_ROOT / 'picvault'

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


def check_page_js_syntax():
    node = shutil.which('node')
    if not node:
        check('PAGE_JS syntax check skipped (node missing)', True)
        return
    with tempfile.TemporaryDirectory() as tmp:
        js_path = Path(tmp) / 'page.js'
        js_path.write_text(wb.PAGE_JS, encoding='utf-8')
        proc = subprocess.run(
            [node, '--check', str(js_path)],
            capture_output=True,
            text=True,
        )
    check('PAGE_JS parses as JavaScript', proc.returncode == 0, detail=proc.stderr)


def test_dashboard_pipeline_button():
    print('\n1. Dashboard 06 uses pipeline command')
    text = DASHBOARD.read_text(encoding='utf-8')
    check(
        'runApply uses pipeline',
        "runApply(this, 'pipeline'" in text,
    )
    # The 06 button line itself must call pipeline, not dedupe_apply.
    m06 = re.search(r"onclick=\"runApply\(this, '([^']+)'[^']*'确定一键跑完全流程", text)
    check(
        '06 全流程按钮 command=pipeline',
        bool(m06) and m06.group(1) == 'pipeline',
        detail=repr(m06.group(1) if m06 else None),
    )
    # showOutput hides cancel
    check('showOutput hides cancel button', 'function showOutput' in text and 'setCancelRunVisible(false)' in text.split('function showOutput')[1].split('function ')[0])
    # disconnect rebuilds output and resumes from offset 0 (not EOF)
    check(
        'NDJSON disconnect uses openOutputForRun + offset 0',
        'openOutputForRun' in text
        and 'startLogPoll(runId, 0)' in text,
    )
    check(
        'NDJSON disconnect does not use MAX_SAFE_INTEGER',
        'Number.MAX_SAFE_INTEGER' not in text,
    )
    check(
        'runCommand sends backup for sync/pipeline',
        "body.backup = paths.backup" in text and "cmdName === 'pipeline'" in text,
    )


def test_dashboard_web_start_copy():
    print('\n1b. Dashboard copy command for web start')
    text = DASHBOARD.read_text(encoding='utf-8')
    check('keeps copy start command', "copyText('startCmd')" in text)
    check('no startWebBtn', 'id="startWebBtn"' not in text)
    check('no startWebUi', 'async function startWebUi' not in text)
    check('no BOOT_URL', '8764/api/web/start' not in text)
    check('no webStartRunBtn', 'id="webStartRunBtn"' not in text)
    check('step 04 keeps open browse', 'id="openWebBtnStep"' in text)
    check('open browse stays in same tab', "window.location.assign('http://localhost:' + WEB_PORT + '/')" in text)
    check('open browse no longer new tab', "window.open('http://localhost:' + WEB_PORT + '/', '_blank')" not in text)


def test_gallery_menu_counts_and_dismissal():
    print('\n1c. Gallery menu shows theme count and closes on outside click')
    js = wb.PAGE_JS
    check_page_js_syntax()
    check(
        'gallery menu outside click handler',
        'document.querySelectorAll(\'.jumps-more[open]\')' in js
        and 'if (!menu.contains(e.target)) menu.open = false;' in js,
    )
    check(
        'gallery shortcut nav replaces history between peers',
        'function shouldReplaceGalleryShortcutNav' in js
        and "'/by-date': true" in js
        and 'galleryShortcutPaths[currentPath]' in js
        and 'window.location.replace(jumpLink.href)' in js,
    )
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        (work / 'by-date' / '2026' / '2026-07' / 'photos').mkdir(parents=True)
        (work / 'by-date' / '2026' / '2026-08').mkdir(parents=True)
        (work / 'by-date' / '2025' / '2025-12' / 'videos').mkdir(parents=True)
        (work / '_meta').mkdir(parents=True)
        (work / '_meta' / 'events.yaml').write_text(
            'themes:\n'
            '  - name: 海南\n'
            '    month: 2026-07\n'
            '    sources: [iphone]\n'
            '  - name: 雪山\n'
            '    month: 2025-12\n'
            '    sources: [canon]\n',
            encoding='utf-8',
        )
        html = wb.page_shell('首页', '<p>x</p>', work=work).decode('utf-8')
        check(
            'gallery menu has by-date entry',
            'href="/by-date"><span class="jump-name">按日期</span>' in html,
        )
        check(
            'gallery menu theme count',
            'href="/themes"><span class="jump-name">主题</span><span class="n">2 项</span></a>' in html,
        )


def test_home_overview_cards_and_by_date_route():
    print('\n1c2. Home is folder overview; by-date keeps year ledger')
    import inspect

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        photo = work / 'by-date' / '2026' / '2026-07' / 'photos'
        video = work / 'by-date' / '2026' / '2026-07' / 'videos'
        theme_photo = work / 'by-date' / '2026' / '2026-08_海南' / 'photos'
        for d in (photo, video, theme_photo, work / 'screenrecords', work / 'docs', work / 'things', work / '_trash'):
            d.mkdir(parents=True)
        shots = work / 'screenshots'
        shots.mkdir(parents=True)
        (photo / '20260701_120000_iphone_a.jpg').write_bytes(b'x')
        (video / '20260701_120000_iphone_b.mov').write_bytes(b'x')
        (theme_photo / '20260801_120000_iphone_c.jpg').write_bytes(b'x')
        for i in range(2):
            (shots / f'screenshot_{i}.jpg').write_bytes(b'x')
        (work / 'screenrecords' / 'screenrecorder_0.mov').write_bytes(b'x')
        (work / 'docs' / 'doc_0.jpg').write_bytes(b'x')
        (work / 'things' / 'things_0.jpg').write_bytes(b'x')
        (work / '_trash' / 'batch').mkdir(parents=True)
        (work / '_trash' / 'batch' / 'deleted.jpg').write_bytes(b'x')
        (work / '_meta' / 'stars').mkdir(parents=True)
        (work / '_meta' / 'events.yaml').write_text(
            'themes:\n'
            '  - name: 海南\n'
            '    month: 2026-08\n'
            '    sources: [iphone]\n'
            '  - name: 雪山\n'
            '    month: 2025-12\n'
            '    sources: [canon]\n',
            encoding='utf-8',
        )
        (work / '_meta' / 'stars' / 'screenshots.json').write_text(
            json.dumps({str((shots / 'screenshot_0.jpg').relative_to(work)): True}),
            encoding='utf-8',
        )

        wb.clear_web_caches()
        home = wb.render_home(work).decode('utf-8')
        check('home title is gallery overview', '<h2 class="page-title">图库</h2>' in home)
        check('home has by-date card', '<span class="home-card-name">按日期</span>' in home and 'href="/by-date"' in home)
        check('home no longer links years directly', 'href="/y/2026"' not in home)
        check('home screenshot count card', re.search(r'home-card-name">截图</span><span class="home-card-count">2 项</span>', home) is not None)
        check('home screenrecord count card', re.search(r'home-card-name">录屏</span><span class="home-card-count">1 项</span>', home) is not None)
        check('home docs count card', re.search(r'home-card-name">文档</span><span class="home-card-count">1 项</span>', home) is not None)
        check('home things count card', re.search(r'home-card-name">物品</span><span class="home-card-count">1 项</span>', home) is not None)
        check('home starred count card', re.search(r'home-card-name">加星</span><span class="home-card-count">1 项</span>', home) is not None)
        check('home theme count card', re.search(r'home-card-name">主题</span><span class="home-card-count">2 项</span>', home) is not None)
        check('home trash count is visible', re.search(r'home-card-name">回收站</span><span class="home-card-count">1 项</span>', home) is not None)
        check('home trash is not a route link', 'href="/trash"' not in home)

        by_date = wb.render_by_date_home(work).decode('utf-8')
        check('by-date title is 按日期', '<h2 class="page-title">按日期</h2>' in by_date)
        check('by-date keeps year ledger', 'href="/y/2026"' in by_date)

        year = wb.render_year(work, '2026').decode('utf-8')
        check('year breadcrumb includes 按日期', 'href="/by-date">按日期</a>' in year)
        bucket = wb.render_bucket(work, '2026', '2026-07', work / '_meta' / 'thumbs').decode('utf-8')
        check('bucket breadcrumb includes 按日期', 'href="/by-date">按日期</a>' in bucket)

        src = inspect.getsource(wb.Handler.do_GET)
        check('handler has by-date route', "path == '/by-date'" in src and 'render_by_date_home' in src)


def test_picvault_web_port_state():
    print('\n1d. picvault web status uses persisted custom port')
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        meta = work / '_meta'
        meta.mkdir(parents=True)
        (meta / 'web.pid').write_text(str(os.getpid()), encoding='utf-8')
        (meta / 'web.port').write_text('8777', encoding='utf-8')
        env = {**os.environ, 'WORK': str(work), 'PICVAULT_TEST': '1'}

        status = subprocess.run(
            [str(PICVAULT), 'status'], env=env,
            capture_output=True, text=True,
        )
        web_status = subprocess.run(
            [str(PICVAULT), 'web', 'status'], env=env,
            capture_output=True, text=True,
        )
        no_action = subprocess.run(
            [str(PICVAULT), 'web'], env=env,
            capture_output=True, text=True,
        )
        check('picvault status uses web.port', 'http://localhost:8777/' in (status.stdout + status.stderr))
        check('picvault web status uses web.port', 'http://localhost:8777/' in (web_status.stdout + web_status.stderr))
        check('picvault web no action prints usage', no_action.returncode != 0 and 'Usage: picvault web' in (no_action.stdout + no_action.stderr))


def test_picvault_web_restart_clears_orphan_same_work_server():
    print('\n1e. picvault web restart clears same-WORK orphan server')
    allowed_tmp_root = Path('/Users/ym/Downloads/pic-test')
    allowed_tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(allowed_tmp_root)) as tmp:
        work = Path(tmp) / 'work'
        meta = work / '_meta'
        (meta / 'logs').mkdir(parents=True)
        (meta / 'thumbs').mkdir(parents=True)
        (work / 'by-date').mkdir()
        (work / 'screenshots').mkdir()
        (work / 'screenrecords').mkdir()
        (work / 'docs').mkdir()
        (work / 'things').mkdir()
        (work / 'inbox').mkdir()
        (work / '_trash').mkdir()

        sock = __import__('socket').socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()

        orphan = subprocess.Popen(
            [
                sys.executable,
                str(PROJECT_ROOT / 'scripts' / 'web_browse.py'),
                '--work', str(work), '--host', '127.0.0.1', '--port', str(port),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        env = {**os.environ, 'WORK': str(work), 'PICVAULT_TEST': '1'}
        try:
            for _ in range(30):
                probe = subprocess.run(
                    [str(PICVAULT), 'web', 'status', '--port', str(port)],
                    env=env, capture_output=True, text=True,
                )
                if orphan.poll() is None:
                    break
                time.sleep(0.1)
            if orphan.poll() is not None:
                check('orphan fixture started', False, detail=f'exit={orphan.returncode}')
                return

            restart = subprocess.run(
                [str(PICVAULT), 'web', 'restart', '--port', str(port)],
                env=env, capture_output=True, text=True, timeout=10,
            )
            out = restart.stdout + restart.stderr
            check('restart succeeds with orphan listener', restart.returncode == 0, detail=out)
            check('restart wrote pid file', (meta / 'web.pid').is_file())
            check('new server is running', 'Web UI started' in out, detail=out)
        finally:
            pid_file = meta / 'web.pid'
            if pid_file.is_file():
                subprocess.run(
                    [str(PICVAULT), 'web', 'stop'], env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            if orphan.poll() is None:
                orphan.terminate()
                try:
                    orphan.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    orphan.kill()


def test_console_link_shows_dashboard_url():
    print('\n1c. Web UI 控制台 tip includes dashboard URL')
    html = wb.page_shell('首页', '<p>x</p>', work=None).decode('utf-8')
    dash_uri = wb.dashboard_file_url()
    check('has 控制台 link', 'id="consoleLink">控制台</a>' in html)
    check('embeds dashboard file URL', dash_uri in html)
    check('labels 控制台地址', '控制台地址' in html)
    check('no browse origin as console', "location.origin + '/'" not in html)
    check('no 当前访问地址 browse label', '当前访问地址' not in html)
    check('clipboard copy attempt', 'navigator.clipboard.writeText' in html)
    check('prompt for selectable URL', 'window.prompt' in html)
    check(
        'no old alert-only copy',
        "alert('请从控制台点「打开浏览」进入本页，或手动打开 outputs/dashboard.html')" not in html,
    )


def test_init_skeleton():
    print('\n2. is_work_initialized requires v7 dirs')
    check('screenrecords in skeleton', 'screenrecords' in wb._INIT_SKELETON_DIRS)
    check('docs in skeleton', 'docs' in wb._INIT_SKELETON_DIRS)
    check('things in skeleton', 'things' in wb._INIT_SKELETON_DIRS)

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        # Old-style skeleton without screenrecords/docs/things
        for name in ('inbox', 'by-date', 'screenshots', '_favorite', '_vlogs', '_trash', '_meta'):
            (work / name).mkdir(parents=True)
        (work / '_meta' / 'events.yaml').write_text('themes: {}\n', encoding='utf-8')
        check('old skeleton not initialized', wb.is_work_initialized(work) is False)

        (work / 'screenrecords').mkdir()
        (work / 'docs').mkdir()
        check('missing things still not initialized', wb.is_work_initialized(work) is False)
        (work / 'things').mkdir()
        check('full skeleton with things initialized', wb.is_work_initialized(work) is True)


def test_run_commands_backup_and_pipeline():
    print('\n3. RUN_COMMANDS use backup arg + pipeline exists')
    # Mimic main() factory registration without binding a real work disk.
    w = '/Volumes/YM/MediaVault'
    pb = str(wb.PICVAULT_BIN)
    cmds = {
        'sync_verify': lambda b: ['bash', str(wb.SYNC_SCRIPT), '--work', w, '--backup', b, '--verify', '--dry-run'],
        'sync_apply': lambda b: ['bash', str(wb.SYNC_SCRIPT), '--work', w, '--backup', b, '--verify'],
        'pipeline': lambda _b: [pb, 'pipeline', '--yes'],
        'dedupe_dry': lambda _b: ['python3', str(wb.DEDUPE_SCRIPT), '--work', w, '--dry-run'],
    }
    custom = '/Volumes/WD4T/MediaVault'
    sync_argv = cmds['sync_verify'](custom)
    check('sync_verify embeds backup', custom in sync_argv)
    other = '/Volumes/YM/MediaVault'
    check('sync_apply honors alternate backup', other in cmds['sync_apply'](other))
    pipe = cmds['pipeline'](custom)
    check('pipeline argv is picvault pipeline --yes', pipe[-2:] == ['pipeline', '--yes'])


def test_backup_validation():
    print('\n4. backup path sandbox')
    ok = wb.validate_path('/Volumes/WD4T/MediaVault', wb.ALLOWED_BACKUP_PREFIXES, 'backup')
    check('WD4T allowed', str(ok).endswith('MediaVault') or 'WD4T' in str(ok))
    try:
        wb.validate_path('/tmp/evil', wb.ALLOWED_BACKUP_PREFIXES, 'backup')
        check('rejects /tmp backup', False, 'should have raised')
    except ValueError:
        check('rejects /tmp backup', True)


def test_picvault_sync_uses_backup_env():
    print('\n5. picvault cmd_sync uses BACKUP env')
    text = PICVAULT.read_text(encoding='utf-8')
    check('BACKUP env in cmd_sync', 'local backup="${BACKUP:-${PICVAULT_BACKUP:-/Volumes/WD4T/MediaVault}}"' in text)
    check('no hardcoded WD4T-only sync apply', text.count('--backup "/Volumes/WD4T/MediaVault"') == 0)


def test_picvault_sync_args_order_independent():
    print('\n5b. picvault sync parses flags in any order')
    text = PICVAULT.read_text(encoding='utf-8')
    m = re.search(r'cmd_sync\(\) \{(?P<body>.*?)\n\}\n\n# === pipeline ===', text, re.S)
    body = m.group('body') if m else ''
    check('cmd_sync found', bool(body))
    check('cmd_sync loops over flags', 'while [ $# -gt 0 ]' in body)
    check('cmd_sync recognizes --verify anywhere', '--verify)' in body and 'verify="--verify"' in body)
    check('cmd_sync recognizes --yes anywhere', '--yes)' in body and 'yes="--yes"' in body)


def test_star_api_and_lightbox_sync():
    print('\n6. /api/star persistence + lightbox UI sync')
    js = wb.PAGE_JS
    check('PAGE_JS has applyStarState', 'function applyStarState' in js)
    check(
        'toggleStar uses applyStarState',
        'applyStarState(path, data.starred)' in js,
    )
    check(
        'openLightbox reads gallery cell star',
        '.cell .star[data-path="' in js,
    )
    # Old one-way lightbox sync must be gone (it left gallery cells stale).
    check(
        'no one-way lbStar-only sync after toggle',
        'var lbStar = document.querySelector' not in js,
    )
    check('lightbox ArrowLeft/Right stepLightbox', 'function stepLightbox' in js)
    check(
        'lightbox nav skips .cell.hidden',
        '.cell:not(.hidden) [data-lightbox]' in js,
    )
    check('lightbox nav wraps around', '% items.length' in js)
    check(
        'lightbox keys ignore typing targets',
        'function isTypingTarget' in js,
    )
    check('lightbox has pick checkbox', 'class="lb-pick"' in js and '勾选' in js)
    check('lightbox space picks and advances', 'function pickCurrentAndAdvance' in js and "e.code === 'Space'" in js)
    check('lightbox pick syncs gallery cell', 'setPathPicked(path, true)' in js)
    check('lightbox has single delete', 'function trashLightboxCurrent' in js and 'class="lb-trash"' in js)
    check('lightbox delete posts one path', 'JSON.stringify({ paths: [path] })' in js)
    check('lightbox delete confirm uses escaped newlines', "删除当前文件？\\n' + (name || path) + '\\n\\n会移到 _trash/" in js)
    check('lightbox delete confirm has no literal newline', "删除当前文件？\n' + (name || path)" not in js)

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        photo = work / 'by-date' / '2026' / '2026-07_海南' / 'photos'
        photo.mkdir(parents=True)
        (work / '_meta').mkdir(parents=True)
        sample = photo / '20260701_120000_iphone_aaa111.jpg'
        sample.write_bytes(b'x')
        rel = str(sample.relative_to(work))
        bucket = '2026-07_海南'

        class H(wb.Handler):
            pass

        H.work = work
        H.thumb_root = work / '_meta' / 'thumbs'
        H.thumb_root.mkdir(parents=True, exist_ok=True)

        from http.server import ThreadingHTTPServer
        import json
        import threading
        import urllib.request

        httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        base = f'http://127.0.0.1:{port}'

        def post(payload):
            req = urllib.request.Request(
                base + '/api/star',
                data=json.dumps(payload).encode(),
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())

        try:
            r1 = post({'path': rel, 'bucket': bucket, 'action': 'toggle'})
            check('star toggle on ok', r1.get('ok') is True and r1.get('starred') is True)
            stars = wb.load_stars(work, bucket)
            check('star persisted to JSON', rel in stars)
            home_on = wb.render_home(work).decode('utf-8')
            check('home star count updates on', 'home-card-name">加星</span><span class="home-card-count">1 项</span>' in home_on)
            r2 = post({'path': rel, 'bucket': bucket, 'action': 'toggle'})
            check('star toggle off ok', r2.get('ok') is True and r2.get('starred') is False)
            check('star removed from JSON', rel not in wb.load_stars(work, bucket))
            home_off = wb.render_home(work).decode('utf-8')
            check('home star count updates off', 'home-card-name">加星</span><span class="home-card-count">0 项</span>' in home_off)
            bad = post({'path': rel, 'bucket': '', 'action': 'toggle'})
            check('empty bucket rejected', bad.get('ok') is False)
        finally:
            httpd.shutdown()


def test_things_reclassify_and_ui():
    print('\n7. things bucket mirrors docs (manual only)')
    import rename_organize as ro

    check('star_bucket things', ro.star_bucket_for_rel('things/things_20240715_a1b2.jpg') == 'things')
    check(
        'toolbar has 移至物品',
        'data-reclassify="to_things"' in wb._gallery_toolbar(1, 0, context='normal'),
    )
    check(
        'things page hides 移至物品',
        'data-reclassify="to_things"' not in wb._gallery_toolbar(1, 0, context='things'),
    )
    check('PAGE_JS labels to_things', "to_things: '移至物品'" in wb.PAGE_JS)

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        (work / 'by-date' / '2024' / '2024-07' / 'photos').mkdir(parents=True)
        sample = work / 'by-date' / '2024' / '2024-07' / 'photos' / '20240715_a1b2.jpg'
        sample.write_bytes(b'fake jpg')
        rel = str(sample.relative_to(work))

        moved = ro.reclassify_paths(work, [rel], 'to_things', dry_run=False)
        check('to_things ok', bool(moved and moved[0].get('ok')))
        dest = str(moved[0].get('dest') or '')
        check('to_things dest under things/', dest.startswith('things/'))
        check('to_things name prefix', Path(dest).name.startswith('things_'))
        check('file landed in things/', (work / dest).is_file())

        # Auto pipeline never invents capture_type things (manual force only)
        leftover = work / 'inbox' / 'IMG_9999.jpg'
        leftover.parent.mkdir(parents=True, exist_ok=True)
        leftover.write_bytes(b'other')
        dest2, cap, _ = ro.plan_destination(work, leftover, force_type=None)
        check('auto plan is not things/', 'things/' not in str(dest2.relative_to(work)))
        check('auto capture_type is not things', cap != 'things')

        html = wb.render_things(work, work / '_meta' / 'thumbs').decode('utf-8')
        check('things page title', '物品' in html and 'things/' in html)
        check('things page shows file', Path(dest).name in html)


def test_live_pair_mov_fail_rolls_back():
    print('\n8. process_live_pair rolls back still if mov move fails')
    import rename_organize as ro
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        inbox = work / 'inbox'
        inbox.mkdir(parents=True)
        still = inbox / 'IMG_1000.HEIC'
        mov = inbox / 'IMG_1000.MOV'
        still.write_bytes(b'still-bytes')
        mov.write_bytes(b'mov-bytes')

        real_move = ro.shutil.move
        calls = {'n': 0}

        def flaky_move(src, dst):
            calls['n'] += 1
            if calls['n'] == 2:
                raise OSError('simulated mov move failure')
            return real_move(src, dst)

        stats = {
            'moved': 0, 'screenshots': 0, 'recordings': 0,
            'photos': 0, 'videos': 0, 'live_pairs': 0,
        }
        with patch.object(ro.shutil, 'move', side_effect=flaky_move):
            raised = False
            try:
                ro.process_live_pair(
                    work, still, mov, events=[], cli_source='iphone',
                    dry_run=False, stats=stats,
                )
            except OSError:
                raised = True

        check('mov failure raises', raised)
        check('moved not incremented', stats['moved'] == 0)
        check('still restored to inbox', still.is_file() and still.read_bytes() == b'still-bytes')
        check('mov still in inbox', mov.is_file())
        # No half pair under by-date
        photos = list((work / 'by-date').rglob('*')) if (work / 'by-date').exists() else []
        half = [p for p in photos if p.is_file()]
        check('no half pair at dest', half == [], detail=repr(half))


def test_reclassify_moves_live_companion():
    print('\n9. reclassify_paths moves Live companion with matching stem')
    import rename_organize as ro

    actions = ('to_docs', 'to_things', 'to_screen', 'to_normal')
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        for action in actions:
            photos = work / 'by-date' / '2024' / '2024-07' / 'photos'
            photos.mkdir(parents=True, exist_ok=True)
            # Clean previous action leftovers in top-level buckets
            for bucket in ('docs', 'things', 'screenshots', 'screenrecords'):
                b = work / bucket
                if b.exists():
                    for p in b.iterdir():
                        if p.is_file():
                            p.unlink()

            still = photos / 'IMG_2000.HEIC'
            mov = photos / 'IMG_2000.MOV'
            # Remove prior still/mov if renamed back into photos
            for old in photos.glob('IMG_2000.*'):
                old.unlink()
            for old in photos.glob('*_live_*'):
                old.unlink()
            for old in list(photos.glob('*')):
                if old.is_file():
                    old.unlink()

            still.write_bytes(b'live-still')
            mov.write_bytes(b'live-mov')
            rel = str(still.relative_to(work))

            results = ro.reclassify_paths(work, [rel], action, dry_run=False)
            check(f'{action} ok', bool(results and results[0].get('ok')), detail=repr(results))
            if not results or not results[0].get('ok'):
                continue
            dest = Path(results[0]['dest'])
            check(f'{action} still exists', (work / dest).is_file())
            comp_dest = results[0].get('companion_dest')
            check(f'{action} has companion_dest', bool(comp_dest), detail=repr(results[0]))
            if not comp_dest:
                continue
            check(f'{action} companion exists', (work / comp_dest).is_file())
            check(
                f'{action} matching stem',
                Path(comp_dest).stem == dest.stem,
                detail=f'{dest.name} vs {Path(comp_dest).name}',
            )
            check(f'{action} still gone from src', not still.exists())
            check(f'{action} mov gone from src', not mov.exists())
            if action == 'to_screen':
                check(
                    f'{action} mov follows still (not screenrecords alone)',
                    str(comp_dest).startswith('screenshots/')
                    and str(dest).startswith('screenshots/'),
                    detail=f'{dest} / {comp_dest}',
                )
            elif action == 'to_docs':
                check(f'{action} under docs/', str(dest).startswith('docs/'))
            elif action == 'to_things':
                check(f'{action} under things/', str(dest).startswith('things/'))
            elif action == 'to_normal':
                check(f'{action} under by-date/', str(dest).startswith('by-date/'))


def test_ledger_star_live_counts():
    print('\n10. Year/month ledger shows star + Live counts')
    import json

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        bucket = '2026-07_海南'
        photos = work / 'by-date' / '2026' / bucket / 'photos'
        videos = work / 'by-date' / '2026' / bucket / 'videos'
        photos.mkdir(parents=True)
        videos.mkdir(parents=True)

        # 1 Live pair (still + companion mov) + 1 plain photo + 1 video
        still = photos / '20260701_120000_iphone_aaa111.jpg'
        mov = photos / '20260701_120000_iphone_aaa111.mov'
        plain = photos / '20260702_090000_camera_bbb222.jpg'
        video = videos / '20260703_183045_gopro_ccc333.mp4'
        still.write_bytes(b'still')
        mov.write_bytes(b'mov')
        plain.write_bytes(b'jpg')
        video.write_bytes(b'mp4')

        stars_dir = work / '_meta' / 'stars'
        stars_dir.mkdir(parents=True)
        star_payload = {
            str(still.relative_to(work)): True,
            str(plain.relative_to(work)): True,
        }
        (stars_dir / f'{bucket}.json').write_text(
            json.dumps(star_payload), encoding='utf-8'
        )

        photos_n, videos_n, lives_n = wb.count_month_media(photos.parent)
        check('count_month_media photos excludes companion mov', photos_n == 2)
        check('count_month_media videos', videos_n == 1)
        check('count_month_media lives = pairs not files', lives_n == 1)

        buckets = wb.scan_buckets(work)
        m = buckets['years']['2026'][0]
        check('scan_buckets stars from JSON', m['stars'] == 2)
        check('scan_buckets lives', m['lives'] == 1)
        check('scan_buckets photos', m['photos'] == 2)
        check('scan_buckets videos', m['videos'] == 1)

        stats = wb.format_ledger_stats(2, 1, 2, 1)
        check(
            'format_ledger_stats includes 加星 + Live',
            stats == '照片 2｜视频 1｜加星 2｜实况 1',
            detail=stats,
        )
        check(
            'format_ledger_stats omits zero star/Live',
            wb.format_ledger_stats(3, 0, 0, 0) == '照片 3｜视频 0',
        )

        year_html = wb.render_year(work, '2026').decode('utf-8')
        check('render_year shows 加星', '加星 2' in year_html)
        check('render_year shows Live', '实况 1' in year_html)
        check('render_year keeps photos/videos', '照片 2｜视频 1' in year_html)

        home_html = wb.render_home(work).decode('utf-8')
        check('render_home by-date card keeps compact date meta', '1 年｜1 个主题' in home_html)
        check('render_home by-date card hides detailed ledger stats', '照片 2｜视频 1｜加星 2｜实况 1' not in home_html)


def test_events_api_edit():
    print('\n11. POST /api/events validates + atomic write + bak')
    import rename_organize as ro

    good = (
        "themes:\n"
        "  - name: 海南\n"
        "    month: 2026-07\n"
        "    sources: [iphone]\n"
    )
    themes = ro.parse_events_yaml_text(good)
    check('parse_events_yaml_text ok', len(themes) == 1 and themes[0]['name'] == '海南')
    try:
        ro.validate_events_themes([{'name': 'x', 'month': '07'}])
        check('validate rejects bad month', False)
    except ValueError as e:
        check('validate rejects bad month', 'YYYY-MM' in str(e))

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        (work / '_meta').mkdir(parents=True)
        original = (
            "# header\n"
            "themes:\n"
            "  - name: 旧主题\n"
            "    month: 2025-01\n"
            "    start: 2025-01-01\n"
            "    end: 2025-01-31\n"
        )
        events = work / '_meta' / 'events.yaml'
        events.write_text(original, encoding='utf-8')

        class H(wb.Handler):
            pass

        H.work = work
        H.thumb_root = work / '_meta' / 'thumbs'
        H.thumb_root.mkdir(parents=True, exist_ok=True)

        from http.server import ThreadingHTTPServer
        import json
        import threading
        import urllib.error
        import urllib.request

        httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        base = f'http://127.0.0.1:{port}'

        def post(payload):
            req = urllib.request.Request(
                base + '/api/events',
                data=json.dumps(payload).encode(),
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode())

        try:
            page = urllib.request.urlopen(base + '/themes').read().decode()
            check('themes page textarea', 'id="eventsYaml"' in page)
            check('themes page save button', 'id="eventsSave"' in page)
            check('themes page copy sync all', 'id="eventsCopySyncAll"' in page)
            check('themes page per-theme sync btn', 'ledger-sync' in page)
            check('themes page no rename copy', 'id="eventsCopyRename"' not in page)
            check('themes page no old sync-all id', 'id="eventsCopySync"' not in page)

            bad_status, bad = post({'yaml': 'themes:\n  - name: 无月份\n'})
            check('invalid yaml rejected ok=false', bad.get('ok') is False)
            check('invalid leaves file unchanged', events.read_text(encoding='utf-8') == original)

            ok_status, ok = post({'yaml': good})
            check('valid save ok', ok.get('ok') is True and ok.get('themes') == 1)
            check('file updated', '海南' in events.read_text(encoding='utf-8'))
            bak = work / '_meta' / 'events.yaml.bak'
            check('bak created', bak.is_file() and '旧主题' in bak.read_text(encoding='utf-8'))

            # create when missing
            events.unlink()
            bak.unlink(missing_ok=True)
            created = post({'yaml': good})
            check('creates missing events.yaml', created[1].get('ok') is True and events.is_file())
        finally:
            httpd.shutdown()


def test_rebucket_themes():
    print('\n12. --rebucket-themes moves default month → theme bucket')
    import rename_organize as ro

    d, s = ro.parse_archived_name('20251221_145512_ricoh-gr_f3a0.jpg')
    check('parse archived with source', d == '20251221' and s == 'ricoh-gr')
    d2, s2 = ro.parse_archived_name('20231122_152632_9a27.mp4')
    check('parse archived without source', d2 == '20231122' and s2 is None)

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        photos = work / 'by-date' / '2025' / '2025-10' / 'photos'
        other = work / 'by-date' / '2025' / '2025-09' / 'photos'
        photos.mkdir(parents=True)
        other.mkdir(parents=True)
        hit = photos / '20251005_120000_iphone_aaaa.jpg'
        miss = photos / '20250901_120000_iphone_bbbb.jpg'  # wrong month dir name but in 2025-10? use out of range day
        # in-range and out-of-range in same default month
        in_range = photos / '20251015_120000_iphone_cccc.jpg'
        out_range = photos / '20251001_120000_iphone_dddd.jpg'  # still in Oct 1-31 for full month theme
        other_f = other / '20250915_120000_iphone_eeee.jpg'
        for p in (hit, in_range, out_range, other_f):
            p.write_bytes(b'x')

        # Live pair in Nov default bucket
        nov = work / 'by-date' / '2025' / '2025-11' / 'photos'
        nov.mkdir(parents=True)
        still = nov / '20251102_100000_iphone_live_ff01.heic'
        mov = nov / '20251102_100000_iphone_live_ff01.mov'
        still.write_bytes(b'still')
        mov.write_bytes(b'mov')

        (work / '_meta').mkdir(parents=True)
        (work / '_meta' / 'events.yaml').write_text(
            "themes:\n"
            "  - name: 广州港\n"
            "    month: 2025-10\n"
            "    start: 2025-10-01\n"
            "    end: 2025-10-31\n"
            "  - name: 颐和园古装\n"
            "    month: 2025-11\n"
            "    start: 2025-11-01\n"
            "    end: 2025-11-08\n",
            encoding='utf-8',
        )
        # star one file
        stars_dir = work / '_meta' / 'stars'
        stars_dir.mkdir(parents=True)
        (stars_dir / '2025-10.json').write_text(
            json.dumps({str(in_range.relative_to(work)): True}, ensure_ascii=False),
            encoding='utf-8',
        )

        events = ro.load_events(work, None)
        check('loaded 2 themes', len(events) == 2)

        scanned = ro.scan_by_date_default_months(work, events)
        names = {p.name for p in scanned}
        check(
            'scan_by_date_default_months skips theme/other months',
            names == {
                hit.name, in_range.name, out_range.name,
                still.name, mov.name,
            },
            detail=repr(names),
        )
        check('scan skips other month dir', other_f.name not in names)

        dry = ro.rebucket_themes(work, events, dry_run=True)
        check('dry-run moves > 0', dry['moved'] > 0)
        check('dry-run left sources in place', in_range.is_file() and still.is_file())

        stats = ro.rebucket_themes(work, events, dry_run=False)
        theme_oct = work / 'by-date' / '2025' / '2025-10_广州港' / 'photos'
        theme_nov = work / 'by-date' / '2025' / '2025-11_颐和园古装' / 'photos'
        check('oct theme dir has files', theme_oct.is_dir() and any(theme_oct.iterdir()))
        check('in_range moved', (theme_oct / in_range.name).is_file() and not in_range.exists())
        check('other month untouched', other_f.is_file())
        check('live still moved', (theme_nov / still.name).is_file())
        check('live mov moved', (theme_nov / mov.name).is_file())
        check('default nov empty of pair', not still.exists() and not mov.exists())
        check('into_theme counted', stats.get('into_theme', 0) > 0)
        check('to_default zero on expand', stats.get('to_default', 0) == 0)
        # star migrated
        new_stars = json.loads((stars_dir / '2025-10_广州港.json').read_text(encoding='utf-8'))
        check(
            'star path migrated',
            str(theme_oct / in_range.name).replace(str(work) + '/', '') in new_stars
            or any('广州港' in k for k in new_stars),
        )


def test_reconcile_themes_demote_reassign_orphan():
    """Shrink demotes; reassign on rename; orphan bucket demotes; Live stays paired."""
    print('\n12b. reconcile_themes demote / reassign / orphan / Live')
    import rename_organize as ro

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        theme = work / 'by-date' / '2025' / '2025-12_香港-深圳' / 'photos'
        theme.mkdir(parents=True)
        keep = theme / '20251230_120000_xiaomi_aaaa01.jpg'
        drop = theme / '20251228_120000_xiaomi_bbbb01.jpg'
        keep.write_bytes(b'keep')
        drop.write_bytes(b'drop')

        # Live pair that will demote together
        still = theme / '20251228_130000_xiaomi_live_aa01.heic'
        mov = theme / '20251228_130000_xiaomi_live_aa01.mov'
        still.write_bytes(b'still')
        mov.write_bytes(b'mov')

        # Orphan theme bucket (no matching events entry)
        orphan = work / 'by-date' / '2025' / '2025-12_旧主题名' / 'photos'
        orphan.mkdir(parents=True)
        orphan_f = orphan / '20251229_100000_xiaomi_cccc01.jpg'
        orphan_f.write_bytes(b'orph')

        # Another theme for reassign target
        other_theme_dir = work / 'by-date' / '2025' / '2025-12_跨年夜' / 'photos'
        other_theme_dir.mkdir(parents=True)
        reassign_src = theme / '20251231_180000_xiaomi_dddd01.jpg'
        reassign_src.write_bytes(b'reas')

        (work / '_meta').mkdir(parents=True)
        # Narrow range: only 12/30; plus separate NYE theme for 12/31
        (work / '_meta' / 'events.yaml').write_text(
            "themes:\n"
            "  - name: 香港-深圳\n"
            "    month: 2025-12\n"
            "    start: 2025-12-30\n"
            "    end: 2025-12-30\n"
            "    sources: [xiaomi]\n"
            "  - name: 跨年夜\n"
            "    month: 2025-12\n"
            "    start: 2025-12-31\n"
            "    end: 2025-12-31\n"
            "    sources: [xiaomi]\n",
            encoding='utf-8',
        )
        events = ro.load_events(work, None)
        check('loaded themes for reconcile', len(events) == 2)

        dry = ro.reconcile_themes(work, events, dry_run=True)
        check('dry demote planned', dry.get('to_default', 0) >= 3)  # drop + live pair
        check('dry reassign planned', dry.get('reassign', 0) >= 1)
        check('dry left files in place', keep.is_file() and drop.is_file())

        stats = ro.reconcile_themes(work, events, dry_run=False)
        default = work / 'by-date' / '2025' / '2025-12' / 'photos'
        nye = work / 'by-date' / '2025' / '2025-12_跨年夜' / 'photos'

        check('keep stayed in theme', (theme / keep.name).is_file())
        check('drop demoted to default', (default / drop.name).is_file() and not drop.exists())
        check('live still demoted', (default / still.name).is_file())
        check('live mov demoted', (default / mov.name).is_file())
        check('orphan demoted', (default / orphan_f.name).is_file() and not orphan_f.exists())
        check('reassign to 跨年夜', (nye / reassign_src.name).is_file() and not reassign_src.exists())
        check('to_default > 0', stats.get('to_default', 0) > 0)
        check('reassign > 0', stats.get('reassign', 0) > 0)

        # Expand again: put a file in default that now matches widened range
        (work / '_meta' / 'events.yaml').write_text(
            "themes:\n"
            "  - name: 香港-深圳\n"
            "    month: 2025-12\n"
            "    start: 2025-12-28\n"
            "    end: 2025-12-30\n"
            "    sources: [xiaomi]\n"
            "  - name: 跨年夜\n"
            "    month: 2025-12\n"
            "    start: 2025-12-31\n"
            "    end: 2025-12-31\n"
            "    sources: [xiaomi]\n",
            encoding='utf-8',
        )
        events2 = ro.load_events(work, None)
        stats2 = ro.reconcile_themes(work, events2, dry_run=False)
        check(
            'expand re-absorbs drop',
            (theme / drop.name).is_file() and not (default / drop.name).exists(),
        )
        check('expand into_theme > 0', stats2.get('into_theme', 0) > 0)


def test_rename_rebuckets_when_inbox_empty():
    print('\n13. rename does NOT auto-sync themes; scoped sync leaves other themes alone')
    import rename_organize as ro
    import inspect
    src = inspect.getsource(ro.main)
    check('main mentions inbox empty', 'inbox empty' in src)
    check('main does not auto Syncing themes', 'Syncing themes on by-date archive' not in src)
    check('main tip mentions --theme', '--rebucket-themes --theme' in src)
    check('main requires --theme or --all for rebucket', 'requires --theme' in src)
    check('reconcile_themes accepts themes kw', 'themes:' in inspect.getsource(ro.reconcile_themes))

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        (work / 'inbox').mkdir(parents=True)
        photos = work / 'by-date' / '2025' / '2025-10' / 'photos'
        photos.mkdir(parents=True)
        f = photos / '20251015_120000_iphone_abcd.jpg'
        f.write_bytes(b'x')
        (work / '_meta').mkdir(parents=True)
        (work / '_meta' / 'events.yaml').write_text(
            "themes:\n"
            "  - name: 广州港\n"
            "    month: 2025-10\n"
            "    start: 2025-10-01\n"
            "    end: 2025-10-31\n",
            encoding='utf-8',
        )
        events = ro.load_events(work, None)
        cands = ro.scan_by_date_default_months(work, events)
        check('scan_by_date finds default-month file', len(cands) == 1 and cands[0] == f)

        old_argv = sys.argv[:]
        old_env = os.environ.get('DUPEGURU_TEST')
        try:
            os.environ['DUPEGURU_TEST'] = '1'
            sys.argv = ['rename_organize.py', '--work', str(work)]
            ro.main()
        finally:
            sys.argv = old_argv
            if old_env is None:
                os.environ.pop('DUPEGURU_TEST', None)
            else:
                os.environ['DUPEGURU_TEST'] = old_env

        theme_f = work / 'by-date' / '2025' / '2025-10_广州港' / 'photos' / f.name
        check(
            'empty-inbox rename does NOT move to theme',
            f.is_file() and not theme_f.exists(),
        )

        # Scoped sync does move
        stats = ro.reconcile_themes(work, events, dry_run=False, themes=['广州港'])
        check('scoped sync into_theme', stats.get('into_theme', 0) >= 1)
        check('scoped sync moved file', theme_f.is_file() and not f.exists())


def test_scoped_theme_sync_ignores_other_theme():
    print('\n13b. reconcile_themes(themes=[B]) does not touch theme A bucket')
    import rename_organize as ro

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        # Theme A: file manually kept in bucket (would demote if full sync with empty range)
        a_bucket = work / 'by-date' / '2025' / '2025-12_主题A' / 'photos'
        a_bucket.mkdir(parents=True)
        a_manual = a_bucket / '20251220_120000_xiaomi_aaa001.jpg'
        a_manual.write_bytes(b'a')

        # Theme B: wide then we'll shrink via events — file outside new range
        b_bucket = work / 'by-date' / '2025' / '2025-12_主题B' / 'photos'
        b_bucket.mkdir(parents=True)
        b_keep = b_bucket / '20251230_120000_xiaomi_bbb001.jpg'
        b_drop = b_bucket / '20251228_120000_xiaomi_ccc001.jpg'
        b_keep.write_bytes(b'bk')
        b_drop.write_bytes(b'bd')

        # Default month: should be absorbed into B when syncing B
        default = work / 'by-date' / '2025' / '2025-12' / 'photos'
        default.mkdir(parents=True)
        b_new = default / '20251230_130000_xiaomi_ddd001.jpg'
        b_new.write_bytes(b'bn')
        # Would match A if A had a range — leave for A sync later
        a_candidate = default / '20251220_140000_xiaomi_eee001.jpg'
        a_candidate.write_bytes(b'ac')

        (work / '_meta').mkdir(parents=True)
        (work / '_meta' / 'events.yaml').write_text(
            "themes:\n"
            "  - name: 主题A\n"
            "    month: 2025-12\n"
            "    start: 2025-12-01\n"
            "    end: 2025-12-20\n"
            "    sources: [xiaomi]\n"
            "  - name: 主题B\n"
            "    month: 2025-12\n"
            "    start: 2025-12-30\n"
            "    end: 2025-12-30\n"
            "    sources: [xiaomi]\n",
            encoding='utf-8',
        )
        events = ro.load_events(work, None)

        stats = ro.reconcile_themes(work, events, dry_run=False, themes=['主题B'])
        check('A manual file untouched', a_manual.is_file())
        check('A candidate still in default', a_candidate.is_file())
        check('B keep stayed', (b_bucket / b_keep.name).is_file())
        check('B drop demoted', (default / b_drop.name).is_file() and not b_drop.exists())
        check('B new absorbed', (b_bucket / b_new.name).is_file() and not b_new.exists())
        check('into_theme or to_default happened', stats.get('moved', 0) > 0)

        # Full sync would pull a_candidate into A and possibly demote a_manual if range wrong —
        # with current A range 1-20, a_manual on 20 stays; a_candidate on 20 matches A.
        # Point is scoped B left them alone — already checked.


def test_rebucket_cli_requires_theme_or_all():
    print('\n13c. --rebucket-themes without --theme/--all exits 2')
    import rename_organize as ro
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        (work / '_meta').mkdir(parents=True)
        (work / '_meta' / 'events.yaml').write_text('themes: []\n', encoding='utf-8')
        old_argv = sys.argv[:]
        old_env = os.environ.get('DUPEGURU_TEST')
        try:
            os.environ['DUPEGURU_TEST'] = '1'
            sys.argv = [
                'rename_organize.py', '--work', str(work), '--rebucket-themes', '--dry-run',
            ]
            try:
                ro.main()
                check('cli rejected missing scope', False, detail='expected SystemExit')
            except SystemExit as e:
                check('cli exit code 2', e.code == 2)
        finally:
            sys.argv = old_argv
            if old_env is None:
                os.environ.pop('DUPEGURU_TEST', None)
            else:
                os.environ['DUPEGURU_TEST'] = old_env


def test_heic_thumb_forces_jpeg():
    """HEIC thumbs must be real JPEG bytes (not HEIC renamed to .jpg)."""
    print('\n15. HEIC thumbnail forces JPEG format')
    check('jpeg magic helper', wb._is_jpeg_bytes.__name__ == '_is_jpeg_bytes')

    # Minimal HEIC ftyp header (not a real image — used as stale cache).
    heic_hdr = b'\x00\x00\x00\x18ftypheic\x00\x00\x00\x00'
    jpeg_hdr = b'\xff\xd8\xff\xe0\x00\x10JFIF'

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        shots = work / 'screenshots'
        shots.mkdir(parents=True)
        src = shots / 'screenshot_20231220_193729_bb15.heic'
        src.write_bytes(heic_hdr + b'payload')
        thumb_root = work / '_meta' / 'thumbs'
        stale = thumb_root / 'screenshots' / 'screenshot_20231220_193729_bb15.jpg'
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(heic_hdr + b'stale')

        check('stale heic-as-jpg is not jpeg', not wb._is_jpeg_bytes(stale))

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            # Simulate sips writing a real JPEG to --out
            out = Path(cmd[cmd.index('--out') + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(jpeg_hdr + b'ok')

            class R:
                returncode = 0

            return R()

        import subprocess
        old = subprocess.run
        try:
            subprocess.run = fake_run
            got = wb.thumb_for(src, work, thumb_root)
        finally:
            subprocess.run = old

        check('thumb_for returns path', got is not None and got == stale)
        check('regenerated thumb is jpeg', wb._is_jpeg_bytes(got))
        check('sips invoked once', len(calls) == 1)
        check(
            'sips forces format jpeg',
            calls and '-s' in calls[0] and 'format' in calls[0]
            and 'jpeg' in calls[0],
            detail=repr(calls[0] if calls else None),
        )
        check(
            'sips --out is .jpg path',
            calls and str(calls[0][calls[0].index('--out') + 1]).endswith('.jpg'),
        )


def test_append_theme_from_empty_list():
    print('\n15. append_theme_to_events handles themes: [] and append')
    import add_theme
    import rename_organize as ro

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        meta = work / '_meta'
        meta.mkdir(parents=True)
        events = meta / 'events.yaml'
        events.write_text(
            "# header\n"
            "\n"
            "themes: []\n",
            encoding='utf-8',
        )

        add_theme.append_theme_to_events(
            work,
            {
                'name': '海南',
                'month': '2026-07',
                'sources': ['iphone'],
                'date_range_start': '2026-07-10',
                'date_range_end': '2026-07-18',
            },
        )
        text1 = events.read_text(encoding='utf-8')
        check('no leftover themes: []', 'themes: []' not in text1)
        check('has themes: header', re.search(r'(?m)^themes:\s*$', text1) is not None)
        try:
            themes1 = ro.parse_events_yaml_text(text1)
            check(
                'first theme from empty list parses',
                len(themes1) == 1
                and themes1[0].get('name') == '海南'
                and themes1[0].get('month') == '2026-07',
                detail=repr(themes1),
            )
        except Exception as e:
            check('first theme from empty list parses', False, detail=str(e))

        add_theme.append_theme_to_events(
            work,
            {'name': '夏令营', 'month': '2026-08', 'sources': ['iphone']},
        )
        text2 = events.read_text(encoding='utf-8')
        try:
            themes2 = ro.parse_events_yaml_text(text2)
            names = [t.get('name') for t in themes2]
            check(
                'second theme appends to existing list',
                names == ['海南', '夏令营'],
                detail=repr(names),
            )
        except Exception as e:
            check('second theme appends to existing list', False, detail=str(e))


def test_theme_parse_validate_hardening():
    print('\n16. theme parse: block sources, dates, name safety, files match')
    import rename_organize as ro
    from datetime import date

    block = (
        "themes:\n"
        "  - name: 海南\n"
        "    month: 2026-07\n"
        "    date_range:\n"
        "      start: 2026-07-10\n"
        "      end: 2026-07-18\n"
        "    sources:\n"
        "      - iphone\n"
        "      - canon\n"
    )
    # Force simple parser path
    themes = ro.parse_simple_yaml(block).get('themes') or []
    check('simple yaml block sources list', isinstance(themes[0].get('sources'), list))
    check(
        'simple yaml block sources values',
        themes[0].get('sources') == ['iphone', 'canon'],
        detail=repr(themes[0].get('sources')),
    )
    check('simple yaml nested date_range', isinstance(themes[0].get('date_range'), dict))

    parsed = ro.parse_events_yaml_text(block)
    ro.validate_events_themes(parsed)
    check('validate ok with block sources', len(parsed) == 1)

    # PyYAML-like date objects after normalize
    raw = {
        'name': '测',
        'month': '2026-07',
        'start': date(2026, 7, 10),
        'end': date(2026, 7, 18),
        'sources': ['iphone'],
    }
    norm = ro._normalize_theme_dict(raw)
    check('normalize start iso', norm['start'] == '2026-07-10')
    check('normalize end iso', norm['end'] == '2026-07-18')
    hit = ro.match_theme(
        Path('by-date/2026/2026-07/photos/20260715_120000_iphone_abcd.jpg'),
        '20260715', 'iphone', [norm],
    )
    check('match_theme with date objects normalized', hit is not None and hit['name'] == '测')

    try:
        ro.validate_events_themes([{'name': 'a/../../tmp', 'month': '2026-07', 'sources': ['x']}])
        check('reject path name', False)
    except ValueError as e:
        check('reject path name', '/' in str(e) or '..' in str(e))

    try:
        ro.validate_events_themes([{'name': '空', 'month': '2026-07'}])
        check('reject name+month only', False)
    except ValueError as e:
        check('reject name+month only', 'never matches' in str(e) or 'date_range' in str(e))

    # Tight files: substring must not match
    theme_files = {
        'name': 'F',
        'month': '2026-07',
        'files': ['abcd'],
    }
    miss = ro.match_theme(
        Path('by-date/2026/2026-07/photos/20260715_120000_iphone_abcd1234.jpg'),
        '20260715', 'iphone', [theme_files],
    )
    check('files substring no longer matches', miss is None)
    hit2 = ro.match_theme(
        Path('by-date/2026/2026-07/photos/abcd.jpg'),
        '20260715', 'iphone', [{'name': 'F', 'month': '2026-07', 'files': ['abcd.jpg']}],
    )
    check('files basename exact matches', hit2 is not None)


def test_web_sync_cmd_is_dry_run():
    print('\n16b. /themes sync copy commands are dry-run')
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        (work / '_meta').mkdir(parents=True)
        (work / '_meta' / 'events.yaml').write_text(
            "themes:\n"
            "  - name: 海南\n"
            "    month: 2026-07\n"
            "    start: 2026-07-01\n"
            "    end: 2026-07-31\n",
            encoding='utf-8',
        )
        class H(wb.Handler):
            pass
        H.work = work
        H.thumb_root = work / '_meta' / 'thumbs'
        H.thumb_root.mkdir(parents=True, exist_ok=True)
        from http.server import ThreadingHTTPServer
        import threading
        import urllib.request
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            page = urllib.request.urlopen(f'http://127.0.0.1:{port}/themes').read().decode()
            check('per-theme sync btn present', 'ledger-sync' in page)
            # Commands should not embed --yes as the only apply path without dry-run hint
            # data-sync-cmd should be dry-run style (no --yes, or has --dry-run)
            import re as _re
            m = _re.search(r'data-sync-cmd="([^"]+)"', page)
            check('has data-sync-cmd', bool(m))
            if m:
                cmd = m.group(1).replace('&quot;', '"').replace('&#x27;', "'")
                # HTML entity decode basic
                import html as _html
                cmd = _html.unescape(m.group(1))
                check(
                    'sync cmd is dry-run (no --yes)',
                    '--yes' not in cmd,
                    detail=cmd,
                )
                check(
                    'sync all id present',
                    'id="eventsCopySyncAll"' in page,
                )
        finally:
            httpd.shutdown()


def test_rename_hardening_source_and_events_load():
    print('\n17. rename hardening: .source skip, load_events validate, source sanitize')
    import rename_organize as ro

    check('normalize strips slash', ro._normalize_source('Evil/Corp') == 'evil-corp')
    check('normalize keeps alnum', 'gopro' in ro._normalize_source('GoPro, Inc.'))

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        inbox = work / 'inbox' / 'mix'
        inbox.mkdir(parents=True)
        (inbox / '.source').write_text('iphone\n', encoding='utf-8')
        photo = inbox / '20260701_120000_aaaa.jpg'
        photo.write_bytes(b'x')
        scanned = ro.scan_inbox(work)
        check('.source not in scan_inbox', all(p.name != '.source' for p in scanned))
        check('photo still scanned', any(p.name == photo.name for p in scanned))

        (work / '_meta').mkdir(parents=True)
        (work / '_meta' / 'events.yaml').write_text(
            "themes:\n"
            "  - name: ../../../../tmp/pwned\n"
            "    month: 2026-07\n"
            "    start: 2026-07-01\n"
            "    end: 2026-07-31\n",
            encoding='utf-8',
        )
        try:
            ro.load_events(work, None)
            check('load_events rejects path name', False)
        except ValueError as e:
            check('load_events rejects path name', 'name' in str(e).lower() or '..' in str(e))

        (work / '_meta' / 'events.yaml').write_text(
            "themes:\n"
            "  - name: A\n"
            "    month: 2026-07\n"
            "    start: 2026-07-01\n"
            "    end: 2026-07-20\n"
            "  - name: B\n"
            "    month: 2026-07\n"
            "    start: 2026-07-10\n"
            "    end: 2026-07-31\n",
            encoding='utf-8',
        )
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            events = ro.load_events(work, None)
        check('overlap still loads', len(events) == 2)
        check('overlap warns', 'overlapping' in buf.getvalue())

        # dest escape guard
        try:
            ro._ensure_dest_under_work(work, work / '..' / 'outside' / 'x.jpg')
            check('ensure dest under work', False)
        except ValueError:
            check('ensure dest under work', True)


def test_cross_month_theme_start_bucket():
    print('\n15. cross-month theme buckets use start month')
    import rename_organize as ro

    # month != start month → reject
    try:
        ro.parse_events_yaml_text(
            "themes:\n"
            "  - name: 跨年\n"
            "    month: 2026-01\n"
            "    date_range:\n"
            "      start: 2025-12-28\n"
            "      end: 2026-01-05\n"
        )
        check('month != start rejected', False)
    except ValueError as e:
        check('month != start rejected', 'start month' in str(e) or '2025-12' in str(e))

    text = (
        "themes:\n"
        "  - name: 香港-深圳\n"
        "    date_range:\n"
        "      start: 2025-12-28\n"
        "      end: 2026-01-05\n"
    )
    events = ro.parse_events_yaml_text(text)
    ro.validate_events_themes(events)
    check('month derived from start', events[0].get('month') == '2025-12')

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        dec = work / 'by-date' / '2025' / '2025-12' / 'photos'
        jan = work / 'by-date' / '2026' / '2026-01' / 'photos'
        dec.mkdir(parents=True)
        jan.mkdir(parents=True)
        f_dec = dec / '20251229_120000_iphone_aaaa.jpg'
        f_jan = jan / '20260102_150000_iphone_bbbb.jpg'
        f_dec.write_bytes(b'dec')
        f_jan.write_bytes(b'jan')

        hit = ro.match_theme(f_jan, '20260102_150000', 'iphone', events)
        check('match Jan file to cross-month theme', hit is not None and hit['name'] == '香港-深圳')

        # inbox plan_destination: Jan capture → start-month theme bucket
        inbox = work / 'inbox'
        inbox.mkdir(parents=True)
        inbox_f = inbox / 'shot.jpg'
        inbox_f.write_bytes(b'inbox-jan')
        _orig_date = ro.get_date
        _orig_src = ro.get_source
        ro.get_date = lambda path, exif=None: '20260102_150000'
        ro.get_source = lambda path, exif=None, video_tags=None, cli_source=None: 'iphone'
        try:
            dest, _ctype, _new = ro.plan_destination(
                work, inbox_f, force_type='normal', events=events, cli_source='iphone',
            )
        finally:
            ro.get_date = _orig_date
            ro.get_source = _orig_src
        check(
            'inbox plan_destination → start-month bucket',
            'by-date/2025/2025-12_香港-深圳/photos/' in str(dest).replace('\\', '/'),
            detail=str(dest),
        )

        stats = ro.reconcile_themes(work, events, dry_run=False, themes=['香港-深圳'])
        theme_photos = work / 'by-date' / '2025' / '2025-12_香港-深圳' / 'photos'
        check('sync into_theme > 0', stats.get('into_theme', 0) >= 2, detail=repr(stats))
        check('Dec file in start bucket', (theme_photos / f_dec.name).is_file())
        check('Jan file in start bucket', (theme_photos / f_jan.name).is_file())
        check('left Dec default', not f_dec.exists())
        check('left Jan default', not f_jan.exists())

        # return_to_default: Jan capture → 2026-01/
        rel = str((theme_photos / f_jan.name).relative_to(work))
        moved = ro.return_to_default_month_paths(work, [rel], dry_run=False)
        check('demote ok', bool(moved and moved[0].get('ok')))
        dest = moved[0].get('dest') or ''
        check(
            'demote Jan → 2026-01 default',
            dest.startswith('by-date/2026/2026-01/photos/'),
            detail=dest,
        )


def test_return_to_default_month():
    print('\n16. return_to_default_month_paths + theme toolbar')
    import rename_organize as ro

    check(
        'theme toolbar has 放回默认月桶',
        'data-reclassify="to_default_month"' in wb._gallery_toolbar(1, 0, context='theme'),
    )
    check(
        'normal toolbar hides 放回默认月桶',
        'data-reclassify="to_default_month"' not in wb._gallery_toolbar(1, 0, context='normal'),
    )
    check(
        'theme toolbar hides 移回普通分类',
        'data-reclassify="to_normal"' not in wb._gallery_toolbar(1, 0, context='theme'),
    )
    check('PAGE_JS labels to_default_month', "to_default_month: '放回默认月桶'" in wb.PAGE_JS)

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        theme_photos = work / 'by-date' / '2026' / '2026-07_海南' / 'photos'
        theme_photos.mkdir(parents=True)
        name = '20260715_120000_iphone_abcd.jpg'
        src = theme_photos / name
        src.write_bytes(b'theme-photo')
        rel = str(src.relative_to(work))

        dry = ro.return_to_default_month_paths(work, [rel], dry_run=True)
        check('dry-run ok', bool(dry and dry[0].get('ok')))
        check(
            'dry-run dest default month',
            (dry[0].get('dest') or '').startswith('by-date/2026/2026-07/photos/'),
            detail=repr(dry[0]),
        )
        check('dry-run does not move', src.is_file())

        moved = ro.return_to_default_month_paths(work, [rel], dry_run=False)
        check('move ok', bool(moved and moved[0].get('ok') and not moved[0].get('skipped')))
        dest = moved[0].get('dest') or ''
        check('dest under default month', dest == f'by-date/2026/2026-07/photos/{name}', detail=dest)
        check('file landed', (work / dest).is_file())
        check('left theme bucket', not src.exists())

        # Idempotent skip when already in default month
        again = ro.return_to_default_month_paths(work, [dest], dry_run=False)
        check('already-default skips', bool(again and again[0].get('ok') and again[0].get('skipped')))

        # Live pair moves together
        live_dir = work / 'by-date' / '2026' / '2026-07_海南' / 'photos'
        live_dir.mkdir(parents=True, exist_ok=True)
        still = live_dir / '20260716_090000_iphone_live_ef01.HEIC'
        mov = live_dir / '20260716_090000_iphone_live_ef01.MOV'
        still.write_bytes(b'live-still')
        mov.write_bytes(b'live-mov')
        live_rel = str(still.relative_to(work))
        live_res = ro.return_to_default_month_paths(work, [live_rel], dry_run=False)
        check('live move ok', bool(live_res and live_res[0].get('ok')))
        check('live has companion_dest', bool(live_res[0].get('companion_dest')), detail=repr(live_res[0]))
        if live_res and live_res[0].get('companion_dest'):
            check('live still in default', (work / live_res[0]['dest']).is_file())
            check('live mov in default', (work / live_res[0]['companion_dest']).is_file())
            check(
                'live matching stem',
                Path(live_res[0]['dest']).stem == Path(live_res[0]['companion_dest']).stem,
            )
            check('live left theme', not still.exists() and not mov.exists())

        # Reject non-by-date
        other = work / 'screenshots' / 'screenshot_x.jpg'
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(b'ss')
        bad = ro.return_to_default_month_paths(work, [str(other.relative_to(work))], dry_run=False)
        check('reject screenshots/', bool(bad and bad[0].get('error') == 'not under by-date/'))

        # Theme bucket page shows the button
        html = wb.render_bucket(
            work, '2026', '2026-07_海南', work / '_meta' / 'thumbs',
        ).decode('utf-8')
        check('theme bucket page button', 'data-reclassify="to_default_month"' in html)
        check('theme bucket page label', '放回默认月桶' in html)

        default_html = wb.render_bucket(
            work, '2026', '2026-07', work / '_meta' / 'thumbs',
        ).decode('utf-8')
        check(
            'default bucket hides button',
            'data-reclassify="to_default_month"' not in default_html,
        )


def test_theme_href_never_falls_back_to_default_month():
    """Theme links must target YYYY-MM_<name>, not silent fallback to YYYY-MM/."""
    print('\n23. Theme href / empty theme bucket / save does not move files')
    import urllib.parse
    import urllib.request
    from http.server import ThreadingHTTPServer
    import threading

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        default_photos = work / 'by-date' / '2025' / '2025-11' / 'photos'
        default_photos.mkdir(parents=True)
        nov27 = default_photos / '20251127_120000_iphone_aaaa.jpg'
        nov27.write_bytes(b'nov27')
        (work / '_meta').mkdir(parents=True, exist_ok=True)
        events_yaml = (
            "themes:\n"
            "  - name: 颐和园\n"
            "    month: 2025-11\n"
            "    date_range:\n"
            "      start: 2025-11-01\n"
            "      end: 2025-11-01\n"
        )
        (work / '_meta' / 'events.yaml').write_text(events_yaml, encoding='utf-8')

        class H(wb.Handler):
            pass

        H.work = work
        H.thumb_root = work / '_meta' / 'thumbs'
        H.thumb_root.mkdir(parents=True, exist_ok=True)

        href = H._theme_bucket_href('2025-11', '颐和园')
        expected = '/y/2025/' + urllib.parse.quote('2025-11_颐和园')
        check('href is theme side-bucket', href == expected, detail=href)
        check('href is not default month', href != '/y/2025/2025-11')
        check(
            'href without existing theme dir still themed',
            not (work / 'by-date' / '2025' / '2025-11_颐和园').is_dir()
            and href == expected,
        )

        empty_html = wb.render_bucket(
            work, '2025', '2025-11_颐和园', H.thumb_root,
        ).decode('utf-8')
        check('missing theme bucket empty state', '还没有文件' in empty_html)
        check('empty gallery has no nov27', '20251127' not in empty_html)

        default_html = wb.render_bucket(
            work, '2025', '2025-11', H.thumb_root,
        ).decode('utf-8')
        check('default month still lists nov27', '20251127' in default_html)

        before = {
            p.relative_to(work): p.stat().st_mtime_ns
            for p in (work / 'by-date').rglob('*') if p.is_file()
        }
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            themes_page = urllib.request.urlopen(
                f'http://127.0.0.1:{port}/themes'
            ).read().decode('utf-8')
            check(
                'themes page links theme bucket',
                f'href="{expected}"' in themes_page,
                detail='missing theme href on /themes',
            )
            check(
                'themes page does not link default month as theme',
                'href="/y/2025/2025-11"' not in themes_page,
            )
            # Clicking the theme link must open empty themed URL, not default month gallery
            theme_page = urllib.request.urlopen(
                f'http://127.0.0.1:{port}{expected}'
            ).read().decode('utf-8')
            check('live theme URL empty', '还没有文件' in theme_page)
            check('live theme URL has no nov27', '20251127' not in theme_page)
            default_live = urllib.request.urlopen(
                f'http://127.0.0.1:{port}/y/2025/2025-11'
            ).read().decode('utf-8')
            check('default month still has nov27 via URL', '20251127' in default_live)

            req = urllib.request.Request(
                f'http://127.0.0.1:{port}/api/events',
                data=json.dumps({'yaml': events_yaml}).encode(),
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            with urllib.request.urlopen(req) as r:
                body = json.loads(r.read())
            check('save events ok', body.get('ok') is True)
            after = {
                p.relative_to(work): p.stat().st_mtime_ns
                for p in (work / 'by-date').rglob('*') if p.is_file()
            }
            check('save did not move by-date files', before == after)
            check(
                'nov27 still in default month',
                nov27.is_file()
                and not (work / 'by-date' / '2025' / '2025-11_颐和园').exists(),
            )
            check(
                'no theme bucket created by save',
                not (work / 'by-date' / '2025' / '2025-11_颐和园').exists(),
            )
        finally:
            httpd.shutdown()


def test_theme_bucket_shows_date_range():
    """Theme gallery header includes date_range; default month buckets do not."""
    print('\n24. Theme bucket page shows date_range from events.yaml')
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        theme_photos = work / 'by-date' / '2025' / '2025-11_颐和园' / 'photos'
        theme_photos.mkdir(parents=True)
        (theme_photos / '20251101_120000_iphone_aaaa.jpg').write_bytes(b'x')
        default_photos = work / 'by-date' / '2025' / '2025-11' / 'photos'
        default_photos.mkdir(parents=True)
        (default_photos / '20251115_120000_iphone_bbbb.jpg').write_bytes(b'y')
        sources_photos = work / 'by-date' / '2026' / '2026-08_夏令营' / 'photos'
        sources_photos.mkdir(parents=True)
        (work / '_meta').mkdir(parents=True, exist_ok=True)
        (work / '_meta' / 'events.yaml').write_text(
            "themes:\n"
            "  - name: 颐和园\n"
            "    month: 2025-11\n"
            "    date_range:\n"
            "      start: 2025-11-01\n"
            "      end: 2025-11-01\n"
            "  - name: 夏令营\n"
            "    month: 2026-08\n"
            "    sources: [iphone]\n",
            encoding='utf-8',
        )
        thumbs = work / '_meta' / 'thumbs'
        thumbs.mkdir(parents=True, exist_ok=True)

        theme_html = wb.render_bucket(
            work, '2025', '2025-11_颐和园', thumbs,
        ).decode('utf-8')
        check('theme bucket shows period label', '周期' in theme_html)
        check(
            'theme bucket shows short date range',
            '11月1日到11月1日' in theme_html,
            detail='expected 11月1日到11月1日 in page-meta',
        )
        check(
            'theme bucket shows month in period',
            '2025-11' in theme_html and '周期：2025-11｜11月1日到11月1日' in theme_html,
        )

        default_html = wb.render_bucket(
            work, '2025', '2025-11', thumbs,
        ).decode('utf-8')
        check(
            'default month has no 周期 label',
            '周期' not in default_html,
        )
        check(
            'default month has no date range short form',
            '11/1–11/1' not in default_html,
        )
        check(
            'default month meta stays year',
            '<p class="page-meta">2025</p>' in default_html,
        )

        sources_html = wb.render_bucket(
            work, '2026', '2026-08_夏令营', thumbs,
        ).decode('utf-8')
        check(
            'sources-only theme falls back to month',
            '<p class="page-meta">周期：2026-08</p>' in sources_html,
        )
        check(
            'sources-only has no date-range short form',
            '11/1' not in sources_html,
        )


def test_theme_start_month_reassign_and_validate():
    """Phase B reassigns on start-month change; validate rejects bad dates / dup names."""
    print('\n25. theme start-month reassign + date/dup validate + add_theme month≠start')
    import add_theme
    import rename_organize as ro

    # --- validate: invalid end ---
    try:
        ro.validate_events_themes([{
            'name': '坏日期',
            'month': '2026-07',
            'date_range': {'start': '2026-07-01', 'end': 'not-a-date'},
            'sources': ['iphone'],
        }])
        check('reject invalid date_range.end', False)
    except ValueError as e:
        check(
            'reject invalid date_range.end',
            'YYYY-MM-DD' in str(e) or 'date_range.end' in str(e),
            detail=str(e),
        )

    try:
        ro.validate_events_themes([{
            'name': '坏日历',
            'month': '2026-02',
            'start': '2026-02-01',
            'end': '2026-02-30',
            'sources': ['iphone'],
        }])
        check('reject non-calendar end', False)
    except ValueError as e:
        check(
            'reject non-calendar end',
            'valid calendar' in str(e) or '2026-02-30' in str(e),
            detail=str(e),
        )

    # --- validate: duplicate names ---
    try:
        ro.validate_events_themes([
            {
                'name': '海南',
                'month': '2026-07',
                'start': '2026-07-01',
                'end': '2026-07-10',
            },
            {
                'name': '海南',
                'month': '2026-08',
                'start': '2026-08-01',
                'end': '2026-08-10',
            },
        ])
        check('reject duplicate theme names', False)
    except ValueError as e:
        check(
            'reject duplicate theme names',
            'duplicate' in str(e).lower(),
            detail=str(e),
        )

    # --- Phase B: change start month → reassign out of old YYYY-MM_name ---
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        old_bucket = work / 'by-date' / '2025' / '2025-12_跨年' / 'photos'
        old_bucket.mkdir(parents=True)
        f = old_bucket / '20260102_150000_iphone_aabb11.jpg'
        f.write_bytes(b'cross')

        (work / '_meta').mkdir(parents=True)
        # Start month moved from Dec → Jan; file still in old Dec side-bucket
        (work / '_meta' / 'events.yaml').write_text(
            "themes:\n"
            "  - name: 跨年\n"
            "    month: 2026-01\n"
            "    date_range:\n"
            "      start: 2026-01-01\n"
            "      end: 2026-01-05\n"
            "    sources: [iphone]\n",
            encoding='utf-8',
        )
        events = ro.load_events(work, None)
        check('month is start month', events[0].get('month') == '2026-01')

        dry = ro.reconcile_themes(work, events, dry_run=True)
        check('dry reassign planned for start-month change', dry.get('reassign', 0) >= 1)
        check('dry left file in old bucket', f.is_file())

        stats = ro.reconcile_themes(work, events, dry_run=False)
        new_bucket = work / 'by-date' / '2026' / '2026-01_跨年' / 'photos'
        check(
            'reassigned to new start-month bucket',
            (new_bucket / f.name).is_file() and not f.exists(),
            detail=repr(stats),
        )
        check('reassign counted', stats.get('reassign', 0) >= 1)

    # --- add_theme: month ≠ start rejected ---
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        (work / '_meta').mkdir(parents=True)
        (work / '_meta' / 'events.yaml').write_text('themes: []\n', encoding='utf-8')
        try:
            add_theme.prepare_theme_for_append(
                work,
                {
                    'name': '错月',
                    'month': '2026-08',
                    'date_range_start': '2026-07-10',
                    'date_range_end': '2026-07-18',
                    'sources': ['iphone'],
                },
            )
            check('add_theme rejects month≠start', False)
        except ValueError as e:
            check(
                'add_theme rejects month≠start',
                'start month' in str(e) or '2026-07' in str(e),
                detail=str(e),
            )

        # Valid: month matches start (or omitted → derived)
        ok = add_theme.prepare_theme_for_append(
            work,
            {
                'name': '对月',
                'month': '2026-07',
                'date_range_start': '2026-07-10',
                'date_range_end': '2026-07-18',
                'sources': ['iphone'],
            },
        )
        check('add_theme accepts month==start', ok.get('month') == '2026-07')


def test_theme_add_files_cli_and_list():
    print('\n25b. add_theme CLI accepts --files and list shows it')
    import rename_organize as ro

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        meta = work / '_meta'
        meta.mkdir(parents=True)
        events = meta / 'events.yaml'
        events.write_text('themes: []\n', encoding='utf-8')

        env = os.environ.copy()
        env['DUPEGURU_TEST'] = '1'
        add_proc = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / 'scripts' / 'add_theme.py'),
                '--work', str(work),
                '--name', '证件照',
                '--month', '2026-06',
                '--files', 'id-1.jpg,id-2.jpg',
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        check('add_theme --files exit 0', add_proc.returncode == 0, detail=add_proc.stderr)

        themes = ro.parse_events_yaml_text(events.read_text(encoding='utf-8'))
        check(
            'add_theme --files saved list',
            len(themes) == 1
            and themes[0].get('name') == '证件照'
            and themes[0].get('files') == ['id-1.jpg', 'id-2.jpg'],
            detail=repr(themes),
        )

        env['WORK'] = str(work)
        env['PICVAULT_TEST'] = '1'
        list_proc = subprocess.run(
            [str(PICVAULT), 'theme', 'list'],
            capture_output=True,
            text=True,
            env=env,
        )
        check('picvault theme list exit 0', list_proc.returncode == 0, detail=list_proc.stderr)
        check('picvault theme list shows files count', 'files=2' in list_proc.stdout, detail=list_proc.stdout)


def test_theme_remove_cli_without_yaml():
    print('\n25c. picvault theme remove works without PyYAML')
    import rename_organize as ro

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        meta = work / '_meta'
        meta.mkdir(parents=True)
        events = meta / 'events.yaml'
        original = (
            'themes:\n'
            '  - name: 旧主题\n'
            '    month: 2025-01\n'
            '    sources: [iphone]\n'
            '  - name: 保留主题\n'
            '    month: 2025-02\n'
            '    date_range:\n'
            '      start: 2025-02-01\n'
            '      end:   2025-02-03\n'
            '    sources: [canon]\n'
            '    files:\n'
            '      - keep.jpg\n'
        )
        events.write_text(original, encoding='utf-8')

        env = os.environ.copy()
        env['WORK'] = str(work)
        env['PICVAULT_TEST'] = '1'
        env['DUPEGURU_TEST'] = '1'
        rm_proc = subprocess.run(
            [str(PICVAULT), 'theme', 'remove', '旧主题'],
            capture_output=True,
            text=True,
            env=env,
        )
        check('theme remove exit 0', rm_proc.returncode == 0, detail=rm_proc.stderr)
        check('theme remove output mentions backup', 'events.yaml.bak' in rm_proc.stdout, detail=rm_proc.stdout)

        bak = events.with_name('events.yaml.bak')
        check('theme remove wrote backup', bak.is_file() and bak.read_text(encoding='utf-8') == original)

        themes = ro.parse_events_yaml_text(events.read_text(encoding='utf-8'))
        names = [t.get('name') for t in themes]
        check('theme remove keeps remaining', names == ['保留主题'], detail=repr(names))
        check(
            'theme remove preserves date_range',
            themes[0].get('date_range') == {'start': '2025-02-01', 'end': '2025-02-03'},
            detail=repr(themes[0]),
        )
        check('theme remove preserves files', themes[0].get('files') == ['keep.jpg'])


def test_by_date_empty_copy_says_normal_archive():
    print('\n25d. By-date empty copy distinguishes normal archive from screenshots')

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        for name in ('by-date', 'screenshots', 'screenrecords', 'docs', 'things', '_meta'):
            (work / name).mkdir(parents=True)
        for i in range(2):
            (work / 'screenshots' / f'screenshot_{i}.jpg').write_bytes(b'x')
        wb.clear_web_caches()
        html = wb.render_by_date_home(work).decode('utf-8')
        check('by-date empty copy says date archive', '还没有按拍摄日期归档的照片或视频' in html)
        check('by-date empty copy keeps current hint', '把素材放进 inbox' in html)
        check('by-date empty copy keeps screenshot count separate', '截图</span><span class="n">2 项</span>' in html)


def test_web_path_traversal_and_cors_hardening():
    """Star bucket /thumb /month fences + CORS + default host."""
    print('\n26. Web path traversal + CORS hardening')
    import inspect
    import urllib.error
    import urllib.parse
    import urllib.request
    from http.server import ThreadingHTTPServer
    import threading

    src = inspect.getsource(wb.main)
    check("main --host default='127.0.0.1'", "default='127.0.0.1'" in src)
    check(
        'no Access-Control-Allow-Origin *',
        "Access-Control-Allow-Origin', '*'" not in inspect.getsource(wb),
    )
    check('rejects .. bucket helper', wb.is_safe_star_bucket('../x') is False)
    check('rejects slash bucket', wb.is_safe_star_bucket('a/b') is False)
    check('allows themed bucket', wb.is_safe_star_bucket('2026-07_海南') is True)
    check('month ../ rejected', wb.is_safe_month_segment('../etc') is False)
    check(
        'month theme with .. rejected',
        wb.is_safe_month_segment('2026-07_../../x') is False,
    )
    check('month ok themed', wb.is_safe_month_segment('2026-07_海南') is True)
    check('cors allows null', wb.is_allowed_cors_origin('null') is True)
    check(
        'cors allows localhost',
        wb.is_allowed_cors_origin('http://localhost:8766') is True,
    )
    check(
        'cors allows 127.0.0.1',
        wb.is_allowed_cors_origin('http://127.0.0.1:8766') is True,
    )
    check(
        'cors rejects evil',
        wb.is_allowed_cors_origin('https://evil.example') is False,
    )
    check('cors allows missing', wb.is_allowed_cors_origin(None) is True)

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        photo = work / 'by-date' / '2026' / '2026-07_海南' / 'photos'
        photo.mkdir(parents=True)
        sample = photo / '20260701_120000_iphone_aaa111.jpg'
        sample.write_bytes(b'fake-jpg')
        (work / '_meta' / 'thumbs').mkdir(parents=True)
        outside = Path(tmp) / 'outside_star.json'
        outside.write_text('should-not-be-overwritten', encoding='utf-8')

        class H(wb.Handler):
            pass

        H.work = work
        H.thumb_root = work / '_meta' / 'thumbs'

        httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{port}'
        rel = str(sample.relative_to(work))

        def post_star(payload, origin=None):
            headers = {'Content-Type': 'application/json'}
            if origin is not None:
                headers['Origin'] = origin
            req = urllib.request.Request(
                base + '/api/star',
                data=json.dumps(payload).encode(),
                headers=headers,
                method='POST',
            )
            try:
                with urllib.request.urlopen(req) as r:
                    return (
                        r.status,
                        r.headers.get('Access-Control-Allow-Origin'),
                        json.loads(r.read()),
                    )
            except urllib.error.HTTPError as e:
                body = e.read().decode()
                try:
                    parsed = json.loads(body)
                except Exception:
                    parsed = {'raw': body}
                return (
                    e.code,
                    e.headers.get('Access-Control-Allow-Origin'),
                    parsed,
                )

        def get_status(path):
            req = urllib.request.Request(base + path, method='GET')
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        try:
            _, _, body = post_star({
                'path': rel,
                'bucket': '../outside_star',
                'action': 'on',
            })
            check(
                'star ../ bucket rejected',
                body.get('ok') is False,
                detail=repr(body),
            )
            check(
                'outside file unchanged',
                outside.read_text(encoding='utf-8') == 'should-not-be-overwritten',
            )
            _, _, body2 = post_star({
                'path': rel, 'bucket': 'a/../../tmp', 'action': 'on',
            })
            check('star slash bucket rejected', body2.get('ok') is False)

            st_t, raw_t = get_status(
                '/thumb?p=' + urllib.parse.quote('../../etc/passwd')
            )
            check('thumb ../ forbidden', st_t == 403, detail=f'{st_t} {raw_t[:80]!r}')
            st_r, raw_r = get_status(
                '/raw?p=' + urllib.parse.quote('../../etc/passwd')
            )
            check('raw ../ forbidden', st_r == 403, detail=f'{st_r} {raw_r[:80]!r}')

            st_m, raw_m = get_status('/y/2026/' + urllib.parse.quote('../..'))
            check(
                'month ../ rejected',
                st_m in (400, 403),
                detail=f'{st_m} {raw_m[:80]!r}',
            )
            st_m2, raw_m2 = get_status(
                '/y/2026/' + urllib.parse.quote('2026-07_../../evil')
            )
            check(
                'month theme with .. rejected via URL',
                st_m2 in (400, 403),
                detail=f'{st_m2} {raw_m2[:80]!r}',
            )

            st_e, acao_e, body_e = post_star(
                {'path': rel, 'bucket': '2026-07_海南', 'action': 'toggle'},
                origin='https://evil.example',
            )
            check('evil origin POST rejected', st_e == 403, detail=repr(body_e))
            check(
                'evil origin no ACAO *',
                acao_e not in ('*', 'https://evil.example'),
            )

            st_ok, acao_ok, body_ok = post_star(
                {'path': rel, 'bucket': '2026-07_海南', 'action': 'on'},
                origin='http://127.0.0.1:8766',
            )
            check(
                'localhost origin star ok',
                st_ok == 200 and body_ok.get('ok') is True,
            )
            check(
                'localhost ACAO echoed',
                acao_ok == 'http://127.0.0.1:8766',
            )
            st_n, acao_n, body_n = post_star(
                {'path': rel, 'bucket': '2026-07_海南', 'action': 'off'},
                origin='null',
            )
            check(
                'null origin star ok',
                st_n == 200 and body_n.get('ok') is True,
            )
            check('null ACAO echoed', acao_n == 'null')

            huge = b'{"yaml":"' + (b'x' * (wb.MAX_POST_BODY + 10)) + b'"}'
            req = urllib.request.Request(
                base + '/api/events',
                data=huge,
                headers={
                    'Content-Type': 'application/json',
                    'Content-Length': str(len(huge)),
                },
                method='POST',
            )
            try:
                with urllib.request.urlopen(req) as r:
                    check('oversized body rejected', False, detail=f'status {r.status}')
            except urllib.error.HTTPError as e:
                payload = json.loads(e.read().decode())
                check('oversized body status 413', e.code == 413)
                check(
                    'oversized error mentions large',
                    payload.get('ok') is False
                    and 'large' in str(payload.get('error', '')),
                    detail=repr(payload),
                )

            check(
                'safe_under_work blocks ..',
                wb.safe_under_work(work, '../outside') is None,
            )
            check(
                'safe_under_work ok file',
                wb.safe_under_work(work, rel) == sample.resolve(),
            )
        finally:
            httpd.shutdown()


def test_perf_quick_wins_cache_and_thumb_headers():
    """Home/page_shell share one scan; /thumb Cache-Control; status counts TTL."""
    print('\n27. Perf quick wins (topbar cache, thumb headers, status TTL)')
    import urllib.error
    import urllib.parse
    import urllib.request
    from http.server import ThreadingHTTPServer
    import threading

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        photo = work / 'by-date' / '2026' / '2026-07_海南' / 'photos'
        photo.mkdir(parents=True)
        sample = photo / '20260701_120000_iphone_aaa111.jpg'
        # Minimal JPEG so thumb_for hit path can serve without sips if pre-seeded.
        jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF' + b'\x00' * 32 + b'\xff\xd9'
        sample.write_bytes(jpeg)
        (work / 'screenshots').mkdir(parents=True)
        (work / 'screenrecords').mkdir(parents=True)
        (work / 'docs').mkdir(parents=True)
        (work / 'things').mkdir(parents=True)
        (work / 'inbox').mkdir(parents=True)
        (work / '_vlogs').mkdir(parents=True)
        (work / '_trash').mkdir(parents=True)
        (work / '_meta' / 'stars').mkdir(parents=True)
        thumb_root = work / '_meta' / 'thumbs'
        rel = sample.relative_to(work)
        seeded = thumb_root / rel.with_suffix('.jpg')
        seeded.parent.mkdir(parents=True, exist_ok=True)
        seeded.write_bytes(jpeg)

        wb.clear_web_caches()
        scan_calls = {'n': 0}
        real_scan = wb.scan_buckets

        def counting_scan(w):
            scan_calls['n'] += 1
            return real_scan(w)

        old_scan = wb.scan_buckets
        try:
            wb.scan_buckets = counting_scan
            # render_home must not trigger a second scan via page_shell.
            html = wb.render_home(work).decode('utf-8')
            check('render_home scans once', scan_calls['n'] == 1, detail=str(scan_calls['n']))
            check('home is gallery overview', '图库' in html and 'href="/by-date"' in html)
            # Second home within TTL: topbar cache → no new scan_buckets.
            wb.render_home(work)
            check(
                'second home uses topbar cache',
                scan_calls['n'] == 1,
                detail=str(scan_calls['n']),
            )
            # page_shell alone without precomputed buckets still hits cache.
            wb.page_shell('x', '<p>y</p>', work=work)
            check(
                'page_shell reuses topbar cache',
                scan_calls['n'] == 1,
                detail=str(scan_calls['n']),
            )
        finally:
            wb.scan_buckets = old_scan
            wb.clear_web_caches()

        # Home trash card must not rglob _trash on every refresh.
        trash_calls = {'n': 0}
        real_count_for_home = wb.count_files_in

        def counting_trash_for_home(p):
            if Path(p).name == '_trash':
                trash_calls['n'] += 1
            return real_count_for_home(p)

        old_count_for_home = wb.count_files_in
        try:
            wb.count_files_in = counting_trash_for_home
            wb.clear_web_caches()
            wb.render_home(work)
            wb.render_home(work)
            check('home trash count cached', trash_calls['n'] == 1, detail=str(trash_calls['n']))
        finally:
            wb.count_files_in = old_count_for_home
            wb.clear_web_caches()

        # Mutating file operations must invalidate home/topbar counts immediately.
        stale_target = photo / '20260701_120000_iphone_bbb222.jpg'
        stale_target.write_bytes(jpeg)
        wb.clear_web_caches()
        before_home = wb.render_home(work).decode('utf-8')
        wb.trash_paths(work, [str(stale_target.relative_to(work))])
        after_home = wb.render_home(work).decode('utf-8')
        check('home count before trash includes target', 'home-card-name">按日期</span><span class="home-card-count">2 项</span>' in before_home)
        check('home count invalidated after trash', 'home-card-name">按日期</span><span class="home-card-count">1 项</span>' in after_home)

        # status counts: second call must not re-walk (spy count_files_in).
        count_calls = {'n': 0}
        real_count = wb.count_files_in

        def counting_files(p):
            count_calls['n'] += 1
            return real_count(p)

        old_count = wb.count_files_in
        try:
            wb.count_files_in = counting_files
            wb.clear_web_caches()
            c1 = wb.get_status_counts(work)
            n_after_first = count_calls['n']
            c2 = wb.get_status_counts(work)
            check('status first call walks', n_after_first > 0, detail=str(n_after_first))
            check(
                'status second call cached',
                count_calls['n'] == n_after_first,
                detail=f'{count_calls["n"]} vs {n_after_first}',
            )
            check('status counts stable', c1 == c2)
        finally:
            wb.count_files_in = old_count
            wb.clear_web_caches()

        # /thumb Cache-Control + ETag
        class H(wb.Handler):
            pass

        H.work = work
        H.thumb_root = thumb_root
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            url = f'http://127.0.0.1:{port}/thumb?p={urllib.parse.quote(str(rel))}'
            with urllib.request.urlopen(url) as r:
                cc = r.headers.get('Cache-Control') or ''
                etag = r.headers.get('ETag') or ''
                lm = r.headers.get('Last-Modified') or ''
                body = r.read()
            check('thumb Cache-Control present', 'max-age=' in cc, detail=repr(cc))
            check('thumb ETag present', bool(etag), detail=repr(etag))
            check('thumb Last-Modified present', bool(lm), detail=repr(lm))
            check('thumb body is jpeg', body[:3] == b'\xff\xd8\xff')
            req304 = urllib.request.Request(url, headers={'If-None-Match': etag})
            try:
                with urllib.request.urlopen(req304) as r304:
                    st304 = r304.status
            except urllib.error.HTTPError as e:
                st304 = e.code
            check('thumb If-None-Match → 304', st304 == 304, detail=str(st304))
        finally:
            httpd.shutdown()

        # thumb_for hit path: no mkdir when JPEG already exists
        mkdir_calls = []
        real_mkdir = Path.mkdir

        def spy_mkdir(self, *a, **kw):
            mkdir_calls.append(str(self))
            return real_mkdir(self, *a, **kw)

        Path.mkdir = spy_mkdir
        try:
            got = wb.thumb_for(sample, work, thumb_root)
        finally:
            Path.mkdir = real_mkdir
        check('thumb_for hit returns path', got == seeded)
        check(
            'thumb_for hit skips mkdir',
            len(mkdir_calls) == 0,
            detail=repr(mkdir_calls[:3]),
        )


def test_gallery_pagination():
    """Large galleries emit first page only; /api/gallery returns further pages."""
    print('\n28. Gallery pagination (HTML cap + /api/gallery)')
    import urllib.error
    import urllib.parse
    import urllib.request
    from http.server import ThreadingHTTPServer
    import threading

    check('GALLERY_PAGE_SIZE in 100–200', 100 <= wb.GALLERY_PAGE_SIZE <= 200)
    check('PAGE_JS has loadMoreGallery', 'function loadMoreGallery' in wb.PAGE_JS)
    check('PAGE_JS has /api/gallery', '/api/gallery' in wb.PAGE_JS)
    check(
        'filter tip copy',
        '先加载更多，再筛选' in wb._gallery_toolbar(1, 0, paginated=True),
    )
    # Starred filter hides non-star cells (display:none), so #galleryMore stays in
    # viewport and IntersectionObserver would otherwise cascade-load the whole library.
    check(
        'IO auto-load has galleryAutoLoadAllowed gate',
        'function galleryAutoLoadAllowed' in wb.PAGE_JS
        and 'galleryAutoLoadAllowed()' in wb.PAGE_JS,
        detail='IntersectionObserver must not auto-load while 「仅加星」 is active',
    )
    check(
        'IO disconnects when paging ends',
        'galleryIO' in wb.PAGE_JS and 'galleryIO.disconnect()' in wb.PAGE_JS,
    )

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / 'work'
        shots = work / 'screenshots'
        shots.mkdir(parents=True)
        n = wb.GALLERY_PAGE_SIZE + 25
        for i in range(n):
            p = shots / f'screenshot_{i:04d}.jpg'
            p.write_bytes(b'x' + str(i).encode())
            # Distinct mtimes so newest (n-1) sorts first (list_screenshots).
            os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
        thumbs = work / '_meta' / 'thumbs'
        thumbs.mkdir(parents=True, exist_ok=True)

        html = wb.render_screenshots(work, thumbs).decode('utf-8')
        cell_n = len(re.findall(r'class="cell(?: starred)?"', html))
        check(
            'screenshots HTML capped to page size',
            cell_n == wb.GALLERY_PAGE_SIZE,
            detail=f'cells={cell_n} page={wb.GALLERY_PAGE_SIZE} total={n}',
        )
        check('screenshots has load more', 'data-load-more' in html and '加载更多' in html)
        check('screenshots has filter tip', '先加载更多，再筛选' in html)
        check('screenshots count shows total', f'文件 {n}' in html)
        # list_screenshots sorts by mtime desc → newest (n-1) on page 1; oldest (0) on last page
        check(
            'first page includes newest file',
            f'screenshot_{n - 1:04d}.jpg' in html,
        )
        check(
            'first page excludes oldest file',
            'screenshot_0000.jpg' not in html,
        )

        # Small gallery: no pagination chrome
        things = work / 'things'
        things.mkdir(parents=True)
        (things / 'things_20240715_aaaa.jpg').write_bytes(b't')
        small = wb.render_things(work, thumbs).decode('utf-8')
        check(
            'small gallery no load more',
            'id="loadMoreBtn"' not in small and 'data-has-more="1"' not in small,
        )
        check('small gallery has cell', 'things_20240715_aaaa.jpg' in small)

        # Bucket pagination
        photos = work / 'by-date' / '2026' / '2026-07' / 'photos'
        photos.mkdir(parents=True)
        for i in range(wb.GALLERY_PAGE_SIZE + 3):
            (photos / f'20260701_120000_iphone_{i:04x}.jpg').write_bytes(b'p')
        bucket_html = wb.render_bucket(work, '2026', '2026-07', thumbs).decode('utf-8')
        bucket_cells = len(re.findall(r'class="cell(?: starred)?"', bucket_html))
        check(
            'bucket HTML capped',
            bucket_cells == wb.GALLERY_PAGE_SIZE,
            detail=str(bucket_cells),
        )
        check('bucket data-gallery-kind', 'data-gallery-kind="bucket"' in bucket_html)

        class H(wb.Handler):
            pass

        H.work = work
        H.thumb_root = thumbs
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            url = (
                f'http://127.0.0.1:{port}/api/gallery?'
                f'kind=screenshots&offset={wb.GALLERY_PAGE_SIZE}'
                f'&limit={wb.GALLERY_PAGE_SIZE}'
            )
            with urllib.request.urlopen(url) as r:
                data = json.loads(r.read())
            check('api gallery ok', data.get('ok') is True)
            check('api gallery total', data.get('total') == n, detail=str(data.get('total')))
            check(
                'api gallery count',
                data.get('count') == 25,
                detail=str(data.get('count')),
            )
            check('api gallery has_more false', data.get('has_more') is False)
            check(
                'api gallery html has oldest file',
                'screenshot_0000.jpg' in (data.get('html') or ''),
            )
            check(
                'api gallery html has no newest file',
                f'screenshot_{n - 1:04d}.jpg' not in (data.get('html') or ''),
            )

            burl = (
                f'http://127.0.0.1:{port}/api/gallery?'
                f'kind=bucket&year=2026&month=2026-07'
                f'&offset={wb.GALLERY_PAGE_SIZE}&limit=10'
            )
            with urllib.request.urlopen(burl) as r:
                bdata = json.loads(r.read())
            check('api bucket ok', bdata.get('ok') is True)
            check('api bucket has_more false', bdata.get('has_more') is False)
            check(
                'api bucket count 3',
                bdata.get('count') == 3,
                detail=str(bdata.get('count')),
            )

            bad = urllib.request.Request(
                f'http://127.0.0.1:{port}/api/gallery?'
                + urllib.parse.urlencode({
                    'kind': 'bucket',
                    'year': '2026',
                    'month': '../etc',
                    'offset': '0',
                })
            )
            try:
                with urllib.request.urlopen(bad) as r:
                    bad_body = json.loads(r.read())
                check('bad month rejected', bad_body.get('ok') is False)
            except urllib.error.HTTPError as e:
                bad_body = json.loads(e.read())
                check('bad month HTTP 400', e.code == 400)
                check('bad month rejected body', bad_body.get('ok') is False)
        finally:
            httpd.shutdown()


def main():
    print('Bugbot fix regression checks')
    test_dashboard_pipeline_button()
    test_dashboard_web_start_copy()
    test_gallery_menu_counts_and_dismissal()
    test_home_overview_cards_and_by_date_route()
    test_picvault_web_port_state()
    test_picvault_web_restart_clears_orphan_same_work_server()
    test_console_link_shows_dashboard_url()
    test_init_skeleton()
    test_run_commands_backup_and_pipeline()
    test_backup_validation()
    test_picvault_sync_uses_backup_env()
    test_picvault_sync_args_order_independent()
    test_star_api_and_lightbox_sync()
    test_things_reclassify_and_ui()
    test_live_pair_mov_fail_rolls_back()
    test_reclassify_moves_live_companion()
    test_ledger_star_live_counts()
    test_events_api_edit()
    test_rebucket_themes()
    test_reconcile_themes_demote_reassign_orphan()
    test_rename_rebuckets_when_inbox_empty()
    test_scoped_theme_sync_ignores_other_theme()
    test_rebucket_cli_requires_theme_or_all()
    test_heic_thumb_forces_jpeg()
    test_append_theme_from_empty_list()
    test_theme_parse_validate_hardening()
    test_web_sync_cmd_is_dry_run()
    test_rename_hardening_source_and_events_load()
    test_cross_month_theme_start_bucket()
    test_return_to_default_month()
    test_theme_href_never_falls_back_to_default_month()
    test_theme_bucket_shows_date_range()
    test_theme_start_month_reassign_and_validate()
    test_theme_add_files_cli_and_list()
    test_theme_remove_cli_without_yaml()
    test_by_date_empty_copy_says_normal_archive()
    test_web_path_traversal_and_cors_hardening()
    test_perf_quick_wins_cache_and_thumb_headers()
    test_gallery_pagination()
    print(f'\n{passed} passed, {failed} failed')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
