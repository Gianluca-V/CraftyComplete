from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from crafty_complete.auth.sessions import Session
from crafty_complete.crafty.client import CraftyError
from crafty_complete.servers.control import (COMMANDS_FORBIDDEN, NEEDS_IDLE_SERVER, PowerAction,
                                             ServerControl)
from crafty_complete.web.deps import crafty_failure, require_crafty, require_session
from crafty_complete.web.server_routes import ServerId

router = APIRouter(prefix="/api/servers")


class PowerRequest(BaseModel):
    action: PowerAction


def _control(request: Request) -> ServerControl:
    require_crafty(request)
    return request.app.state.control


@router.get("/{server_id}/status")
async def status(
    server_id: ServerId,
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    try:
        return await _control(request).status(session.crafty_token, server_id)
    except CraftyError as exc:
        raise crafty_failure(request, session, exc) from exc


@router.post("/{server_id}/power")
async def power(
    server_id: ServerId,
    body: PowerRequest,
    request: Request,
    session: Session = Depends(require_session),
) -> dict:
    control = _control(request)
    if body.action in NEEDS_IDLE_SERVER:
        reason = request.app.state.locks.busy(server_id)
        if reason:
            raise HTTPException(status_code=409, detail=reason)
    try:
        await control.power(session.crafty_token, server_id, body.action)
    except CraftyError as exc:
        if exc.state == "forbidden":
            raise HTTPException(status_code=403, detail=COMMANDS_FORBIDDEN) from exc
        raise crafty_failure(request, session, exc) from exc
    return {"action": body.action.value, "sent": True}
