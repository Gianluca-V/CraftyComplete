from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from crafty_complete.auth.sessions import SESSION_COOKIE, Session
from crafty_complete.crafty.client import CraftyClient, CraftyError, CraftyLoginError
from crafty_complete.web.deps import (
    crafty_failure,
    require_crafty,
    require_session,
    sessions,
)

router = APIRouter(prefix="/api")

BLOCKED_ACCOUNT_MESSAGES = {
    "ACCOUNT_DISABLED": "This Crafty account is disabled.",
    "PASSWORD_AUTH_DISABLED": "Password login is disabled for this account in Crafty.",
}


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    totp: str | None = Field(default=None, max_length=32)


def _login_error(exc: CraftyLoginError) -> HTTPException:
    if exc.status_code == 429:
        wait = f" Try again in {exc.cooldown}." if exc.cooldown else ""
        return HTTPException(status_code=429, detail=f"Too many login attempts.{wait}")
    if exc.status_code == 403 and exc.error in BLOCKED_ACCOUNT_MESSAGES:
        return HTTPException(status_code=403, detail=BLOCKED_ACCOUNT_MESSAGES[exc.error])
    if exc.status_code in (400, 401, 403):
        return HTTPException(status_code=401, detail="Incorrect username, password or 2FA code.")
    return HTTPException(status_code=502, detail=f"Crafty login failed: {exc.error}")


@router.post("/auth/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    crafty: CraftyClient = Depends(require_crafty),
) -> dict:
    try:
        token = await crafty.login(body.username, body.password, body.totp or None)
        user = await crafty.get_me(token)
    except CraftyLoginError as exc:
        raise _login_error(exc) from exc
    except CraftyError as exc:
        if exc.state in ("unauthorized", "forbidden"):
            # Crafty issued a token but refuses to honour it, e.g. 2FA is enforced.
            raise HTTPException(
                status_code=403,
                detail="Crafty refused access to this account (it may require 2FA to be set up).",
            ) from exc
        detail = "Crafty is unreachable." if exc.state == "unreachable" else (
            f"Crafty login failed: {exc.message}")
        raise HTTPException(status_code=502, detail=detail) from exc

    settings = request.app.state.settings
    sessions(request).delete(request.cookies.get(SESSION_COOKIE))
    session = sessions(request).create(
        crafty_token=token, user_id=user.user_id, username=user.username,
        superuser=user.superuser,
    )
    secure = settings.session_cookie_secure
    response.set_cookie(
        SESSION_COOKIE,
        session.id,
        max_age=int(settings.session_max_hours * 3600),
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https" if secure is None else secure,
        path="/",
    )
    return {"user_id": user.user_id, "username": user.username, "superuser": user.superuser}


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response) -> None:
    # A JSON content type cannot be sent cross-site without a CORS preflight.
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(status_code=415, detail="Send logout as a JSON request.")
    # Only the companion session ends: revoking Crafty tokens would also log the
    # user out of the Crafty panel itself.
    sessions(request).delete(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")


@router.get("/me")
async def me(
    request: Request,
    session: Session = Depends(require_session),
    crafty: CraftyClient = Depends(require_crafty),
) -> dict:
    try:
        permissions = await crafty.get_my_permissions(session.crafty_token, session.superuser)
    except CraftyError as exc:
        raise crafty_failure(request, session, exc) from exc
    return {
        "user_id": session.user_id,
        "username": session.username,
        "superuser": session.superuser,
        "permissions": permissions,
    }


@router.get("/servers")
async def servers(
    request: Request,
    session: Session = Depends(require_session),
    crafty: CraftyClient = Depends(require_crafty),
) -> list[dict]:
    try:
        data = await crafty.list_servers(session.crafty_token)
    except CraftyError as exc:
        raise crafty_failure(request, session, exc) from exc
    return [
        {"server_id": s["server_id"], "server_name": s["server_name"], "type": s.get("type")}
        for s in data
    ]
