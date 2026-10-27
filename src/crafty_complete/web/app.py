from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from crafty_complete import __version__
from crafty_complete.catalog.modrinth import ModrinthProvider
from crafty_complete.config import Settings
from crafty_complete.crafty.client import CraftyClient, CraftyStatus
from crafty_complete.web.catalog_routes import router as catalog_router

WEB_DIR = Path(__file__).parent


def create_app(
    settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    """Build the application. `transport` lets tests replace all outbound HTTP."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        modrinth_http = httpx.AsyncClient(
            base_url=settings.modrinth_url,
            headers={"User-Agent": settings.user_agent},
            timeout=settings.http_timeout_seconds,
            transport=transport,
        )
        app.state.catalog = ModrinthProvider(modrinth_http)

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
        await modrinth_http.aclose()
        if crafty_http:
            await crafty_http.aclose()

    app = FastAPI(title="craftyComplete", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    app.include_router(catalog_router)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(request, "index.html", {"version": __version__})

    @app.get("/api/health")
    async def health(request: Request) -> dict:
        crafty: CraftyClient | None = request.app.state.crafty
        status = await crafty.status() if crafty else CraftyStatus(state="not_configured")
        return {"status": "ok", "crafty": asdict(status)}

    return app
