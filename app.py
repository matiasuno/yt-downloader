import sys
import os
import tempfile
import shutil

# PyInstaller compatibility: find templates/resources relative to the bundle
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
    _open_browser_on_start = True
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _open_browser_on_start = False

def _setup_ffmpeg():
    """Find the bundled ffmpeg binary and ensure it's named 'ffmpeg'/'ffmpeg.exe'
    so yt-dlp can find it — imageio-ffmpeg stores it with a versioned name."""
    src = None
    try:
        import imageio_ffmpeg
        candidate = imageio_ffmpeg.get_ffmpeg_exe()
        if candidate and os.path.isfile(candidate):
            src = candidate
    except Exception:
        pass

    # When frozen, also scan _MEIPASS directly in case imageio_ffmpeg lookup fails
    if src is None and getattr(sys, 'frozen', False):
        for search_dir in [
            sys._MEIPASS,
            os.path.join(sys._MEIPASS, 'imageio_ffmpeg', 'binaries'),
        ]:
            if not os.path.isdir(search_dir):
                continue
            for f in os.listdir(search_dir):
                if f.lower().startswith('ffmpeg') and not f.endswith('.py'):
                    src = os.path.join(search_dir, f)
                    break
            if src:
                break

    if src is None:
        return 'ffmpeg', ''  # fall back to system PATH

    expected = 'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg'
    if os.path.basename(src).lower() == expected:
        return src, os.path.dirname(src)

    # Copy to a stable temp dir under the standard name so yt-dlp finds it
    ffmpeg_tmpdir = os.path.join(tempfile.gettempdir(), 'ytdl_ffmpeg_bin')
    os.makedirs(ffmpeg_tmpdir, exist_ok=True)
    dst = os.path.join(ffmpeg_tmpdir, expected)
    if not os.path.isfile(dst) or os.path.getsize(dst) != os.path.getsize(src):
        shutil.copy2(src, dst)
        if sys.platform != 'win32':
            os.chmod(dst, 0o755)
    return dst, ffmpeg_tmpdir

FFMPEG_BIN, FFMPEG_DIR = _setup_ffmpeg()

from flask import Flask, request, jsonify, send_file, render_template, Response, stream_with_context
import yt_dlp
import subprocess
import uuid
import threading
import time
import json
import re
import webbrowser

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, 'templates'))

jobs = {}
jobs_lock = threading.Lock()

PORT = 5001


def cleanup_later(path, delay=120):
    def do_cleanup():
        time.sleep(delay)
        shutil.rmtree(path, ignore_errors=True)
    threading.Thread(target=do_cleanup, daemon=True).start()


def parse_time(s):
    if not s or not s.strip():
        return None
    parts = s.strip().split(':')
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except (ValueError, IndexError):
        return None


def run_download_job(job_id, url, fmt, start, end):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return

    tmpdir = job['tmpdir']

    try:
        def progress_hook(d):
            with jobs_lock:
                j = jobs.get(job_id)
                if not j:
                    return
                if d['status'] == 'downloading':
                    total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                    downloaded = d.get('downloaded_bytes', 0)
                    pct = round(downloaded / total * 90, 1) if total > 0 else 0
                    j['progress'] = {
                        'phase': 'downloading',
                        'percent': pct,
                        'speed': d.get('_speed_str', '').strip(),
                        'eta': d.get('_eta_str', '').strip(),
                    }
                elif d['status'] == 'finished':
                    j['progress'] = {'phase': 'processing', 'percent': 92}

        if fmt in ('mp3', 'wav'):
            pp = {'key': 'FFmpegExtractAudio', 'preferredcodec': fmt}
            if fmt == 'mp3':
                pp['preferredquality'] = '192'
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': os.path.join(tmpdir, f'{job_id}.%(ext)s'),
                'postprocessors': [pp],
                'ffmpeg_location': FFMPEG_DIR or FFMPEG_BIN,
                'quiet': True,
                'no_warnings': True,
                'progress_hooks': [progress_hook],
            }
        else:
            ydl_opts = {
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best',
                'outtmpl': os.path.join(tmpdir, f'{job_id}.%(ext)s'),
                'ffmpeg_location': FFMPEG_DIR or FFMPEG_BIN,
                'quiet': True,
                'no_warnings': True,
                'merge_output_format': 'mp4',
                'progress_hooks': [progress_hook],
            }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', 'download')

        with jobs_lock:
            j = jobs.get(job_id)
            if j:
                j['title'] = title
                j['progress'] = {
                    'phase': 'trimming' if (start is not None or end is not None) else 'done',
                    'percent': 95,
                }

        files = [f for f in os.listdir(tmpdir) if f.startswith(job_id)]
        if not files:
            raise RuntimeError('No output file found after download')

        out_file = os.path.join(tmpdir, files[0])
        ext = os.path.splitext(out_file)[1].lstrip('.')

        if start is not None or end is not None:
            trimmed = os.path.join(tmpdir, f'{job_id}_clip.{ext}')
            cmd = [FFMPEG_BIN, '-y']
            if start is not None:
                cmd += ['-ss', str(start)]
            cmd += ['-i', out_file]
            if end is not None:
                if start is not None:
                    cmd += ['-t', str(end - start)]
                else:
                    cmd += ['-to', str(end)]
            if fmt == 'mp3':
                cmd += ['-codec:a', 'libmp3lame', '-qscale:a', '2']
            elif fmt == 'wav':
                cmd += ['-codec:a', 'pcm_s16le']
            else:
                cmd += ['-c', 'copy']
            cmd.append(trimmed)
            result = subprocess.run(cmd, capture_output=True)
            if result.returncode != 0:
                raise RuntimeError(f'FFmpeg trim failed: {result.stderr.decode()[:400]}')
            os.remove(out_file)
            out_file = trimmed

        with jobs_lock:
            j = jobs.get(job_id)
            if j:
                j['status'] = 'done'
                j['result_file'] = out_file
                j['progress'] = {'phase': 'done', 'percent': 100}

    except Exception as e:
        with jobs_lock:
            j = jobs.get(job_id)
            if j:
                j['status'] = 'error'
                j['error'] = str(e)
        cleanup_later(tmpdir, 5)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/info', methods=['POST'])
