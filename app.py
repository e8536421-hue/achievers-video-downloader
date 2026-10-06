import ipaddress
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import uuid

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator


# ============================================================
# Configuration
# ============================================================

MAX_MB = int(os.getenv("MAX_MB", "500"))
WORKERS = max(1, int(os.getenv("WORKERS", "2")))
JOB_TTL = max(60, int(os.getenv("JOB_TTL", "900")))

RATE_INFO = max(1, int(os.getenv("RATE_INFO", "20")))
RATE_JOBS = max(1, int(os.getenv("RATE_JOBS", "6")))
RATE_WINDOW = max(10, int(os.getenv("RATE_WINDOW", "600")))
MAX_PENDING = max(0, int(os.getenv("MAX_PENDING", "4")))
RATE_STATUS = max(1, int(os.getenv("RATE_STATUS", "180")))
RATE_FILE = max(1, int(os.getenv("RATE_FILE", "20")))

# Never infer trust from a header or trust all private networks. Configure only
# verified ingress proxy addresses, including intermediate proxies in XFF.
TRUSTED_PROXY_CIDRS = tuple(
    ipaddress.ip_network(value.strip(), strict=False)
    for value in os.getenv("TRUSTED_PROXY_CIDRS", "").split(",")
    if value.strip()
)
if any(network.prefixlen == 0 for network in TRUSTED_PROXY_CIDRS):
    raise ValueError("Universal proxy trust is not allowed.")

WORKDIR = Path(
    os.getenv(
        "WORKDIR",
        tempfile.mkdtemp(prefix="video_dl_"),
    )
)

WORKDIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# FastAPI application
# ============================================================

