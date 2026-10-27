from fastapi import HTTPException, Request

from crafty_complete.auth.sessions import SESSION_COOKIE, Session, SessionStore
from crafty_complete.crafty.client import CraftyClient, CraftyError


def sessions(request: Request) -> SessionStore:
    return request.app.state.sessions


def current_session(request: Request) -> Session | None:
    return sessions(request).get(request.cookies.get(SESSION_COOKIE))


def require_session(request: Request) -> Session:
    session = current_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="Not logged in.")
    return session


def require_crafty(request: Request) -> CraftyClient:
    crafty: CraftyClient | None = request.app.state.crafty
    if crafty is None:
        raise HTTPException(status_code=503, detail="Crafty URL is not configured.")
    return crafty


def crafty_failure(request: Request, session: Session, exc: CraftyError) -> HTTPException:
    """Map a failed Crafty call made with a user's token to an HTTP error."""
    if exc.state == "unauthorized":
        # Crafty no longer accepts this token (revoked, user disabled or deleted).
        sessions(request).delete(session.id)
        return HTTPException(status_code=401, detail="Crafty session expired. Log in again.")
    if exc.state == "forbidden":
        return HTTPException(status_code=403, detail="Crafty denied this action for your account.")
    if exc.state == "not_found":
        return HTTPException(status_code=404, detail="Not found in Crafty.")
    if exc.state == "unreachable":
        return HTTPException(status_code=502, detail="Crafty is unreachable.")
    return HTTPException(status_code=502, detail=f"Crafty request failed: {exc.message}")
