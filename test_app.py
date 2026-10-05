import ipaddress
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

import app as backend


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(backend, "WORKDIR", tmp_path)
    monkeypatch.setattr(backend, "TRUSTED_PROXY_CIDRS", ())
    monkeypatch.setattr(backend, "pool", Mock())
    monkeypatch.setattr(backend, "RATE_JOBS", 100)
    monkeypatch.setattr(backend.socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
    ])
    with backend.jobs_lock:
        backend.jobs.clear()
    with backend.hits_lock:
        backend.hits.clear()
    yield
    with backend.jobs_lock:
        backend.jobs.clear()
    with backend.hits_lock:
        backend.hits.clear()


@pytest.fixture
def client():
    with TestClient(backend.app) as instance:
        yield instance


def request(peer="8.8.8.8", forwarded=None):
    headers = [] if forwarded is None else [(b"x-forwarded-for", forwarded.encode())]
    return Request({"type": "http", "client": (peer, 1234), "headers": headers})


def test_health(client):
    assert client.get("/healthz").json()["ok"] is True


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/video", "http://localhost/video",
    "http://LOCALHOST./video", "http://x.localhost/video", "http://127.0.0.1/v",
    "http://10.0.0.1/v", "http://172.16.0.1/v", "http://192.168.0.1/v",
    "http://169.254.169.254/v", "http://100.64.0.1/v", "http://0.0.0.0/v",
    "http://224.0.0.1/v", "http://[::1]/v", "http://[fc00::1]/v", "http://[4000::1]/v",
    "http://[fe80::1]/v", "http://[::ffff:127.0.0.1]/v",
    "http://[fe80::1%25eth0]/v", "https://user:password@example.com/v",
    "https://@example.com/v", "http://[broken/v", "http://8.8.8.8:bad/v",
    "http://8.8.8.8:99999/v", "https://example.com/a b",
    "https://example.com/" + chr(92) + "evil",
])
def test_rejects_unsafe_urls(url):
    with pytest.raises(HTTPException) as exc:
        backend.check_url(url)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("url", [
    "https://example.com/video?a=1", "http://8.8.8.8/v",
    "https://[2606:4700:4700::1111]/v", "https://[::ffff:8.8.8.8]/v",
])
def test_public_urls_allowed(url):
    assert backend.check_url(url).scheme in {"http", "https"}


def test_all_dns_answers_checked(monkeypatch):
    monkeypatch.setattr(backend.socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0)),
    ])
    with pytest.raises(HTTPException):
        backend.check_url("https://example.com/v")


@pytest.mark.parametrize("empty", [True, False])
def test_unresolvable_url(monkeypatch, empty):
    def resolve(*a, **k):
        if empty:
            return []
        raise socket.gaierror("no name")
    monkeypatch.setattr(backend.socket, "getaddrinfo", resolve)
    with pytest.raises(HTTPException) as exc:
        backend.check_url("https://example.com/v")
    assert exc.value.status_code == 400


def test_untrusted_forwarded_header_ignored(monkeypatch):
    monkeypatch.setenv("TRUST_PROXY", "true")
    assert backend.client_ip(request(forwarded="1.1.1.1")) == "8.8.8.8"
    monkeypatch.setattr(backend, "TRUSTED_PROXY_CIDRS", (ipaddress.ip_network("10.1.0.0/24"),))
    assert backend.client_ip(request(forwarded="1.1.1.1")) == "8.8.8.8"


@pytest.mark.parametrize("prefix", ["1.1.1.1", "9.9.9.9, 4.4.4.4", "malformed-client-prefix"])
def test_trusted_chain_ignores_spoofed_prefix(monkeypatch, prefix):
    monkeypatch.setattr(backend, "TRUSTED_PROXY_CIDRS", (
        ipaddress.ip_network("10.1.0.0/24"), ipaddress.ip_network("173.245.48.0/20"),
    ))
    assert backend.client_ip(request("10.1.0.2", prefix + ", 8.8.8.8, 173.245.48.1")) == "8.8.8.8"


