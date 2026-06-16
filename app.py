#!/usr/bin/env python
"""Flask UI for the Styleus Instagram post agent."""

import os
import sys
import json
import logging
import subprocess
from datetime import datetime
from flask import Flask, render_template, request, Response, stream_with_context, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from zoneinfo import ZoneInfo

app = Flask(__name__)
HERE = os.path.dirname(os.path.abspath(__file__))
USED_FILE    = os.path.join(HERE, 'instagram_used_titles.json')
LOG_FILE     = os.path.join(HERE, 'agent.log')
SCHEDULE_FILE = os.path.join(HERE, 'schedule.json')

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
