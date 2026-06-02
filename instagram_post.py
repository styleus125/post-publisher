#!/usr/bin/env python
"""
Daily Instagram post/reel agent for Styleus.
- Picks titles sequentially from instagram_styleus_titles.txt
- Tracks used titles in instagram_used_titles.json
- Fetches relevant image/video from Pexels
- Adds professional text overlay with hook + branding
- Posts to Instagram via Meta Graph API

Usage:
    python instagram_post.py                    # photo post, next scheduled title
    python instagram_post.py --reel             # reel post, next scheduled title
    python instagram_post.py --title "custom"   # photo post, custom title
    python instagram_post.py --reel --title "x" # reel post, custom title
"""

import io
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile

# Force UTF-8 I/O on Windows (fixes ₹, emoji, etc. showing as mojibake)
os.environ.setdefault('PYTHONUTF8', '1')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
import time
import textwrap
import uuid
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

# moviepy 1.x uses Image.ANTIALIAS which was removed in Pillow 10+
if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = Image.LANCZOS

try:
    from moviepy.editor import VideoFileClip, ImageClip, CompositeVideoClip
    import numpy as np
    MOVIEPY_OK = True
except ImportError:
    MOVIEPY_OK = False

try:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    GDRIVE_OK = True
except ImportError:
    GDRIVE_OK = False

GDRIVE_SCOPES       = ['https://www.googleapis.com/auth/drive.file']
GDRIVE_CREDS_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'credentials.json')
GDRIVE_TOKEN_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'token.json')
GDRIVE_FOLDER_NAME  = 'Styleus Reels'

# ── Load .env ─────────────────────────────────────────────────────────────────
def load_env(path: str):
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(here, path)
    if not os.path.exists(env_path):
        print(f"ERROR: .env file not found at {env_path}")
        sys.exit(1)
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            os.environ.setdefault(key.strip(), value.strip())

load_env('.env')

INSTAGRAM_ACCOUNT_ID   = os.environ.get('INSTAGRAM_ACCOUNT_ID', '')
INSTAGRAM_ACCESS_TOKEN = os.environ.get('INSTAGRAM_ACCESS_TOKEN', '')
META_APP_ID            = os.environ.get('META_APP_ID', '')
META_APP_SECRET        = os.environ.get('META_APP_SECRET', '')
PEXELS_API_KEY         = os.environ.get('PEXELS_API_KEY', '')
STYLEUS_API_URL        = os.environ.get('STYLEUS_API_URL', 'https://styleus.co.in/api')
STYLEUS_API_TOKEN      = os.environ.get('STYLEUS_API_TOKEN', '')
TELEGRAM_BOT_TOKENS    = [t.strip() for t in os.environ.get('TELEGRAM_BOT_TOKEN', '').split(',') if t.strip()]
TELEGRAM_CHAT_IDS      = [c.strip() for c in os.environ.get('TELEGRAM_CHAT_ID', '').split(',') if c.strip()]

CLAUDE_CMD    = r"C:\Users\spli5\AppData\Roaming\npm\claude.cmd"
GRAPH_API     = "https://graph.facebook.com/v19.0"
MAX_RETRIES   = 3
RETRY_DELAY   = 30
REEL_DURATION = 20  # seconds to trim Reels to

HERE        = os.path.dirname(os.path.abspath(__file__))
TITLES_FILE = os.path.join(HERE, 'instagram_styleus_titles.txt')
USED_FILE   = os.path.join(HERE, 'instagram_used_titles.json')

# Styleus brand colours
BRAND_NAVY = (10, 22, 40)
BRAND_BLUE = (59, 130, 246)
WHITE      = (255, 255, 255)
LIGHT_GREY = (200, 210, 220)

