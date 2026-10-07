#!/usr/bin/env python3
"""Single-threaded download worker for Happy Downloader.
Polls FILES/job_*.json for status=queued and runs yt-dlp.
Single-threaded => subprocess fork is safe (no fork-from-threads deadlock).
"""
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent
VENV_PY = sys.executable
COOKIES = BASE / "youtube_cookies.txt"
YTDLP = [VENV_PY, "-m", "yt_dlp"]
FILES = BASE / "files"
FILES.mkdir(exist_ok=True)

def job_path(job_id):
    if not re.fullmatch(r"[a-f0-9]{12}", job_id):
        return None
    return FILES / f"job_{job_id}.json"

def job_get(job_id):
    p = job_path(job_id)
    if not p or not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None

def job_set(job_id, data):
    p = job_path(job_id)
    if p:
        p.write_text(json.dumps(data))

def process(job_id):
    job = job_get(job_id)
    if not job or job.get("status") != "queued":
        return
    url = job.get("url", "")
    height = job.get("height", 720)
    format_id = job.get("format_id")
    needs_merge = job.get("needs_merge", False)
    if not url.startswith("http"):
        job_set(job_id, {**job, "status": "error", "error": "bad url"})
        return

    def upd(**kw):
        j = job_get(job_id) or {}
        j.update(kw)
        job_set(job_id, j)

    try:
        upd(status="downloading", progress=0.0)
        fid = uuid.uuid4().hex[:12]
        out = str(FILES / f"{fid}.%(ext)s")
        if format_id:
            selector = f"{format_id}+bestaudio/best" if needs_merge else format_id
        else:
            selector = f"bv*[height<={height}]+ba/b[height<={height}]/b"
        cmd = YTDLP + ["-f", selector, "--merge-output-format", "mp4",
                       "-o", out, "--no-playlist", "--no-warnings",
                       "--extractor-args", "youtube:player_client=ios,android"]
        if COOKIES.exists():
            cmd += ["--cookies", str(COOKIES)]
        cmd += ["--socket-timeout", "30", "--retries", "10",
                "--fragment-retries", "10", "--progress", url]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, start_new_session=True)
        for line in p.stdout:
            m = re.search(r"(\d{1,3}\.\d)%", line)
            if m:
                upd(progress=float(m.group(1)))
        p.wait()
        if p.returncode != 0:
            upd(status="error", error="download/merge failed")
            return
        produced = sorted(FILES.glob(f"{fid}.*"), key=lambda x: x.stat().st_size, reverse=True)
        produced = [x for x in produced if x.suffix not in (".json",)]
        if not produced:
            upd(status="error", error="no file produced")
            return
        upd(file_id=produced[0].name, filename=produced[0].name,
            status="done", progress=100.0)
    except Exception as e:
        upd(status="error", error=str(e)[:300])

def main():
    print("download daemon started", flush=True)
    while True:
        try:
            for jf in FILES.glob("job_*.json"):
                job_id = jf.stem[4:]
                job = job_get(job_id)
                if job and job.get("status") == "queued":
                    print(f"processing {job_id}", flush=True)
                    process(job_id)
        except Exception as e:
            print(f"daemon error: {e}", flush=True)
        time.sleep(3)

if __name__ == "__main__":
    main()
