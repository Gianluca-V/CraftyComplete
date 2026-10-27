import json

import httpx
import pytest

from crafty_complete.auth.sessions import SESSION_COOKIE
from tests.conftest import CRAFTY, USER_TOKEN, FakeClock, log_in, make_client

ME = {"user_id": 1, "username": "steve", "superuser": False, "enabled": True}


def crafty_handler(seen: list[httpx.Request], login_status=200, login_body=None):
    """Fake Crafty: login, @me, @me/permissions and the server list."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/api/v2/auth/login":
            body = login_body or {"status": "ok",
                                  "data": {"token": USER_TOKEN, "user_id": "1"}}
            return httpx.Response(login_status, json=body)
        if request.headers.get("Authorization") != f"Bearer {USER_TOKEN}":
            return httpx.Response(403, json={"status": "error", "error": "NOT_AUTHORIZED"})
        if path == "/api/v2/users/@me":
            return httpx.Response(200, json={"status": "ok", "data": ME})
        if path == "/api/v2/users/@me/permissions":
            return httpx.Response(200, json={"status": "ok", "data": {
                "permissions": "100", "counters": {}, "limits": {}}})
        if path == "/api/v2/servers":
            return httpx.Response(200, json={"status": "ok", "data": [
                {"server_id": "abc", "server_name": "Survival", "type": "minecraft-java",
                 "path": "/opt/crafty/servers/abc", "execution_command": "java -jar x.jar"},
            ]})
        raise AssertionError(f"unexpected Crafty call {request.method} {path}")

    return handler


def login(client, **extra):
    return client.post("/api/auth/login",
                       json={"username": "steve", "password": "hunter2hunter2", **extra})


# --- login -------------------------------------------------------------------

def test_login_forwards_credentials_and_sets_session_cookie(requests_seen):
    with make_client(crafty_handler(requests_seen), crafty_url=CRAFTY) as client:
        response = login(client)

    assert response.status_code == 200
    assert response.json() == {"user_id": "1", "username": "steve", "superuser": False}

    sent = requests_seen[0]
    assert str(sent.url) == f"{CRAFTY}/api/v2/auth/login"
    assert json.loads(sent.content) == {"username": "steve", "password": "hunter2hunter2"}
    assert "Authorization" not in sent.headers

    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert USER_TOKEN not in cookie
    assert USER_TOKEN not in response.text


def test_login_forwards_totp_only_when_given(requests_seen):
    with make_client(crafty_handler(requests_seen), crafty_url=CRAFTY) as client:
        login(client, totp="123456")
        login(client, totp="")

    assert json.loads(requests_seen[0].content)["totp"] == "123456"
    later_logins = [r for r in requests_seen[1:] if r.url.path == "/api/v2/auth/login"]
    assert "totp" not in json.loads(later_logins[0].content)


@pytest.mark.parametrize("status", [400, 401])
def test_login_rejects_bad_credentials_with_generic_message(status, requests_seen):
    body = {"status": "error", "error": "INCORRECT_CREDENTIALS", "error_data": "Incorrect"}
    handler = crafty_handler(requests_seen, login_status=status, login_body=body)
    with make_client(handler, crafty_url=CRAFTY) as client:
        response = login(client)

    assert response.status_code == 401
    assert response.json() == {"detail": "Incorrect username, password or 2FA code."}
    assert "set-cookie" not in response.headers
    assert client.app.state.sessions.count() == 0


@pytest.mark.parametrize(
    ("error", "detail"),
    [("ACCOUNT_DISABLED", "This Crafty account is disabled."),
     ("PASSWORD_AUTH_DISABLED", "Password login is disabled for this account in Crafty.")],
)
def test_login_reports_blocked_accounts(error, detail, requests_seen):
    handler = crafty_handler(requests_seen, login_status=403,
                             login_body={"status": "error", "error": error})
    with make_client(handler, crafty_url=CRAFTY) as client:
        response = login(client)

    assert response.status_code == 403
    assert response.json() == {"detail": detail}


def test_login_reports_crafty_cooldown(requests_seen):
    handler = crafty_handler(requests_seen, login_status=429, login_body={
        "status": "error", "error": "TOO_MANY_ATTEMPTS", "cooldown_time": "04:12"})
    with make_client(handler, crafty_url=CRAFTY) as client:
        response = login(client)

    assert response.status_code == 429
    assert response.json() == {"detail": "Too many login attempts. Try again in 04:12."}


def test_login_reports_unreachable_crafty():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with make_client(handler, crafty_url=CRAFTY) as client:
        response = login(client)

    assert response.status_code == 502
    assert response.json() == {"detail": "Crafty is unreachable."}


def test_login_requires_crafty_url():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no outbound call expected")

    with make_client(handler) as client:
        response = login(client)

    assert response.status_code == 503
    assert response.json() == {"detail": "Crafty URL is not configured."}


def test_login_only_accepts_json_bodies(requests_seen):
    with make_client(crafty_handler(requests_seen), crafty_url=CRAFTY) as client:
        response = client.post("/api/auth/login",
                               data={"username": "steve", "password": "hunter2hunter2"})

    assert response.status_code == 422
    assert requests_seen == []


# --- session guard -----------------------------------------------------------

@pytest.mark.parametrize("path", ["/api/me", "/api/servers", "/api/catalog/search",
                                  "/api/catalog/filters"])
def test_api_requires_session(path, requests_seen):
    with make_client(crafty_handler(requests_seen), crafty_url=CRAFTY) as client:
        response = client.get(path)

    assert response.status_code == 401
    assert response.json() == {"detail": "Not logged in."}
    assert requests_seen == []


def test_unknown_session_cookie_is_rejected(requests_seen):
    with make_client(crafty_handler(requests_seen), crafty_url=CRAFTY) as client:
        client.cookies.set(SESSION_COOKIE, "forged-session-id")
        response = client.get("/api/me")

    assert response.status_code == 401


def test_pages_redirect_to_login_without_session(requests_seen):
    with make_client(crafty_handler(requests_seen), crafty_url=CRAFTY) as client:
        response = client.get("/", follow_redirects=False)
        login_page = client.get("/login")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert login_page.status_code == 200
    assert 'id="login-form"' in login_page.text
    assert 'name="totp"' in login_page.text


def test_login_page_redirects_home_when_logged_in(requests_seen):
    with make_client(crafty_handler(requests_seen), crafty_url=CRAFTY) as client:
        log_in(client)
        response = client.get("/login", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_full_login_flow_unlocks_api(requests_seen):
    with make_client(crafty_handler(requests_seen), crafty_url=CRAFTY) as client:
        login(client)
        response = client.get("/api/me")

    assert response.status_code == 200


# --- delegated Crafty permissions ---------------------------------------------

def test_me_returns_user_and_crafty_permissions(requests_seen):
    with make_client(crafty_handler(requests_seen), crafty_url=CRAFTY) as client:
        log_in(client)
        response = client.get("/api/me")

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "1",
        "username": "steve",
        "superuser": False,
        "permissions": {"server_creation": True, "user_config": False,
                        "roles_config": False},
    }


def test_servers_are_listed_with_the_users_own_token(requests_seen):
    with make_client(crafty_handler(requests_seen), crafty_url=CRAFTY,
                     crafty_api_token="service-token") as client:
        log_in(client)
        response = client.get("/api/servers")

    assert response.status_code == 200
    assert response.json() == [
        {"server_id": "abc", "server_name": "Survival", "type": "minecraft-java"}
    ]
    assert requests_seen[0].headers["Authorization"] == f"Bearer {USER_TOKEN}"


def test_rejected_crafty_token_ends_the_session(requests_seen):
    with make_client(crafty_handler(requests_seen), crafty_url=CRAFTY) as client:
        log_in(client, token="revoked-token")
        first = client.get("/api/servers")
        calls_after_first = len(requests_seen)
        second = client.get("/api/me")

    assert first.status_code == 401
    assert first.json() == {"detail": "Crafty session expired. Log in again."}
    assert second.status_code == 401
    assert len(requests_seen) == calls_after_first
    assert client.app.state.sessions.count() == 0


# --- expiry and logout -------------------------------------------------------

def test_session_expires_after_idle_timeout(requests_seen):
    clock = FakeClock()
    with make_client(crafty_handler(requests_seen), clock=clock, crafty_url=CRAFTY,
                     session_idle_minutes=30) as client:
        log_in(client)
        clock.now += 29 * 60
        assert client.get("/api/me").status_code == 200
        clock.now += 29 * 60
        assert client.get("/api/me").status_code == 200
        clock.now += 31 * 60
        assert client.get("/api/me").status_code == 401


def test_session_expires_after_absolute_lifetime(requests_seen):
    clock = FakeClock()
    with make_client(crafty_handler(requests_seen), clock=clock, crafty_url=CRAFTY,
                     session_idle_minutes=60, session_max_hours=2) as client:
        log_in(client)
        clock.now += 50 * 60
        assert client.get("/api/me").status_code == 200
        clock.now += 50 * 60
        assert client.get("/api/me").status_code == 200  # 100 min: still inside 2 h
        clock.now += 21 * 60
        assert client.get("/api/me").status_code == 401  # 121 min: past 2 h


def test_logout_drops_session_without_revoking_crafty_tokens(requests_seen):
    with make_client(crafty_handler(requests_seen), crafty_url=CRAFTY) as client:
        log_in(client)
        response = client.post("/api/auth/logout", json={})
        after = client.get("/api/me")

    assert response.status_code == 204
    assert f'{SESSION_COOKIE}=""' in response.headers["set-cookie"]
    assert after.status_code == 401
    assert all("invalidate" not in r.url.path for r in requests_seen)


def test_crafty_login_cookies_are_never_replayed(requests_seen):
    """Crafty's login also sets a `token` cookie; it must not leak into other calls."""
    base = crafty_handler(requests_seen)

    def handler(request: httpx.Request) -> httpx.Response:
        response = base(request)
        if request.url.path == "/api/v2/auth/login":
            response.headers["Set-Cookie"] = f"token={USER_TOKEN}; Path=/"
        return response

    with make_client(handler, crafty_url=CRAFTY, crafty_api_token="service-token") as client:
        login(client)
        client.get("/api/servers")
        client.get("/api/health")

    assert len(requests_seen) == 4
    assert all("cookie" not in request.headers for request in requests_seen)