app = FastAPI(
    title="Video Downloader",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def is_utility_path(path):
    return path == "/api" or path.startswith("/api/") or path in {
        "/healthz", "/openapi.json",
    }


class UtilityNoIndexMiddleware:
    """Apply indexing policy without buffering streamed download responses."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not is_utility_path(scope["path"]):
            return await self.app(scope, receive, send)

        async def send_noindex(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = [
                    (key, value) for key, value in message.get("headers", [])
                    if key.lower() != b"x-robots-tag"
                ] + [(b"x-robots-tag", b"noindex")]
            await send(message)

        await self.app(scope, receive, send_noindex)


app.add_middleware(UtilityNoIndexMiddleware)


# ============================================================
# Runtime state
# ============================================================

pool = ThreadPoolExecutor(
    max_workers=WORKERS,
    thread_name_prefix="download",
)

jobs = {}
jobs_lock = threading.RLock()

hits = {}
hits_lock = threading.Lock()


# ============================================================
# Request / rate-limit helpers
# ============================================================

def normalize_ip(value: str):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if getattr(address, "scope_id", None):
        raise ValueError("Scoped addresses are not accepted.")
    return address


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    try:
        address = normalize_ip(peer)
        peer = str(address)
        trusted = lambda ip: any(ip in net for net in TRUSTED_PROXY_CIDRS)
        if not trusted(address):
            return peer
        # Combine duplicate headers in their original order, then walk from
        # the socket peer towards the first untrusted hop. Ignore spoofed left
        # prefixes. Invalid/oversized chains fall back to the socket peer.
        forwarded = ",".join(request.headers.getlist("x-forwarded-for"))
        if not forwarded or len(forwarded) > 4096:
            return peer
        for item in reversed(forwarded.split(",")):
            address = normalize_ip(item.strip())
            if not trusted(address):
                return str(address)
        return peer
    except ValueError:
        return peer


def prune_hits(now=None):
    """Discard all idle buckets, using each bucket's actual window."""
    now = time.monotonic() if now is None else now
    with hits_lock:
        for key, (window, queue) in list(hits.items()):
            while queue and now - queue[0] >= window:
                queue.popleft()
            if not queue:
                del hits[key]


def limit(request: Request, bucket: str, n: int, window: int):
    ip = client_ip(request)
    now = time.monotonic()
    with hits_lock:
        _, queue = hits.setdefault((bucket, ip), (window, deque()))
        while queue and now - queue[0] >= window:
            queue.popleft()
        if len(queue) >= n:
            retry = max(1, int(window - (now - queue[0]) + 0.999))
            raise HTTPException(
                429, "Too many requests. Please wait and try again.",
                headers={"Retry-After": str(retry)},
            )
        queue.append(now)


# ============================================================
# Network / SSRF protection
# ============================================================

def is_public_ip(ip: str) -> bool:
    """
    Only allow publicly routable IP addresses.
    """
    obj = normalize_ip(ip)
    return obj.is_global and not (
        obj.is_multicast or obj.is_reserved or obj.is_unspecified
        or obj.is_loopback or obj.is_link_local
    )


def check_url(url: str):
    """
    Validate a supplied HTTP/HTTPS URL and prevent requests
    to private/local network addresses.
    """
    if len(url) > 4096:
        raise HTTPException(
            400,
            "That URL is too long.",
        )

    url = url.strip()

    try:
        parsed = urlparse(url)
        # Access port here even for literal IPs, so malformed ports are rejected.
        parsed.port
    except ValueError:
        raise HTTPException(400, "Enter a valid website address.")
    if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
        raise HTTPException(400, "Enter a valid website address.")

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            400,
            "Enter a full link starting with http:// or https://",
        )

    if not parsed.hostname:
        raise HTTPException(
            400,
            "Enter a valid website address.",
        )

    hostname = parsed.hostname.strip().rstrip(".")

    if not hostname:
        raise HTTPException(
            400,
            "Enter a valid website address.",
        )

    # Reject credentials embedded in the URL.
    # Example:
    # https://username:password@example.com/video
    if parsed.username is not None or parsed.password is not None:
        raise HTTPException(
            400,
            "URLs containing embedded credentials are not allowed.",
        )

    # Reject localhost and common local hostnames.
    blocked_hostnames = {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
    }

    if hostname.lower() in blocked_hostnames or hostname.lower().endswith(".localhost"):
        raise HTTPException(
            400,
            "That address isn't allowed.",
        )

    if "%" in hostname:
        raise HTTPException(400, "That address isn't allowed.")

    # If the hostname itself is an IP address,
    # validate it directly.
    try:
        direct_ip = ipaddress.ip_address(
            hostname
        )

        if not is_public_ip(
            str(direct_ip)
        ):
            raise HTTPException(
                400,
                "That address isn't allowed.",
            )

        return parsed

    except ValueError:
        # Not a literal IP address.
        # Continue with DNS validation.
        pass

    try:
        addresses = socket.getaddrinfo(
            hostname,
            parsed.port
            or (
                443
                if parsed.scheme == "https"
                else 80
            ),
            type=socket.SOCK_STREAM,
        )

    except (
        socket.gaierror,
        ValueError,
    ):
        raise HTTPException(
            400,
            "Couldn't find that website.",
        )

    if not addresses:
        raise HTTPException(
            400,
            "Couldn't find that website.",
        )

    # Every resolved address must be publicly routable.
    for address in addresses:
        try:
            resolved_ip = (
                address[4][0]
                .split("%")[0]
            )

            if not is_public_ip(
                resolved_ip
            ):
                raise HTTPException(
                    400,
                    "That address isn't allowed.",
                )

        except ValueError:
            raise HTTPException(
                400,
                "That address isn't allowed.",
            )

    return parsed


class PublicYoutubeDL(yt_dlp.YoutubeDL):
    """Revalidate extractor/media request destinations before dispatch.

    This is defense in depth, not DNS pinning or a redirect firewall: transports
    may follow redirects internally, and FFmpeg uses its own network stack.
    """

    def urlopen(self, request):
        check_url(request if isinstance(request, str) else request.url)
        return super().urlopen(request)


# ============================================================
# Error handling
# ============================================================

