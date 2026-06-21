#!/usr/bin/env python
"""Flask UI for the Styleus Instagram post agent."""

import os
import re
import sys
import json
import logging
import subprocess
from datetime import datetime
from flask import Flask, render_template, request, Response, stream_with_context, jsonify, send_file
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from zoneinfo import ZoneInfo

app = Flask(__name__)
HERE = os.path.dirname(os.path.abspath(__file__))
USED_FILE     = os.path.join(HERE, 'instagram_used_titles.json')
LOG_FILE      = os.path.join(HERE, 'agent.log')
SCHEDULE_FILE = os.path.join(HERE, 'schedule.json')
ADV_CONFIG    = os.path.join(HERE, 'advanced_config.json')
PREVIEW_DIR   = os.path.join(HERE, 'tmp_previews')

# ── File logger ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
    ],
)
logger = logging.getLogger('styleus')
logging.getLogger('apscheduler').setLevel(logging.INFO)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _used_count():
    if not os.path.exists(USED_FILE):
        return 0
    with open(USED_FILE) as f:
        return len(json.load(f))


def _load_schedule():
    defaults = {
        'photo': {'enabled': False, 'slots': []},
        'reel':  {'enabled': False, 'slots': []},
        'blog':  {'enabled': False, 'slots': []},
    }
    if not os.path.exists(SCHEDULE_FILE):
        return defaults
    with open(SCHEDULE_FILE) as f:
        data = json.load(f)
    for mode, dflt in defaults.items():
        if mode not in data:
            data[mode] = dflt
            continue
        # migrate old times/days format — no date info, drop it
        if 'slots' not in data[mode]:
            data[mode]['slots'] = []
        for old_key in ('times', 'days', 'time'):
            data[mode].pop(old_key, None)
    return data


def _save_schedule(data: dict):
    with open(SCHEDULE_FILE, 'w') as f:
        json.dump(data, f)


# ── Scheduler ─────────────────────────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone='Asia/Kolkata')


SCRIPT_MAP = {
    'photo': (os.path.join(HERE, 'instagram_post.py'), []),
    'reel':  (os.path.join(HERE, 'instagram_post.py'), ['--reel', '--publish']),
    'blog':  (os.path.join(HERE, 'post_blog_draft.py'), []),
}


