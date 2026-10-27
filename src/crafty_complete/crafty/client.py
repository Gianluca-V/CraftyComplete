from dataclasses import dataclass
from typing import Any, Literal

import httpx

CraftyState = Literal["not_configured", "connected", "unauthorized", "unreachable", "error"]


class CraftyError(Exception):
    def __init__(self, state: CraftyState, message: str):
        super().__init__(message)
        self.state = state
        self.message = message


@dataclass(frozen=True)
class CraftyStatus:
    state: CraftyState
    servers_visible: int | None = None
    error: str | None = None


class CraftyClient:
    """Thin wrapper over the Crafty Controller 4 REST API v2."""

    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    async def _get(self, path: str) -> Any:
        try:
            response = await self._http.get(path)
        except httpx.HTTPError as exc:
            raise CraftyError("unreachable", str(exc)) from exc

        try:
            body = response.json()
        except ValueError:
            body = {}

        if response.status_code in (401, 403):
            raise CraftyError("unauthorized", body.get("error", "NOT_AUTHORIZED"))
        if response.status_code >= 400 or body.get("status") != "ok":
            raise CraftyError("error", body.get("error", f"HTTP {response.status_code}"))
        return body.get("data")

    async def list_servers(self) -> list[dict[str, Any]]:
        """Servers visible to the configured API token."""
        return await self._get("/api/v2/servers")

    async def status(self) -> CraftyStatus:
        try:
            servers = await self.list_servers()
        except CraftyError as exc:
            return CraftyStatus(state=exc.state, error=exc.message)
        return CraftyStatus(state="connected", servers_visible=len(servers))
