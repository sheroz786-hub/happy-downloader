FROM python:3.12-slim

# FFmpeg + deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY app.py .
COPY download_daemon.py .

# Files dir
RUN mkdir -p files

# API key via env var
ENV API_KEY=""

EXPOSE 8000

# Run gunicorn + download daemon
CMD ["sh", "-c", "python download_daemon.py & exec gunicorn -w 2 --threads 2 -b 0.0.0.0:8000 --timeout 120 app:app"]
