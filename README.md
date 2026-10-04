# QuickSave — Production Video Downloader

A responsive FastAPI + yt-dlp downloader with quality selection, MP3 extraction,
clipping, subtitles, progress tracking, rate limiting, cleanup, and PWA support.

## Requirements

- Python 3.10+
- FFmpeg
- A current yt-dlp installation
- For Docker deployments, Docker/Podman

## Local development

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
uvicorn app:app --reload
```

Open `http://127.0.0.1:8000`.

## Docker deployment

```bash
docker build -t quicksave .
docker run --rm -p 8000:8000 quicksave
```

For a hosted deployment, use a persistent-capable VM/container service if you
expect large downloads or many simultaneous users. The application deliberately
uses temporary local storage and is designed to delete jobs after `JOB_TTL`
seconds.

## Environment variables

| Variable | Default | Purpose |
|---|---:|---|
| `PORT` | `8000` | HTTP port |
| `MAX_MB` | `500` | Maximum output/download size |
| `WORKERS` | `2` | Concurrent download workers |
| `JOB_TTL` | `900` | Seconds before job files are purged |
| `RATE_INFO` | `20` | Info requests per IP per minute |
| `RATE_JOBS` | `6` | Download jobs per IP per rate window |
| `RATE_WINDOW` | `600` | Download rate-limit window in seconds |
| `WORKDIR` | system temp | Temporary job storage |
| `TRUST_PROXY` | unset | Set to `true` only when your proxy is configured to provide the client IP |

## Production notes

- Put the service behind HTTPS and a reverse proxy/load balancer.
- Keep FFmpeg and yt-dlp current.
- Add authentication, a persistent queue, object storage, and a shared job store
  before scaling to multiple application instances.
- Monitor CPU, RAM, disk, outbound bandwidth, and failed downloads.
- The downloader includes basic SSRF protections and in-memory rate limiting,
  but these are not a substitute for a hardened network boundary.
- Download only material you own or are authorized to save. Platform terms and
  copyright rules may restrict downloading.

## Health check

`GET /healthz` returns a small JSON health response and is suitable for a
container/service health check.

## Supported output modes

- Best available video
- Selected video height
- MP3 audio
- Video clipping by start/end time
- Subtitle-only SRT output where the source exposes subtitles
