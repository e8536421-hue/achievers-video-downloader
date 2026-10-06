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
    assert response.headers["x-robots-tag"] == "noindex"
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
    assert client.get("/api/file/job").headers["x-robots-tag"] == "noindex"
    assert client.get("/api/jobs/job").json()["state"] == "done"
    assert client.get("/api/jobs/job").headers["x-robots-tag"] == "noindex"
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
    assert response.headers["x-robots-tag"] == "noindex"
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


@pytest.mark.parametrize("query", ["", "?url=https%3A%2F%2Fexample.com%2Fvideo", "?text=shared&title=Example"])
def test_homepage_canonical_and_integrations(client, query):
    from html.parser import HTMLParser

    class Tags(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags = []

        def handle_starttag(self, tag, attrs):
            self.tags.append((tag, dict(attrs)))

    response = client.get("/" + query)
    assert response.status_code == 200
    assert "x-robots-tag" not in response.headers
    parser = Tags()
    parser.feed(response.text)
    assert not any(tag == "meta" and attrs.get("name", "").lower() in {"robots", "googlebot"}
                   and "noindex" in attrs.get("content", "").lower()
                   for tag, attrs in parser.tags)
    canonicals = [attrs["href"] for tag, attrs in parser.tags
                  if tag == "link" and attrs.get("rel") == "canonical"]
    assert canonicals == ["https://achievers-video-downloader.onrender.com/"]
    assert any(tag == "meta" and attrs.get("name") == "google-site-verification"
               and attrs.get("content") == "TsskHwINy31pjUNyqwZNyB_PYHBbP6miLl_3xh6TSHg"
               for tag, attrs in parser.tags)
    assert any(tag == "link" and attrs.get("rel") == "manifest"
               and attrs.get("href") == "/manifest.json" for tag, attrs in parser.tags)
    assert any(tag == "form" and attrs.get("id") == "f" for tag, attrs in parser.tags)
    assert "query.get('url')" in response.text and "query.get('text')" in response.text
    assert ".register('/sw.js')" in response.text
    assert "https://bellnewyork.org/22/c1607918ab2d91ade8037d36a7b7c333" in response.text
    assert "https://bellnewyork.org/21/173bd25a12e240a688efa71a28dc9bbf" in response.text
    assert "frame.setAttribute('sandbox', 'allow-scripts')" in response.text


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("query", ["", "?url=https%3A%2F%2Fexample.com%2Fv&text=shared"])
def test_index_alias_permanent_redirect(client, method, query):
    response = client.request(method, "/index.html" + query, follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/" + query
    assert "x-robots-tag" not in response.headers
    assert client.get(response.headers["location"]).status_code == 200


@pytest.mark.parametrize("path,status", [
    ("/healthz", 200), ("/openapi.json", 404), ("/api", 404),
    ("/api/jobs/missing", 404), ("/api/file/missing", 404), ("/api/unknown", 404),
])
def test_utility_get_responses_noindex(client, path, status):
    response = client.get(path)
    assert response.status_code == status
    assert response.headers["x-robots-tag"] == "noindex"


@pytest.mark.parametrize("path", ["/api/info", "/api/jobs"])
def test_api_validation_noindex(client, path):
    response = client.post(path, json={})
    assert response.status_code == 422
    assert response.headers["x-robots-tag"] == "noindex"


def test_job_creation_noindex(client):
    response = client.post("/api/jobs", json={"url": "https://example.com/v"})
    assert response.status_code == 200
    assert response.headers["x-robots-tag"] == "noindex"
    assert response.json()["id"] in backend.jobs


def test_rate_limit_noindex(client, monkeypatch):
    monkeypatch.setattr(backend, "RATE_INFO", 1)
    first = client.post("/api/info", json={"url": "file:///etc/passwd"})
    assert first.status_code == 400 and first.headers["x-robots-tag"] == "noindex"
    response = client.post("/api/info", json={"url": "file:///etc/passwd"})
    assert response.status_code == 429
    assert response.headers["x-robots-tag"] == "noindex"
    assert "retry-after" in response.headers


def test_unhandled_utility_error_noindex(monkeypatch):
    monkeypatch.setattr(backend, "limit", Mock(side_effect=RuntimeError("internal")))
    with TestClient(backend.app, raise_server_exceptions=False) as instance:
        response = instance.get("/api/jobs/missing")
    assert response.status_code == 500
    assert response.headers["x-robots-tag"] == "noindex"
    assert response.json() == {"detail": "An unexpected server error occurred."}


def test_robots_and_public_pages_sitemap(client):
    import xml.etree.ElementTree as ET

    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert robots.headers["content-type"].startswith("text/plain")
    assert "User-agent: *" in robots.text and "Allow: /" in robots.text
    assert "Disallow:" not in robots.text
    assert "Sitemap: https://achievers-video-downloader.onrender.com/sitemap.xml" in robots.text
    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    assert "xml" in sitemap.headers["content-type"]
    root = ET.fromstring(sitemap.content)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    assert root.tag == "{" + ns["s"] + "}urlset"
    assert [node.text for node in root.findall("s:url/s:loc", ns)] == [
        "https://achievers-video-downloader.onrender.com/",
        *["https://achievers-video-downloader.onrender.com/" + slug
          for slug in ("about", "privacy", "terms", "contact")]
    ]
    assert root.findall(".//s:lastmod", ns) == []
    assert "x-robots-tag" not in robots.headers and "x-robots-tag" not in sitemap.headers


def test_pwa_resources_and_missing_pages(client):
    manifest = client.get("/manifest.json")
    assert manifest.status_code == 200
    data = manifest.json()
    assert data["start_url"] == "/"
    assert data["share_target"] == {
        "action": "/", "method": "GET",
        "params": {"title": "title", "text": "text", "url": "url"},
    }
    for path in ["/sw.js", "/icon-192.png", "/icon-512.png"]:
        response = client.get(path)
        assert response.status_code == 200
        assert "x-robots-tag" not in response.headers
    assert client.get("/missing-page").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


def homepage_document(client, path="/"):
    """Parse server-delivered content, excluding scripts and styles from copy."""
    from html.parser import HTMLParser

    class Document(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags = []
            self.text = []
            self.headings = []
            self.schemas = []
            self.excluded = None
            self.heading = None
            self.schema = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            self.tags.append((tag, attrs))
            if tag in {'script', 'style'}:
                self.excluded = tag
                if attrs.get('type') == 'application/ld+json':
                    self.schema = []
            if tag in {'h1', 'h2', 'title'}:
                self.heading = (tag, [])

        def handle_data(self, data):
            if self.schema is not None:
                self.schema.append(data)
            if not self.excluded:
                self.text.append(data)
                if self.heading:
                    self.heading[1].append(data)

        def handle_endtag(self, tag):
            if self.heading and tag == self.heading[0]:
                self.headings.append((tag, ' '.join(''.join(self.heading[1]).split())))
                self.heading = None
            if tag == self.excluded:
                if self.schema is not None:
                    import json
                    self.schemas.append(json.loads(''.join(self.schema)))
                    self.schema = None
                self.excluded = None

    document = Document()
    document.feed(client.get(path).text)
    return document


def test_homepage_seo_metadata_and_schema(client):
    document = homepage_document(client)
    title = 'Free Online Video Downloader | Achievers'
    assert [text for tag, text in document.headings if tag == 'title'] == [title]
    assert [text for tag, text in document.headings if tag == 'h1'] == ['Free Online Video Downloader']
    metas = {attrs.get('name', attrs.get('property')): attrs.get('content')
             for tag, attrs in document.tags if tag == 'meta'}
    description = metas['description']
    assert 'supported public videos' in description and 'where supported' in description
    assert metas['og:title'] == metas['twitter:title'] == title
    assert metas['og:description'] == metas['twitter:description'] == description
    assert metas['og:type'] == 'website' and metas['twitter:card'] == 'summary'
    assert metas['og:url'] == 'https://achievers-video-downloader.onrender.com/'
    assert 'og:image' not in metas and 'twitter:image' not in metas
    assert len(document.schemas) == 1
    schema = document.schemas[0]
    assert schema['@context'] == 'https://schema.org'
    website, application = schema['@graph']
    assert website['@type'] == 'WebSite' and application['@type'] == 'WebApplication'
    for entity in schema['@graph']:
        assert entity['url'] == metas['og:url']
        assert entity['name'] == 'Achievers Video Downloader'
        assert not ({'aggregateRating', 'review', 'publisher'} & entity.keys())
    assert application['description'] == description
    assert application['offers'] == {'@type': 'Offer', 'price': '0', 'priceCurrency': 'USD'}


def test_homepage_visible_help_and_conservative_claims(client):
    import re
    document = homepage_document(client)
    text = ' '.join(' '.join(document.text).split())
    headings = [heading for tag, heading in document.headings if tag == 'h2']
    for heading in ['How to Download a Video', 'Supported Platforms', 'Video Quality & Formats',
                    'Troubleshooting', 'Frequently Asked Questions']:
        assert heading in headings
    assert 'TikTok and Facebook:' in text and 'tested working on this service' in text
    assert 'YouTube: downloads are currently unreliable' in text and 'anti-bot challenges' in text
    assert 'YouTube is not currently offered as a working supported platform.' in text
    assert not re.search(r'no[ -]?watermark|any website|all websites|guaranteed (HD|MP4|MP3)', text, re.I)
    assert 'MP4 is not guaranteed' in text
    assert 'conversion may fail' in text
    assert 'Public visibility alone does not grant permission' in text
    assert 'do not bypass those controls' in text
    assert 'free to use' in text and 'does not require an Achievers account' in text
    ids = [attrs.get('id') for _, attrs in document.tags if attrs.get('id')]
    assert len(ids) == len(set(ids))
    targets = ['how-to-download', 'supported-platforms', 'quality-formats', 'troubleshooting', 'faq']
    links = [attrs.get('href') for tag, attrs in document.tags if tag == 'a']
    for target in targets:
        assert target in ids and '#' + target in links
    assert {'/about', '/privacy', '/terms', '/contact'} <= set(links)
    html = client.get('/').text
    assert html.index('id="f"') < html.index('id="how-to-download"')


def test_homepage_ad_placements_and_reserved_space(client):
    document = homepage_document(client)
    placements = [attrs['data-placement-id'] for tag, attrs in document.tags
                  if tag == 'aside' and 'data-placement-id' in attrs]
    assert placements == ['31565233', '31565232']
    html = client.get('/').text
    assert html.count('>Advertisement</p>') == 2
    assert "!desktop.matches || slot.getBoundingClientRect().width < 728" in html
    assert "frame.setAttribute('sandbox', 'allow-scripts')" in html
    assert "'key': 'c1607918ab2d91ade8037d36a7b7c333'" in html
    assert 'script async="async" data-cfasync="false" src="https://bellnewyork.org/21/173bd25a12e240a688efa71a28dc9bbf"' in html
    assert 'id="container-173bd25a12e240a688efa71a28dc9bbf"' in html
    assert 'min-height: 180px;' in html and 'height: 90px;' in html


@pytest.mark.parametrize("slug", ["about", "privacy", "terms", "contact"])
def test_trust_routes_and_navigation(client, slug):
    path = "/" + slug
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "noindex" not in response.headers.get("x-robots-tag", "").lower()
    document = homepage_document(client, path)
    assert len([text for tag, text in document.headings if tag == "h1"]) == 1
    assert len([text for tag, text in document.headings if tag == "title"]) == 1
    assert [attrs["href"] for tag, attrs in document.tags
            if tag == "link" and attrs.get("rel") == "canonical"] == [
                "https://achievers-video-downloader.onrender.com" + path]
    metas = [attrs for tag, attrs in document.tags if tag == "meta"]
    assert any(attrs.get("name") == "description" and attrs.get("content") for attrs in metas)
    assert not any("noindex" in attrs.get("content", "").lower() for attrs in metas)
    links = {attrs.get("href") for tag, attrs in document.tags if tag == "a"}
    assert {"/", "/about", "/privacy", "/terms", "/contact"} <= links
    assert any(tag == "footer" for tag, attrs in document.tags)
    assert not any(tag in {"script", "iframe", "form"} for tag, attrs in document.tags)
    assert "bellnewyork.org" not in response.text
    assert client.head(path).status_code == 200
    for alias in (path + "/", path + ".html"):
        redirect = client.get(alias, follow_redirects=False)
        assert redirect.status_code == 308 and redirect.headers["location"] == path
        assert client.get(alias).status_code == 200


def test_trust_unique_metadata_and_truthfulness(client):
    import re
    documents = [homepage_document(client, "/" + slug)
                 for slug in ("about", "privacy", "terms", "contact")]
    for heading in ("title", "h1"):
        values = [text for doc in documents for tag, text in doc.headings if tag == heading]
        assert len(values) == len(set(values)) == 4
    descriptions = [attrs["content"] for doc in documents for tag, attrs in doc.tags
                    if tag == "meta" and attrs.get("name") == "description"]
    assert len(set(descriptions)) == 4
    texts = [" ".join(" ".join(doc.text).split()) for doc in documents]
    about, privacy, terms, contact = texts
    assert "TikTok and Facebook" in about and "anti-bot challenges" in about
    assert "own or are authorized to download" in about and "own or are authorized to download" in terms
    for restriction in ("DRM", "authentication", "paywalls", "geographic", "technical restrictions"):
        assert restriction in terms
    assert "Do not use the service to bypass" in terms
    for detail in ("localStorage", "six", "Clear history", "JOB_TTL", "15 minutes", "30 seconds",
                   "no startup scan", "rate limiting", "cookies", "Google Fonts", "does not implement response caching"):
        assert detail in privacy
    assert "Adult ads are currently enabled and locked" in privacy
    assert "direct contact channel is not yet published" in contact
    for text in texts:
        assert not re.search(r"registered company|registered office|GDPR.compliant|CCPA.compliant|we collect no data|we never use cookies", text, re.I)
    for doc in documents:
        links = [attrs.get("href", "") for tag, attrs in doc.tags if tag == "a"]
        assert not any(link.startswith(("mailto:", "tel:")) or "github.com" in link for link in links)
    home = client.get("/").text
    footer = home[home.index("<footer>"):home.index("</footer>")]
    for slug in ("about", "privacy", "terms", "contact"):
        assert 'href="/' + slug + '"' in footer
    assert "Files are automatically deleted from the server after 15 minutes." not in footer
