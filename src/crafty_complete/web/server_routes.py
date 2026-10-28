from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field

from crafty_complete.auth.sessions import Session
from crafty_complete.catalog.models import CatalogError, Loader
from crafty_complete.crafty.client import CraftyError
from crafty_complete.mods.adoption import AdoptionService, NoModsFolder
from crafty_complete.mods.installer import ModOperationError
from crafty_complete.web.deps import crafty_failure, require_crafty, require_session

router = APIRouter(prefix="/api/servers")

# Crafty server ids are UUIDs; anything else never reaches Crafty or the database.
ServerId = Annotated[str, Path(pattern=r"^[a-z0-9-]{1,64}$")]

FILES_FORBIDDEN = "You do not have file access to this server in Crafty."


class ScanRequest(BaseModel):
    loader: Loader | None = None
    game_version: str | None = Field(default=None, min_length=1, max_length=64)


class PinRequest(BaseModel):
    pinned: bool


def _adoption(request: Request) -> AdoptionService:
    require_crafty(request)
    return request.app.state.adoption


def _fail(request: Request, session: Session, exc: CraftyError) -> HTTPException:
    if exc.state == "forbidden":
        return HTTPException(status_code=403, detail=FILES_FORBIDDEN)
    return crafty_failure(request, session, exc)


def require_not_busy(server_id: ServerId, request: Request,
                     session: Session = Depends(require_session)) -> None:
    """Mod changes during a modpack update would wait minutes for its lock: refuse them."""
    reason = request.app.state.locks.busy(server_id)
    if reason:
        raise HTTPException(status_code=409, detail=reason)


@router.post("/{server_id}/scan", dependencies=[Depends(require_not_busy)])
async def scan(
    server_id: ServerId,
    body: ScanRequest,
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    """Adopt the server on first call, rescan it afterwards."""
    adoption = _adoption(request)
    try:
        server = await adoption.scan(
            session.crafty_token, server_id, body.loader, body.game_version
        )
    except CraftyError as exc:
        raise _fail(request, session, exc) from exc
    except NoModsFolder as exc:
        raise HTTPException(status_code=404, detail="This server has no mods folder.") from exc
    except CatalogError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return server.to_dict()


@router.get("/{server_id}/mods")
async def mods(
    server_id: ServerId,
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    adoption = _adoption(request)
    try:
        await adoption.ensure_file_access(session.crafty_token, server_id)
    except CraftyError as exc:
        raise _fail(request, session, exc) from exc
    return request.app.state.adoption_store.get(server_id).to_dict()


@router.patch("/{server_id}/mods/{file_name}", dependencies=[Depends(require_not_busy)])
async def pin(
    server_id: ServerId,
    file_name: str,
    body: PinRequest,
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    adoption = _adoption(request)
    try:
        await adoption.ensure_file_access(session.crafty_token, server_id)
    except CraftyError as exc:
        raise _fail(request, session, exc) from exc
    # Wait for a running scan or update, which rewrites this server's records.
    async with request.app.state.locks(server_id):
        mod = request.app.state.adoption_store.set_pinned(server_id, file_name, body.pinned)
    if mod is None:
        raise HTTPException(status_code=404, detail="No adopted mod with that file name.")
    return mod.to_dict()


class InstallRequest(BaseModel):
    # A Modrinth project id or slug.
    project_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")


def require_json(request: Request) -> None:
    """Endpoints without a body still demand JSON, which cross-site forms cannot send."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="Send this request as JSON.")


async def _run(request: Request, session: Session, operation) -> dict:
    """Run an installer operation and translate its failures into HTTP errors."""
    require_crafty(request)
    try:
        return await operation(request.app.state.installer)
    except ModOperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except CraftyError as exc:
        raise _fail(request, session, exc) from exc
    except CatalogError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/{server_id}/mods", dependencies=[Depends(require_not_busy)])
async def install(
    server_id: ServerId,
    body: InstallRequest,
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    """Install a Modrinth mod and its missing required dependencies."""
    return await _run(request, session, lambda installer: installer.install(
        session.crafty_token, server_id, body.project_id))


@router.post("/{server_id}/mods/{file_name}/update", dependencies=[Depends(require_json), Depends(require_not_busy)])
async def update(
    server_id: ServerId,
    file_name: str,
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    return await _run(request, session, lambda installer: installer.update(
        session.crafty_token, server_id, file_name))


@router.post("/{server_id}/update-all", dependencies=[Depends(require_json), Depends(require_not_busy)])
async def update_all(
    server_id: ServerId,
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    return await _run(request, session, lambda installer: installer.update_all(
        session.crafty_token, server_id))


@router.post("/{server_id}/check-updates", dependencies=[Depends(require_json), Depends(require_not_busy)])
async def check_updates(
    server_id: ServerId,
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    """Refresh "update available" now. Installs nothing."""
    adoption = _adoption(request)
    try:
        await adoption.ensure_file_access(session.crafty_token, server_id)
        server = await adoption.check_updates(server_id)
    except CraftyError as exc:
        raise _fail(request, session, exc) from exc
    except CatalogError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return server.to_dict()
