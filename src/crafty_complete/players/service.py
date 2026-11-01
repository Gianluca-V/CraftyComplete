import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import quote

import httpx

from crafty_complete.crafty.client import CraftyClient, CraftyError
from crafty_complete.mods.locks import ServerLocks
from crafty_complete.servers.control import COMMANDS_FORBIDDEN, server_status

FILES_FORBIDDEN = "You do not have file access to this server in Crafty."


class PlayerAction(str, Enum):
    WHITELIST_ADD = "whitelist_add"
    WHITELIST_REMOVE = "whitelist_remove"
    OP = "op"
    DEOP = "deop"
    BAN = "ban"
    PARDON = "pardon"
    KICK = "kick"


COMMANDS = {
    PlayerAction.WHITELIST_ADD: "whitelist add",
    PlayerAction.WHITELIST_REMOVE: "whitelist remove",
    PlayerAction.OP: "op",
    PlayerAction.DEOP: "deop",
    PlayerAction.BAN: "ban",
    PlayerAction.PARDON: "pardon",
    PlayerAction.KICK: "kick",
}
# What a stopped server's player files need: (file, adds an entry?).
FILE_EDITS = {
    PlayerAction.WHITELIST_ADD: ("whitelist.json", True),
    PlayerAction.WHITELIST_REMOVE: ("whitelist.json", False),
    PlayerAction.OP: ("ops.json", True),
    PlayerAction.DEOP: ("ops.json", False),
    PlayerAction.BAN: ("banned-players.json", True),
    PlayerAction.PARDON: ("banned-players.json", False),
}
# Matches what vanilla writes for a ban made from the console.
DEFAULT_BAN_REASON = "Banned by an operator."
# Player names and reasons are typed into the server console: no line breaks or controls.
PLAYER_NAME = re.compile(r"[A-Za-z0-9_]{1,16}")
UNSAFE_REASON = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
REASON_ACTIONS = {PlayerAction.BAN, PlayerAction.KICK}


class PlayerError(Exception):
    """A refusal or failure the user should read, with its HTTP status."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def offline_uuid(name: str) -> str:
    """The UUID an offline-mode server gives a name (Java's nameUUIDFromBytes)."""
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode()).digest())
    digest[6] = digest[6] & 0x0F | 0x30
    digest[8] = digest[8] & 0x3F | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def _online_mode(properties: bytes | None) -> bool:
    for line in (properties or b"").decode(errors="replace").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "online-mode":
            return value.strip().lower() != "false"
    return True


