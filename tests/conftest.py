from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from crafty_complete.config import Settings
from crafty_complete.web.app import create_app

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler, **settings_overrides) -> TestClient:
    """Build a TestClient whose outbound HTTP calls go to `handler`."""
    settings = Settings(_env_file=None, **settings_overrides)
    app = create_app(settings, transport=httpx.MockTransport(handler))
    return TestClient(app)


@pytest.fixture
def requests_seen() -> list[httpx.Request]:
    return []
