#!/usr/bin/env python
"""
Daily blog draft poster for Styleus.
Uses the `claude` CLI (Claude Code) to generate content — no separate API key needed.
Picks the topic based on the current day of the week (UK time).
Run via Windows Task Scheduler daily at 13:50 UK time.

Checks performed before posting:
  1. Claude self-evaluation  — flags robotic/generic writing, rewrites if score < 7
  2. difflib similarity      — compares against existing posts, regenerates if > 40% similar
  3. Google spot-check       — searches 3 sentences in quotes, flags exact web matches

Usage:
    python post_blog_draft.py                        # uses today's scheduled topic
    python post_blog_draft.py --topic "custom topic" # override topic
"""

import difflib
import html
import json
import os
import pathlib
import random
import re
import subprocess
import sys
import time
import uuid
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
import zoneinfo

def _load_env():
    env_path = pathlib.Path(__file__).parent / '.env'
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            os.environ.setdefault(k.strip(), v.strip())

_load_env()

TELEGRAM_BOT_TOKENS = [t.strip() for t in os.environ.get('TELEGRAM_BOT_TOKEN', '').split(',') if t.strip()]
TELEGRAM_CHAT_IDS   = [c.strip() for c in os.environ.get('TELEGRAM_CHAT_ID', '').split(',') if c.strip()]


def send_telegram(message: str):
    for token, chat in zip(TELEGRAM_BOT_TOKENS, TELEGRAM_CHAT_IDS):
        try:
            payload = urllib.parse.urlencode({
                'chat_id': chat, 'text': message, 'parse_mode': 'HTML'
            }).encode()
            urllib.request.urlopen(
                urllib.request.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=payload,
                ),
                timeout=10,
            )
        except Exception as e:
            print(f"  Telegram notify failed ({chat}): {e}")


AUTHORS = [
    "Written By Ashok Kumar, tech specialist at Styleus",
    "Written By Aman Kumar, tech specialist at Styleus",
]

STATE_FILE = pathlib.Path(r"C:\Users\spli5\AppData\Local\Temp\styleus_blog_state.json")

TOPIC_GEN_PROMPT = (
    'You are a content strategist for Styleus (styleus.co.in), an Indian tech marketplace. '
    'Suggest one fresh, specific blog topic relevant to Indian tech buyers in 2026. '
    'It must be practical, search-friendly, and clearly different from these recent topics: {recent}. '
    'Respond with ONLY the topic as a plain string — no quotes, no explanation, no punctuation at the end.'
)

MAX_RETRIES       = 5
RETRY_DELAY       = 30    # seconds between retries
SIMILARITY_LIMIT  = 0.40  # regenerate if > 40% similar to an existing post
QUALITY_THRESHOLD = 7     # regenerate if Claude scores the post below this

# ── Config ────────────────────────────────────────────────────────────────────
API_URL      = "https://styleus.co.in/api/blog/draft"
API_TOKEN    = "ad1c31490bb470ac82e18c1a9f9a24658dca639b63d6398f8554ad0d067bcc7a"
CLAUDE_CMD   = r"C:\Users\spli5\AppData\Roaming\npm\claude.cmd"
PEXELS_KEY   = "LyPEJ7offkzyh4yWM5v36RprxsyLexlDTeNDXsM28o6osPWGRLLNFLGG"