# ── Category → Pexels search keyword map ─────────────────────────────────────
CATEGORY_KEYWORDS = {
    'refurbished laptops & pcs':    'laptop computer desk',
    'tech accessories':              'tech accessories desk setup',
    'portronics products':           'bluetooth speaker earphone',
    'antivirus & security':          'cyber security computer',
    'ram, ssd & hardware':           'computer hardware upgrade',
    'cctv & security systems':       'security camera surveillance',
    'software & web services':       'software development coding',
    'deals & offers':                'shopping sale deal india',
    'tech tips & buying guides':     'technology buying guide',
    'styleus.co.in specials':        'online shopping india tech',
}

# ── Caption prompt ────────────────────────────────────────────────────────────
CAPTION_PROMPT = (
    'Write an Instagram caption for Styleus (styleus.co.in), an Indian tech marketplace. '
    'Title/topic: "{title}". Category: "{category}". '
    'Rules: '
    'Max 120 words. '
    'First line: a bold scroll-stopping hook — surprising fact, bold claim, or relatable frustration. Make it punchy. '
    'Body: 2-3 short lines with a key tip or insight. Natural Indian English. Direct second-person ("you"). '
    'End: soft CTA — "Check the link in bio" or "DM us to know more". '
    'Hashtags: 12 relevant tags on a new line (mix popular + niche Indian tech tags). '
    'Respond with ONLY raw JSON, no markdown fences: '
    '{{"caption": "<full caption with hashtags>", "hook": "<first line only, max 60 chars>"}}'
)


# ── Title management ──────────────────────────────────────────────────────────

