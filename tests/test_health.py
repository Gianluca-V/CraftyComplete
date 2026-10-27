import httpx

from tests.conftest import make_client

CRAFTY = "https://crafty.local:8443"


def test_health_reports_not_configured_without_crafty_url():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no outbound call expected")

    with make_client(handler) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "crafty": {"state": "not_configured", "servers_visible": None, "error": None},
    }


def test_health_calls_crafty_with_bearer_token(requests_seen):
    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(
            200, json={"status": "ok", "data": [{"server_id": "a"}, {"server_id": "b"}]}
        )

    with make_client(handler, crafty_url=CRAFTY, crafty_api_token="tok") as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["crafty"] == {
        "state": "connected",
        "servers_visible": 2,
        "error": None,
    }
    assert str(requests_seen[0].url) == f"{CRAFTY}/api/v2/servers"
    assert requests_seen[0].headers["Authorization"] == "Bearer tok"


def test_health_reports_rejected_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"status": "error", "error": "NOT_AUTHORIZED"})

    with make_client(handler, crafty_url=CRAFTY, crafty_api_token="bad") as client:
        body = client.get("/api/health").json()

    assert body["crafty"]["state"] == "unauthorized"
    assert body["crafty"]["error"] == "NOT_AUTHORIZED"


def test_health_reports_unreachable_crafty():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with make_client(handler, crafty_url=CRAFTY, crafty_api_token="tok") as client:
        body = client.get("/api/health").json()

    assert body["crafty"]["state"] == "unreachable"
    assert body["crafty"]["servers_visible"] is None


def test_health_requires_token_when_url_is_set():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no outbound call expected")

    with make_client(handler, crafty_url=CRAFTY) as client:
        body = client.get("/api/health").json()

    assert body["crafty"]["state"] == "not_configured"
