from dataclasses import dataclass
from typing import Any, Literal

import httpx

CraftyState = Literal["not_configured", "connected", "unauthorized", "unreachable", "error"]

# Index of each flag in Crafty's crafty-permissions mask (EnumPermissionsCrafty).
CRAFTY_PERMISSIONS = ("server_creation", "user_config", "roles_config")


class CraftyError(Exception):
    def __init__(self, state: CraftyState, message: str):
        super().__init__(message)
        self.state = state
        self.message = message


class CraftyLoginError(Exception):
    def __init__(self, status_code: int, error: str, cooldown: str | None = None):
        super().__init__(error)
        self.status_code = status_code
        self.error = error
        self.cooldown = cooldown


@dataclass(frozen=True)
class CraftyStatus:
    state: CraftyState
    servers_visible: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class CraftyUser:
    user_id: str
    username: str
    superuser: bool


class CookielessTransport(httpx.AsyncBaseTransport):
    """Strip cookies in both directions.

    Crafty's login also sets a `token` cookie holding the user's JWT. The Crafty HTTP
    client is shared by every user, so a stored cookie would ride along on other
    users' requests. Identity must come only from the per-request Authorization header.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if "cookie" in request.headers:
            del request.headers["cookie"]
        response = await self._inner.handle_async_request(request)
        if "set-cookie" in response.headers:
            del response.headers["set-cookie"]
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class CraftyClient:
    """Thin wrapper over the Crafty Controller 4 REST API v2.

    Every call carries the token of whoever is acting, so Crafty itself enforces
    that user's permissions.
    """

    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    async def _get(self, path: str, token: str) -> Any:
        try:
            response = await self._http.get(
                path, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            raise CraftyError("unreachable", str(exc)) from exc

        body = _json(response)
        if response.status_code in (401, 403):
            raise CraftyError("unauthorized", body.get("error", "NOT_AUTHORIZED"))
        if response.status_code >= 400 or body.get("status") != "ok":
            raise CraftyError("error", body.get("error", f"HTTP {response.status_code}"))
        return body.get("data")

    async def login(self, username: str, password: str, totp: str | None = None) -> str:
        """Exchange Crafty credentials for that user's API token."""
        payload = {"username": username, "password": password}
        if totp:
            payload["totp"] = totp
        try:
            response = await self._http.post("/api/v2/auth/login", json=payload)
        except httpx.HTTPError as exc:
            raise CraftyError("unreachable", str(exc)) from exc

        body = _json(response)
        token = (body.get("data") or {}).get("token") if response.status_code == 200 else None
        if not token:
            raise CraftyLoginError(
                response.status_code,
                body.get("error", f"HTTP {response.status_code}"),
                body.get("cooldown_time"),
            )
        return token

    async def get_me(self, token: str) -> CraftyUser:
        data = await self._get("/api/v2/users/@me", token)
        return CraftyUser(
            user_id=str(data["user_id"]),
            username=data["username"],
            superuser=bool(data.get("superuser")),
        )

    async def get_my_permissions(self, token: str, superuser: bool) -> dict[str, bool]:
        data = await self._get("/api/v2/users/@me/permissions", token)
        mask = str(data.get("permissions", ""))
        return {
            name: superuser or (index < len(mask) and mask[index] == "1")
            for index, name in enumerate(CRAFTY_PERMISSIONS)
        }

    async def list_servers(self, token: str) -> list[dict[str, Any]]:
        """Servers visible to the user that owns `token`."""
        return await self._get("/api/v2/servers", token)

    async def status(self, token: str) -> CraftyStatus:
        try:
            servers = await self.list_servers(token)
        except CraftyError as exc:
            return CraftyStatus(state=exc.state, error=exc.message)
        return CraftyStatus(state="connected", servers_visible=len(servers))