def clean_err(exc: Exception) -> str:
    """
    Convert internal yt-dlp / FFmpeg errors into safe,
    concise user-facing messages.
    """
    msg = re.sub(
        r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))",
        "",
        str(exc),
    )
    msg = re.sub(r"\bERROR:\s*", "", msg, flags=re.IGNORECASE).strip()
    lower = msg.lower().replace("’", "'")

    if any(text in lower for text in (
        "sign in to confirm you're not a bot",
        "sign in to confirm you are not a bot",
        "cookies-from-browser", "login required", "authentication required",
        "login is required", "sign in required", "please sign in",
        "requires authentication", "requires login", "confirm you're not a bot",
    )):
        return (
            "This video provider is currently blocking server-based downloads. "
            "Please try another supported public video or try again later."
        )

    if any(text in lower for text in (
        "not available in your country", "not available in your region",
        "geo restricted", "geo-restricted", "geographic restriction",
        "geographical restriction", "blocked in your country",
        "not available from your location",
    )):
        return "This video is not available in the server's region."

    if any(text in lower for text in (
        "private video", "video is private", "video unavailable",
        "video is unavailable", "not available", "video has been removed",
        "video has been deleted", "members-only", "age-restricted",
    )):
        return "This video is unavailable or cannot be accessed publicly."

    if any(text in lower for text in (
        "unsupported url", "unsupported provider", "unsupported site",
        "no suitable extractor", "not a valid url", "invalid url",
    )):
        return "This link or video provider is not supported. Please try another link."

    if re.search(r"\b429\b", lower) or any(text in lower for text in (
        "too many requests", "rate limit", "rate-limit",
    )):
        return "This video provider is receiving too many requests. Please try again later."

    if isinstance(exc, TimeoutError) or any(text in lower for text in (
        "timed out", "timeout", "time out",
    )):
        return "The video provider took too long to respond. Please try again later."

    if isinstance(exc, (socket.gaierror, ConnectionError)) or any(text in lower for text in (
        "getaddrinfo failed", "name or service not known",
        "temporary failure in name resolution", "name resolution",
        "nodename nor servname", "unable to resolve", "dns",
        "connection refused", "connection reset", "connection aborted",
        "network is unreachable", "network unreachable", "network error",
        "remote end closed connection", "unable to download webpage",
        "unable to download video data",
    )):
        return "Couldn't connect to the video provider. Please try again later."

    if any(text in lower for text in (
        "max-filesize", "max_filesize", "maximum file size",
        "larger than max", "file is too large", "filesize limit",
        "file size limit", "may exceed the",
    )):
        return f"This download exceeds the {MAX_MB} MB size limit. Please choose a smaller video or lower quality."

    if any(text in lower for text in (
        "ffmpeg", "ffprobe", "postprocessing", "post-processing",
    )):
        return "The downloaded media could not be processed. Please try another format or try again later."

    return "Couldn't complete this request. Please try another supported public video or try again later."


# ============================================================
# Job cleanup
# ============================================================

def purge():
    """
    Remove expired jobs and their files.
    """
    cutoff = time.time() - JOB_TTL

    with jobs_lock:
        expired = [
            jid
            for jid, job in jobs.items()
            if job.get("state") in {"done", "error"}
            and job.get("finished") is not None
            and job["finished"] < cutoff
        ]

        for jid in expired:
            shutil.rmtree(
                WORKDIR / jid,
                ignore_errors=True,
            )

            jobs.pop(jid, None)


def sweeper():
    """
    Background cleanup loop.
    """
    while True:
        time.sleep(30)

        try:
            purge()
            prune_hits()
        except Exception:
            pass


threading.Thread(
    target=sweeper,
    daemon=True,
    name="job-sweeper",
).start()


# ============================================================
# Request models
# ============================================================

class InfoReq(BaseModel):
    url: str = Field(
        min_length=8,
        max_length=4096,
    )

    @field_validator("url")
    @classmethod
    def strip_url(cls, value):
        return value.strip()


class JobReq(InfoReq):
    height: int | None = Field(
        default=None,
        ge=144,
        le=4320,
    )

    audio: bool = False

    start: float | None = Field(
        default=None,
        ge=0,
    )

    end: float | None = Field(
        default=None,
        gt=0,
    )

    subs: str | None = Field(
        default=None,
        max_length=20,
    )

    @field_validator("subs")
    @classmethod
    def validate_subs(cls, value):
        if value is None:
            return value

        value = value.strip()

        if not re.fullmatch(
            r"[A-Za-z0-9_-]{1,20}",
            value,
        ):
            raise ValueError(
                "Invalid subtitle language."
            )

        return value


