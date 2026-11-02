from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from crafty_complete.auth.sessions import Session
from crafty_complete.crafty.client import CraftyError
from crafty_complete.servers.backups import BackupError, BackupService
from crafty_complete.web.deps import crafty_failure, require_crafty, require_session
from crafty_complete.web.server_routes import ServerId, require_not_busy

router = APIRouter(prefix="/api/servers")


class BackupRequest(BaseModel):
    # Crafty backup ids are UUIDs; omit it to use the server's default configuration.
    backup_id: str | None = Field(default=None, pattern=r"^[a-z0-9-]{1,64}$")


def _backups(request: Request) -> BackupService:
    require_crafty(request)
    return request.app.state.backups


async def _run(request: Request, session: Session, operation) -> dict:
    try:
        return await operation(_backups(request))
    except BackupError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except CraftyError as exc:
        raise crafty_failure(request, session, exc) from exc


@router.get("/{server_id}/backups")
async def list_backups(
    server_id: ServerId,
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    """Each Crafty backup configuration of the server and its backups, newest first."""
    return await _run(request, session, lambda backups: backups.list(
        session.crafty_token, server_id))


# A backup taken while a modpack update moves files would capture a half-updated server.
@router.post("/{server_id}/backups", dependencies=[Depends(require_not_busy)])
async def start_backup(
    server_id: ServerId,
    body: BackupRequest,
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    return await _run(request, session, lambda backups: backups.start(
        session.crafty_token, server_id, body.backup_id))
