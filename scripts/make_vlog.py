#!/usr/bin/env python3
"""
make_vlog.py - Render a simple vlog from EDL JSON + ffmpeg.

Usage:
    ./make_vlog.py --work /Volumes/Storage --theme 2026-07_海南
    ./make_vlog.py --work /Volumes/Storage --theme 2026-07_海南 --style concat

Reads: _meta/edl/<theme>.json
Writes: _vlogs/<theme>.mp4

EDL JSON schema:
{
  "theme": "2026-07_海南",
  "transition": "crossfade-1s",
  "music": null,
  "clips": [
    {"path": "by-date/2026/2026-07_海南/videos/xxx.mp4", "in": 0, "out": 15.2},
    ...
  ]
}
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ALLOWED_WORK_PREFIXES = ('/Volumes/Storage', '/Volumes/YM/MediaVault', '/Users/ym/Downloads/pic-test')
ALLOWED_TRANSITIONS = frozenset({
    'concat',
    'crossfade-0.5s',
    'crossfade-1s',
})


def validate_path(path_str: str, allowed_prefixes, kind: str) -> Path:
    if os.environ.get("DUPEGURU_TEST") == "1":
        return Path(path_str).expanduser().resolve()
    p = Path(path_str).expanduser().resolve()
    for prefix in allowed_prefixes:
        prefix_resolved = str(Path(prefix).resolve())
        if str(p) == prefix_resolved or str(p).startswith(prefix_resolved + '/'):
            return p
    raise ValueError(
        f"--{kind} {path_str} is not in path whitelist.\n"
        f"  Allowed: {', '.join(allowed_prefixes)}"
    )


def _under_work(work: Path, path: Path) -> bool:
    work_r = work.resolve()
    path_r = path.resolve()
    return path_r == work_r or str(path_r).startswith(str(work_r) + os.sep)


def validate_clip(work: Path, clip: dict, index: int) -> dict:
    """Sanitize one EDL clip: path under work, in/out as floats."""
    if not isinstance(clip, dict):
        raise ValueError(f"clips[{index}]: must be an object")
    raw_path = clip.get('path')
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError(f"clips[{index}]: path required")
    p = Path(raw_path)
    if p.is_absolute() or '..' in p.parts:
        raise ValueError(f"clips[{index}]: path must be work-relative without '..': {raw_path}")
    full = (work / p).resolve()
    if not _under_work(work, full):
        raise ValueError(f"clips[{index}]: path escapes work: {raw_path}")
    try:
        in_t = float(clip.get('in', 0))
        out_t = float(clip['out'])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"clips[{index}]: in/out must be numbers ({e})") from e
    if out_t <= in_t:
        raise ValueError(f"clips[{index}]: out ({out_t}) must be > in ({in_t})")
    return {'path': full, 'in': in_t, 'out': out_t}


def validate_transition(transition: str) -> str:
    if transition not in ALLOWED_TRANSITIONS:
        raise ValueError(
            f"transition {transition!r} not allowed.\n"
            f"  Allowed: {', '.join(sorted(ALLOWED_TRANSITIONS))}"
        )
    return transition


def find_ffmpeg() -> str:
    """Find ffmpeg in PATH."""
    path = shutil.which('ffmpeg')
    if not path:
        raise RuntimeError("ffmpeg not found; install with: brew install ffmpeg")
    return path


def find_ffprobe() -> str:
    """Find ffprobe in PATH."""
    path = shutil.which('ffprobe')
    if not path:
        raise RuntimeError("ffprobe not found; install with: brew install ffmpeg")
    return path


def probe_has_audio(path: Path) -> bool:
    """Return True when ffprobe sees an audio stream."""
    result = subprocess.run(
        [find_ffprobe(), '-v', 'error', '-select_streams', 'a:0',
         '-show_entries', 'stream=index', '-of', 'json', str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {path}: {(result.stderr or result.stdout).strip() or 'unknown error'}"
        )
    try:
        data = json.loads(result.stdout or '{}')
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ffprobe returned invalid JSON for {path}") from e
    return bool(data.get('streams'))


def build_filter_complex(clips: list, transition: str) -> tuple[str, str, str]:
    """Build ffmpeg -filter_complex for trim/xfade/concat.

    Returns (filter_str, video_label, audio_label) for:
        ffmpeg -filter_complex <filter_str> -map <video_label> -map <audio_label>
    """
    n = len(clips)
    if n == 0:
        raise ValueError("EDL has no clips")

    def audio_branch(i: int, clip: dict) -> str:
        in_t = clip.get('in', 0)
        out_t = clip['out']
        dur = out_t - in_t
        if clip.get('has_audio', True):
            return (
                f"[{i}:a]atrim=start={in_t}:end={out_t},asetpts=PTS-STARTPTS,"
                f"aresample=48000,aformat=channel_layouts=stereo[a{i}]"
            )
        return f"anullsrc=r=48000:cl=stereo,atrim=duration={dur},asetpts=PTS-STARTPTS[a{i}]"

    if n == 1:
        # Single clip: just trim
        c = clips[0]
        in_t = c.get('in', 0)
        out_t = c['out']
        filter_str = (
            f"[0:v]trim=start={in_t}:end={out_t},setpts=PTS-STARTPTS[v];"
            f"{audio_branch(0, c)}"
        )
        return filter_str, "[v]", "[a]"

    if 'crossfade' in transition:
        # Parse fade duration from style ("crossfade-1s" -> 1.0)
        fade_dur = 1.0
        if '-' in transition:
            try:
                fade_dur = float(transition.split('-')[1].rstrip('s'))
            except Exception:
                fade_dur = 1.0

        # First pass: produce trimmed labels [v0][a0], [v1][a1], ...
        parts = []
        for i, c in enumerate(clips):
            in_t = c.get('in', 0)
            out_t = c['out']
            parts.append(f"[{i}:v]trim=start={in_t}:end={out_t},setpts=PTS-STARTPTS[v{i}]")
            parts.append(audio_branch(i, c))

        # Second pass: chain xfade (video) + acrossfade (audio)
        last_v = "[v0]"
        last_a = "[a0]"
        offset = clips[0]['out'] - clips[0].get('in', 0)
        for i in range(1, n):
            in_t = clips[i].get('in', 0)
            out_t = clips[i]['out']
            dur = out_t - in_t
            v_in = f"[v{i}]"
            a_in = f"[a{i}]"
            xv_out = f"[xv{i}]"
            xa_out = f"[xa{i}]"
            # xfade takes 2 video inputs: [in0][in1]xfade=...[out]
            parts.append(
                f"{last_v}{v_in}xfade=transition=fade:duration={fade_dur}:offset={offset - fade_dur}{xv_out}"
            )
            # acrossfade takes 2 audio inputs
            parts.append(
                f"{last_a}{a_in}acrossfade=d={fade_dur}{xa_out}"
            )
            last_v = xv_out
            last_a = xa_out
            offset = offset + dur - fade_dur

        filter_str = ";" + chr(10) + "".join(parts)
        return filter_str, last_v, last_a

    # Plain concat (no transition)
    parts = []
    for i, c in enumerate(clips):
        in_t = c.get('in', 0)
        out_t = c['out']
        parts.append(f"[{i}:v]trim=start={in_t}:end={out_t},setpts=PTS-STARTPTS[v{i}]")
        parts.append(audio_branch(i, c))
    concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(n))
    parts.append(f"{concat_inputs}concat=n={n}:v=1:a=1[v][a]")
    filter_str = ";" + chr(10) + "".join(parts)
    return filter_str, "[v]", "[a]"


def main():
    parser = argparse.ArgumentParser(description='Render vlog from EDL JSON')
    parser.add_argument('--work', default='/Volumes/Storage')
    parser.add_argument('--theme', required=True)
    parser.add_argument('--style', default='crossfade-1s',
                        help='crossfade-1s, crossfade-0.5s, or concat')
    args = parser.parse_args()

    try:
        work = validate_path(args.work, ALLOWED_WORK_PREFIXES, 'work')
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    edl_path = work / '_meta' / 'edl' / f"{args.theme}.json"
    if not edl_path.exists():
        print(f"ERROR: EDL not found: {edl_path}")
        print("Create _meta/edl/<theme>.json with clip list first.")
        sys.exit(1)

    try:
        edl = json.loads(edl_path.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid EDL JSON: {e}")
        sys.exit(1)

    clips_raw = edl.get('clips', [])
    if not clips_raw:
        print("ERROR: EDL has no clips")
        sys.exit(1)

    try:
        transition = validate_transition(args.style)
        clips = [validate_clip(work, c, i) for i, c in enumerate(clips_raw)]
        for clip in clips:
            clip['has_audio'] = probe_has_audio(clip['path'])
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    filter_str, v_label, a_label = build_filter_complex(clips, transition)

    output_path = work / '_vlogs' / f"{args.theme}.mp4"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [find_ffmpeg(), '-y']
    for c in clips:
        cmd += ['-i', str(c['path'])]
    cmd += [
        '-filter_complex', filter_str,
        '-map', v_label,
        '-map', a_label,
        '-c:v', 'libx264', '-preset', 'medium', '-crf', '23',
        '-c:a', 'aac', '-b:a', '128k',
        '-movflags', '+faststart',
        str(output_path)
    ]

    print(f"→ Rendering {len(clips)} clips -> {output_path}")
    print(f"  transition: {transition}")
    try:
        subprocess.run(cmd, check=True, capture_output=False)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: ffmpeg failed (rc={e.returncode})", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("ERROR: ffmpeg not installed", file=sys.stderr)
        sys.exit(1)

    print(f"✓ Wrote {output_path}")


if __name__ == '__main__':
    main()