def _make_runner(mode: str):
    def _run():
        logger.info(f"Scheduled {mode} triggered")
        script, extra_args = SCRIPT_MAP[mode]
        cmd = [sys.executable, script] + extra_args
        env = os.environ.copy()
        env['PYTHONUTF8'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', cwd=HERE, env=env)
        for line in result.stdout.splitlines():
            logger.info(line)
        if result.returncode == 0:
            logger.info(f"Scheduled {mode} completed successfully")
        else:
            logger.error(f"Scheduled {mode} failed (exit {result.returncode})")
    _run.__name__ = f'run_{mode}'
    return _run


def _apply_schedule():
    scheduler.remove_all_jobs()
    sched = _load_schedule()
    ist = ZoneInfo('Asia/Kolkata')
    now = datetime.now(ist)
    for mode in ('photo', 'reel', 'blog'):
        cfg = sched.get(mode, {})
        if not cfg.get('enabled'):
            continue
        for slot in cfg.get('slots', []):
            date_str = slot.get('date', '')
            time_str = slot.get('time', '00:00')
            if not date_str:
                continue
            try:
                year, month, day = map(int, date_str.split('-'))
                hour, minute     = map(int, time_str.split(':'))
                run_date = datetime(year, month, day, hour, minute, tzinfo=ist)
            except Exception as exc:
                logger.warning(f"Bad slot {mode} {date_str} {time_str}: {exc}")
                continue
            if run_date <= now:
                logger.info(f"Skipping past slot: {mode} on {date_str} at {time_str}")
                continue
            job_id = f'once_{mode}_{date_str}_{time_str.replace(":", "")}'
            scheduler.add_job(
                _make_runner(mode),
                DateTrigger(run_date=run_date),
                id=job_id,
                replace_existing=True,
                misfire_grace_time=3600,
            )
            logger.info(f"Schedule set: {mode} once on {date_str} at {time_str} IST")


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html', used=_used_count(), schedule=_load_schedule())


@app.route('/blog')
def blog():
    topic = request.args.get('topic', '').strip()
    cmd   = [sys.executable, os.path.join(HERE, 'post_blog_draft.py')]
    if topic:
        cmd.extend(['--topic', topic])

    logger.info(f"Blog post started" + (f" — topic={topic}" if topic else " — scheduled topic"))

    def generate():
        env = os.environ.copy()
        env['PYTHONUTF8'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', cwd=HERE, env=env,
        )
        for line in process.stdout:
            stripped = line.rstrip()
            logger.info(stripped)
            yield f"data: {stripped}\n\n"
        process.wait()
        if process.returncode == 0:
            logger.info("Blog post completed successfully")
        else:
            logger.error(f"Blog post failed (exit {process.returncode})")
        yield f"data: __EXIT__{process.returncode}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/post')
def post():
    mode    = request.args.get('mode', 'photo')
    title   = request.args.get('title', '').strip()
    publish = request.args.get('publish', '0') == '1'
    drive   = request.args.get('drive',   '0') == '1'

    cmd = [sys.executable, os.path.join(HERE, 'instagram_post.py')]
    if mode == 'reel':
        cmd.append('--reel')
    if title:
        cmd.extend(['--title', title])
    if publish:
        cmd.append('--publish')
    if drive:
        cmd.append('--drive')

    logger.info(f"Manual post started — mode={mode}" + (f", title={title}" if title else ""))

    def generate():
        env = os.environ.copy()
        env['PYTHONUTF8'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', cwd=HERE, env=env,
        )
        for line in process.stdout:
            stripped = line.rstrip()
            logger.info(stripped)
            yield f"data: {stripped}\n\n"
        process.wait()
        if process.returncode == 0:
            logger.info("Manual post completed successfully")
        else:
            logger.error(f"Manual post failed (exit {process.returncode})")
        yield f"data: __EXIT__{process.returncode}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/schedule', methods=['GET', 'POST'])
def schedule():
    if request.method == 'POST':
        data = request.get_json()
        _save_schedule(data)
        _apply_schedule()
        logger.info(f"Schedule updated: {data}")
        return jsonify({'ok': True})
    return jsonify(_load_schedule())


@app.route('/logs')
def logs():
    lines = int(request.args.get('lines', 150))
    if not os.path.exists(LOG_FILE):
        return jsonify({'log': ''})
    with open(LOG_FILE, encoding='utf-8') as f:
        all_lines = f.readlines()
    return jsonify({'log': ''.join(all_lines[-lines:])})


# ── Advanced config helpers ───────────────────────────────────────────────────

def _load_adv_config() -> dict:
    if not os.path.exists(ADV_CONFIG):
        return {'posts_folder': '', 'used_images': []}
    with open(ADV_CONFIG, encoding='utf-8') as f:
        return json.load(f)


def _save_adv_config(data: dict):
    with open(ADV_CONFIG, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


# ── Posts Advanced routes ─────────────────────────────────────────────────────

@app.route('/advanced/posts/config', methods=['GET', 'POST'])
def adv_posts_config():
    if request.method == 'POST':
        data = request.get_json()
        cfg = _load_adv_config()
        cfg['posts_folder'] = data.get('posts_folder', '').strip()
        _save_adv_config(cfg)
        logger.info(f"Posts Advanced config saved: folder={cfg['posts_folder']}")
        return jsonify({'ok': True})
    return jsonify(_load_adv_config())


@app.route('/advanced/posts/generate')
def adv_posts_generate():
    folder = request.args.get('folder', '').strip()
    title  = request.args.get('title',  '').strip()

    def _err(msg):
        def _gen():
            yield f"data: ERROR: {msg}\n\n"
            yield "data: __EXIT__1\n\n"
        return Response(stream_with_context(_gen()), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    if not folder:
        return _err("No folder configured — set the folder path and save first.")
    if not os.path.isdir(folder):
        return _err(f"Folder not found: {folder}")

    script = os.path.join(HERE, 'instagram_post_advanced.py')
    cmd    = [sys.executable, script, 'generate', '--folder', folder]
    if title:
        cmd.extend(['--title', title])

    def generate():
        env = os.environ.copy()
        env['PYTHONUTF8'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', cwd=HERE, env=env,
        )
        for line in process.stdout:
            stripped = line.rstrip()
            logger.info(f"[adv_gen] {stripped}")
            yield f"data: {stripped}\n\n"
        process.wait()
        if process.returncode != 0:
            logger.error(f"Posts Advanced generate failed (exit {process.returncode})")
        yield f"data: __EXIT__{process.returncode}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/advanced/posts/preview-image/<preview_id>')
def adv_preview_image(preview_id):
    preview_id = re.sub(r'[^a-f0-9]', '', preview_id)
    path = os.path.join(PREVIEW_DIR, f"{preview_id}.jpg")
    if not os.path.exists(path):
        return 'Preview not found', 404
    with open(path, 'rb') as f:
        data = f.read()
    return Response(data, mimetype='image/jpeg', headers={'Cache-Control': 'no-store'})


@app.route('/advanced/posts/publish')
def adv_posts_publish():
    preview_id = re.sub(r'[^a-f0-9]', '', request.args.get('id', ''))
    caption    = request.args.get('caption', '').strip()

    if not preview_id:
        def _err():
            yield "data: ERROR: Missing preview id.\n\n"
            yield "data: __EXIT__1\n\n"
        return Response(stream_with_context(_err()), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    script = os.path.join(HERE, 'instagram_post_advanced.py')
    cmd    = [sys.executable, script, 'publish', '--id', preview_id, '--caption', caption]

    def publish_stream():
        env = os.environ.copy()
        env['PYTHONUTF8'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', cwd=HERE, env=env,
        )
        for line in process.stdout:
            stripped = line.rstrip()
            logger.info(f"[adv_pub] {stripped}")
            yield f"data: {stripped}\n\n"
        process.wait()
        if process.returncode == 0:
            logger.info("Posts Advanced publish completed")
        else:
            logger.error(f"Posts Advanced publish failed (exit {process.returncode})")
        yield f"data: __EXIT__{process.returncode}\n\n"

    return Response(stream_with_context(publish_stream()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ── Native file/folder picker ─────────────────────────────────────────────────

@app.route('/browse/file')
def browse_file():
    import tkinter as tk
    from tkinter import filedialog
    exts = request.args.get('exts', '')
    root = tk.Tk()
    root.withdraw()
    root.wm_attributes('-topmost', 1)
    filetypes = [
        ("Video files", " ".join(f"*.{e}" for e in exts.split())) if exts else ("All files", "*.*"),
        ("All files", "*.*"),
    ]
    path = filedialog.askopenfilename(title="Select File", filetypes=filetypes, parent=root)
    root.destroy()
    return jsonify({'path': path or ''})


@app.route('/browse/folder')
def browse_folder():
    import tkinter as tk
    from tkinter import filedialog
    title = request.args.get('title', 'Select Folder')
    root  = tk.Tk()
    root.withdraw()
    root.wm_attributes('-topmost', 1)
    path = filedialog.askdirectory(title=title, parent=root)
    root.destroy()
    return jsonify({'path': path or ''})


# ── YouTube Shorts routes ─────────────────────────────────────────────────────

@app.route('/yt-shorts/config', methods=['GET', 'POST'])
def yt_shorts_config():
    cfg = _load_adv_config()
    if request.method == 'POST':
        data = request.get_json()
        cfg.setdefault('yt_shorts', {})
        cfg['yt_shorts']['video_path']    = data.get('video_path',    '').strip()
        cfg['yt_shorts']['output_folder'] = data.get('output_folder', '').strip()
        cfg['yt_shorts']['clip_count']    = int(data.get('clip_count',    5))
        cfg['yt_shorts']['clip_duration'] = int(data.get('clip_duration', 18))
        cfg['yt_shorts']['add_quote']     = bool(data.get('add_quote', False))
        _save_adv_config(cfg)
        logger.info(f"YT Shorts config saved: {cfg['yt_shorts']}")
        return jsonify({'ok': True})
    return jsonify(cfg.get('yt_shorts', {}))


@app.route('/yt-shorts/cut')
def yt_shorts_cut():
    video_path    = request.args.get('video_path',    '').strip()
    output_folder = request.args.get('output_folder', '').strip()
    clip_count    = request.args.get('clip_count',    '5').strip()
    clip_duration = request.args.get('clip_duration', '18').strip()
    add_quote     = request.args.get('add_quote',     '0') == '1'

    def _err(msg):
        def _g():
            yield f"data: ERROR: {msg}\n\n"
            yield "data: __EXIT__1\n\n"
        return Response(stream_with_context(_g()), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    if not video_path:
        return _err("No video path provided.")
    if not os.path.isfile(video_path):
        return _err(f"Video file not found: {video_path}")
    if not output_folder:
        return _err("No output folder provided.")

    script = os.path.join(HERE, 'youtube_shorts.py')
    cmd = [
        sys.executable, script, 'cut',
        '--video',    video_path,
        '--output',   output_folder,
        '--count',    clip_count,
        '--duration', clip_duration,
    ]
    if add_quote:
        cmd.append('--add-quote')

    logger.info(f"YT Shorts cut — video={video_path}, count={clip_count}, "
                f"dur={clip_duration}s, add_quote={add_quote}")

    def stream():
        env = os.environ.copy()
        env['PYTHONUTF8'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUNBUFFERED'] = '1'
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', cwd=HERE, env=env,
        )
        for line in process.stdout:
            stripped = line.rstrip()
            logger.info(f"[yt_shorts] {stripped}")
            yield f"data: {stripped}\n\n"
        process.wait()
        if process.returncode == 0:
            logger.info("YT Shorts cut completed successfully")
        else:
            logger.error(f"YT Shorts cut failed (exit {process.returncode})")
        yield f"data: __EXIT__{process.returncode}\n\n"

    return Response(stream_with_context(stream()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/yt-shorts/video')
def yt_shorts_video():
    path = request.args.get('path', '').strip()
    if not path or not os.path.isfile(path):
        return 'Not found', 404
    if os.path.splitext(path)[1].lower() not in {'.mp4', '.mov', '.avi', '.mkv', '.webm'}:
        return 'Invalid file type', 400
    return send_file(path, mimetype='video/mp4', conditional=True)


# ── Quotes routes ─────────────────────────────────────────────────────────────
QUOTES_FILE = os.path.join(HERE, 'quotes.json')


def _load_quotes_data():
    if not os.path.isfile(QUOTES_FILE):
        return {'quotes': [], 'used': []}
    with open(QUOTES_FILE, encoding='utf-8') as f:
        return json.load(f)


def _save_quotes_data(data: dict):
    with open(QUOTES_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


@app.route('/quotes', methods=['GET'])
def quotes_list():
    data  = _load_quotes_data()
    used  = set(data.get('used', []))
    items = [
        {'index': i, 'quote': q['quote'], 'author': q['author'], 'locked': i in used}
        for i, q in enumerate(data.get('quotes', []))
    ]
    return jsonify({'quotes': items, 'total': len(items), 'used_count': len(used)})


@app.route('/quotes/toggle', methods=['POST'])
def quotes_toggle():
    idx = request.get_json(force=True).get('index')
    if idx is None:
        return jsonify({'ok': False, 'error': 'Missing index'}), 400
    data = _load_quotes_data()
    used = set(data.get('used', []))
    if idx in used:
        used.discard(idx)
        locked = False
    else:
        used.add(idx)
        locked = True
    data['used'] = list(used)
    _save_quotes_data(data)
    return jsonify({'ok': True, 'locked': locked, 'used_count': len(used)})


@app.route('/quotes/reset', methods=['POST'])
def quotes_reset():
    data = _load_quotes_data()
    data['used'] = []
    _save_quotes_data(data)
    return jsonify({'ok': True})


if __name__ == '__main__':
    import atexit
    PID_FILE = os.path.join(HERE, 'app.pid')

    # Kill any stale previous instance
    if os.path.exists(PID_FILE):
        try:
            old_pid = int(open(PID_FILE).read().strip())
            import signal, psutil
            p = psutil.Process(old_pid)
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            pass
        try:
            os.remove(PID_FILE)
        except OSError:
            pass

    # Write our PID
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))

    @atexit.register
    def _cleanup():
        try:
            os.remove(PID_FILE)
        except OSError:
            pass

    scheduler.start()
    _apply_schedule()
    print("Starting Styleus Instagram Agent UI -> http://localhost:5000")
    app.run(debug=True, use_reloader=False, port=5000)
