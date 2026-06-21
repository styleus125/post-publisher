#!/usr/bin/env python
"""
Posts Advanced: Uses images from a user-configured local folder instead of Pexels.

Usage:
    python instagram_post_advanced.py generate --folder "C:/path/to/images" [--title "text"]
    python instagram_post_advanced.py publish  --id <preview_id> --caption "full caption"
"""

import argparse
import json
import os
import re
import sys
import uuid

os.environ.setdefault('PYTHONUTF8', '1')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import time

HERE        = os.path.dirname(os.path.abspath(__file__))
PREVIEW_DIR = os.path.join(HERE, 'tmp_previews')
ADV_CONFIG  = os.path.join(HERE, 'advanced_config.json')
IMAGE_EXTS  = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff'}

# Reuse core logic from the existing script
sys.path.insert(0, HERE)
from instagram_post import (
    generate_caption, add_text_overlay,
    upload_to_styleus, create_container, publish_container,
    send_telegram, validate_config,
    load_env,
)

load_env('.env')


# ── Config helpers ────────────────────────────────────────────────────────────

def load_adv_config() -> dict:
    if not os.path.exists(ADV_CONFIG):
        return {'posts_folder': '', 'used_images': []}
    with open(ADV_CONFIG, encoding='utf-8') as f:
        data = json.load(f)
    data.setdefault('posts_folder', '')
    data.setdefault('used_images', [])
    return data


def save_adv_config(cfg: dict):
    with open(ADV_CONFIG, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)


# ── Image picking ─────────────────────────────────────────────────────────────

def get_next_image(folder: str) -> tuple[str, str]:
    """Return (full_path, filename) of the next unused image in folder."""
    if not os.path.isdir(folder):
        raise RuntimeError(f"Folder not found: {folder}")

    all_images = sorted(
        f for f in os.listdir(folder)
        if os.path.splitext(f)[1].lower() in IMAGE_EXTS
    )
    if not all_images:
        raise RuntimeError(f"No images found in: {folder}")

    cfg  = load_adv_config()
    used = cfg.get('used_images', [])

    unused = [f for f in all_images if f not in used]
    if not unused:
        print("  All images used — resetting cycle.")
        cfg['used_images'] = []
        save_adv_config(cfg)
        unused = all_images

    chosen = unused[0]
    return os.path.join(folder, chosen), chosen


def mark_image_used(filename: str):
    cfg = load_adv_config()
    used = cfg.get('used_images', [])
    if filename not in used:
        used.append(filename)
        cfg['used_images'] = used
        save_adv_config(cfg)


# ── Generate command ──────────────────────────────────────────────────────────

def cmd_generate(folder: str, title: str | None = None):
    os.makedirs(PREVIEW_DIR, exist_ok=True)

    print(f"[Posts Advanced] Folder: {folder}")
    img_path, img_filename = get_next_image(folder)
    print(f"  Selected image : {img_filename}")

    if not title:
        stem  = os.path.splitext(img_filename)[0]
        title = re.sub(r'[\-_]+', ' ', stem).title()
        print(f"  Auto-title     : {title}")

    category = 'styleus.co.in specials'

    print("  [1/2] Generating caption via Claude...")
    content = generate_caption(title, category)
    caption = content.get('caption', '')
    hook    = content.get('hook', title)
    print(f"  Hook    : {hook}")
    print(f"  Caption : {caption[:80]}...")

    print("  [2/2] Applying brand overlay to image...")
    with open(img_path, 'rb') as f:
        image_data = f.read()
    branded = add_text_overlay(image_data, hook, title)
    print(f"  Overlay applied ({len(branded) // 1024} KB)")

    preview_id   = uuid.uuid4().hex
    preview_path = os.path.join(PREVIEW_DIR, f"{preview_id}.jpg")
    meta_path    = os.path.join(PREVIEW_DIR, f"{preview_id}.json")

    with open(preview_path, 'wb') as f:
        f.write(branded)

    hashtags = re.findall(r'#\w+', caption)
    # Caption without trailing hashtag block for the editable field
    caption_body = re.split(r'\n+#', caption)[0].strip()

    meta = {
        'preview_id'     : preview_id,
        'image_filename' : img_filename,
        'folder_path'    : folder,
        'title'          : title,
        'caption'        : caption,
        'caption_body'   : caption_body,
        'hook'           : hook,
        'hashtags'       : hashtags,
    }
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False)

    mark_image_used(img_filename)
    print(f"  Marked as used : {img_filename}")
    print(f"  Preview ready  : {preview_id}")
    # Sentinel line parsed by the UI
    print(f"__PREVIEW__{json.dumps(meta, ensure_ascii=False)}")


# ── Publish command ───────────────────────────────────────────────────────────

def cmd_publish(preview_id: str, caption: str):
    preview_id   = re.sub(r'[^a-f0-9]', '', preview_id)
    preview_path = os.path.join(PREVIEW_DIR, f"{preview_id}.jpg")
    meta_path    = os.path.join(PREVIEW_DIR, f"{preview_id}.json")

    if not os.path.exists(preview_path):
        print(f"ERROR: Preview not found: {preview_id}")
        sys.exit(1)

    with open(meta_path, encoding='utf-8') as f:
        meta = json.load(f)

    with open(preview_path, 'rb') as f:
        branded = f.read()

    print(f"[Posts Advanced] Publishing: {meta['image_filename']}")

    print("  Uploading branded image to Styleus CDN...")
    public_url = upload_to_styleus(branded)
    print(f"  CDN URL: {public_url}")

    print("  Creating Instagram media container...")
    creation_id = create_container(public_url, caption)
    print(f"  Container: {creation_id}")

    time.sleep(3)

    print("  Publishing to Instagram...")
    post_id = publish_container(creation_id)
    print(f"\n  Done -> Post ID: {post_id}")

    print(f"  Images used in this cycle: {len(load_adv_config().get('used_images', []))}")

    send_telegram(
        f"📷 <b>Posts Advanced Published</b>\n"
        f"<b>{meta['title']}</b>\n"
        f"Image: {meta['image_filename']}\n"
        f"Post ID: {post_id}"
    )

    for p in (preview_path, meta_path):
        try:
            os.unlink(p)
        except OSError:
            pass

    # Sentinel line parsed by the UI
    print(f"__PUBLISHED__{post_id}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(prog='instagram_post_advanced')
    sub    = parser.add_subparsers(dest='cmd', required=True)

    gen = sub.add_parser('generate')
    gen.add_argument('--folder', required=True)
    gen.add_argument('--title',  default='')

    pub = sub.add_parser('publish')
    pub.add_argument('--id',      required=True)
    pub.add_argument('--caption', required=True)

    args = parser.parse_args()
    validate_config()

    if args.cmd == 'generate':
        cmd_generate(args.folder, args.title.strip() or None)
    elif args.cmd == 'publish':
        cmd_publish(args.id, args.caption)


if __name__ == '__main__':
    main()
