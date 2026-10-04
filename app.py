import ipaddress
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import uuid

from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
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
)


# ============================================================
# Runtime state
# ============================================================

pool = ThreadPoolExecutor(
    max_workers=WORKERS,
    thread_name_prefix="download",
)

jobs = {}
jobs_lock = threading.RLock()

hits = defaultdict(deque)
hits_lock = threading.Lock()


# ============================================================
# Request / rate-limit helpers
# ============================================================

def client_ip(request: Request) -> str:
    """
    Return the client IP.

    X-Forwarded-For is only trusted when TRUST_PROXY is
    explicitly enabled.
    """
    if os.getenv("TRUST_PROXY", "").lower() in {
        "1",
        "true",
        "yes",
    }:
        forwarded = request.headers.get("x-forwarded-for")

        if forwarded:
            return forwarded.split(",")[0].strip()

    return request.client.host if request.client else "unknown"


def limit(
    request: Request,
    bucket: str,
    n: int,
    window: int,
):
    """
    Simple in-memory rate limiter.
    """
    ip = client_ip(request)
    now = time.time()

    with hits_lock:
        q = hits[(bucket, ip)]

        while q and now - q[0] > window:
            q.popleft()

        if len(q) >= n:
            raise HTTPException(
                429,
                "Too many requests. Please wait and try again.",
            )

        q.append(now)


# ============================================================
# Network / SSRF protection
# ============================================================

def is_public_ip(ip: str) -> bool:
    """
    Only allow publicly routable IP addresses.
    """
    obj = ipaddress.ip_address(ip)

    return not (
        obj.is_private
        or obj.is_loopback
        or obj.is_link_local
        or obj.is_reserved
        or obj.is_multicast
        or obj.is_unspecified
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

    parsed = urlparse(url)

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
    if parsed.username or parsed.password:
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

    if hostname.lower() in blocked_hostnames:
        raise HTTPException(
            400,
            "That address isn't allowed.",
        )

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


# ============================================================
# Error handling
# ============================================================

def clean_err(exc: Exception) -> str:
    """
    Convert internal yt-dlp / FFmpeg errors into a concise
    user-facing message.
    """
    msg = re.sub(
        r"\x1b\[[0-9;]*m",
        "",
        str(exc),
    )

    msg = msg.replace(
        "ERROR: ",
        "",
    ).strip()

    first = next(
        (
            line.strip()
            for line in msg.splitlines()
            if line.strip()
        ),
        "",
    )

    return first[:300] or "Something went wrong."


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
            if job["created"] < cutoff
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
        with yt_dlp.YoutubeDL(options) as ydl:
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

def run_job(
    jid: str,
    req: JobReq,
):
    with jobs_lock:
        job = jobs.get(jid)

        if not job:
            return

        job["state"] = "running"

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
        with yt_dlp.YoutubeDL(options) as ydl:
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
        jobs[jid] = {
            "state": "queued",
            "progress": 0,
            "created": time.time(),
        }

    pool.submit(
        run_job,
        jid,
        req,
    )

    return {
        "id": jid
    }


# ============================================================
# Job status
# ============================================================

@app.get("/api/jobs/{jid}")
def job_status(jid: str):
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
def get_file(jid: str):
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

app.mount(
    "/",
    StaticFiles(
        directory=static_dir,
        html=True,
    ),
    name="static",
)