# ============================================================
# Global exception handler
# ============================================================

@app.exception_handler(Exception)
async def unexpected_error(
    request: Request,
    exc: Exception,
):
    """
    Keep internal exceptions out of production responses.
    """
    if isinstance(exc, HTTPException):
        raise exc

    return JSONResponse(
        status_code=500,
        # Unhandled errors are rendered outside user middleware by Starlette.
        headers={"X-Robots-Tag": "noindex"} if is_utility_path(request.url.path) else None,
        content={
            "detail":
                "An unexpected server error occurred."
        },
    )


# ============================================================
# Health check
# ============================================================

@app.get("/healthz")
def health():
    return {
        "ok": True,
        "service": "video-downloader",
    }


# ============================================================
# Video information
# ============================================================

@app.post("/api/info")
def info(
    req: InfoReq,
    request: Request,
):
    limit(
        request,
        "info",
        RATE_INFO,
        60,
    )

    check_url(req.url)

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 15,
        "retries": 2,
        "extract_flat": False,
    }

    try:
        with PublicYoutubeDL(options) as ydl:
            data = ydl.extract_info(
                req.url,
                download=False,
            )

    except Exception as exc:
        raise HTTPException(
            422,
            clean_err(exc),
        )

    if (
        data.get("_type") == "playlist"
        and data.get("entries")
    ):
        data = next(
            (
                entry
                for entry in data["entries"]
                if entry
            ),
            data,
        )

    formats = data.get("formats") or []

    heights = sorted(
        {
            int(f["height"])
            for f in formats
            if (
                f.get("height")
                and f.get("height") >= 144
                and f.get("vcodec") not in (None, "none")
            )
        },
        reverse=True,
    )

    def filesize(fmt):
        return (
            fmt.get("filesize")
            or fmt.get("filesize_approx")
            or 0
        )

    audio_size = max(
        [
            filesize(f)
            for f in formats
            if f.get("vcodec") == "none"
        ]
        or [0]
    )

    sizes = {}

    for height in heights:
        video_sizes = [
            filesize(f)
            for f in formats
            if f.get("height") == height
        ]

        sizes[height] = (
            max(video_sizes or [0])
            + audio_size
        )

    common = {
        "en",
        "es",
        "fr",
        "de",
        "pt",
        "it",
        "ru",
        "hi",
        "ar",
        "ja",
        "ko",
        "zh-Hans",
    }

    subtitles = list(
        (data.get("subtitles") or {}).keys()
    )

    subtitles += [
        key
        for key in (
            data.get("automatic_captions") or {}
        )
        if key in common and key not in subtitles
    ]

    return {
        "title":
            data.get("title")
            or "Untitled video",

        "thumbnail":
            data.get("thumbnail"),

        "duration":
            data.get("duration"),

        "uploader":
            data.get("uploader"),

        "site":
            data.get("extractor_key"),

        "heights":
            heights[:12],

        "sizes":
            sizes,

        "subs":
            subtitles[:40],
    }


# ============================================================
# Download worker
# ============================================================

