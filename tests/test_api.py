import http.client
import json
from collections.abc import Iterator

import pytest

from focuslock.api import ApiServer
from focuslock.client import ApiClient
from focuslock.errors import FocusLockError


@pytest.fixture
def server(app) -> Iterator[ApiServer]:
    with ApiServer(app, "127.0.0.1", 0, token="test-token") as running:
        yield running


@pytest.fixture
def client(server: ApiServer) -> ApiClient:
    return ApiClient(server.url, "test-token")


def raw(server: ApiServer, method: str, path: str, headers=None, body: bytes | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, dict(response.getheaders()), data


def test_health_needs_no_token(server) -> None:
    status, _, body = raw(server, "GET", "/api/v1/health")
    assert status == 200 and json.loads(body)["status"] == "ok"


def test_token_is_required(server) -> None:
    status, headers, _ = raw(server, "GET", "/api/v1/status")
    assert status == 401 and headers["WWW-Authenticate"] == "Bearer"
    status, _, _ = raw(server, "GET", "/api/v1/status", {"Authorization": "Bearer nope"})
    assert status == 401


def test_foreign_host_header_is_rejected(server) -> None:
    headers = {"Host": "evil.example:80", "Authorization": "Bearer test-token"}
    status, _, _ = raw(server, "GET", "/api/v1/status", headers)
    assert status == 403


def test_unknown_route_and_wrong_method(server) -> None:
    auth = {"Authorization": "Bearer test-token"}
    assert raw(server, "GET", "/api/v1/nope", auth)[0] == 404
    status, headers, _ = raw(server, "PUT", "/api/v1/blocklist", auth)
    assert status == 405 and headers["Allow"] == "GET, POST"


def test_oversized_and_invalid_bodies(server) -> None:
    auth = {"Authorization": "Bearer test-token", "Content-Type": "application/json"}
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.putrequest("POST", "/api/v1/blocklist")
    for name, value in {**auth, "Content-Length": "999999"}.items():
        conn.putheader(name, value)
    conn.endheaders()
    assert conn.getresponse().status == 413
    conn.close()
    assert raw(server, "POST", "/api/v1/blocklist", auth, b"{not json")[0] == 400
    assert raw(server, "POST", "/api/v1/blocklist", auth, b"[1, 2]")[0] == 400


def test_blocklist_crud(client: ApiClient) -> None:
    result = client.post("/blocklist", {"domain": "netflix.com"})
    assert result["added"] == ["netflix.com", "www.netflix.com"]
    assert "netflix.com" in client.get("/blocklist")["domains"]
    assert client.delete("/blocklist/netflix.com") is None
    with pytest.raises(FocusLockError, match="404"):
        client.delete("/blocklist/netflix.com")
    with pytest.raises(FocusLockError, match="422"):
        client.post("/blocklist", {"domain": "not a domain"})


def test_session_lifecycle(client: ApiClient) -> None:
    started = client.post("/sessions", {"mode": "timer", "minutes": 25})
    assert started["state"] == "focusing" and started["planned_seconds"] == 1500
    with pytest.raises(FocusLockError, match="409"):
        client.post("/sessions", {"mode": "flow"})
    assert client.post("/sessions/current/break")["state"] == "on_break"
    assert client.post("/sessions/current/skip-break")["state"] == "focusing"
    with pytest.raises(FocusLockError, match="404"):
        client.post("/sessions/current/pause")
    assert client.post("/sessions/current/stop")["status"] == "abandoned"
    assert client.get("/sessions?limit=5")["sessions"][0]["status"] == "abandoned"
    with pytest.raises(FocusLockError, match="422"):
        client.post("/sessions", {"mode": "timer", "minutes": "ten"})
    with pytest.raises(FocusLockError, match="400"):
        client.post("/sessions", {"mode": "nap"})


def test_rewards_and_stats(client: ApiClient, app) -> None:
    app.db.add_ledger(1200, "earned", app.clock.now())
    assert client.post("/rewards/spend", {"minutes": 5})["window_remaining_seconds"] == 300
    with pytest.raises(FocusLockError, match="409"):
        client.post("/rewards/spend", {"minutes": 500})
    assert len(client.get("/stats?days=14")["days"]) == 14
    with pytest.raises(FocusLockError, match="400"):
        client.get("/stats?days=0")


def test_password_header_and_cheats(client: ApiClient, app) -> None:
    app.set_password("hunter22")
    client.post("/blocklist", {"domain": "netflix.com"})
    with pytest.raises(FocusLockError, match="403"):
        client.delete("/blocklist/netflix.com")
    with pytest.raises(FocusLockError, match="403"):
        client.delete("/blocklist/netflix.com", password="wrong!")
    assert client.delete("/blocklist/netflix.com", password="hunter22") is None
    client.post("/sessions", {"mode": "timer", "minutes": 25})
    with pytest.raises(FocusLockError, match="403"):
        client.post("/sessions/current/stop")
    assert client.post("/sessions/current/stop", password="hunter22")["status"] == "abandoned"
    kinds = [cheat["kind"] for cheat in client.get("/cheats")["cheats"]]
    assert kinds.count("password_override") == 2 and kinds.count("password_failed") == 1
