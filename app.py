#!/usr/bin/env python3
"""Happy Downloader backend: yt-dlp info/fetch + ffmpeg convert tools.
Single secret API key (X-API-Key header or ?key=). CORS open for the AI Studio frontend.
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_file, Response

BASE = Path(__file__).resolve().parent
VENV_PY = sys.executable  # works both locally (venv) and in Docker (system python)
YTDLP = [str(VENV_PY), "-m", "yt_dlp"]
FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
FILES = BASE / "files"
FILES.mkdir(exist_ok=True)
KEY_FILE = BASE / ".api_key"
PORT = int(os.environ.get("PORT", "8000"))

def load_key():
    # Render/Docker: API_KEY env var; local: .api_key file
    env_key = os.environ.get("API_KEY", "").strip()
    if env_key:
        return env_key
    return KEY_FILE.read_text().strip()

API_KEY = load_key()

app = Flask(__name__)

def _job_path(job_id):
    # file-based jobs: visible to all gunicorn workers (and survive restarts)
    if not re.fullmatch(r"[a-f0-9]{12}", job_id):
        return None
    return FILES / f"job_{job_id}.json"

def job_get(job_id):
    p = _job_path(job_id)
    if not p or not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None

def job_set(job_id, data):
    p = _job_path(job_id)
    if p:
        p.write_text(json.dumps(data))

# ---------- helpers ----------

def check_auth():
    given = request.headers.get("X-API-Key") or request.args.get("key")
    return bool(given) and secrets.compare_digest(given, API_KEY)

def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, ngrok-skip-browser-warning"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

@app.before_request
def guard():
    if request.method == "OPTIONS":
        return cors(Response(""))
    if request.path.startswith("/api/") and not check_auth():
        return cors(jsonify({"ok": False, "error": "unauthorized: bad or missing API key"})), 401

@app.after_request
def add_cors(resp):
    return cors(resp)

def run(cmd, timeout=120):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p

def safe_name(name, ext):
    name = re.sub(r"[^\w\- ]+", "", name or "video").strip()[:80] or "video"
    return f"{name}.{ext}"

# ---------- endpoints ----------

@app.get("/api/health")
def health():
    return jsonify({"ok": True, "time": datetime.utcnow().isoformat()})

@app.post("/api/info")
def info():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url or not url.startswith("http"):
        return jsonify({"ok": False, "error": "valid video URL required"}), 400
    try:
        # Use android client to bypass YouTube bot detection
        p = run(YTDLP + ["--dump-json", "--no-download", "--no-playlist",
                          "--no-warnings",
                          "--extractor-args", "youtube:player_client=android",
                          url], timeout=90)
        if p.returncode != 0:
            return jsonify({"ok": False, "error": (p.stderr or "yt-dlp failed")[-500:]}), 502
        meta = json.loads(p.stdout)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:300]}), 502

    formats = []
    for f in meta.get("formats") or []:
        if not f.get("url"):
            continue
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        h = f.get("height")
        formats.append({
            "format_id": f.get("format_id"),
            "label": f"{h}p" if h else (f.get("format_note") or f.get("ext")),
            "height": h,
            "ext": f.get("ext"),
            "fps": f.get("fps"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "vcodec": vcodec,
            "acodec": acodec,
            "needs_merge": vcodec != "none" and acodec == "none",
            "direct_url": f["url"],
        })
    # server-side merged qualities: unique heights, best-first
    seen, server_qualities = set(), []
    for f in sorted(formats, key=lambda x: (x["height"] or 0), reverse=True):
        h = f["height"]
        if h and h not in seen:
            seen.add(h)
            server_qualities.append({"label": f"{h}p", "height": h})

    return jsonify({
        "ok": True,
        "title": meta.get("title"),
        "thumbnail": meta.get("thumbnail"),
        "duration": meta.get("duration"),
        "uploader": meta.get("uploader"),
        "formats": formats,
        "server_qualities": server_qualities,
    })

@app.post("/api/fetch")
def fetch():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    height = int(data.get("height") or 720)
    format_id = (data.get("format_id") or "").strip() or None
    needs_merge = bool(data.get("needs_merge"))
    if not url.startswith("http"):
        return jsonify({"ok": False, "error": "valid video URL required"}), 400
    job_id = uuid.uuid4().hex[:12]
    # download_daemon.py (separate single-threaded process) picks up queued jobs
    job_set(job_id, {"status": "queued", "progress": 0.0, "url": url,
                     "height": height, "format_id": format_id,
                     "needs_merge": needs_merge})
    return jsonify({"ok": True, "job_id": job_id})

@app.get("/api/job/<job_id>")
def job_status(job_id):
    job = job_get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown job"}), 404
    out = {"ok": True, **job}
    if job.get("status") == "done":
        out["download_url"] = f"/api/file/{job['file_id']}"
    return jsonify(out)

@app.get("/api/file/<file_id>")
def get_file(file_id):
    # fetch outputs: 12hex.ext | convert outputs: 12hex_out.ext (op suffixes allowed)
    if not re.fullmatch(r"[a-f0-9]{12}(?:_[a-z]+)?\.[a-z0-9]{2,5}", file_id):
        return jsonify({"ok": False, "error": "bad id"}), 400
    path = FILES / file_id
    if not path.exists():
        return jsonify({"ok": False, "error": "expired or missing"}), 404
    return send_file(path, as_attachment=True, download_name=f"happy_{file_id}")

OPS = {"to_mp4", "to_mkv", "to_webm", "to_avi", "to_mov", "compress", "mp3",
       "trim", "resize", "thumbnail", "gif", "merge", "watermark", "concat",
       "speed", "phone", "subtitles"}

def _atempo_chain(speed):
    """Chain atempo filters to support 0.25x-4x (atempo handles 0.5-2.0 each)."""
    parts = []
    s = float(speed)
    while s > 2.0:
        parts.append("atempo=2.0"); s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5"); s /= 0.5
    parts.append(f"atempo={s:.4f}")
    return ",".join(parts)

def _overlay_pos(pos):
    return {"topleft": "10:10", "topright": "W-w-10:10",
            "bottomleft": "10:H-h-10", "bottomright": "W-w-10:H-h-10",
            "center": "(W-w)/2:(H-h)/2"}.get(pos, "W-w-10:H-h-10")

@app.post("/api/convert")
def convert():
    op = (request.form.get("operation") or "").strip()
    uploads = request.files.getlist("files") or ([request.files["file"]] if "file" in request.files else [])
    uploads = [u for u in uploads if u and u.filename]
    if op not in OPS or not uploads:
        return jsonify({"ok": False, "error": "operation + file(s) required"}), 400
    total = sum(getattr(u, "content_length", 0) or 0 for u in uploads)
    if total > 800 * 1024 * 1024:
        return jsonify({"ok": False, "error": "files too large (800MB max)"}), 400

    fid = uuid.uuid4().hex[:12]
    srcs = []
    for i, up in enumerate(uploads):
        p = FILES / f"{fid}_in{i}_{safe_name(up.filename, 'bin')}"
        up.save(p)
        srcs.append(p)

    try:
        start = request.form.get("start", "0") or "0"
        end = (request.form.get("end") or "").strip()
        height = (request.form.get("height") or "720").strip()
        v = srcs[0]

        if op == "to_mp4":
            dst, cmd = f"{fid}_out.mp4", ["-i", str(v), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"]
        elif op == "to_mkv":
            dst, cmd = f"{fid}_out.mkv", ["-i", str(v), "-c", "copy"]
        elif op == "to_webm":
            dst, cmd = f"{fid}_out.webm", ["-i", str(v), "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "30", "-c:a", "libopus"]
        elif op == "to_avi":
            dst, cmd = f"{fid}_out.avi", ["-i", str(v), "-c:v", "mpeg4", "-q:v", "4", "-c:a", "libmp3lame", "-q:a", "4"]
        elif op == "to_mov":
            dst, cmd = f"{fid}_out.mov", ["-i", str(v), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"]
        elif op == "compress":
            dst, cmd = f"{fid}_out.mp4", ["-i", str(v), "-c:v", "libx264", "-crf", "28", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"]
        elif op == "mp3":
            dst, cmd = f"{fid}_out.mp3", ["-i", str(v), "-vn", "-c:a", "libmp3lame", "-q:a", "4"]
        elif op == "trim":
            dst, cmd = f"{fid}_out.mp4", (["-ss", start] + (["-to", end] if end else []) + ["-i", str(v),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"])
        elif op == "resize":
            dst, cmd = f"{fid}_out.mp4", ["-i", str(v), "-vf", f"scale=-2:{height}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"]
        elif op == "thumbnail":
            dst, cmd = f"{fid}_out.jpg", ["-ss", start or "1", "-i", str(v), "-vframes", "1", "-q:v", "3"]
        elif op == "gif":
            dst, cmd = f"{fid}_out.gif", (["-ss", start or "0"] + (["-to", end or "3"] if True else []) +
                ["-i", str(v), "-vf", "fps=12,scale=480:-1"])
        elif op == "phone":
            dst, cmd = f"{fid}_out.mp4", ["-i", str(v), "-c:v", "libx264", "-profile:v", "baseline",
                "-level", "3.0", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"]
        elif op == "speed":
            spd = max(0.25, min(4.0, float(request.form.get("speed") or 1.0)))
            dst, cmd = f"{fid}_out.mp4", ["-i", str(v), "-filter_complex",
                f"[0:v]setpts={1.0/spd:.4f}*PTS[v];[0:a]{_atempo_chain(spd)}[a]",
                "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart"]
        elif op == "merge":
            if len(srcs) < 2:
                return jsonify({"ok": False, "error": "merge needs video + audio files"}), 400
            dst, cmd = f"{fid}_merge.mp4", ["-i", str(srcs[0]), "-i", str(srcs[1]),
                "-c:v", "copy", "-c:a", "aac", "-shortest", "-movflags", "+faststart"]
        elif op == "watermark":
            if len(srcs) < 2:
                return jsonify({"ok": False, "error": "watermark needs video + logo image"}), 400
            pos = _overlay_pos((request.form.get("position") or "").strip())
            dst, cmd = f"{fid}_watermark.mp4", ["-i", str(srcs[0]), "-i", str(srcs[1]),
                "-filter_complex", f"[0:v][1:v]overlay={pos}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart"]
        elif op == "concat":
            if len(srcs) < 2:
                return jsonify({"ok": False, "error": "concat needs at least 2 videos"}), 400
            n = len(srcs)
            ins = []
            for s in srcs:
                ins += ["-i", str(s)]
            filt = "".join(f"[{i}:v][{i}:a]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"
            dst, cmd = f"{fid}_concat.mp4", ins + ["-filter_complex", filt, "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"]
        elif op == "subtitles":
            if len(srcs) < 2:
                return jsonify({"ok": False, "error": "subtitles needs video + .srt file"}), 400
            srt = str(srcs[1]).replace("'", r"'\''")
            dst, cmd = f"{fid}_subtitles.mp4", ["-i", str(srcs[0]),
                "-vf", f"subtitles='{srt}'",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart"]
        else:
            return jsonify({"ok": False, "error": "unknown operation"}), 400

        dst_path = FILES / dst
        p = run([FFMPEG, "-y"] + cmd + [str(dst_path)], timeout=900)
        if p.returncode != 0 or not dst_path.exists():
            return jsonify({"ok": False, "error": (p.stderr or "ffmpeg failed")[-500:]}), 502
        return jsonify({"ok": True, "file_id": dst, "download_url": f"/api/file/{dst}",
                        "size": dst_path.stat().st_size})
    finally:
        for s in srcs:
            try: s.unlink()
            except OSError: pass

def cleaner():
    while True:
        time.sleep(3600)
        cutoff = datetime.now() - timedelta(hours=48)
        for f in FILES.iterdir():
            try:
                if f.name.startswith("job_") and f.suffix == ".json":
                    continue  # job files cleaned with their media
                if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                    f.unlink()
            except OSError:
                pass

# start cleaner on import (gunicorn) and on direct run
threading.Thread(target=cleaner, daemon=True).start()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, threaded=True)