def _run_job(
    jid: str,
    req: JobReq,
):
    with jobs_lock:
        job = jobs.get(jid)

        if not job:
            return

        job["state"] = "running"

    check_url(req.url)  # DNS may have changed while this job was queued.
    out = WORKDIR / jid
    out.mkdir(
        parents=True,
        exist_ok=True,
    )

    def hook(status):
        if status.get("status") == "downloading":
            total = (
                status.get("total_bytes")
                or status.get("total_bytes_estimate")
            )

            downloaded = (
                status.get("downloaded_bytes")
                or 0
            )

            if total:
                with jobs_lock:
                    if jid in jobs:
                        jobs[jid]["progress"] = min(
                            99,
                            int(
                                downloaded
                                * 100
                                / total
                            ),
                        )

    # --------------------------------------------------------
    # Select output format
    # --------------------------------------------------------

    if req.audio:
        fmt = "bestaudio/best"

    elif req.height:
        fmt = (
            f"bv*[height<={req.height}]"
            f"+ba/"
            f"b[height<={req.height}]"
            f"/b"
        )

    else:
        fmt = "bv*+ba/b"

    options = {
        "format": fmt,

        "format_sort": [
            "res",
            "ext:mp4:m4a",
        ],

        "merge_output_format": "mp4",

        "outtmpl": str(
            out / "%(title).80s.%(ext)s"
        ),

        "noplaylist": True,

        "quiet": True,

        "no_warnings": True,

        "retries": 3,

        "fragment_retries": 3,

        "socket_timeout": 20,

        "max_filesize":
            MAX_MB * 1024 * 1024,

        "progress_hooks": [
            hook
        ],
    }

    # --------------------------------------------------------
    # Audio mode
    # --------------------------------------------------------

    if req.audio:
        options["postprocessors"] = [
            {
                "key":
                    "FFmpegExtractAudio",

                "preferredcodec":
                    "mp3",

                "preferredquality":
                    "192",
            }
        ]

    # --------------------------------------------------------
    # Subtitle mode
    # --------------------------------------------------------

    elif req.subs:
        options.update(
            skip_download=True,

            writesubtitles=True,

            writeautomaticsub=True,

            subtitleslangs=[
                req.subs
            ],

            subtitlesformat=
                "srt/vtt/best",

            postprocessors=[
                {
                    "key":
                        "FFmpegSubtitlesConvertor",

                    "format":
                        "srt",
                }
            ],
        )

    # --------------------------------------------------------
    # Clip mode
    # --------------------------------------------------------

    elif (
        req.start is not None
        or req.end is not None
    ):
        from yt_dlp.utils import (
            download_range_func,
        )

        end = (
            req.end
            if req.end is not None
            else float("inf")
        )

        options["download_ranges"] = (
            download_range_func(
                None,
                [
                    (
                        req.start or 0,
                        end,
                    )
                ],
            )
        )

        options[
            "force_keyframes_at_cuts"
        ] = True

    # --------------------------------------------------------
    # Perform download
    # --------------------------------------------------------

    try:
        with PublicYoutubeDL(options) as ydl:
            ydl.download(
                [req.url]
            )

        files = [
            p
            for p in out.iterdir()
            if (
                p.is_file()
                and not p.name.endswith(
                    (
                        ".part",
                        ".ytdl",
                    )
                )
            )
        ]

        if not files:
            raise RuntimeError(
                "No file was produced. "
                f"It may exceed the {MAX_MB} MB limit."
            )

        # This check runs after merge, clipping and audio/subtitle conversion.
        # Remove the whole job's artifacts if any final output exceeds the cap.
        if any(p.stat().st_size > MAX_MB * 1024 * 1024 for p in files):
            raise RuntimeError("File size limit exceeded.")

        result = max(
            files,
            key=lambda p: p.stat().st_size,
        )

        with jobs_lock:
            if jid in jobs:
                jobs[jid].update(
                    state="done",
                    progress=100,
                    path=str(result),
                    name=result.name,
                )

    except Exception as exc:
        with jobs_lock:
            if jid in jobs:
                jobs[jid].update(
                    state="error",
                    error=clean_err(exc),
                )


def run_job(jid: str, req: JobReq):
    try:
        _run_job(jid, req)
    except Exception as exc:
        with jobs_lock:
            if jid in jobs:
                jobs[jid].update(state="error", error=clean_err(exc))
    finally:
        with jobs_lock:
            job = jobs.get(jid)
            if job and job.get("state") in {"done", "error"}:
                if job["state"] == "error":
                    shutil.rmtree(WORKDIR / jid, ignore_errors=True)
                job["finished"] = time.time()


# ============================================================
# Create download job
# ============================================================

