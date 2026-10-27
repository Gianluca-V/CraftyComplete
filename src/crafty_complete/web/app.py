from contextlib import asynccontextmanager
from dataclasses import asdict

import httpx
from fastapi import FastAPI, Request

from crafty_complete import __version__
from crafty_complete.config import Settings
from crafty_complete.crafty.client import CraftyClient, CraftyStatus


def create_app(
    settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    """Build the application. `transport` lets tests replace all outbound HTTP."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        crafty_http = None
        if settings.crafty_configured:
            crafty_http = httpx.AsyncClient(
                base_url=settings.crafty_url.rstrip("/"),
                headers={"Authorization": f"Bearer {settings.crafty_api_token}"},
                verify=settings.crafty_verify_tls,
                timeout=settings.http_timeout_seconds,
                transport=transport,
            )
            app.state.crafty = CraftyClient(crafty_http)
        else:
            app.state.crafty = None
        yield
        if crafty_http:
            await crafty_http.aclose()

    app = FastAPI(title="craftyComplete", version=__version__, lifespan=lifespan)
    app.state.settings = settings

    @app.get("/api/health")
    async def health(request: Request) -> dict:
        crafty: CraftyClient | None = request.app.state.crafty
        status = await crafty.status() if crafty else CraftyStatus(state="not_configured")
        return {"status": "ok", "crafty": asdict(status)}

    return app
