import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, fields
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from crafty_complete import __version__
from crafty_complete.auth.sessions import SessionStore
from crafty_complete.config import Settings
from crafty_complete.crafty.client import CraftyClient, CraftyStatus
from crafty_complete.services import open_services
from crafty_complete.web.auth_routes import router as auth_router
from crafty_complete.web.catalog_routes import router as catalog_router
from crafty_complete.web.control_routes import router as control_router
from crafty_complete.web.create_routes import router as create_router
from crafty_complete.web.file_routes import router as file_router
from crafty_complete.web.player_routes import router as player_router
from crafty_complete.web.modpack_routes import ProjectRef
from crafty_complete.web.modpack_routes import router as modpack_router
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
        async with open_services(settings, transport) as services:
            for field in fields(services):
                setattr(app.state, field.name, getattr(services, field.name))
            services.update_checker.start()
            try:
                yield
            finally:
                await services.update_checker.stop()

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
    # Before the /api/servers/{server_id} routes, so "new" is never read as a server id.
    app.include_router(create_router)
    app.include_router(server_router)
    app.include_router(control_router)
    app.include_router(player_router)
    app.include_router(file_router)
    app.include_router(modpack_router)

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

    @app.get("/modpacks/{project_ref}/create", response_class=HTMLResponse)
    async def create_from_modpack_page(request: Request, project_ref: ProjectRef):
        return page(request, "modpack_create.html", active="browse", project_ref=project_ref)

    # Registered before /servers/{server_id}, which would also match "new".
    @app.get("/servers/new", response_class=HTMLResponse)
    async def new_server_page(request: Request):
        return page(request, "server_new.html", active="servers")

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