def test_login_replaces_the_previous_session(requests_seen):
    with make_client(crafty_handler(requests_seen), crafty_url=CRAFTY) as client:
        old_session = log_in(client)
        login(client)

        assert client.app.state.sessions.get(old_session) is None
        assert client.app.state.sessions.count() == 1


def test_login_reports_crafty_refusing_the_account(requests_seen):
    """Crafty may issue a token and still refuse it, e.g. when it enforces 2FA."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, json={"status": "ok", "data": {"token": "t"}})
        return httpx.Response(403, json={"status": "error", "error": "ACCESS_DENIED"})

    with make_client(handler, crafty_url=CRAFTY) as client:
        response = login(client)

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Crafty refused access to this account (it may require 2FA to be set up)."
    }
    assert client.app.state.sessions.count() == 0


def test_logout_requires_json_request(requests_seen):
    with make_client(crafty_handler(requests_seen), crafty_url=CRAFTY) as client:
        log_in(client)
        response = client.post("/api/auth/logout", data={"x": "1"})

        assert response.status_code == 415
        assert client.get("/api/me").status_code == 200


def test_api_docs_are_not_published(requests_seen):
    with make_client(crafty_handler(requests_seen), crafty_url=CRAFTY) as client:
        statuses = [client.get(path).status_code
                    for path in ("/docs", "/redoc", "/openapi.json")]

    assert statuses == [404, 404, 404]


@pytest.mark.parametrize(
    ("setting", "base_url", "secure"),
    [(None, "http://testserver", False), (None, "https://testserver", True),
     (True, "http://testserver", True), (False, "https://testserver", False)],
)
def test_session_cookie_secure_flag(setting, base_url, secure, requests_seen):
    with make_client(crafty_handler(requests_seen), crafty_url=CRAFTY,
                     session_cookie_secure=setting) as client:
        client.base_url = base_url
        response = login(client)

    assert ("Secure" in response.headers["set-cookie"]) is secure