MONTHLY_TOPICS = {
     1: "how to choose between refurbished and new laptops in India 2026",
     2: "top 5 reasons to upgrade your RAM in 2026 and how to do it",
     3: "SSD vs HDD: which storage is right for your PC in India",
     4: "best antivirus software for Indian home and small business users 2026",
     5: "how to set up CCTV at home in India: a beginner's buying guide",
     6: "Microsoft Office vs Microsoft 365: which plan saves you more money",
     7: "5 signs your laptop needs a repair or upgrade before it dies on you",
     8: "best budget smartphones under ₹15,000 in India 2026",
     9: "how to choose the right printer for home and office use in India",
    10: "smart TV buying guide: what to look for in an Indian home 2026",
    11: "best wireless earbuds under ₹2,000 in India: worth it or not",
    12: "how to speed up a slow Windows PC without spending a rupee",
    13: "power bank buying guide: specs that actually matter in India",
    14: "best gaming keyboards for Indian gamers on a budget 2026",
    15: "how to secure your home Wi-Fi: simple steps most people skip",
    16: "webcam buying guide for work-from-home setups in India 2026",
    17: "best monitors for home office under ₹15,000 in India",
    18: "how to extend your laptop battery life in 5 practical steps",
    19: "smart home on a budget: best smart plugs and devices in India 2026",
    20: "how to choose the right UPS for your home PC in India",
    21: "best routers for fast and reliable home internet in India 2026",
    22: "pen drive vs portable SSD: which should you buy in India",
    23: "best budget tablets in India for students and professionals 2026",
    24: "how to clean and maintain your laptop to make it last longer",
    25: "best smartwatches under ₹5,000 in India: value picks for 2026",
    26: "how to back up your data the right way: a simple guide for Indians",
    27: "desktop vs laptop: which is better for working from home in India",
    28: "best budget gaming GPUs available in India in 2026",
    29: "how to pick the right laptop charger or adapter in India",
    30: "best noise-cancelling headphones under ₹3,000 in India 2026",
    31: "RGB vs performance: what actually matters when building a PC in India",
}
# ─────────────────────────────────────────────────────────────────────────────

PROMPT_TEMPLATE = (
    'Write a blog post for Styleus (styleus.co.in), an Indian tech marketplace, about: "{topic}". '
    'The post must read like it was written by a real person — someone who actually uses and understands this tech, '
    'not a content machine. Guidelines: '
    'Write in first-person or second-person ("you", "I", "we") — never passive or corporate voice. '
    'Start with a relatable situation or frustration the reader has actually felt — no generic intros. '
    'Use natural Indian English — occasional phrases like "honestly", "here\'s the thing", "trust me", "no cap" are fine. '
    'Share a specific opinion or recommendation — don\'t sit on the fence. '
    'Keep sentences short and punchy. Mix short paragraphs with bullet points. '
    'Avoid buzzwords like "leverage", "seamless", "robust", "cutting-edge", "dive into". '
    'Avoid starting every section with the topic name. Vary how you open each paragraph. '
    'End with a natural, low-pressure CTA — not salesy. Link to [our collection](https://styleus.co.in/products). '
    'Respond with ONLY a raw JSON object (no markdown fences, no explanation) with keys: '
    '"title" (punchy, curiosity-driven, 50-60 chars — can ask a question or make a bold claim), '
    '"excerpt" (1-2 sentences, 120-160 chars, sounds like something a friend texted you, ends with a period), '
    '"body" (500-650 word Markdown, ## subheadings, mix of short paragraphs and bullets), '
    '"cover_image_url" (empty string).'
)

EVAL_PROMPT_TEMPLATE = (
    'Rate this blog post excerpt for a tech marketplace. '
    'Score 1-10 on: human tone, clear opinion, no buzzwords, relatable opening. '
    'Respond with ONLY raw JSON, no fences: '
    '{{"score": <int>, "issues": [<strings>], "verdict": "approve" or "rewrite"}} '
    'Verdict must be "approve" if score >= 7, else "rewrite". '
    'Post excerpt (first 500 chars): {excerpt}'
)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state))


def generate_topic_via_claude(recent_topics: list) -> str:
    recent = "; ".join(recent_topics[-10:]) if recent_topics else "none"
    return claude(TOPIC_GEN_PROMPT.format(recent=recent)).strip().strip('"').strip("'")


def pick_topic(argv) -> tuple:
    """Returns (topic, today_str, is_extra_run)."""
    uk_now = datetime.now(tz=zoneinfo.ZoneInfo("Europe/London"))
    today = uk_now.strftime("%Y-%m-%d")

    if len(argv) > 2 and argv[1] == "--topic":
        return " ".join(argv[2:]), today, False

    state = load_state()
    if state.get("last_run_date") == today:
        recent = state.get("recent_topics", [])
        topic = generate_topic_via_claude(recent)
        return topic, today, True

    return MONTHLY_TOPICS[uk_now.day], today, False


