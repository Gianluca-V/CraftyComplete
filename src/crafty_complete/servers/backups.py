from datetime import datetime
from typing import Any

from crafty_complete.crafty.client import CraftyClient, CraftyError

BACKUP_FORBIDDEN = "You do not have backup access to this server in Crafty."
BACKUP_COMMANDS_FORBIDDEN = ("You do not have permission to back up this server in Crafty "
                             "(it needs the Commands permission).")
NO_CONFIG = ("This server has no backup configuration in Crafty; "
             "create one in the Crafty panel.")
# How Crafty names zip backups, in its own timezone (backup_mgr.zip_vault).
ARCHIVE_DATE = "%Y-%m-%d_%H-%M-%S"


class BackupError(Exception):
    """A refusal or failure the user should read, with its HTTP status."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _created(file_name: str) -> str | None:
    try:
        return datetime.strptime(file_name.removesuffix(".zip"), ARCHIVE_DATE).isoformat()
    except ValueError:
        return None


def _archive(path: str) -> dict[str, Any]:
    # Crafty runs on Linux or Windows: split on either separator.
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return {"file": name, "created": _created(name)}


class BackupService:
    """Crafty backups of a server: list them and start one. Shared by the web and the MCP."""

    def __init__(self, crafty: CraftyClient):
        self._crafty = crafty

    async def _configs(self, token: str, server_id: str) -> list[dict[str, Any]]:
        try:
            raw = await self._crafty.backup_configs(token, server_id)
        except CraftyError as exc:
            if exc.state == "forbidden":
                raise BackupError(403, BACKUP_FORBIDDEN) from exc
            raise
        configs = [c for c in raw.values() if isinstance(c, dict) and c.get("backup_id")]
        configs.sort(key=lambda c: (not c.get("default"), str(c.get("backup_name", "")).casefold()))
        return configs

    async def list(self, token: str, server_id: str) -> dict[str, Any]:
        listed = []
        for config in await self._configs(token, server_id):
            backup_id = str(config["backup_id"])
            try:
                paths = await self._crafty.backup_archives(token, server_id, backup_id)
            except CraftyError as exc:
                if exc.state == "forbidden":
                    raise BackupError(403, BACKUP_FORBIDDEN) from exc
                raise
            archives = [_archive(path) for path in paths]
            # Newest first; names Crafty did not date go last.
            archives.sort(key=lambda a: (a["created"] is not None, a["created"] or ""),
                          reverse=True)
            listed.append({
                "backup_id": backup_id, "name": config.get("backup_name") or backup_id,
                "default": bool(config.get("default")),
                "type": config.get("backup_type") or "zip_vault",
                "stops_server": bool(config.get("shutdown")), "backups": archives,
            })
        return {"configs": listed}

    async def plan(self, token: str, server_id: str,
                   backup_id: str | None = None) -> dict[str, Any]:
        """The configuration a backup would use: the given one, else the default (or only) one."""
        configs = await self._configs(token, server_id)
        if not configs:
            raise BackupError(409, NO_CONFIG)
        if backup_id is None:
            chosen = next((c for c in configs if c.get("default")), None)
            if chosen is None and len(configs) == 1:
                chosen = configs[0]
            if chosen is None:
                raise BackupError(409, "This server has several backup configurations and no "
                                       "default; choose one.")
        else:
            chosen = next((c for c in configs if str(c["backup_id"]) == backup_id), None)
            if chosen is None:
                raise BackupError(404, "This server has no backup configuration with that id.")
        return {"backup_id": str(chosen["backup_id"]),
                "name": chosen.get("backup_name") or str(chosen["backup_id"]),
                "stops_server": bool(chosen.get("shutdown"))}

    async def start(self, token: str, server_id: str,
                    backup_id: str | None = None) -> dict[str, Any]:
        """Ask Crafty to make a backup now. Crafty does not say when it finishes."""
        chosen = await self.plan(token, server_id, backup_id)
        try:
            await self._crafty.server_action(token, server_id,
                                             f"backup_server/{chosen['backup_id']}")
        except CraftyError as exc:
            if exc.state == "forbidden":
                raise BackupError(403, BACKUP_COMMANDS_FORBIDDEN) from exc
            raise
        return {**chosen, "started": True}