@pytest.mark.parametrize("header", ["bad", "", "1.1.1.1, bad", "1.1.1.1," , "a" * 4097])
def test_invalid_proxy_chain_falls_back(monkeypatch, header):
    monkeypatch.setattr(backend, "TRUSTED_PROXY_CIDRS", (ipaddress.ip_network("10.1.0.0/24"),))
    assert backend.client_ip(request("10.1.0.2", header)) == "10.1.0.2"


def test_duplicate_forwarded_headers(monkeypatch):
    monkeypatch.setattr(backend, "TRUSTED_PROXY_CIDRS", (ipaddress.ip_network("10.1.0.0/24"),))
    req = request("10.1.0.2", "1.1.1.1")
    req.scope["headers"].append((b"x-forwarded-for", b"8.8.8.8"))
    assert backend.client_ip(req) == "8.8.8.8"


def test_rate_limit_expiry_and_buckets(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(backend.time, "monotonic", lambda: clock[0])
    backend.limit(request(), "jobs", 1, 60)
    backend.limit(request(), "status", 1, 60)
    backend.limit(request("1.1.1.1"), "jobs", 1, 60)
    with pytest.raises(HTTPException) as exc:
        backend.limit(request(forwarded="9.9.9.9"), "jobs", 1, 60)
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "60"
    clock[0] = 160
    backend.limit(request(), "jobs", 1, 60)


def test_idle_bookkeeping_pruned_per_window(monkeypatch):
    monkeypatch.setattr(backend.time, "monotonic", lambda: 100)
    backend.limit(request(), "info", 1, 60)
    backend.limit(request("1.1.1.1"), "jobs", 1, 600)
    backend.prune_hits(160)
    assert list(backend.hits) == [("jobs", "1.1.1.1")]
    backend.prune_hits(700)
    assert not backend.hits


def test_status_allows_frontend_polling_then_limits(client):
    for _ in range(180):
        assert client.get("/api/jobs/missing").status_code == 404
    assert client.get("/api/jobs/missing").status_code == 429


def test_file_limit_includes_failed_lookups(client):
    for _ in range(20):
        assert client.get("/api/file/missing").status_code == 404
    assert client.get("/api/file/missing").status_code == 429


def test_jobs_rate_limit_not_bypassed(client, monkeypatch):
    monkeypatch.setattr(backend, "RATE_JOBS", 1)
    assert client.post("/api/jobs", json={"url": "https://example.com/v"}, headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert client.post("/api/jobs", json={"url": "https://example.com/v"}, headers={"X-Forwarded-For": "9.9.9.9"}).status_code == 429


def test_queue_full_and_finished_releases_capacity(client, monkeypatch):
    monkeypatch.setattr(backend, "WORKERS", 1)
    monkeypatch.setattr(backend, "MAX_PENDING", 1)
    ids = [client.post("/api/jobs", json={"url": "https://example.com/v"}).json()["id"] for _ in range(2)]
    backend.jobs[ids[0]]["state"] = "running"
    response = client.post("/api/jobs", json={"url": "https://example.com/v"})
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    assert backend.pool.submit.call_count == 2
    backend.jobs[ids[0]].update(state="done", finished=backend.time.time())
    assert client.post("/api/jobs", json={"url": "https://example.com/v"}).status_code == 200


def test_queue_admission_atomic(monkeypatch):
    monkeypatch.setattr(backend, "WORKERS", 1)
    monkeypatch.setattr(backend, "MAX_PENDING", 0)
    barrier = threading.Barrier(8)
    def submit(index):
        barrier.wait()
        try:
            backend.create_job(backend.JobReq(url="https://example.com/v"), request(f"8.8.8.{index + 1}"))
            return 200
        except HTTPException as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    assert results.count(200) == 1
    assert results.count(503) == 7
    assert len(backend.jobs) == 1


def test_failed_submission_does_not_leak_capacity(client):
    backend.pool.submit.side_effect = RuntimeError("shutdown")
    assert client.post("/api/jobs", json={"url": "https://example.com/v"}).status_code == 503
    assert not backend.jobs


def test_cleanup_lifecycle(monkeypatch):
    monkeypatch.setattr(backend.time, "time", lambda: 2000)
    monkeypatch.setattr(backend, "JOB_TTL", 900)
    states = {
        "queued": ("queued", None), "running": ("running", 1),
        "recent": ("done", 1999), "failed_recent": ("error", 1999),
        "old": ("done", 1), "failed_old": ("error", 1),
        "unfinished": ("done", None),
    }
    for jid, (state, finished) in states.items():
        backend.jobs[jid] = {"state": state, "created": 1, "finished": finished}
        (backend.WORKDIR / jid).mkdir()
    backend.purge()
    assert set(backend.jobs) == {"queued", "running", "recent", "failed_recent", "unfinished"}
    assert {p.name for p in backend.WORKDIR.iterdir()} == set(backend.jobs)


def fake_download(monkeypatch, suffix="mp4", size=100, failure=None, produce=True):
    class Downloader:
        def __init__(self, options):
            self.options = options
            fake_download.options = options
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def download(self, urls):
            assert backend.jobs["job"]["state"] == "running"
            out = backend.WORKDIR / "job"
            (out / "unfinished.part").write_bytes(b"x")
            self.options["progress_hooks"][0]({"status": "downloading", "total_bytes": 100, "downloaded_bytes": 50})
            assert backend.jobs["job"]["progress"] == 50
            if failure:
                raise failure
            if produce:
                with (out / ("video." + suffix)).open("wb") as handle:
                    handle.truncate(size)
    monkeypatch.setattr(backend, "PublicYoutubeDL", Downloader)
    backend.jobs["job"] = {"state": "queued", "created": 1, "finished": None, "progress": 0}


@pytest.mark.parametrize("mode,suffix", [({}, "mp4"), ({"audio": True}, "mp3"), ({"start": 1, "end": 3}, "mp4"), ({"subs": "en"}, "srt")])
def test_mocked_job_lifecycle(client, monkeypatch, mode, suffix):
    fake_download(monkeypatch, suffix=suffix)
    backend.run_job("job", backend.JobReq(url="https://example.com/v", **mode))
    job = backend.jobs["job"]
    assert job["state"] == "done" and job["progress"] == 100
    assert job["finished"] >= job["created"]
    assert client.get("/api/file/job").content == bytes(100)
    assert client.get("/api/jobs/job").json()["state"] == "done"
    if mode.get("audio"):
        assert fake_download.options["postprocessors"][0]["key"] == "FFmpegExtractAudio"
    if "start" in mode:
        assert "download_ranges" in fake_download.options


@pytest.mark.parametrize("mode,suffix", [({}, "mp4"), ({"audio": True}, "mp3"), ({"start": 1, "end": 3}, "mp4")])
def test_final_size_enforced_after_processing(client, monkeypatch, mode, suffix):
    monkeypatch.setattr(backend, "MAX_MB", 1)
    fake_download(monkeypatch, suffix=suffix, size=1024 * 1024 + 1)
    backend.run_job("job", backend.JobReq(url="https://example.com/v", **mode))
    job = backend.jobs["job"]
    assert job["state"] == "error" and job["finished"] is not None
    assert "1 MB size limit" in job["error"]
    assert not (backend.WORKDIR / "job").exists()
    assert client.get("/api/file/job").status_code == 404


def test_exact_size_limit_allowed(monkeypatch):
    monkeypatch.setattr(backend, "MAX_MB", 1)
    fake_download(monkeypatch, size=1024 * 1024)
    backend.run_job("job", backend.JobReq(url="https://example.com/v"))
    assert backend.jobs["job"]["state"] == "done"


@pytest.mark.parametrize("failure,produce", [(RuntimeError("network error"), True), (None, False)])
def test_failed_job_lifecycle(monkeypatch, failure, produce):
    fake_download(monkeypatch, failure=failure, produce=produce)
    backend.run_job("job", backend.JobReq(url="https://example.com/v"))
    assert backend.jobs["job"]["state"] == "error"
    assert backend.jobs["job"]["finished"] is not None
    assert not (backend.WORKDIR / "job").exists()


def test_setup_failure_finishes_job(monkeypatch):
    fake_download(monkeypatch)
    monkeypatch.setattr(Path, "mkdir", Mock(side_effect=OSError("disk full")))
    backend.run_job("job", backend.JobReq(url="https://example.com/v"))
    assert backend.jobs["job"]["state"] == "error"
    assert backend.jobs["job"]["finished"] is not None


def test_queued_job_dns_rechecked(monkeypatch):
    fake_download(monkeypatch)
    monkeypatch.setattr(backend.socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ])
    backend.run_job("job", backend.JobReq(url="https://example.com/v"))
    assert backend.jobs["job"]["state"] == "error"
    assert backend.jobs["job"]["finished"] is not None
    assert not (backend.WORKDIR / "job").exists()


def test_ytdlp_requests_revalidated(monkeypatch):
    send = Mock(return_value="response")
    monkeypatch.setattr(backend.yt_dlp.YoutubeDL, "urlopen", send)
    with backend.PublicYoutubeDL({"quiet": True}) as downloader:
        assert downloader.urlopen("https://example.com/v") == "response"
        monkeypatch.setattr(backend.socket, "getaddrinfo", lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ])
        with pytest.raises(HTTPException):
            downloader.urlopen("https://example.com/v")
        from yt_dlp.networking.common import Request as NetworkRequest
        with pytest.raises(HTTPException):
            downloader.urlopen(NetworkRequest("http://127.0.0.1/v"))
    assert send.call_count == 1


