#!/usr/bin/env python
"""
YouTube Shorts Auto-Cutter
Finds the most visually active 15-20s windows in continuous footage and cuts them with ffmpeg.

Usage:
    python youtube_shorts.py cut --video "D:/footage.mp4" --output "D:/shorts" --count 5 --duration 18
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import textwrap

os.environ.setdefault('PYTHONUTF8', '1')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

HERE       = os.path.dirname(os.path.abspath(__file__))
QUOTES_FILE = os.path.join(HERE, 'quotes.json')


def _pick_quotes(count: int) -> list:
    """
    Pick `count` unused quotes from quotes.json, mark them used, and return
    [(quote, author), ...].  Resets the cycle automatically once all quotes
    have been shown at least once.
    """
    if not os.path.isfile(QUOTES_FILE):
        print(f"  Warning: quotes.json not found — no quote overlay.")
        return []

    with open(QUOTES_FILE, encoding='utf-8') as f:
        data = json.load(f)

    quotes = data.get('quotes', [])
    used   = set(data.get('used', []))

    if not quotes:
        return []

    unused = [i for i in range(len(quotes)) if i not in used]

    if len(unused) < count:
        print("  All quotes used — resetting quote cycle.")
        used   = set()
        unused = list(range(len(quotes)))
        data['used'] = []

    selected = random.sample(unused, min(count, len(unused)))
    data['used'] = list(used | set(selected))

    with open(QUOTES_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    return [(quotes[i]['quote'], quotes[i]['author']) for i in selected]


def _ffmpeg_exe() -> str:
    """Return ffmpeg path: system PATH first, then imageio_ffmpeg bundle."""
    import shutil
    ff = shutil.which('ffmpeg')
    if ff:
        return ff
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass
    raise RuntimeError(
        "ffmpeg not found. Install it or run: pip install imageio-ffmpeg"
    )


def _require_cv2():
    try:
        import cv2
        import numpy as np
        return cv2, np
    except ImportError:
        print("ERROR: opencv-python not installed. Run: pip install opencv-python numpy")
        sys.exit(1)


def analyze_motion(video_path: str) -> tuple:
    """
    Sample frames and compute per-sample motion scores.
    Seeks directly to each sample position — never decodes skipped frames.
    Returns (scores, fps, total_duration, sample_every_frames).
    """
    cv2, np = _require_cv2()

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    fps          = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    total_dur    = total_frames / fps

    print(f"  Duration : {total_dur:.1f}s  |  FPS: {fps:.1f}  |  Frames: {total_frames}")

    # 1 sample per second is enough for motion scoring; faster than 2/s
    sample_every = max(1, int(fps))

    scores     = []
    prev_gray  = None
    frame_idx  = 0
    total_samples = total_frames // sample_every

    while frame_idx < total_frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = cap.read()
        if not ret:
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (320, 180))
        if prev_gray is not None:
            score = float(np.mean(cv2.absdiff(gray, prev_gray)))
        else:
            score = 0.0
        scores.append(score)
        prev_gray = gray

        if len(scores) % 30 == 0:
            pct = len(scores) * 100 // max(total_samples, 1)
            print(f"  Analysing... {pct}% ({len(scores)}/{total_samples} samples)", flush=True)

        frame_idx += sample_every

    cap.release()
    return scores, fps, total_dur, sample_every


def _smooth(scores: list, window: int = 5) -> list:
    result = []
    n = len(scores)
    for i in range(n):
        s = max(0, i - window)
        e = min(n, i + window + 1)
        result.append(sum(scores[s:e]) / (e - s))
    return result


def find_best_windows(scores: list, fps: float, sample_every: int,
                      clip_duration: int, count: int) -> list:
    """
    Return up to `count` non-overlapping (start_sec, end_sec, avg_score) tuples,
    sorted chronologically.
    """
    spc = int(clip_duration * fps / sample_every)   # samples per clip
    if spc >= len(scores):
        raise RuntimeError("Video is too short for the requested clip duration.")

    # Score every possible start position
    candidates = []
    for i in range(len(scores) - spc):
        avg   = sum(scores[i:i + spc]) / spc
        start = i * sample_every / fps
        end   = start + clip_duration
        candidates.append((avg, start, end))

    candidates.sort(reverse=True)   # best score first

    selected = []   # [(start, end, score)]
    for avg, start, end in candidates:
        overlap = any(not (end <= s2 or start >= e2) for s2, e2, _ in selected)
        if not overlap:
            selected.append((start, end, avg))
        if len(selected) >= count:
            break

    selected.sort(key=lambda x: x[0])   # chronological order
    return selected


def _font_path(bold: bool = False) -> str:
    candidates = (
        [
            r'C:\Windows\Fonts\arialbd.ttf',   # Arial Bold
            r'C:\Windows\Fonts\Arial Bold.ttf',
            r'/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        ] if bold else []
    ) + [
        r'C:\Windows\Fonts\arial.ttf',
        r'C:\Windows\Fonts\Arial.ttf',
        r'/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return ''


def _escape_dt(s: str) -> str:
    """Escape a string for use in ffmpeg drawtext text= option."""
    s = s.replace('\\', '\\\\')  # backslash first
    s = s.replace("'",  "\\'")   # single quote
    s = s.replace(':',  '\\:')   # colon (option separator)
    s = s.replace('%',  '%%')    # percent (strftime escape)
    return s


def _quote_filter(quote: str, philosopher: str, duration: float) -> tuple[str, None]:
    """
    Build ffmpeg filter string for a scrolling philosopher quote overlay.
    Each sentence/wrapped segment is its own drawtext layer; all share the
    same x-scroll expression so the whole block moves as one paragraph.
    """
    font = _font_path(bold=True)
    if not font:
        print("  Warning: no font found, skipping quote overlay")
        return '', None

    # Sentence-aware split, then wrap long sentences
    sentences = re.split(r'(?<=[.!?])\s+', quote.strip())
    lines: list = []
    for sent in sentences:
        if len(sent) <= 36:
            lines.append(sent)
        else:
            lines.extend(textwrap.wrap(sent, width=36))

    author_line  = f'— {philosopher}'
    font_ff      = font.replace('\\', '/').replace(':', '\\:')
    fontsize     = 40
    px_per_char  = 22           # ~22 px/char for Arial Bold at fontsize=40
    line_h       = 52           # px per line (font size + spacing)
    bottom_margin = 180         # px from frame bottom to author baseline

    # Block width from the widest quote line
    max_chars    = max(len(l) for l in lines)
    text_block_w = max_chars * px_per_char

    # Single scroll expression shared by both filters
    scroll_x = f"w-(t*(w+{text_block_w})/{duration:.2f})"

    # ── Quote block: all lines in ONE drawtext using \n (2 filters total) ──
    text_esc = '\\n'.join(_escape_dt(l) for l in lines)
    quote_y  = f"h-{bottom_margin + (len(lines) + 1) * line_h}"

    quote_dt = (
        f"drawtext=text='{text_esc}'"
        f":fontfile='{font_ff}'"
        f":fontsize={fontsize}:fontcolor=0xFFD700"
        f":bordercolor=black:borderw=2"
        f":y={quote_y}"
        f":x='{scroll_x}'"
    )

    # ── Author line: right-aligned via static width estimate ──
    # Use ASCII "--" instead of em dash to avoid Unicode in the filter expression
    author_ascii = f"-- {philosopher}"
    auth_esc     = _escape_dt(author_ascii)
    auth_w       = len(author_ascii) * px_per_char
    # right edge of author = right edge of block → x = block_left + block_w - author_w
    author_x = f"({scroll_x})+{text_block_w}-{auth_w}"
    auth_y   = f"h-{bottom_margin}"

    author_dt = (
        f"drawtext=text='{auth_esc}'"
        f":fontfile='{font_ff}'"
        f":fontsize={fontsize}:fontcolor=0xFFD700"
        f":bordercolor=black:borderw=2"
        f":y={auth_y}"
        f":x='{author_x}'"
    )

    return f"{quote_dt},{author_dt}", None


def cut_clips(video_path: str, windows: list, output_dir: str,
              add_quote: bool = False) -> list:
    """Cut mute clips using ffmpeg. Returns list of output file paths."""
    os.makedirs(output_dir, exist_ok=True)
    base     = os.path.splitext(os.path.basename(video_path))[0]
    outputs  = []

    quote_pool = _pick_quotes(len(windows)) if add_quote else []

    for i, (start, end, score) in enumerate(windows, 1):
        out_path = os.path.join(output_dir, f"{base}_short_{i:02d}.mp4")
        dur      = end - start

        base_vf  = 'scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920'
        tmp_file = None

        if add_quote and quote_pool:
            quote_text, philosopher = quote_pool[(i - 1) % len(quote_pool)]
            extra_vf, tmp_file = _quote_filter(quote_text, philosopher, dur)
            vf = f"{base_vf},{extra_vf}" if extra_vf else base_vf
            print(f"  Quote: \"{quote_text[:50]}...\" — {philosopher}")
            print(f"  VF: {vf}", flush=True)
        else:
            vf = base_vf

        cmd = [
            _ffmpeg_exe(), '-y',
            '-ss', f'{start:.3f}',
            '-i', video_path,
            '-t', f'{dur:.3f}',
            '-vf', vf,
            '-c:v', 'libx264',
            '-an',
            '-preset', 'fast',
            '-crf', '23',
            out_path,
        ]

        mins = int(start // 60)
        secs = start % 60
        print(f"  Clip {i:02d}  {mins:02d}:{secs:05.2f} → {mins:02d}:{(secs+dur)%60:05.2f}"
              f"  (motion score: {score:.1f})")

        result = subprocess.run(cmd, capture_output=True, text=True, errors='replace')

        if tmp_file and os.path.exists(tmp_file):
            os.unlink(tmp_file)

        if result.returncode == 0:
            print(f"         Saved: {out_path}")
            outputs.append({'path': out_path, 'start': start, 'end': end, 'score': score})
        else:
            # Print last 10 lines of stderr for diagnosis
            err_lines = result.stderr.strip().splitlines()
            for line in err_lines[-10:]:
                print(f"         {line}")
            print(f"         ERROR: ffmpeg exit {result.returncode}")

    return outputs


def cmd_cut(video_path: str, output_dir: str, count: int, duration: int, add_quote: bool = False):
    if not os.path.isfile(video_path):
        print(f"ERROR: Video not found: {video_path}")
        sys.exit(1)

    print(f"[YouTube Shorts] Video  : {video_path}")
    print(f"[YouTube Shorts] Output : {output_dir}")
    print(f"[YouTube Shorts] Target : {count} clips × {duration}s"
          + ("  [+ philosopher quotes]" if add_quote else ""))

    print("\n  [1/3] Sampling frames for motion analysis...")
    scores, fps, total_dur, sample_every = analyze_motion(video_path)
    print(f"  Sampled {len(scores)} frames from {total_dur:.0f}s of footage")

    print("\n  [2/3] Finding best motion windows...")
    smoothed = _smooth(scores, window=int(fps / sample_every))
    windows  = find_best_windows(smoothed, fps, sample_every, duration, count)
    print(f"  Selected {len(windows)} windows:")
    for i, (s, e, sc) in enumerate(windows, 1):
        m = int(s // 60)
        print(f"    {i}. {m:02d}:{s%60:05.2f} → {m:02d}:{(s%60+duration)%60:05.2f}  score={sc:.1f}")

    print(f"\n  [3/3] Cutting clips with ffmpeg...")
    clips = cut_clips(video_path, windows, output_dir, add_quote=add_quote)

    print(f"\n  Done! {len(clips)} clips saved to: {output_dir}")
    print(f"__YT_DONE__{json.dumps({'clips': clips, 'output_dir': output_dir})}")


def main():
    parser = argparse.ArgumentParser(prog='youtube_shorts')
    sub    = parser.add_subparsers(dest='cmd', required=True)

    c = sub.add_parser('cut')
    c.add_argument('--video',     required=True)
    c.add_argument('--output',    required=True)
    c.add_argument('--count',     type=int, default=5)
    c.add_argument('--duration',  type=int, default=18)
    c.add_argument('--add-quote', action='store_true')

    args = parser.parse_args()
    if args.cmd == 'cut':
        cmd_cut(args.video, args.output, args.count, args.duration,
                add_quote=args.add_quote)


if __name__ == '__main__':
    main()