def load_titles() -> list:
    titles = []
    current_category = "General"
    with open(TITLES_FILE, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('©'):
                continue
            if re.match(r'^[A-Z][A-Za-z &,\.]+$', line) and not re.match(r'^\d+\.', line):
                current_category = line
                continue
            m = re.match(r'^(\d+)\.\s+(.+)$', line)
            if m:
                titles.append({
                    'number': int(m.group(1)),
                    'title': m.group(2).strip(),
                    'category': current_category,
                })
    return titles


def load_used() -> list:
    if not os.path.exists(USED_FILE):
        return []
    with open(USED_FILE) as f:
        return json.load(f)


def save_used(used: list):
    with open(USED_FILE, 'w') as f:
        json.dump(used, f)


def pick_title(argv: list) -> dict:
    # Support: --title "text" anywhere in argv
    if '--title' in argv:
        idx = argv.index('--title')
        if idx + 1 < len(argv):
            return {'number': 0, 'title': argv[idx + 1], 'category': 'General'}

    titles = load_titles()
    used   = load_used()
    unused = [t for t in titles if t['number'] not in used]

    if not unused:
        print("  All 500 titles used — resetting cycle.")
        save_used([])
        unused = titles

    return random.choice(unused)


def mark_used(number: int):
    used = load_used()
    if number not in used:
        used.append(number)
        save_used(used)


# ── Claude CLI ────────────────────────────────────────────────────────────────

def claude(prompt: str) -> str:
    result = subprocess.run(
        f'"{CLAUDE_CMD}" -p "{prompt.replace(chr(34), chr(39))}"',
        capture_output=True, text=True, timeout=120, shell=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude CLI failed:\n{result.stderr}")
    raw = result.stdout.strip()
    if raw.startswith('```'):
        raw = raw.split('```')[1]
        if raw.startswith('json'):
            raw = raw[4:]
        raw = raw.rsplit('```', 1)[0].strip()
    return raw


def generate_caption(title: str, category: str) -> dict:
    raw = claude(CAPTION_PROMPT.format(title=title, category=category))
    return json.loads(raw)


# ── Keyword builder ───────────────────────────────────────────────────────────

_STOP = {'is','are','the','a','an','and','or','for','to','in','on','at','vs',
         'win','now','done','right','your','you','we','our','it','its','be',
         'buy','get','top','best','must','have','new','big','by','of','up'}

def _build_keyword(category: str, title: str) -> str:
    base = CATEGORY_KEYWORDS.get(category.lower(), 'technology india')
    title_words = [w.lower() for w in re.sub(r'[^\w\s]', '', title).split()
                   if w.lower() not in _STOP]
    extra = ' '.join(title_words[:2])
    return f"{base} {extra}".strip()


# ── Pexels image ──────────────────────────────────────────────────────────────

def fetch_pexels_image(category: str, title: str, number: int = 0) -> bytes:
    keyword = _build_keyword(category, title)
    url = (f"https://api.pexels.com/v1/search"
           f"?query={urllib.parse.quote(keyword)}&per_page=15&orientation=portrait")
    req = urllib.request.Request(url, headers={
        'Authorization': PEXELS_API_KEY,
        'User-Agent': 'Mozilla/5.0'
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    photos = data.get('photos', [])
    if not photos:
        raise RuntimeError(f"No Pexels images found for: {keyword}")
    idx = number % len(photos)
    print(f"        Keyword: {keyword} [{idx+1}/{len(photos)}]")
    img_url = photos[idx]['src']['large2x']
    img_req = urllib.request.Request(img_url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(img_req, timeout=15) as resp:
        return resp.read()


# ── Pexels video ──────────────────────────────────────────────────────────────

def fetch_pexels_video(category: str, title: str, number: int = 0) -> bytes:
    keyword = _build_keyword(category, title)

    def _search(extra_params=''):
        url = (f"https://api.pexels.com/videos/search"
               f"?query={urllib.parse.quote(keyword)}&per_page=15{extra_params}")
        req = urllib.request.Request(url, headers={
            'Authorization': PEXELS_API_KEY,
            'User-Agent': 'Mozilla/5.0'
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()).get('videos', [])

    # Prefer portrait, fall back to any orientation
    videos = _search('&orientation=portrait&size=medium')
    if not videos:
        videos = _search('&size=medium')
    if not videos:
        raise RuntimeError(f"No Pexels videos found for: {keyword}")

    idx = number % len(videos)
    print(f"        Keyword: {keyword} [{idx+1}/{len(videos)}]")
    video = videos[idx]

    # Pick medium quality MP4 (not the largest, not the smallest)
    mp4_files = [f for f in video.get('video_files', [])
                 if f.get('file_type') == 'video/mp4']
    mp4_files.sort(key=lambda x: x.get('width', 0))

    if not mp4_files:
        raise RuntimeError("No MP4 files in Pexels video response")

    # Pick near-middle quality to balance size and sharpness
    target = mp4_files[max(0, len(mp4_files) // 2)]
    print(f"        Video: {target.get('width')}x{target.get('height')} "
          f"({target.get('quality', '?')})")

    video_url = target['link']
    req = urllib.request.Request(video_url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read()


# ── Photo overlay (PIL) ───────────────────────────────────────────────────────

def add_text_overlay(image_data: bytes, hook: str, title: str) -> bytes:
    img = Image.open(io.BytesIO(image_data)).convert("RGBA")

    # Resize and crop to standard Reel/portrait dimensions (1080x1920)
    TARGET_W, TARGET_H = 1080, 1920
    w, h = img.size
    if (w / h) > (TARGET_W / TARGET_H):
        scale = TARGET_H / h
        img = img.resize((int(w * scale), TARGET_H), Image.LANCZOS)
        new_w = img.size[0]
        x1 = (new_w - TARGET_W) // 2
        img = img.crop((x1, 0, x1 + TARGET_W, TARGET_H))
    else:
        scale = TARGET_W / w
        img = img.resize((TARGET_W, int(h * scale)), Image.LANCZOS)
        new_h = img.size[1]
        y1 = (new_h - TARGET_H) // 2
        img = img.crop((0, y1, TARGET_W, y1 + TARGET_H))

    W, H = img.size  # now always 1080x1920

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    grad_top = int(H * 0.50)
    for y in range(grad_top, H):
        alpha = int(200 * ((y - grad_top) / (H - grad_top)) ** 0.65)
        draw.line([(0, y), (W, y)], fill=(BRAND_NAVY[0], BRAND_NAVY[1], BRAND_NAVY[2], alpha))

    # Thin blue accent bar at bottom (decorative only)
    draw.rectangle([(0, H - int(H * 0.008)), (W, H)],
                   fill=(BRAND_BLUE[0], BRAND_BLUE[1], BRAND_BLUE[2], 230))

    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    try:
        font_logo    = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", size=int(H * 0.020))
        font_hook    = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", size=int(H * 0.030))
        font_title   = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf",   size=int(H * 0.018))
    except Exception:
        font_logo = font_hook = font_title = ImageFont.load_default()

    # ── Center logo pill ─────────────────────────────────────────────────────
    logo_text = "styleus.co.in"
    logo_pad_x, logo_pad_y = int(W * 0.035), int(H * 0.012)
    bbox = draw.textbbox((0, 0), logo_text, font=font_logo)
    lw, lh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pill_w  = lw + logo_pad_x * 2
    pill_x1 = (W - pill_w) // 2
    pill_x2 = pill_x1 + pill_w
    pill_y1 = int(H * 0.13)
    pill_y2 = pill_y1 + lh + logo_pad_y * 2
    radius  = (pill_y2 - pill_y1) // 2
    draw.rounded_rectangle([pill_x1, pill_y1, pill_x2, pill_y2],
                            radius=radius, fill=(10, 22, 40, 200))
    draw.rounded_rectangle([pill_x1, pill_y1, pill_x2, pill_y2],
                            radius=radius, outline=BRAND_BLUE + (180,), width=2)
    draw.text((pill_x1 + logo_pad_x, pill_y1 + logo_pad_y),
              logo_text, font=font_logo, fill=WHITE + (255,))

    # ── Title text centered below logo pill ──────────────────────────────────
    title_lines = textwrap.wrap(title, width=32)[:1]
    title_y = pill_y2 + int(H * 0.010)
    for line in title_lines:
        tb = draw.textbbox((0, 0), line, font=font_title)
        tx = (W - (tb[2] - tb[0])) // 2
        draw.text((tx, title_y), line, font=font_title, fill=LIGHT_GREY + (200,))

    # ── Hook text (lower third, no blue line) ────────────────────────────────
    pad   = int(W * 0.07)
    max_w = 26

    hook_clean = re.sub(r'[^\x00-\x7F]+', '', hook).strip()
    hook_lines = textwrap.wrap(hook_clean, width=max_w)[:2]
    line_h     = int(H * 0.038)
    hook_total = len(hook_lines) * line_h

    y = H - hook_total - int(H * 0.12)

    for line in hook_lines:
        draw.text((pad + 2, y + 2), line, font=font_hook, fill=(0, 0, 0, 120))
        draw.text((pad, y), line, font=font_hook, fill=WHITE + (255,))
        y += line_h

    final = img.convert("RGB")
    buf   = io.BytesIO()
    final.save(buf, format="JPEG", quality=93)
    return buf.getvalue()


# ── Reel overlay image (PIL, RGBA) ────────────────────────────────────────────

def build_reel_overlay(W: int, H: int, hook: str, title: str) -> Image.Image:
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    # Dark gradient lower 50%
    grad_top = int(H * 0.50)
    for y in range(grad_top, H):
        alpha = int(200 * ((y - grad_top) / (H - grad_top)) ** 0.65)
        draw.line([(0, y), (W, y)],
                  fill=(BRAND_NAVY[0], BRAND_NAVY[1], BRAND_NAVY[2], alpha))

    # Thin blue accent bar at bottom (decorative only)
    draw.rectangle([(0, H - int(H * 0.008)), (W, H)],
                   fill=(BRAND_BLUE[0], BRAND_BLUE[1], BRAND_BLUE[2], 230))

    try:
        font_logo    = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", size=int(H * 0.020))
        font_hook    = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", size=int(H * 0.030))
        font_title   = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf",   size=int(H * 0.018))
    except Exception:
        font_logo = font_hook = font_title = ImageFont.load_default()

    # ── Center logo pill ─────────────────────────────────────────────────────
    logo_text = "styleus.co.in"
    logo_pad_x, logo_pad_y = int(W * 0.035), int(H * 0.012)
    bbox = draw.textbbox((0, 0), logo_text, font=font_logo)
    lw, lh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pill_w  = lw + logo_pad_x * 2
    pill_x1 = (W - pill_w) // 2
    pill_x2 = pill_x1 + pill_w
    pill_y1 = int(H * 0.13)
    pill_y2 = pill_y1 + lh + logo_pad_y * 2
    radius  = (pill_y2 - pill_y1) // 2
    draw.rounded_rectangle([pill_x1, pill_y1, pill_x2, pill_y2],
                            radius=radius, fill=(10, 22, 40, 200))
    draw.rounded_rectangle([pill_x1, pill_y1, pill_x2, pill_y2],
                            radius=radius, outline=BRAND_BLUE + (180,), width=2)
    draw.text((pill_x1 + logo_pad_x, pill_y1 + logo_pad_y),
              logo_text, font=font_logo, fill=WHITE + (255,))

    # ── Title text centered below logo pill ──────────────────────────────────
    title_lines = textwrap.wrap(title, width=32)[:1]
    title_y = pill_y2 + int(H * 0.010)
    for line in title_lines:
        tb = draw.textbbox((0, 0), line, font=font_title)
        tx = (W - (tb[2] - tb[0])) // 2
        draw.text((tx, title_y), line, font=font_title, fill=LIGHT_GREY + (200,))

    # ── Hook text (lower third, no blue line) ────────────────────────────────
    pad   = int(W * 0.07)
    max_w = 26

    hook_clean = re.sub(r'[^\x00-\x7F]+', '', hook).strip()
    hook_lines = textwrap.wrap(hook_clean, width=max_w)[:2]
    line_h     = int(H * 0.038)
    hook_total = len(hook_lines) * line_h

    y = H - hook_total - int(H * 0.12)

    for line in hook_lines:
        draw.text((pad + 2, y + 2), line, font=font_hook, fill=(0, 0, 0, 120))
        draw.text((pad, y), line, font=font_hook, fill=WHITE + (255,))
        y += line_h

    return overlay


# ── Reel video processing (ffmpeg native) ────────────────────────────────────

def _ffmpeg_bin() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return 'ffmpeg'


def process_reel_video(video_data: bytes, hook: str, title: str) -> bytes:
    TARGET_W, TARGET_H = 1080, 1920
    ffmpeg = _ffmpeg_bin()

    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tmp_in:
        tmp_in.write(video_data)
        in_path = tmp_in.name

    overlay_pil = build_reel_overlay(TARGET_W, TARGET_H, hook, title)
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp_ovr:
        overlay_pil.save(tmp_ovr, format='PNG')
        ovr_path = tmp_ovr.name

    out_path = in_path.replace('.mp4', '_out.mp4')

    try:
        # Probe source duration to decide whether to loop
        probe = subprocess.run(
            [ffmpeg, '-v', 'quiet', '-print_format', 'json', '-show_streams', '-i', in_path],
            capture_output=True, text=True,
        )
        # ffprobe is a separate binary; fall back to ffmpeg stderr duration parse
        src_duration = REEL_DURATION  # safe default
        for token in (probe.stdout + probe.stderr).split():
            if token.startswith('Duration:'):
                pass
        # Use ffprobe if available, otherwise assume loop needed
        try:
            import imageio_ffmpeg
            ffprobe = imageio_ffmpeg.get_ffmpeg_exe().replace('ffmpeg', 'ffprobe')
            pr = subprocess.run(
                [ffprobe, '-v', 'quiet', '-print_format', 'json',
                 '-show_entries', 'format=duration', in_path],
                capture_output=True, text=True,
            )
            src_duration = float(json.loads(pr.stdout).get('format', {}).get('duration', REEL_DURATION))
        except Exception:
            pass

        loop_args = ['-stream_loop', '-1'] if src_duration < REEL_DURATION else []

        # Single ffmpeg pass: loop if short → scale+crop to 9:16 → overlay → encode
        filter_graph = (
            f'[0:v]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,'
            f'crop={TARGET_W}:{TARGET_H},setsar=1[v];'
            f'[v][1:v]overlay=0:0,format=yuv420p[out]'
        )

        cmd = [
            ffmpeg, '-y',
            *loop_args, '-i', in_path,
            '-i', ovr_path,
            '-filter_complex', filter_graph,
            '-map', '[out]',
            '-map', '0:a?',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
            '-c:a', 'aac', '-b:a', '128k',
            '-t', str(REEL_DURATION),
            '-movflags', '+faststart',
            out_path,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed:\n{result.stderr[-800:]}")

        with open(out_path, 'rb') as f:
            return f.read()

    finally:
        for p in (in_path, ovr_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass


# ── Upload helpers ────────────────────────────────────────────────────────────

def _multipart_upload(endpoint: str, data: bytes, filename: str, content_type: str) -> str:
    boundary = "----FormBoundary" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{STYLEUS_API_URL}/{endpoint}",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-API-Token": STYLEUS_API_TOKEN,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Upload {e.code} from {endpoint}: {e.read().decode()}")
    relative = result.get('url', '')
    if not relative:
        raise RuntimeError(f"Upload to {endpoint} returned no URL: {result}")
    base = STYLEUS_API_URL.replace('/api', '')
    return f"{base}{relative}"


def upload_to_styleus(image_data: bytes) -> str:
    filename = f"insta_{uuid.uuid4().hex}.jpg"
    return _multipart_upload('upload-image', image_data, filename, 'image/jpeg')


def upload_video_to_styleus(video_data: bytes) -> str:
    filename = f"reel_{uuid.uuid4().hex}.mp4"
    return _multipart_upload('upload-image', video_data, filename, 'video/mp4')


# ── Instagram Graph API ───────────────────────────────────────────────────────

def create_container(image_url: str, caption: str) -> str:
    params = urllib.parse.urlencode({
        'image_url': image_url,
        'caption': caption,
        'access_token': INSTAGRAM_ACCESS_TOKEN,
    })
    req = urllib.request.Request(
        f"{GRAPH_API}/{INSTAGRAM_ACCOUNT_ID}/media",
        data=params.encode(), method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Container error: {e.read().decode()}")
    if 'id' not in result:
        raise RuntimeError(f"Container creation failed: {result}")
    return result['id']


def create_reel_container(video_url: str, caption: str) -> str:
    params = urllib.parse.urlencode({
        'media_type': 'REELS',
        'video_url': video_url,
        'caption': caption,
        'share_to_feed': 'true',
        'access_token': INSTAGRAM_ACCESS_TOKEN,
    })
    req = urllib.request.Request(
        f"{GRAPH_API}/{INSTAGRAM_ACCOUNT_ID}/media",
        data=params.encode(), method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Reel container error: {e.read().decode()}")
    if 'id' not in result:
        raise RuntimeError(f"Reel container creation failed: {result}")
    return result['id']


def wait_for_reel_ready(container_id: str, timeout: int = 360) -> None:
    params = urllib.parse.urlencode({
        'fields': 'status_code',
        'access_token': INSTAGRAM_ACCESS_TOKEN,
    })
    url      = f"{GRAPH_API}/{container_id}?{params}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        with urllib.request.urlopen(url, timeout=15) as resp:
            status = json.loads(resp.read()).get('status_code', '')
        print(f"        Reel status: {status}")
        if status == 'FINISHED':
            return
        if status in ('ERROR', 'EXPIRED'):
            raise RuntimeError(f"Reel processing failed: {status}")
        time.sleep(15)
    raise RuntimeError("Reel processing timed out after 6 minutes")


def publish_container(creation_id: str) -> str:
    params = urllib.parse.urlencode({
        'creation_id': creation_id,
        'access_token': INSTAGRAM_ACCESS_TOKEN,
    })
    req = urllib.request.Request(
        f"{GRAPH_API}/{INSTAGRAM_ACCOUNT_ID}/media_publish",
        data=params.encode(), method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Publish error: {e.read().decode()}")
    if 'id' not in result:
        raise RuntimeError(f"Publishing failed: {result}")
    return result['id']


# ── Google Drive upload ───────────────────────────────────────────────────────

def _gdrive_service():
    if not GDRIVE_OK:
        raise RuntimeError("Google API libraries not installed — run: pip install google-api-python-client google-auth-oauthlib google-auth-httplib2")
    creds = None
    if os.path.exists(GDRIVE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GDRIVE_TOKEN_FILE, GDRIVE_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GDRIVE_CREDS_FILE, GDRIVE_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GDRIVE_TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
    return build('drive', 'v3', credentials=creds)


def _get_or_create_folder(service, name: str) -> str:
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=q, fields='files(id)').execute()
    files = results.get('files', [])
    if files:
        return files[0]['id']
    folder = service.files().create(
        body={'name': name, 'mimeType': 'application/vnd.google-apps.folder'},
        fields='id',
    ).execute()
    return folder['id']


def upload_reel_to_drive(video_path: str, filename: str) -> str:
    service   = _gdrive_service()
    folder_id = _get_or_create_folder(service, GDRIVE_FOLDER_NAME)
    media     = MediaFileUpload(video_path, mimetype='video/mp4', resumable=True)
    file_meta = {'name': filename, 'parents': [folder_id]}
    uploaded  = service.files().create(body=file_meta, media_body=media, fields='id,webViewLink').execute()
    # Make it anyone-with-link viewable
    service.permissions().create(
        fileId=uploaded['id'],
        body={'type': 'anyone', 'role': 'reader'},
    ).execute()
    return uploaded.get('webViewLink', '')


# ── Telegram notifications ────────────────────────────────────────────────────

def send_telegram(message: str):
    for token, chat in zip(TELEGRAM_BOT_TOKENS, TELEGRAM_CHAT_IDS):
        try:
            payload = urllib.parse.urlencode({
                'chat_id': chat, 'text': message, 'parse_mode': 'HTML'
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=payload,
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"  Telegram notify failed ({chat}): {e}")


# ── Validation ────────────────────────────────────────────────────────────────

def validate_config():
    missing = []
    if not INSTAGRAM_ACCOUNT_ID or 'your_' in INSTAGRAM_ACCOUNT_ID:
        missing.append('INSTAGRAM_ACCOUNT_ID')
    if not INSTAGRAM_ACCESS_TOKEN or 'your_' in INSTAGRAM_ACCESS_TOKEN:
        missing.append('INSTAGRAM_ACCESS_TOKEN')
    if not PEXELS_API_KEY:
        missing.append('PEXELS_API_KEY')
    if missing:
        print(f"ERROR: Missing credentials in .env: {', '.join(missing)}")
        sys.exit(1)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    validate_config()

    is_reel = '--reel' in sys.argv
    entry   = pick_title(sys.argv)
    title    = entry['title']
    category = entry['category']
    number   = entry['number']
    mode     = 'REEL' if is_reel else 'PHOTO'

    print(f"[{datetime.now():%Y-%m-%d %H:%M}] [{mode}] #{number} — {title}  [{category}]")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt > 1:
                print(f"  Retry {attempt}/{MAX_RETRIES} (waiting {RETRY_DELAY}s)...")
                time.sleep(RETRY_DELAY)

            # 1 — Generate caption
            print(f"  [1/4] Generating caption... (attempt {attempt})")
            content = generate_caption(title, category)
            caption = content.get('caption', '')
            hook    = content.get('hook', title)
            print(f"        Hook   : {hook}")
            print(f"        Caption: {caption[:80]}...")

            if is_reel:
                # 2 — Fetch Pexels video
                print(f"  [2/4] Fetching video from Pexels [{category}]...")
                video_raw = fetch_pexels_video(category, title, number)
                print(f"        Downloaded {len(video_raw)//1024} KB")

                # 3 — Process reel (crop + overlay + encode)
                print("  [3/4] Processing reel video (crop + overlay + encode)...")
                video_branded = process_reel_video(video_raw, hook, title)
                print(f"        Processed {len(video_branded)//1024} KB")

                # 4 — Save locally (user uploads manually with trending music)
                out_dir = os.path.join(HERE, 'reels_output')
                os.makedirs(out_dir, exist_ok=True)
                safe_title = re.sub(r'[^\w\s-]', '', title).strip().replace(' ', '_')[:40]
                base_name  = f"{number:03d}_{safe_title}"
                video_path = os.path.join(out_dir, f"{base_name}.mp4")
                caption_path = os.path.join(out_dir, f"{base_name}.txt")

                with open(video_path, 'wb') as f:
                    f.write(video_branded)
                with open(caption_path, 'w', encoding='utf-8') as f:
                    f.write(f"HOOK: {hook}\n\n{caption}")

                mark_used(number)
                print(f"\n  Reel saved -> {video_path}")
                print(f"  Caption   -> {caption_path}")

                # Upload to Google Drive
                drive_link = ''
                if GDRIVE_OK and os.path.exists(GDRIVE_CREDS_FILE):
                    print("  Uploading to Google Drive...")
                    try:
                        drive_link = upload_reel_to_drive(video_path, f"{base_name}.mp4")
                        print(f"  Drive link -> {drive_link}")
                    except Exception as drive_err:
                        print(f"  Drive upload failed: {drive_err}")

                send_telegram(
                    f"🎬 <b>Reel Ready</b>\n"
                    f"<b>{title}</b>\n"
                    f"Category: {category}\n"
                    + (f"Drive: {drive_link}" if drive_link else "")
                )

                print(f"  Titles used so far: {len(load_used())}/500")
                print(f"\n  Next steps:")
                print(f"  1. Open Instagram app")
                print(f"  2. Create Reel -> pick the video above")
                print(f"  3. Add trending music")
                print(f"  4. Paste caption from the .txt file")
                return 0

            else:
                # 2 — Fetch Pexels image
                print(f"  [2/4] Fetching image from Pexels [{category}]...")
                image_data = fetch_pexels_image(category, title, number)
                print(f"        Downloaded {len(image_data)//1024} KB")

                # 3 — Add overlay and upload
                print("  [3/4] Adding text overlay and uploading...")
                branded = add_text_overlay(image_data, hook, title)
                public_url = upload_to_styleus(branded)
                print(f"        Uploaded: {public_url}")

                # 4 — Post photo
                print("  [4/4] Posting to Instagram...")
                creation_id = create_container(public_url, caption)
                time.sleep(3)
                post_id = publish_container(creation_id)

            mark_used(number)
            print(f"\n  Done -> Post ID: {post_id}")  # noqa: F821
            print(f"  Titles used so far: {len(load_used())}/500")
            send_telegram(
                f"📷 <b>Instagram Post Published</b>\n"
                f"<b>{title}</b>\n"
                f"Category: {category}\n"
                f"Post ID: {post_id}"
            )
            return 0

        except Exception as e:
            print(f"  ERROR (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt == MAX_RETRIES:
                print("  All retries exhausted. Giving up.")
                return 1

    return 1


if __name__ == '__main__':
    sys.exit(main())
