from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from crafty_complete.auth.sessions import Session
from crafty_complete.crafty.client import CraftyError
from crafty_complete.players.service import PlayerAction, PlayerError, PlayerService
from crafty_complete.web.deps import crafty_failure, require_crafty, require_session
from crafty_complete.web.server_routes import ServerId, require_not_busy

router = APIRouter(prefix="/api/servers")


class PlayerRequest(BaseModel):
    action: PlayerAction
    # Minecraft names: these values are typed into the server console.
    name: str = Field(pattern=r"^[A-Za-z0-9_]{1,16}$")
    reason: str | None = Field(default=None, max_length=200,
                               pattern=r"^[^\x00-\x1f\x7f-\x9f\u2028\u2029]*$")


def _players(request: Request) -> PlayerService:
    require_crafty(request)
    return request.app.state.players


async def _run(request: Request, session: Session, operation) -> dict:
    try:
        return await operation(_players(request))
    except PlayerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except CraftyError as exc:
        raise crafty_failure(request, session, exc) from exc


@router.get("/{server_id}/players")
async def players(
    server_id: ServerId,
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    """Whitelist, operators, bans and who is online."""
    return await _run(request, session, lambda service: service.players(
        session.crafty_token, server_id))


@router.post("/{server_id}/players", dependencies=[Depends(require_not_busy)])
async def player_action(
    server_id: ServerId,
    body: PlayerRequest,
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    return await _run(request, session, lambda service: service.apply(
        session.crafty_token, server_id, body.action, body.name, body.reason))