def get_info():
    data = request.get_json() or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl:
            info = ydl.extract_info(url, download=False)
        return jsonify({
            'title': info.get('title', 'Unknown'),
            'duration': info.get('duration', 0),
            'thumbnail': info.get('thumbnail', ''),
            'uploader': info.get('uploader', ''),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/download', methods=['POST'])
def start_download():
    data = request.get_json() or {}
    url = data.get('url', '').strip()
    fmt = data.get('format', 'mp3')
    start = parse_time(data.get('start', ''))
    end = parse_time(data.get('end', ''))

    if not url:
        return jsonify({'error': 'URL is required'}), 400
    if fmt not in ('mp3', 'wav', 'video'):
        return jsonify({'error': 'Invalid format'}), 400
    if start is not None and end is not None and end <= start:
        return jsonify({'error': 'End time must be after start time'}), 400

    job_id = str(uuid.uuid4())
    tmpdir = tempfile.mkdtemp()

    with jobs_lock:
        jobs[job_id] = {
            'status': 'running',
            'progress': {'phase': 'starting', 'percent': 0},
            'tmpdir': tmpdir,
            'result_file': None,
            'title': 'download',
            'error': None,
        }

    t = threading.Thread(target=run_download_job, args=(job_id, url, fmt, start, end))
    t.daemon = True
    t.start()

    return jsonify({'job_id': job_id})


@app.route('/api/progress/<job_id>')
def get_progress(job_id):
    def generate():
        while True:
            with jobs_lock:
                job = jobs.get(job_id)
            if not job:
                yield f"data: {json.dumps({'status': 'error', 'error': 'Job not found'})}\n\n"
                break
            payload = {'status': job['status'], 'progress': job.get('progress', {})}
            if job['status'] == 'error':
                payload['error'] = job.get('error', 'Unknown error')
            yield f"data: {json.dumps(payload)}\n\n"
            if job['status'] in ('done', 'error'):
                break
            time.sleep(0.3)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'},
    )


@app.route('/api/result/<job_id>')
def get_result(job_id):
    with jobs_lock:
        job = jobs.get(job_id)

    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job['status'] == 'error':
        return jsonify({'error': job.get('error', 'Download failed')}), 400
    if job['status'] != 'done':
        return jsonify({'error': 'Not ready yet'}), 400

    out_file = job['result_file']
    if not out_file or not os.path.exists(out_file):
        return jsonify({'error': 'Result file missing'}), 500

    ext = os.path.splitext(out_file)[1].lstrip('.')
    mime_map = {
        'mp3': 'audio/mpeg', 'wav': 'audio/wav',
        'mp4': 'video/mp4', 'webm': 'video/webm', 'mkv': 'video/x-matroska',
    }
    mime = mime_map.get(ext, 'application/octet-stream')
    safe_title = re.sub(r'[^\w\s\-]', '', job.get('title', 'download'))[:60].strip()
    download_name = f'{safe_title}.{ext}' if safe_title else f'download.{ext}'

    cleanup_later(job['tmpdir'], 120)
    with jobs_lock:
        jobs.pop(job_id, None)

    return send_file(out_file, mimetype=mime, as_attachment=True, download_name=download_name)


def _open_browser():
    time.sleep(1.5)
    try:
        if sys.platform == 'darwin':
            subprocess.Popen(['open', f'http://localhost:{PORT}'])
        elif sys.platform == 'win32':
            subprocess.Popen(['cmd', '/c', 'start', f'http://localhost:{PORT}'])
        else:
            webbrowser.open(f'http://localhost:{PORT}')
    except Exception:
        webbrowser.open(f'http://localhost:{PORT}')


if __name__ == '__main__':
    if _open_browser_on_start:
        threading.Thread(target=_open_browser, daemon=True).start()
    app.run(debug=False, port=PORT, threaded=True)
