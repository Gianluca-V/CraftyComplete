import ast
from enum import Enum
from typing import Any

from crafty_complete.crafty.client import CraftyClient


class PowerAction(str, Enum):
    START = "start"
    STOP = "stop"
    RESTART = "restart"
    KILL = "kill"


COMMANDS_FORBIDDEN = "You do not have permission to control this server in Crafty."

# Starting while a modpack update moves files would load a half-updated server.
NEEDS_IDLE_SERVER = {PowerAction.START, PowerAction.RESTART}


def _players(raw: Any) -> list[str]:
    """Crafty stores the player list as the text of a Python list ("['Steve']")."""
    if isinstance(raw, str):
        try:
            raw = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return []
    if not isinstance(raw, list):
        return []
    return [str(name) for name in raw]


def _state(stats: dict[str, Any]) -> str:
    if stats.get("running"):
        return "running"
    for flag, state in (("crashed", "crashed"), ("updating", "updating"),
                        ("waiting_start", "starting")):
        if stats.get(flag):
            return state
    return "stopped"


def _known(value: Any) -> Any:
    """Crafty writes "False" (or 0 for memory) where it has no value yet; that means unknown."""
    return None if value in ("False", False, "", 0) else value


def server_status(stats: dict[str, Any]) -> dict[str, Any]:
    """The parts of Crafty's server stats a person or an agent needs."""
    return {
        "state": _state(stats),
        "running": bool(stats.get("running")),
        "players_online": int(stats.get("online") or 0),
        "max_players": int(stats.get("max") or 0),
        "players": _players(stats.get("players")),
        "cpu_percent": stats.get("cpu"),
        "memory": _known(stats.get("mem")),
        "memory_percent": stats.get("mem_percent"),
        "started": _known(stats.get("started")),
        "version": _known(stats.get("version")),
        "world_size": _known(stats.get("world_size")),
        "port": stats.get("server_port"),
    }


class ServerControl:
    """Power and status of Crafty servers, shared by the web routes and the MCP."""

    def __init__(self, crafty: CraftyClient):
        self._crafty = crafty

    async def status(self, token: str, server_id: str) -> dict[str, Any]:
        return server_status(await self._crafty.server_stats(token, server_id))

    async def power(self, token: str, server_id: str, action: PowerAction) -> None:
        await self._crafty.server_action(token, server_id, f"{action.value}_server")