class PlayerService:
    """Whitelist, operators, bans and kicks, shared by the web routes and the MCP.

    A running server gets console commands; a stopped one gets its player files edited.
    """

    def __init__(self, crafty: CraftyClient, mojang: httpx.AsyncClient, locks: ServerLocks):
        self._crafty = crafty
        self._mojang = mojang
        self._locks = locks

    async def _read_list(self, token: str, server_id: str, file_name: str) -> list[dict]:
        try:
            raw = await self._crafty.read_file(token, server_id, file_name)
        except CraftyError as exc:
            if exc.state == "forbidden":
                raise PlayerError(403, FILES_FORBIDDEN) from exc
            raise
        if raw is None or not raw.strip():
            return []
        try:
            entries = json.loads(raw)
        except ValueError:
            entries = None
        if not isinstance(entries, list):
            raise PlayerError(502, f"{file_name} on the server is not valid JSON; "
                                   "fix or delete it in Crafty first.")
        return [e for e in entries if isinstance(e, dict)]

    async def players(self, token: str, server_id: str) -> dict[str, Any]:
        status = server_status(await self._crafty.server_stats(token, server_id))
        whitelist = await self._read_list(token, server_id, "whitelist.json")
        ops = await self._read_list(token, server_id, "ops.json")
        banned = await self._read_list(token, server_id, "banned-players.json")
        return {
            "running": status["running"],
            "online": status["players"],
            "whitelist": [{"name": e.get("name"), "uuid": e.get("uuid")} for e in whitelist],
            "ops": [{"name": e.get("name"), "uuid": e.get("uuid"), "level": e.get("level")}
                    for e in ops],
            "banned": [{"name": e.get("name"), "uuid": e.get("uuid"), "reason": e.get("reason"),
                        "created": e.get("created"), "expires": e.get("expires")}
                       for e in banned],
        }

    async def apply(self, token: str, server_id: str, action: PlayerAction, name: str,
                    reason: str | None = None) -> dict[str, Any]:
        if not PLAYER_NAME.fullmatch(name):
            raise PlayerError(422, f"{name!r} is not a valid Minecraft player name.")
        reason = (reason or "").strip() or None
        if reason and action not in REASON_ACTIONS:
            raise PlayerError(422, "A reason only applies to ban and kick.")
        if reason and (len(reason) > 200 or UNSAFE_REASON.search(reason)):
            raise PlayerError(422, "The reason must be one line of at most 200 characters.")
        running = bool((await self._crafty.server_stats(token, server_id)).get("running"))
        if running:
            command = f"{COMMANDS[action]} {name}" + (f" {reason}" if reason else "")
            try:
                await self._crafty.send_command(token, server_id, command)
            except CraftyError as exc:
                if exc.state == "forbidden":
                    raise PlayerError(403, COMMANDS_FORBIDDEN) from exc
                raise
            return {"action": action.value, "name": name, "applied": "command", "changed": True}
        if action is PlayerAction.KICK:
            raise PlayerError(409, "Players can only be kicked while the server is running.")
        file_name, adds = FILE_EDITS[action]
        async with self._locks(server_id):
            entries = await self._read_list(token, server_id, file_name)
            if adds:
                name, changed = await self._add(token, server_id, action, entries, name, reason)
            else:
                kept = [e for e in entries if str(e.get("name", "")).casefold() != name.casefold()]
                changed = len(kept) != len(entries)
                entries = kept
            if changed:
                await self._write(token, server_id, file_name, entries)
        return {"action": action.value, "name": name, "applied": "file", "changed": changed}

    async def _add(self, token: str, server_id: str, action: PlayerAction, entries: list[dict],
                   name: str, reason: str | None) -> tuple[str, bool]:
        existing = next((e for e in entries
                         if str(e.get("name", "")).casefold() == name.casefold()), None)
        if existing:
            return existing.get("name") or name, False
        properties = await self._crafty.read_file(token, server_id, "server.properties")
        if _online_mode(properties):
            player_uuid, name = await self._mojang_profile(name)
        else:
            player_uuid = offline_uuid(name)
        if any(e.get("uuid") == player_uuid for e in entries):
            return name, False
        entry: dict[str, Any] = {"uuid": player_uuid, "name": name}
        if action is PlayerAction.OP:
            entry.update(level=4, bypassesPlayerLimit=False)
        elif action is PlayerAction.BAN:
            created = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000")
            entry.update(created=created, source="craftyComplete", expires="forever",
                         reason=reason or DEFAULT_BAN_REASON)
        entries.append(entry)
        return name, True

    async def _mojang_profile(self, name: str) -> tuple[str, str]:
        """(dashed UUID, correctly cased name) of a Minecraft account."""
        unavailable = PlayerError(502, f"Mojang could not be asked for {name}'s UUID; "
                                       "try again later.")
        try:
            response = await self._mojang.get(f"/users/profiles/minecraft/{quote(name)}")
        except httpx.HTTPError as exc:
            raise unavailable from exc
        if response.status_code in (204, 404):
            raise PlayerError(404, f"There is no Minecraft account named {name}.")
        if response.status_code != 200:
            raise unavailable
        try:
            profile = response.json()
            return str(uuid.UUID(profile["id"])), profile["name"]
        except (ValueError, KeyError, TypeError) as exc:
            raise unavailable from exc

    async def _write(self, token: str, server_id: str, file_name: str,
                     entries: list[dict]) -> None:
        data = json.dumps(entries, indent=2).encode()
        try:
            await self._crafty.upload_file(token, server_id, "", file_name, data)
        except CraftyError as exc:
            if exc.state == "forbidden":
                raise PlayerError(403, FILES_FORBIDDEN) from exc
            raise
