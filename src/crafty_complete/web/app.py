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
from crafty_complete.crafty.client import CookielessTransport, CraftyClient, CraftyStatus
from crafty_complete.web.auth_routes import router as auth_router
from crafty_complete.web.catalog_routes import router as catalog_router
from crafty_complete.mods.adoption import AdoptionService
from crafty_complete.mods.installer import ModInstaller
from crafty_complete.mods.locks import ServerLocks
from crafty_complete.mods.store import AdoptionStore
from crafty_complete.mods.update_checker import UpdateChecker
from crafty_complete.web.deps import current_session, require_session
from crafty_complete.web.server_routes import ServerId
from crafty_complete.web.server_routes import router as server_router

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
                timeout=settings.http_timeout_seconds,
                transport=CookielessTransport(
                    transport or httpx.AsyncHTTPTransport(verify=settings.crafty_verify_tls)
                ),
            )
            app.state.crafty = CraftyClient(crafty_http)
        else:
            app.state.crafty = None

        store = app.state.adoption_store = AdoptionStore(settings.database_path)
        locks = app.state.locks = ServerLocks()
        # Routes refuse to use these while Crafty is not configured (require_crafty);
        # the background update check only needs Modrinth and the store.
        adoption = app.state.adoption = AdoptionService(
            app.state.crafty, app.state.catalog, store, locks)
        app.state.installer = ModInstaller(
            app.state.crafty, app.state.catalog, store, adoption, locks)
        checker = app.state.update_checker = UpdateChecker(
            adoption, store, settings.update_check_hours)
        checker.start()
        yield
        await checker.stop()
        store.close()
        await modrinth_http.aclose()
        if crafty_http:
            await crafty_http.aclose()

    # Interactive API docs would be the only pages reachable without a session.
    app = FastAPI(title="craftyComplete", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
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
    app.include_router(server_router)

    def page(request: Request, template: str, **context):
        session = current_session(request)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request, template,
            {"version": __version__, "username": session.username, **context},
        )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return page(request, "index.html", active="browse")

    @app.get("/servers", response_class=HTMLResponse)
    async def servers_page(request: Request):
        return page(request, "servers.html", active="servers")

    @app.get("/servers/{server_id}", response_class=HTMLResponse)
    async def server_page(request: Request, server_id: ServerId):
        return page(request, "server.html", active="servers", server_id=server_id)

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
