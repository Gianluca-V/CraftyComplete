from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field

from crafty_complete.auth.sessions import Session
from crafty_complete.catalog.models import CatalogError, CatalogNotFound
from crafty_complete.crafty.client import CraftyError
from crafty_complete.modpacks.creator import (
    CRAFTY_TYPES,
    CreateRequest,
    CreationRefused,
    ModpackServerCreator,
)
from crafty_complete.modpacks.linker import ModpackLinker, version_summary
from crafty_complete.modpacks.packs import describe_failure
from crafty_complete.web.deps import crafty_failure, require_crafty, require_session
from crafty_complete.web.server_routes import FILES_FORBIDDEN

router = APIRouter(prefix="/api")

ProjectRef = Annotated[str, Path(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
JOB_RUNNING = "You already have a modpack job running; wait for it to finish."
EULA_REQUIRED = ("You must accept the Minecraft EULA (https://aka.ms/MinecraftEULA) "
                 "to create a server.")


class CreateServerRequest(BaseModel):
    project_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    version_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    # Same rule as Crafty's server name: no slashes or '#'.
    name: str = Field(min_length=2, max_length=64, pattern=r"^[^/\\#]*$")
    port: int = Field(default=25565, ge=1, le=65535)
    mem_min: float = Field(default=2, gt=0, le=256)
    mem_max: float = Field(default=4, gt=0, le=256)
    agree_to_eula: bool = False


def _creator(request: Request) -> ModpackServerCreator:
    require_crafty(request)
    return request.app.state.modpack_creator


@router.get("/modpacks/{project_ref}/versions")
async def versions(project_ref: ProjectRef, request: Request,
                   session: Session = Depends(require_session)) -> dict:
    catalog = request.app.state.catalog
    try:
        project = await catalog.project(project_ref)
        found = await catalog.project_versions(project["id"])
    except CatalogNotFound as exc:
        raise HTTPException(status_code=404, detail="No such Modrinth project.") from exc
    except CatalogError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "project_id": project["id"],
        "title": project["title"],
        "versions": [{
            "id": v["id"], "version_number": v.get("version_number"),
            "version_type": v.get("version_type"),
            "loaders": v.get("loaders") or [], "game_versions": v.get("game_versions") or [],
            "date_published": v.get("date_published"),
            "supported": bool(set(v.get("loaders") or []) & set(CRAFTY_TYPES)),
        } for v in found],
    }


@router.post("/modpacks/servers", status_code=202)
async def create_server(body: CreateServerRequest, request: Request,
                        session: Session = Depends(require_session)) -> dict:
    """Start building a Crafty server from a modpack; poll /api/jobs/{job_id}."""
    if not body.agree_to_eula:
        raise HTTPException(status_code=422, detail=EULA_REQUIRED)
    if body.mem_min > body.mem_max:
        raise HTTPException(status_code=422,
                            detail="Minimum memory cannot exceed maximum memory.")
    creator = _creator(request)
    if request.app.state.jobs.running(session.user_id):
        raise HTTPException(status_code=409, detail=JOB_RUNNING)
    create = CreateRequest(project_id=body.project_id, version_id=body.version_id,
                           name=body.name, port=body.port, mem_min=body.mem_min,
                           mem_max=body.mem_max)
    try:
        prepared = await creator.prepare(session.crafty_token, session.superuser, create)
    except CreationRefused as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except CraftyError as exc:
        raise crafty_failure(request, session, exc) from exc
    except CatalogError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # The job keeps using this Crafty token until it ends, even if the user logs out.
    token = session.crafty_token
    started = request.app.state.jobs.start(
        session.user_id, f"{prepared.project['title']} {prepared.version.get('version_number')}",
        lambda running_job: creator.run(running_job, token, prepared, create),
        # Called only after start() returned, so `started` is assigned by then.
        lambda exc: creator.describe_error(started, exc),
    )
    return {"job_id": started.id}


ServerId = Annotated[str, Path(pattern=r"^[a-z0-9-]{1,64}$")]


class LinkRequest(BaseModel):
    project_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    version_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")


def _linker(request: Request) -> ModpackLinker:
    require_crafty(request)
    return request.app.state.modpack_linker


async def _guarded(request: Request, session: Session, operation):
    """Run a linker/updater step, translating refusals and Crafty/Modrinth failures."""
    try:
        return await operation()
    except CreationRefused as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except CraftyError as exc:
        if exc.state == "forbidden":
            raise HTTPException(status_code=403, detail=FILES_FORBIDDEN) from exc
        raise crafty_failure(request, session, exc) from exc
    except CatalogError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/servers/{server_id}/modpack")
async def modpack_status(server_id: ServerId, request: Request,
                         session: Session = Depends(require_session)) -> dict:
    """The linked modpack version and the newest one for the same Minecraft and loader."""
    linker = _linker(request)

    async def status() -> dict:
        await request.app.state.adoption.ensure_file_access(session.crafty_token, server_id)
        server = request.app.state.adoption_store.get(server_id)
        if not server.modpack_project_id:
            return {"linked": None, "latest": None, "update_available": False, "versions": []}
        versions = await linker.compatible_versions(server.modpack_project_id, server)
        latest = next((v for v in versions if v.get("version_type") == "release"),
                      versions[0] if versions else None)
        return {
            "linked": server.to_dict()["modpack"],
            "latest": version_summary(latest) if latest else None,
            "update_available": bool(latest and latest["id"] != server.modpack_version_id),
            "versions": [version_summary(v) for v in versions],
        }

    return await _guarded(request, session, status)


@router.post("/servers/{server_id}/modpack/link", status_code=202)
async def link_modpack(server_id: ServerId, body: LinkRequest, request: Request,
                       session: Session = Depends(require_session)) -> dict:
    """Record which modpack version an adopted server runs; poll /api/jobs/{job_id}."""
    linker = _linker(request)
    target = await _guarded(request, session, lambda: linker.prepare(
        session.crafty_token, server_id, body.project_id, body.version_id))
    if request.app.state.jobs.running(session.user_id):
        raise HTTPException(status_code=409, detail=JOB_RUNNING)
    started = request.app.state.jobs.start(
        session.user_id, f"Link {target.project['title']} {target.version.get('version_number')}",
        lambda running_job: linker.link(running_job, server_id, target),
        describe_failure,
    )
    return {"job_id": started.id}


@router.get("/jobs/{job_id}")
async def job_status(job_id: Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")],
                     request: Request, session: Session = Depends(require_session)) -> dict:
    job = request.app.state.jobs.get(job_id, session.user_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    return job.to_dict()