@app.post("/api/jobs")
def create_job(
    req: JobReq,
    request: Request,
):
    limit(
        request,
        "jobs",
        RATE_JOBS,
        RATE_WINDOW,
    )

    check_url(req.url)

    if req.audio and req.height:
        raise HTTPException(
            400,
            "Choose either video quality or audio-only.",
        )

    if (
        req.subs
        and (
            req.audio
            or req.height
            or req.start is not None
            or req.end is not None
        )
    ):
        raise HTTPException(
            400,
            "Subtitle downloads cannot be combined with another output mode.",
        )

    if (
        req.end is not None
        and req.end <= (req.start or 0)
    ):
        raise HTTPException(
            400,
            "The clip end must be later than its start.",
        )

    purge()

    jid = uuid.uuid4().hex

    with jobs_lock:
        active = sum(job["state"] in {"queued", "running"} for job in jobs.values())
        if active >= WORKERS + MAX_PENDING:
            raise HTTPException(
                503, "The download queue is full. Please try again shortly.",
                headers={"Retry-After": "30"},
            )
        jobs[jid] = {
            "state": "queued", "progress": 0,
            "created": time.time(), "finished": None,
        }
        try:
            pool.submit(run_job, jid, req)
        except Exception:
            jobs.pop(jid, None)
            raise HTTPException(
                503, "Downloads are temporarily unavailable. Please try again shortly.",
                headers={"Retry-After": "30"},
            )

    return {
        "id": jid
    }


# ============================================================
# Job status
# ============================================================

@app.get("/api/jobs/{jid}")
def job_status(jid: str, request: Request):
    limit(request, "status", RATE_STATUS, 60)
    purge()
    with jobs_lock:
        job = jobs.get(jid)

        if not job:
            raise HTTPException(
                404,
                "This download expired. Start it again.",
            )

        queued_before = sum(
            1
            for item in jobs.values()
            if (
                item["state"] == "queued"
                and item["created"] < job["created"]
            )
        )

        return {
            "state":
                job["state"],

            "progress":
                job.get("progress", 0),

            "error":
                job.get("error"),

            "name":
                job.get("name"),

            "queue":
                (
                    queued_before + 1
                    if job["state"] == "queued"
                    else 0
                ),
        }


# ============================================================
# Serve completed file
# ============================================================

@app.get("/api/file/{jid}")
def get_file(jid: str, request: Request):
    limit(request, "file", RATE_FILE, 60)
    purge()
    with jobs_lock:
        job = jobs.get(jid)

        if (
            not job
            or job.get("state") != "done"
        ):
            raise HTTPException(
                404,
                "The file isn't ready or has expired.",
            )

        raw_path = job.get("path")
        name = job.get("name")

    if not raw_path or not name:
        raise HTTPException(
            404,
            "The file is no longer available.",
        )

    try:
        path = Path(raw_path).resolve()
        workdir = WORKDIR.resolve()
        job_dir = (
            WORKDIR / jid
        ).resolve()

        # The output must remain inside this
        # specific job directory.
        if path.parent != job_dir:
            raise HTTPException(
                404,
                "The file is no longer available.",
            )

        # The job directory itself must remain
        # inside WORKDIR.
        if workdir not in job_dir.parents:
            raise HTTPException(
                404,
                "The file is no longer available.",
            )

        if not path.is_file():
            raise HTTPException(
                404,
                "The file is no longer available.",
            )

    except (
        OSError,
        RuntimeError,
    ):
        raise HTTPException(
            404,
            "The file is no longer available.",
        )

    return FileResponse(
        path,
        filename=name,
        media_type="application/octet-stream",
    )


# ============================================================
# Static frontend
# ============================================================

static_dir = (
    Path(__file__).resolve().parent
    / "static"
)

@app.api_route("/index.html", methods=["GET", "HEAD"], include_in_schema=False)
def redirect_home(request: Request):
    # Preserve PWA/shared-link parameters while consolidating the HTML alias.
    query = request.url.query
    return RedirectResponse("/" + ("?" + query if query else ""), status_code=308)


app.mount(
    "/",
    StaticFiles(
        directory=static_dir,
        html=True,
    ),
    name="static",
)
