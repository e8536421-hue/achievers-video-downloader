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
| `JOB_TTL` | `900` | Retention in seconds after a job finishes (success or failure) |
| `RATE_INFO` | `20` | Info requests per IP per minute |
| `RATE_JOBS` | `6` | Download jobs per IP per rate window |
| `RATE_WINDOW` | `600` | Download rate-limit window in seconds |
| `WORKDIR` | system temp | Temporary job storage |
| `MAX_PENDING` | `4` | Extra admitted jobs beyond `WORKERS`; total queued/running cap is `WORKERS + MAX_PENDING` |
| `RATE_STATUS` | `180` | Status requests per IP per minute; allows three concurrent one-second polls |
| `RATE_FILE` | `20` | File requests per IP per minute, including retries/range requests |
| `TRUSTED_PROXY_CIDRS` | unset | Comma-separated verified ingress proxy IPs/CIDRs; universal `/0` trust is rejected |

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

## Stage 1 deployment security

Keep one Uvicorn process and one service instance. The Docker command explicitly
uses `--workers 1 --no-proxy-headers`: the application must see the socket peer.
Use the same flags if Render overrides the Docker start command. `WORKERS` controls
threads, not Uvicorn processes. Excess queued/running work returns 503 with
`Retry-After`; finished jobs no longer consume capacity. A final-output check
covers post-processing and removes failed-job artifacts. It does not cap peak
FFmpeg temporary disk use or total storage across retained successful jobs.

`TRUST_PROXY=true` is deliberately no longer honored. With no trusted proxy
configuration, all forwarded headers are ignored. Behind Render this safely
shares limits among visitors using the same ingress peer, but may limit legitimate
visitors together. Before enabling per-visitor limits, verify the actual service's
socket peers and X-Forwarded-For chain (including spoofed-prefix requests), and set
`TRUSTED_PROXY_CIDRS` to only the verified ingress and intermediate proxy ranges.
The application walks the chain from right to left and stops at the first
untrusted IP; arbitrary client-supplied left prefixes cannot change that result.
Do not guess a hop count, trust all private ranges, or use Render outbound ranges
as ingress ranges. Restrict direct/private ingress to trusted proxies when using
forwarded IPs. The local repository does not contain enough deployment details
to verify those addresses; fail-closed connection-IP limiting remains the default.

Rate limits include unsuccessful status/file lookups, use separate buckets, and
idle entries are swept every 30 seconds. Status defaults accommodate the existing
one-second frontend poll without frontend changes. All limits remain per process.

URL validation rejects credentials and nonpublic IPv4/IPv6 destinations and checks
all DNS answers. Queued jobs revalidate when starting; yt-dlp's extractor/media
HTTP requests revalidate before dispatch. These checks do **not** pin DNS, inspect
transport-internal redirects before following them, or constrain FFmpeg/external
network clients. DNS rebinding and redirect SSRF therefore remain deployment-level
issues. A network egress policy must deny loopback, private, link-local/metadata,
reserved and other nonpublic destinations for both IPv4 and IPv6, including every
redirect and fresh DNS resolution. No such network policy is installed by this
patch, and its availability on the current free Render service is unverified.

Run the complete offline mocked test suite with `python -m pytest -v`.
