import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from crafty_complete import __version__
from crafty_complete.auth.sessions import SessionStore
from crafty_complete.catalog.modrinth import ModrinthProvider
from crafty_complete.config import Settings
from crafty_complete.crafty.client import CraftyClient, CraftyStatus
from crafty_complete.web.auth_routes import router as auth_router
from crafty_complete.web.catalog_routes import router as catalog_router
from crafty_complete.web.deps import current_session, require_session

WEB_DIR = Path(__file__).parent


def create_app(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] | None = None,
) -> FastAPI:
    """Build the application. `transport` and `clock` let tests replace I/O and time."""
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
        if settings.crafty_url:
            crafty_http = httpx.AsyncClient(
                base_url=settings.crafty_url.rstrip("/"),
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
    app.state.sessions = SessionStore(
        idle_seconds=settings.session_idle_minutes * 60,
        max_seconds=settings.session_max_hours * 3600,
        clock=clock or time.monotonic,
    )
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    app.include_router(auth_router)
    app.include_router(catalog_router, dependencies=[Depends(require_session)])

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        session = current_session(request)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request, "index.html", {"version": __version__, "username": session.username}
        )

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if current_session(request) is not None:
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"version": __version__})

    @app.get("/api/health")
    async def health(request: Request) -> dict:
        crafty: CraftyClient | None = request.app.state.crafty
        if crafty and settings.crafty_api_token:
            status = await crafty.status(settings.crafty_api_token)
        else:
            status = CraftyStatus(state="not_configured")
        return {"status": "ok", "crafty": asdict(status)}

    return app
