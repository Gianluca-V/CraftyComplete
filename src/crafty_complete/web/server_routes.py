from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field

from crafty_complete.auth.sessions import Session
from crafty_complete.catalog.models import CatalogError, Loader
from crafty_complete.crafty.client import CraftyError
from crafty_complete.mods.adoption import AdoptionService, NoModsFolder
from crafty_complete.mods.installer import MAX_UPLOAD_BYTES, ModOperationError
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


async def _jar_body(request: Request, file_name: str) -> bytes:
    """The raw request body, refused unless it is binary and at most MAX_UPLOAD_BYTES.

    Cross-site forms cannot send application/octet-stream.
    """
    content_type = request.headers.get("content-type", "").split(";")[0].strip()
    if content_type != "application/octet-stream":
        raise HTTPException(status_code=415, detail="Send the jar as application/octet-stream.")
    too_large = HTTPException(status_code=413, detail=(
        f"{file_name} is larger than 90 MiB; upload it in Crafty instead."))
    if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES:
        raise too_large
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_UPLOAD_BYTES:
            raise too_large
    return bytes(data)


@router.post("/{server_id}/mods/upload", dependencies=[Depends(require_not_busy)])
async def upload_mod(
    server_id: ServerId,
    file_name: Annotated[str, Query(max_length=255)],
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    """Add a jar the user dropped on the page to mods/ (one file per request)."""
    data = await _jar_body(request, file_name)
    return await _run(request, session, lambda installer: installer.upload(
        session.crafty_token, server_id, file_name, data))


@router.post("/{server_id}/mods/{file_name}/update",dependencies=[Depends(require_json), Depends(require_not_busy)])
async def update(
    server_id: ServerId,
    file_name: str,
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    return await _run(request, session, lambda installer: installer.update(
        session.crafty_token, server_id, file_name))


@router.get("/{server_id}/mods/{file_name}/versions")
async def versions(
    server_id: ServerId,
    file_name: str,
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    """Versions of an installed mod that fit the server."""
    return await _run(request, session, lambda installer: installer.versions(
        session.crafty_token, server_id, file_name))


class VersionRequest(BaseModel):
    version_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


@router.post("/{server_id}/mods/{file_name}/version", dependencies=[Depends(require_not_busy)])
async def set_version(
    server_id: ServerId,
    file_name: str,
    body: VersionRequest,
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    """Install a chosen version of a mod (not pinned)."""
    return await _run(request, session, lambda installer: installer.set_version(
        session.crafty_token, server_id, file_name, body.version_id))


@router.get("/{server_id}/mods/{file_name}/removal")
async def removal(
    server_id: ServerId,
    file_name: str,
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    """What removing a mod affects. Removes nothing."""
    return await _run(request, session, lambda installer: installer.removal_plan(
        session.crafty_token, server_id, file_name))


class RemoveRequest(BaseModel):
    # Remove the mod even though other mods require it.
    force: bool = False
    # Unused dependencies (file names from the removal plan) to remove as well.
    remove_dependencies: list[str] = Field(default_factory=list, max_length=200)


@router.post("/{server_id}/mods/{file_name}/remove", dependencies=[Depends(require_not_busy)])
async def remove(
    server_id: ServerId,
    file_name: str,
    body: RemoveRequest,
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    return await _run(request, session, lambda installer: installer.remove(
        session.crafty_token, server_id, file_name, body.force, body.remove_dependencies))


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
