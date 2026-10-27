from collections.abc import Callable, Iterator
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from crafty_complete.auth.sessions import SESSION_COOKIE
from crafty_complete.config import Settings
from crafty_complete.web.app import create_app

Handler = Callable[[httpx.Request], httpx.Response]

CRAFTY = "https://crafty.local:8443"
USER_TOKEN = "user-jwt"


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_client(handler: Handler, clock: Callable[[], float] | None = None,
                **settings_overrides) -> TestClient:
    """Build a TestClient whose outbound HTTP calls go to `handler`."""
    settings = Settings(_env_file=None, **settings_overrides)
    app = create_app(settings, transport=httpx.MockTransport(handler), clock=clock)
    return TestClient(app)


def log_in(client: TestClient, token: str = USER_TOKEN) -> str:
    """Seed a session for a Crafty user directly, bypassing the login endpoint."""
    session = client.app.state.sessions.create(
        crafty_token=token, user_id="1", username="steve", superuser=False
    )
    client.cookies.set(SESSION_COOKIE, session.id)
    return session.id


@contextmanager
def make_logged_in_client(handler: Handler, **kwargs) -> Iterator[TestClient]:
    with make_client(handler, **kwargs) as client:
        log_in(client)
        yield client


@pytest.fixture
def requests_seen() -> list[httpx.Request]:
    return []
