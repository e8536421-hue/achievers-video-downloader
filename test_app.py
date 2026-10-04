from fastapi.testclient import TestClient
from app import app

client = TestClient(app)

def test_health():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True

def test_rejects_non_http_url():
    response = client.post("/api/info", json={"url": "file:///etc/passwd"})
    assert response.status_code == 400