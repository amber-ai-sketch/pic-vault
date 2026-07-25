#!/usr/bin/env python3
"""
Smoke tests for PicVault scripts.

Tests run against a sandbox test directory, NOT /Volumes/Storage.
Tests verify:
  - Path sandbox validation rejects paths outside whitelist
  - dedupe.py finds exact-byte duplicates
  - rename_organize.py correctly classifies screenshot vs photo vs video
  - Theme matching works
  - AppleScript generation works

Run from project root:
    python3 scripts/tests/run_tests.py
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Add scripts dir to path so we can import them as modules
sys.path.insert(0, str(Path(__file__).parent.parent))

PROJECT_ROOT = Path(__file__).parent.parent.parent
SCRIPTS = PROJECT_ROOT / 'scripts'


def run(cmd, cwd=None, expect_fail=False):
    """Run a shell command and return (rc, stdout, stderr)."""
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    return result.returncode, result.stdout, result.stderr


def test(name):
    """Decorator for tests."""
    def deco(fn):
        fn.__test_name__ = name
        return fn
    return deco


def make_test_image(path: Path, exif_make=None, date='2024:07:15 14:30:12'):
    """Create a minimal JPEG with given EXIF Make and date."""
    from PIL import Image
    img = Image.new('RGB', (100, 100), color='blue')
    if exif_make:
        from PIL.ExifTags import Base as ExifBase
        exif = img.getexif()
        exif[0x010F] = exif_make
        exif[0x9003] = date  # DateTimeOriginal
        # GPS coordinates stored in degrees directly
        from PIL.TiffImagePlugin import IFDRational
        gps_ifd = {
            1: 'N',  # GPSLatitudeRef
            2: (IFDRational(34), IFDRational(0), IFDRational(0)),  # GPSLatitude
            3: 'W',
            4: (IFDRational(118), IFDRational(0), IFDRational(0)),  # GPSLongitude
        }
        exif[0x8825] = gps_ifd
        img.save(str(path), 'JPEG', exif=exif.tobytes())
    else:
        img.save(str(path), 'JPEG')


def make_png(path: Path):
    """Create a minimal PNG (no EXIF)."""
    from PIL import Image
    img = Image.new('RGB', (50, 50), color='red')
    img.save(str(path), 'PNG')


def section(name):
    print(f"\n{'='*60}\n  {name}\n{'='*60}")


def main():
    # Tests 1 & 11 (sandbox rejection) require running without DUPEGURU_TEST
    # So we set it ONLY for the other tests
    import os

    # === Run sandbox tests WITHOUT DUPEGURU_TEST ===
    saved_env = os.environ.pop('DUPEGURU_TEST', None)
    section("1. Path sandbox validation")
    rc, _, err = run([
        'python3', str(SCRIPTS / 'dedupe.py'),
        '--work', '/Users/foo'
    ])
    if rc != 0 and 'whitelist' in err.lower():
        print("  ✓ dedupe.py rejects /Users/foo (not in whitelist)")
    else:
        print(f"  ✗ FAIL: rc={rc}")
    rc, _, err = run([
        'python3', str(SCRIPTS / 'rename_organize.py'),
        '--work', '/Volumes/WD4T'
    ])
    if rc != 0 and 'whitelist' in err.lower():
        print("  ✓ rename_organize.py rejects /Volumes/WD4T")
    else:
        print(f"  ✗ FAIL: rc={rc}")
    section("11. init_storage.sh - sandbox rejection")
    rc, out, err = run([
        'bash', str(SCRIPTS / 'init_storage.sh'),
        '--work', '/Users/foo/bar'
    ])
    if rc != 0 and 'whitelist' in err.lower():
        print("  ✓ init_storage.sh rejects /Users/foo/bar")
    else:
        print(f"  ✗ FAIL: rc={rc}")

    # Restore DUPEGURU_TEST for other tests
    if saved_env:
        os.environ['DUPEGURU_TEST'] = saved_env
    else:
        os.environ['DUPEGURU_TEST'] = '1'

    test_root = Path('/tmp/PicVault_test/work')
    if test_root.exists():
        shutil.rmtree(test_root)
    test_root.mkdir(parents=True)
    work = test_root / 'work'
    work.mkdir()
    inbox = work / 'inbox'
    inbox.mkdir()

    print(f"Test work directory: {work}")

    # === Test 1: Sandbox validation ===

    # === Test 2: dedupe finds duplicates ===
    section("2. dedupe.py - SHA-256 duplicate detection")

    # Reset test work dir
    if (work / 'by-date').exists():
        shutil.rmtree(work / 'by-date')
    if (work / '_trash').exists():
        shutil.rmtree(work / '_trash')
    work.mkdir(exist_ok=True)
    (work / 'inbox').mkdir(exist_ok=True)

    # Create: 1 original, 1 duplicate, 1 unique
    make_test_image(inbox / 'IMG_001.jpg', exif_make='Canon', date='2024:07:15 14:30:12')
    shutil.copy(inbox / 'IMG_001.jpg', inbox / 'IMG_001_copy.jpg')
    make_test_image(inbox / 'IMG_002.jpg', exif_make='Canon', date='2024:07:16 10:00:00')

    rc, out, err = run([
        'python3', str(SCRIPTS / 'dedupe.py'),
        '--work', str(work)
    ])
    if rc == 0:
        # Check trash
        trash_files = list((work / '_trash').rglob('*'))
        trash_files = [f for f in trash_files if f.is_file()]
        if len(trash_files) == 1:
            print(f"  ✓ dedupe found 1 duplicate, moved to {trash_files[0]}")
        else:
            print(f"  ✗ FAIL: expected 1 trashed file, got {len(trash_files)}")
            print(f"    trash: {trash_files}")
    else:
        print(f"  ✗ FAIL: dedupe rc={rc}, stderr={err}")

    # === Test 3: dedupe --dry-run ===
    section("3. dedupe.py --dry-run")

    # Reset
    shutil.rmtree(work / '_trash', ignore_errors=True)
    inbox.mkdir(exist_ok=True)
    for f in inbox.iterdir():
        f.unlink()
    make_test_image(inbox / 'A.jpg', exif_make='Apple')
    shutil.copy(inbox / 'A.jpg', inbox / 'A_dup.jpg')
    make_test_image(inbox / 'B.jpg', exif_make='Apple')

    rc, out, err = run([
        'python3', str(SCRIPTS / 'dedupe.py'),
        '--work', str(work),
        '--dry-run'
    ])
    if rc == 0 and 'dry-run' in out.lower():
        print("  ✓ dedupe --dry-run completed without moving files")
        # Verify nothing in trash
        trash_files = list((work / '_trash').rglob('*')) if (work / '_trash').exists() else []
        trash_files = [f for f in trash_files if f.is_file()]
        if len(trash_files) == 0:
            print("  ✓ No files moved to trash (dry-run correct)")
        else:
            print(f"  ✗ FAIL: dry-run shouldn't move files, found {len(trash_files)} in trash")
    else:
        print(f"  ✗ FAIL: rc={rc}, stderr={err}")

    # === Test 4: rename_organize detects screenshots by filename ===
    section("4. rename_organize.py - screenshot detection (filename keyword)")

    # Reset
    shutil.rmtree(work / 'inbox', ignore_errors=True)
    shutil.rmtree(work / 'by-date', ignore_errors=True)
    shutil.rmtree(work / 'screenshots', ignore_errors=True)
    work.mkdir(exist_ok=True)
    (work / 'inbox').mkdir(exist_ok=True)

    # Create a screenshot-named file
    (inbox / 'Screenshot_2024-07-15_18-30-22.png').write_bytes(b'\x89PNG fake content')

    rc, out, err = run([
        'python3', str(SCRIPTS / 'rename_organize.py'),
        '--work', str(work),
        '--dry-run'
    ])
    if rc == 0:
        if 'screenshots/' in out:
            print("  ✓ Screenshot_xxx.png routed to screenshots/")
        else:
            print(f"  ✗ FAIL: screenshot not routed, output: {out}")
    else:
        print(f"  ✗ FAIL: rc={rc}, stderr={err}")

    # === Test 5: rename_organize detects screen recordings ===
    section("5. rename_organize.py - recording keyword → screenrecords/")

    shutil.rmtree(work / 'inbox', ignore_errors=True)
    shutil.rmtree(work / 'by-date', ignore_errors=True)
    shutil.rmtree(work / 'screenshots', ignore_errors=True)
    shutil.rmtree(work / 'screenrecords', ignore_errors=True)
    work.mkdir(exist_ok=True)
    (work / 'inbox').mkdir(exist_ok=True)

    (inbox / 'RPReplay_Final_1689496200.mov').write_bytes(b'fake mov content')
    (inbox / 'Screenrecorder_20240715_140012.mp4').write_bytes(b'fake mp4 content')

    rc, out, err = run([
        'python3', str(SCRIPTS / 'rename_organize.py'),
        '--work', str(work),
        '--dry-run'
    ])
    if rc == 0:
        # RPReplay has no "record" keyword but no Make/GPS → screenrecords via rule 2
        if 'screenrecords/' in out and ('RPReplay' in out or 'screenrecorder_' in out):
            print("  ✓ RPReplay / recording routed to screenrecords/")
        else:
            print(f"  ✗ FAIL: RPReplay not in screenrecords/, output: {out}")
        if 'Screenrecorder' in out and 'screenrecords/' in out:
            print("  ✓ Screenrecorder_xxx.mp4 routed to screenrecords/")
        else:
            print(f"  ✗ FAIL: Screenrecorder path, output: {out}")
    else:
        print(f"  ✗ FAIL: rc={rc}, stderr={err}")

    # === Test 6: rename_organize no-GPS image → screenshots (v7 default on) ===
    section("6. rename_organize.py - no-GPS image → screenshots/ (v7)")

    shutil.rmtree(work / 'inbox', ignore_errors=True)
    shutil.rmtree(work / 'by-date', ignore_errors=True)
    shutil.rmtree(work / 'screenshots', ignore_errors=True)
    shutil.rmtree(work / 'screenrecords', ignore_errors=True)
    work.mkdir(exist_ok=True)
    (work / 'inbox').mkdir(exist_ok=True)

    # Image with no EXIF (no GPS). Filename "image.png" is not a camera pattern.
    make_png(inbox / 'image.png')

    rc, out, err = run([
        'python3', str(SCRIPTS / 'rename_organize.py'),
        '--work', str(work),
        '--dry-run'
    ])
    if rc == 0 and 'screenshots/' in out:
        print("  ✓ PNG without GPS routed to screenshots/ (v7)")
    else:
        print(f"  ✗ FAIL: output: {out}")

    # Camera-named video without metadata stays by-date (rule 0)
    shutil.rmtree(work / 'inbox', ignore_errors=True)
    (work / 'inbox').mkdir(exist_ok=True)
    (inbox / 'VID_20240715_140012.mp4').write_bytes(b'fake mp4 content')
    rc, out, err = run([
        'python3', str(SCRIPTS / 'rename_organize.py'),
        '--work', str(work),
        '--dry-run'
    ])
    if rc == 0 and 'by-date/' in out and 'screenrecords/' not in out:
        print("  ✓ VID_ camera video stays in by-date/")
    else:
        print(f"  ✗ FAIL VID_ routing: {out}")

    # === Test 6a: Live Photo pair ===
    section("6a. rename_organize.py - Live Photo pair → photos/ with _live_")

    shutil.rmtree(work / 'inbox', ignore_errors=True)
    shutil.rmtree(work / 'by-date', ignore_errors=True)
    shutil.rmtree(work / 'screenshots', ignore_errors=True)
    shutil.rmtree(work / 'screenrecords', ignore_errors=True)
    (work / 'inbox').mkdir(parents=True)
    (inbox / 'IMG_1000.HEIC').write_bytes(b'fake heic live still')
    (inbox / 'IMG_1000.MOV').write_bytes(b'fake mov live motion')
    (inbox / 'random.jpg').write_bytes(b'fake jpg')
    (inbox / 'random.mov').write_bytes(b'fake random mov')

    rc, out, err = run([
        'python3', str(SCRIPTS / 'rename_organize.py'),
        '--work', str(work),
        '--dry-run',
        '--source', 'iphone',
    ])
    if rc == 0:
        if ('[live-pair]' in out and 'photos/' in out and '_live_' in out
                and 'IMG_1000.HEIC' in out and 'IMG_1000.MOV' in out):
            # both sides under photos/, not videos/ for the MOV half
            live_lines = [ln for ln in out.splitlines() if 'live-pair' in ln and 'IMG_1000' in ln]
            mov_ok = any('photos/' in ln and '.mov' in ln.lower() for ln in live_lines)
            if mov_ok and 'live_pairs:' in out:
                print("  ✓ Live Photo pair → photos/ with shared _live_ stem")
            else:
                print(f"  ✗ FAIL live pair dest: {live_lines}")
        else:
            print(f"  ✗ FAIL live pair missing markers: {out}")
        # non-camera random.jpg+.mov must not be force-paired
        if '[live-pair]' in out and 'random.jpg' in out:
            # only fail if random was paired
            bad = [ln for ln in out.splitlines() if 'live-pair' in ln and 'random' in ln]
            if bad:
                print(f"  ✗ FAIL: random.jpg/.mov should not pair: {bad}")
            else:
                print("  ✓ non-camera jpg+mov not paired")
        else:
            print("  ✓ non-camera jpg+mov not paired")
    else:
        print(f"  ✗ FAIL: rc={rc}, stderr={err}")

    # orphan HEIC (no mov) — no _live_
    shutil.rmtree(work / 'inbox', ignore_errors=True)
    (work / 'inbox').mkdir(parents=True)
    (inbox / 'IMG_2000.HEIC').write_bytes(b'orphan heic')
    rc, out, err = run([
        'python3', str(SCRIPTS / 'rename_organize.py'),
        '--work', str(work),
        '--dry-run',
        '--source', 'iphone',
    ])
    if rc == 0 and '[live-pair]' not in out and '_live_' not in out and 'photos/' in out:
        print("  ✓ orphan HEIC has no _live_ marker")
    else:
        print(f"  ✗ FAIL orphan HEIC: {out}")

    # Cross-folder split (stock layout): HEIC in A/, MOV in B/
    shutil.rmtree(work / 'inbox', ignore_errors=True)
    (work / 'inbox' / 'Photos').mkdir(parents=True)
    (work / 'inbox' / 'Videos').mkdir(parents=True)
    (work / 'inbox' / 'Photos' / 'IMG_3000.HEIC').write_bytes(b'cross still')
    (work / 'inbox' / 'Videos' / 'IMG_3000.MOV').write_bytes(b'cross mov')
    rc, out, err = run([
        'python3', str(SCRIPTS / 'rename_organize.py'),
        '--work', str(work),
        '--dry-run',
        '--source', 'iphone',
    ])
    if rc == 0 and 'live-pair-cross' in out and '_live_' in out and 'photos/' in out:
        print("  ✓ cross-folder Live Photo reunited into photos/")
    else:
        print(f"  ✗ FAIL cross-folder pair: {out}\nstderr={err}")

    # === Test 6b: reclassify helpers ===
    section("6b. rename_organize.reclassify_paths")
    import rename_organize as ro
    shutil.rmtree(work / 'inbox', ignore_errors=True)
    shutil.rmtree(work / 'by-date', ignore_errors=True)
    shutil.rmtree(work / 'screenshots', ignore_errors=True)
    shutil.rmtree(work / 'screenrecords', ignore_errors=True)
    (work / 'by-date' / '2024' / '2024-07' / 'photos').mkdir(parents=True)
    sample = work / 'by-date' / '2024' / '2024-07' / 'photos' / '20240715_a1b2.jpg'
    sample.write_bytes(b'fake jpg')
    rel = str(sample.relative_to(work))
    results = ro.reclassify_paths(work, [rel], 'to_screen', dry_run=False)
    if results and results[0].get('ok') and str(results[0].get('dest', '')).startswith('screenshots/'):
        print("  ✓ to_screen moved image into screenshots/")
        to_docs = ro.reclassify_paths(work, [results[0]['dest']], 'to_docs', dry_run=False)
        if to_docs and to_docs[0].get('ok') and str(to_docs[0].get('dest', '')).startswith('docs/'):
            print("  ✓ to_docs moved into docs/")
            to_things = ro.reclassify_paths(work, [to_docs[0]['dest']], 'to_things', dry_run=False)
            if to_things and to_things[0].get('ok') and str(to_things[0].get('dest', '')).startswith('things/'):
                dest_name = Path(to_things[0]['dest']).name
                if dest_name.startswith('things_'):
                    print("  ✓ to_things moved into things/ with things_ prefix")
                else:
                    print(f"  ✗ FAIL to_things naming: {dest_name}")
                back = ro.reclassify_paths(work, [to_things[0]['dest']], 'to_normal', dry_run=False)
                if back and back[0].get('ok') and 'by-date/' in str(back[0].get('dest', '')):
                    print("  ✓ to_normal moved image back to by-date/")
                else:
                    print(f"  ✗ FAIL to_normal: {back}")
            else:
                print(f"  ✗ FAIL to_things: {to_things}")
        else:
            print(f"  ✗ FAIL to_docs: {to_docs}")
    else:
        print(f"  ✗ FAIL to_screen: {results}")

    # === Test 7: rename_organize real photo with Make goes to by-date ===
    section("7. rename_organize.py - real photo with Make goes to by-date")

    shutil.rmtree(work / 'inbox', ignore_errors=True)
    shutil.rmtree(work / 'by-date', ignore_errors=True)
    shutil.rmtree(work / 'screenshots', ignore_errors=True)
    work.mkdir(exist_ok=True)
    (work / 'inbox').mkdir(exist_ok=True)

    # Real Canon photo with EXIF Make
    make_test_image(inbox / 'IMG_4521.jpg', exif_make='Canon', date='2024:07:15 14:30:12')

    rc, out, err = run([
        'python3', str(SCRIPTS / 'rename_organize.py'),
        '--work', str(work),
        '--dry-run'
    ])
    if rc == 0:
        if 'by-date/' in out and 'screenshots/' not in out:
            print("  ✓ Canon EXIF photo routed to by-date/photos/")
        else:
            print(f"  ✗ FAIL: output: {out}")
    else:
        print(f"  ✗ FAIL: rc={rc}")

    # === Test 8: theme matching ===
    section("8. rename_organize.py - theme matching")

    shutil.rmtree(work / 'inbox', ignore_errors=True)
    shutil.rmtree(work / 'by-date', ignore_errors=True)
    shutil.rmtree(work / 'screenshots', ignore_errors=True)
    work.mkdir(exist_ok=True)
    (work / 'inbox').mkdir(exist_ok=True)

    # Create events.yaml
    meta = work / '_meta'
    meta.mkdir(exist_ok=True)
    events = '''themes:
  - name: 海南
    month: 2024-07
    date_range:
      start: 2024-07-10
      end:   2024-07-18
    sources: [iphone, canon]
'''
    (meta / 'events.yaml').write_text(events)

    # Photo in date range with canon source
    make_test_image(inbox / 'IMG_hainan1.jpg', exif_make='Canon', date='2024:07:15 14:30:12')
    # Photo out of date range - should go to default bucket
    make_test_image(inbox / 'IMG_other.jpg', exif_make='Canon', date='2024:07:25 10:00:00')

    rc, out, err = run([
        'python3', str(SCRIPTS / 'rename_organize.py'),
        '--work', str(work)
    ])
    if rc == 0:
        themed_dir = work / 'by-date' / '2024' / '2024-07_海南' / 'photos'
        default_dir = work / 'by-date' / '2024' / '2024-07' / 'photos'
        if themed_dir.exists():
            print(f"  ✓ Themed photo went to {themed_dir.relative_to(work)}")
        else:
            print(f"  ✗ FAIL: themed dir not created")
        if default_dir.exists():
            print(f"  ✓ Out-of-range photo went to {default_dir.relative_to(work)}")
        else:
            print(f"  ✗ FAIL: default dir not created")
    else:
        print(f"  ✗ FAIL: rc={rc}, stderr={err}")

    # === Test 9: pick_to_iphone AppleScript generation ===
    section("9. pick_to_iphone.py - AppleScript generation")

    shutil.rmtree(work / '_favorite', ignore_errors=True)
    work.mkdir(exist_ok=True)

    # Set up starred file
    by_date_themed = work / 'by-date' / '2024' / '2024-07_海南' / 'photos'
    if not by_date_themed.exists():
        by_date_themed.mkdir(parents=True, exist_ok=True)
    sample_file = by_date_themed / '20240715_143012_canon_a3f2.jpg'
    sample_file.write_bytes(b'fake jpg content')

    stars_dir = work / '_meta' / 'stars'
    stars_dir.mkdir(parents=True, exist_ok=True)
    (stars_dir / '2024-07_海南.json').write_text(json.dumps([str(sample_file.relative_to(work))]))

    rc, out, err = run([
        'python3', str(SCRIPTS / 'pick_to_iphone.py'),
        '--work', str(work),
        '--bucket', '2024-07_海南'
    ])
    if rc == 0:
        scpt = work / '_meta' / 'scripts' / 'favorite-2024-07_海南.scpt'
        fav_dir = work / '_favorite' / '2024-07_海南'
        if scpt.exists():
            content = scpt.read_text()
            if 'skip checking duplicates yes' in content and 'favorite' in content:
                print(f"  ✓ AppleScript generated with skip duplicates + favorite")
            else:
                print(f"  ✗ FAIL: AppleScript missing required content")
                print(content)
        if fav_dir.exists():
            files = list(fav_dir.iterdir())
            if len(files) == 1:
                print(f"  ✓ File copied to _favorite/{fav_dir.name}/")
            else:
                print(f"  ✗ FAIL: expected 1 file, got {len(files)}")
        else:
            print(f"  ✗ FAIL: _favorite/2024-07_海南/ not created")
    else:
        print(f"  ✗ FAIL: rc={rc}, stderr={err}")

    # === Test 10: init_storage.sh ===
    section("10. init_storage.sh")

    test_root2 = Path('/tmp/PicVault_test/work2')
    if test_root2.exists():
        shutil.rmtree(test_root2)
    test_root2.mkdir(parents=True)
    work2 = test_root2 / 'Storage'
    work2.mkdir()
    rc, out, err = run([
        'bash', str(SCRIPTS / 'init_storage.sh'),
        '--work', str(work2)
    ])
    if rc == 0:
        for d in ('inbox', 'by-date', 'screenshots', 'screenrecords', 'docs', 'things', '_favorite', '_vlogs', '_trash', '_meta'):
            if (work2 / d).exists():
                print(f"  ✓ Created {d}/")
            else:
                print(f"  ✗ FAIL: missing {d}/")
    else:
        print(f"  ✗ FAIL: rc={rc}, stderr={err}")

    shutil.rmtree(test_root2, ignore_errors=True)

    # === Test 11: init_storage.sh sandbox rejection ===

    # Cleanup
    if test_root.exists():
        shutil.rmtree(test_root)
    if test_root2.exists():
        shutil.rmtree(test_root2)
    print(f"\n✓ Test cleanup done")


if __name__ == '__main__':
    main()
