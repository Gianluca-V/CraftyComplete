from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from crafty_complete.auth.sessions import Session
from crafty_complete.catalog.models import CatalogError
from crafty_complete.crafty.client import CraftyError
from crafty_complete.modpacks.creator import CreationRefused
from crafty_complete.servers.creator import KINDS, NewServerRequest, ServerCreator
from crafty_complete.web.deps import crafty_failure, require_crafty, require_session
from crafty_complete.web.modpack_routes import EULA_REQUIRED, JOB_RUNNING

router = APIRouter(prefix="/api/servers/new")

ServerKind = Literal[tuple(KINDS)]


class NewServerBody(BaseModel):
    kind: ServerKind
    minecraft: str = Field(pattern=r"^[A-Za-z0-9_.+-]{1,32}$")
    # Same rule as Crafty's server name: no slashes or '#'.
    name: str = Field(min_length=2, max_length=64, pattern=r"^[^/\\#]*$")
    port: int = Field(default=25565, ge=1, le=65535)
    mem_min: float = Field(default=2, gt=0, le=256)
    mem_max: float = Field(default=4, gt=0, le=256)
    # Modrinth project ids or slugs, installed with their required dependencies.
    mods: list[str] = Field(default_factory=list, max_length=200)
    agree_to_eula: bool = False


def _creator(request: Request) -> ServerCreator:
    require_crafty(request)
    return request.app.state.server_creator


@router.get("/options")
async def options(request: Request, session: Session = Depends(require_session)) -> dict:
    """Kinds of server and Minecraft versions this Crafty can install."""
    try:
        return await _creator(request).options(session.crafty_token)
    except CraftyError as exc:
        raise crafty_failure(request, session, exc) from exc


@router.post("", status_code=202)
async def create(body: NewServerBody, request: Request,
                 session: Session = Depends(require_session)) -> dict:
    """Start creating a server; poll /api/jobs/{job_id}."""
    if not body.agree_to_eula:
        raise HTTPException(status_code=422, detail=EULA_REQUIRED)
    if body.mem_min > body.mem_max:
        raise HTTPException(status_code=422, detail="Minimum memory cannot exceed maximum memory.")
    mods = [ref.strip() for ref in body.mods if ref.strip()]
    if any(not _valid_ref(ref) for ref in mods):
        raise HTTPException(status_code=422, detail="Mods must be Modrinth project ids or slugs.")
    creator = _creator(request)
    if request.app.state.jobs.running(session.user_id):
        raise HTTPException(status_code=409, detail=JOB_RUNNING)
    new = NewServerRequest(kind=body.kind, minecraft=body.minecraft, name=body.name,
                           port=body.port, mem_min=body.mem_min, mem_max=body.mem_max,
                           mods=tuple(mods))
    try:
        prepared = await creator.prepare(session.crafty_token, session.superuser, new)
    except CreationRefused as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except CraftyError as exc:
        raise crafty_failure(request, session, exc) from exc
    except CatalogError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # The job keeps using this Crafty token until it ends, even if the user logs out.
    token = session.crafty_token
    started = request.app.state.jobs.start(
        session.user_id, f"{prepared.kind.label} {body.minecraft}: {body.name}",
        lambda running_job: creator.run(running_job, token, prepared),
        lambda exc: creator.describe_error(started, exc),
    )
    return {"job_id": started.id}


def _valid_ref(ref: str) -> bool:
    return 0 < len(ref) <= 64 and all(c.isascii() and (c.isalnum() or c in "_.-") for c in ref)