def test_docker_disables_universal_proxy_trust():
    docker = Path(backend.__file__).with_name("Dockerfile").read_text()
    assert "--no-proxy-headers" in docker and "--workers 1" in docker
    assert "forwarded-allow-ips" not in docker


def test_info_endpoint_uses_guarded_downloader(client, monkeypatch):
    downloader = Mock()
    downloader.__enter__ = Mock(return_value=downloader)
    downloader.__exit__ = Mock(return_value=False)
    downloader.extract_info.return_value = {
        "title": "Example", "formats": [{"height": 720, "vcodec": "h264", "filesize": 10}],
    }
    constructor = Mock(return_value=downloader)
    monkeypatch.setattr(backend, "PublicYoutubeDL", constructor)
    response = client.post("/api/info", json={"url": "https://example.com/v"})
    assert response.status_code == 200
    assert response.json()["heights"] == [720]
    downloader.extract_info.assert_called_once_with("https://example.com/v", download=False)


def test_info_limit(client, monkeypatch):
    monkeypatch.setattr(backend, "RATE_INFO", 1)
    assert client.post("/api/info", json={"url": "file:///etc/passwd"}).status_code == 400
    assert client.post("/api/info", json={"url": "file:///etc/passwd"}).status_code == 429


def test_mapped_ipv6_cannot_change_rate_bucket():
    assert backend.client_ip(request("::ffff:8.8.8.8")) == "8.8.8.8"


def test_expired_completed_file_is_not_served(client):
    out = backend.WORKDIR / "old"
    out.mkdir()
    (out / "video.mp4").write_bytes(b"video")
    backend.jobs["old"] = {
        "state": "done", "created": 1, "finished": 1,
        "path": str(out / "video.mp4"), "name": "video.mp4",
    }
    assert client.get("/api/file/old").status_code == 404
    assert not out.exists()
