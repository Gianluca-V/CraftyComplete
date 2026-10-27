import hashlib
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import httpx

CraftyState = Literal[
    "not_configured", "connected", "unauthorized", "forbidden", "not_found", "unreachable",
    "error",
]

# A file name no real server contains; see CraftyClient.check_files_permission.
PERMISSION_PROBE_PATH = ".crafty-complete-permission-check"
# The `error` Crafty's download endpoint returns for a missing file.
FILE_NOT_FOUND = "File not found"

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
class DirEntry:
    name: str
    path: str  # relative to the server directory
    is_dir: bool


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

    @staticmethod
    def _raise_for(response: httpx.Response, body: dict[str, Any]) -> None:
        """Translate a Crafty error response.

        Crafty answers an invalid or revoked token with 403 ACCESS_DENIED, but a valid
        user lacking a server permission with 400 (or 403) NOT_AUTHORIZED.
        """
        error = body.get("error", f"HTTP {response.status_code}")
        status = response.status_code
        if status == 401 or (status == 403 and error == "ACCESS_DENIED"):
            raise CraftyError("unauthorized", error)
        if status == 403 or (status == 400 and error == "NOT_AUTHORIZED"):
            raise CraftyError("forbidden", error)
        if status == 404:
            raise CraftyError("not_found", error)
        raise CraftyError("error", error)

    async def _request(self, method: str, path: str, token: str, json: Any = None) -> Any:
        try:
            response = await self._http.request(
                method, path, json=json, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            raise CraftyError("unreachable", str(exc)) from exc

        body = _json(response)
        if response.status_code >= 400 or body.get("status") != "ok":
            self._raise_for(response, body)
        return body.get("data")

    async def _get(self, path: str, token: str) -> Any:
        return await self._request("GET", path, token)

    async def list_dir(self, token: str, server_id: str, path: str) -> list[DirEntry]:
        """List a directory of a server. Needs the FILES (or BACKUP) server permission."""
        data = await self._request(
            "POST", f"/api/v2/servers/{server_id}/files", token,
            json={"page": "files", "path": path},
        )
        return [
            DirEntry(name=name, path=entry.get("path", name), is_dir=bool(entry.get("dir")))
            for name, entry in (data or {}).items()
            if name != "root_path" and isinstance(entry, dict)
        ]

    @staticmethod
    def _download_url(server_id: str, path: str) -> str:
        # Crafty URL-decodes the path and then html-unescapes it, so a literal `&`
        # must be sent as `&amp;` (otherwise `a&amp;b.jar` would become `a&b.jar`).
        return f"/api/v2/servers/{server_id}/files/{quote(path.replace('&', '&amp;'), safe='')}/download"

    async def check_files_permission(self, token: str, server_id: str) -> None:
        """Raise CraftyError unless this user has the FILES permission on the server.

        Listing a directory also succeeds with only the BACKUP permission, so this asks
        for the download of a path that does not exist: Crafty checks FILES first and
        answers 404 when it is granted, 400 NOT_AUTHORIZED when it is not.
        """
        url = self._download_url(server_id, PERMISSION_PROBE_PATH)
        try:
            async with self._http.stream(
                "GET", url, headers={"Authorization": f"Bearer {token}"}
            ) as response:
                if response.status_code == 200:
                    return
                await response.aread()
                body = _json(response)
                if response.status_code == 404:
                    if body.get("error") == FILE_NOT_FOUND:
                        return
                    # Any other 404 (e.g. a proxy) proves nothing: fail closed.
                    raise CraftyError("forbidden", body.get("error", "HTTP 404"))
                self._raise_for(response, body)
        except httpx.HTTPError as exc:
            raise CraftyError("unreachable", str(exc)) from exc

    async def file_sha1(self, token: str, server_id: str, path: str) -> str | None:
        """Stream a server file through Crafty and return its SHA-1.

        Returns None when Crafty reports the file no longer exists (removed after listing).
        """
        url = self._download_url(server_id, path)
        digest = hashlib.sha1()
        try:
            async with self._http.stream(
                "GET", url, headers={"Authorization": f"Bearer {token}"}
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    body = _json(response)
                    if response.status_code == 404 and body.get("error") == FILE_NOT_FOUND:
                        return None
                    self._raise_for(response, body)
                async for chunk in response.aiter_bytes():
                    digest.update(chunk)
        except httpx.HTTPError as exc:
            raise CraftyError("unreachable", str(exc)) from exc
        return digest.hexdigest()

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