def claude(prompt: str) -> str:
    result = subprocess.run(
        [CLAUDE_CMD, "-p", prompt],
        capture_output=True, text=True, timeout=120, shell=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed:\n{result.stderr}")
    raw = result.stdout.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()
    return raw


def generate_post(topic: str) -> dict:
    return json.loads(claude(PROMPT_TEMPLATE.format(topic=topic)))


# ── Check 1: Claude self-evaluation ──────────────────────────────────────────
def claude_evaluate(data: dict) -> dict:
    excerpt = (data.get("body", ""))[:500].replace('"', "'")
    raw = claude(EVAL_PROMPT_TEMPLATE.format(excerpt=excerpt))
    return json.loads(raw)


# ── Check 2: difflib similarity against existing posts ───────────────────────
def fetch_existing_posts() -> list[str]:
    try:
        req = urllib.request.Request(
            "https://styleus.co.in/blog",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            page = resp.read().decode("utf-8", errors="ignore")
        # Extract visible text from blog post excerpts
        texts = re.findall(r'<p[^>]*class="[^"]*line-clamp[^"]*"[^>]*>(.*?)</p>', page, re.DOTALL)
        return [html.unescape(re.sub(r"<[^>]+>", "", t)).strip() for t in texts if t.strip()]
    except Exception:
        return []


def similarity_check(new_body: str, existing_posts: list[str]) -> tuple[bool, float]:
    if not existing_posts:
        return False, 0.0
    scores = [
        difflib.SequenceMatcher(None, new_body.lower(), post.lower()).ratio()
        for post in existing_posts
    ]
    top = max(scores)
    return top > SIMILARITY_LIMIT, round(top, 2)


# ── Check 3: Google spot-check ───────────────────────────────────────────────
def extract_sentences(text: str, n: int = 3) -> list[str]:
    sentences = re.split(r'(?<=[.!?])\s+', re.sub(r'[#*`>]', '', text))
    sentences = [s.strip() for s in sentences if len(s.strip()) > 60]
    step = max(1, len(sentences) // n)
    return sentences[::step][:n]


def google_spot_check(body: str) -> list[str]:
    sentences = extract_sentences(body)
    matches = []
    for sentence in sentences:
        query = f'"{sentence}" -site:styleus.co.in'
        encoded = urllib.request.quote(query)
        url = f"https://www.google.com/search?q={encoded}&num=3"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                page = resp.read().decode("utf-8", errors="ignore")
            # If Google returns results containing the exact sentence
            if sentence[:40].lower() in page.lower():
                matches.append(sentence[:80])
        except Exception:
            pass
        time.sleep(2)  # be polite to Google
    return matches


def fetch_and_upload_image(topic: str) -> str:
    # Step 1 — search Pexels for a relevant photo
    keyword = " ".join(topic.split()[:4])
    search_url = f"https://api.pexels.com/v1/search?query={urllib.request.quote(keyword)}&per_page=5&orientation=landscape"
    req = urllib.request.Request(search_url, headers={"Authorization": PEXELS_KEY, "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        photos = result.get("photos", [])
        if not photos:
            return ""
        image_url = photos[0]["src"]["large"]

        # Step 2 — download the image
        img_req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(img_req, timeout=15) as img_resp:
            image_data = img_resp.read()
            content_type = img_resp.headers.get("Content-Type", "image/jpeg")

        ext = ".jpg" if "jpeg" in content_type else ".png"
        filename = f"pexels_image{ext}"

        # Step 3 — upload to our server
        boundary = "----FormBoundary" + uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode() + image_data + f"\r\n--{boundary}--\r\n".encode()

        upload_req = urllib.request.Request(
            API_URL.replace("/blog/draft", "/upload-image"),
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-API-Token": API_TOKEN,
            },
            method="POST",
        )
        with urllib.request.urlopen(upload_req, timeout=30) as upload_resp:
            upload_result = json.loads(upload_resp.read())
        return upload_result.get("url", "")

    except Exception as e:
        print(f"        Image upload failed: {e}")
        return ""


def post_draft(data: dict) -> dict:
    payload = json.dumps(data).encode()
    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={"Content-Type": "application/json", "X-API-Token": API_TOKEN},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main():
    topic, today_str, is_extra = pick_topic(sys.argv)
    run_label = "extra run — self-generated topic" if is_extra else "scheduled topic"
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Topic ({run_label}): {topic}")

    print("  Fetching existing posts for similarity check...")
    existing_posts = fetch_existing_posts()
    print(f"  Found {len(existing_posts)} existing post excerpts.")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt > 1:
                print(f"\n  Retry {attempt}/{MAX_RETRIES} (waiting {RETRY_DELAY}s)...")
                time.sleep(RETRY_DELAY)

            # ── Generate ──────────────────────────────────────────────────────
            print(f"\n  [1/4] Generating content... (attempt {attempt})")
            data = generate_post(topic)
            print(f"        Title  : {data.get('title', '')}")
            print(f"        Excerpt: {data.get('excerpt', '')[:80]}...")
            print(f"        Body   : {len(data.get('body', ''))} chars")

            # ── Check 1: Claude evaluation ────────────────────────────────────
            print("  [2/4] Claude self-evaluation...")
            evaluation = claude_evaluate(data)
            score   = evaluation.get("score", 0)
            verdict = evaluation.get("verdict", "rewrite")
            issues  = evaluation.get("issues", [])
            print(f"        Score: {score}/10  Verdict: {verdict}")
            if issues:
                for issue in issues:
                    print(f"        - {issue}")
            if verdict == "rewrite":
                print("        Failed quality check — regenerating.")
                continue

            # ── Check 2: difflib similarity ───────────────────────────────────
            print("  [3/4] Similarity check against existing posts...")
            too_similar, sim_score = similarity_check(data.get("body", ""), existing_posts)
            print(f"        Similarity score: {sim_score:.0%}")
            if too_similar:
                print(f"        Too similar to existing content ({sim_score:.0%} > {SIMILARITY_LIMIT:.0%}) — regenerating.")
                continue

            # ── Check 3: Google spot-check ────────────────────────────────────
            print("  [4/4] Google spot-check (3 sentences)...")
            matches = google_spot_check(data.get("body", ""))
            if matches:
                print(f"        Found {len(matches)} sentence(s) matching web content:")
                for m in matches:
                    print(f"        - \"{m}...\"")
                print("        Possible plagiarism detected — regenerating.")
                continue
            print("        No matches found.")

            # ── Fetch and upload cover image ───────────────────────────────────
            print("  [+] Fetching image from Pexels and uploading to server...")
            image_url = fetch_and_upload_image(topic)
            data["cover_image_url"] = image_url
            print(f"        Saved: {image_url}" if image_url else "        No image found, posting without.")

            # ── Append random author byline ───────────────────────────────────
            author = random.choice(AUTHORS)
            data["body"] = data["body"].rstrip() + f"\n\n---\n\n*{author}*"

            # ── All checks passed — post ──────────────────────────────────────
            print("\n  All checks passed. Posting draft to Styleus...")
            result = post_draft(data)
            print(f"  Done -> id={result['id']}  slug={result['slug']}")
            print(f"  Review at: https://styleus.co.in{result['admin_url']}")

            state = load_state()
            recent = state.get("recent_topics", [])
            recent.append(topic)
            save_state({"last_run_date": today_str, "recent_topics": recent[-30:]})
            send_telegram(
                f"✍️ <b>Blog Draft Posted</b>\n"
                f"<b>{data.get('title', topic)}</b>\n"
                f"Topic: {topic}\n"
                f"Review: https://styleus.co.in{result['admin_url']}"
            )
            return 0

        except Exception as e:
            print(f"  ERROR (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt == MAX_RETRIES:
                print("  All retries exhausted. Giving up.")
                return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
