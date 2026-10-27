import asyncio
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from crafty_complete.catalog.models import SERVER_LOADERS, compatible_loaders
from crafty_complete.catalog.modrinth import RELEASE_VERSION, ModrinthProvider
from crafty_complete.crafty.client import CraftyClient
from crafty_complete.mods.locks import ServerLocks
from crafty_complete.mods.models import AdoptedServer, InstalledMod
from crafty_complete.mods.store import AdoptionStore

MODS_DIR = "mods"
# Parallel downloads from Crafty while hashing jars.
HASH_CONCURRENCY = 4


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class NoModsFolder(Exception):
    """The server directory has no `mods/` folder."""


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def infer_loader(versions: list[dict[str, Any]]) -> str | None:
    """Most common server loader among the identified versions (ties: SERVER_LOADERS order)."""
    counts = Counter(
        loader for version in versions for loader in version.get("loaders", [])
        if loader in SERVER_LOADERS
    )
    if not counts:
        return None
    return max(SERVER_LOADERS, key=lambda loader: (counts[loader], -SERVER_LOADERS.index(loader)))


def infer_game_version(versions: list[dict[str, Any]]) -> str | None:
    """The release Minecraft version supported by the most mods (ties: newest)."""
    counts = Counter(
        game_version for version in versions for game_version in version.get("game_versions", [])
        if RELEASE_VERSION.match(game_version)
    )
    if not counts:
        return None
    return max(counts, key=lambda gv: (counts[gv], _version_key(gv)))


class AdoptionService:
    """Find the mods of a Crafty server, identify them on Modrinth and remember them."""

    def __init__(self, crafty: CraftyClient, modrinth: ModrinthProvider, store: AdoptionStore,
                 locks: ServerLocks):
        self._crafty = crafty
        self._modrinth = modrinth
        self._store = store
        self._locks = locks

    async def ensure_file_access(self, token: str, server_id: str) -> None:
        """Raise CraftyError unless this user has Crafty's FILES permission on the server."""
        await self._crafty.check_files_permission(token, server_id)

    async def _hash_jars(self, token: str, server_id: str) -> dict[str, str]:
        """SHA-1 of every jar in mods/, skipping jars removed while the scan runs."""
        root = await self._crafty.list_dir(token, server_id, "")
        if not any(entry.is_dir and entry.name == MODS_DIR for entry in root):
            raise NoModsFolder()
        entries = await self._crafty.list_dir(token, server_id, MODS_DIR)
        jars = [e for e in entries if not e.is_dir and e.name.lower().endswith(".jar")]

        limit = asyncio.Semaphore(HASH_CONCURRENCY)

        async def hash_one(path: str) -> str | None:
            async with limit:
                return await self._crafty.file_sha1(token, server_id, path)

        # A TaskGroup cancels the remaining downloads as soon as one fails.
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(hash_one(jar.path)) for jar in jars]
        except ExceptionGroup as group_error:
            raise group_error.exceptions[0] from None
        return {
            jar.name: task.result()
            for jar, task in zip(jars, tasks)
            if task.result() is not None
        }

    async def scan(self, token: str, server_id: str, loader: str | None = None,
                   game_version: str | None = None) -> AdoptedServer:
        """(Re)scan the server. Nothing is saved unless every step succeeds."""
        async with self._locks(server_id):
            return await self._scan(token, server_id, loader, game_version)

    async def _scan(self, token: str, server_id: str, loader: str | None,
                    game_version: str | None) -> AdoptedServer:
        await self.ensure_file_access(token, server_id)
        hashes = await self._hash_jars(token, server_id)
        return await self.record(server_id, hashes, loader, game_version)

    async def record(self, server_id: str, hashes: dict[str, str], loader: str | None = None,
                     game_version: str | None = None,
                     modpack: dict[str, str] | None = None) -> AdoptedServer:
        """Identify jars (file name -> SHA-1) on Modrinth and save them as the server's mods.

        Loader and Minecraft version: the given ones, else the stored ones, else inferred.
        Pins and the modpack link are kept unless a new `modpack` is given. Callers hold
        the server lock.
        """
        previous = self._store.get(server_id)
        versions = await self._modrinth.versions_by_sha1(sorted(set(hashes.values())))
        identified = list(versions.values())
        projects = await self._modrinth.projects(
            sorted({v["project_id"] for v in identified})
        )

        loader = loader or previous.loader or infer_loader(identified)
        game_version = game_version or previous.game_version or infer_game_version(identified)
        latest: dict[str, dict[str, Any]] = {}
        checked = bool(versions and loader and game_version)
        if checked:
            latest = await self._modrinth.latest_versions(
                sorted(versions), compatible_loaders(loader), game_version
            )

        # Re-read pins now: the user may have pinned something while Crafty and Modrinth
        # were being queried.
        pinned = {mod.pin_key for mod in self._store.get(server_id).mods if mod.pinned}
        mods = []
        for file_name, sha1 in hashes.items():
            version = versions.get(sha1)
            if version is None:
                mod = InstalledMod(file_name=file_name, sha1=sha1, state="unknown")
            else:
                project = projects.get(version["project_id"], {})
                newest = latest.get(sha1, {})
                mod = InstalledMod(
                    file_name=file_name,
                    sha1=sha1,
                    state="identified",
                    project_id=version["project_id"],
                    slug=project.get("slug"),
                    title=project.get("title"),
                    icon_url=project.get("icon_url"),
                    version_id=version["id"],
                    version_number=version.get("version_number"),
                    server_side=project.get("server_side"),
                    latest_version_id=newest.get("id"),
                    latest_version_number=newest.get("version_number"),
                )
            mod.pinned = mod.pin_key in pinned
            mods.append(mod)

        link = modpack or {
            "project_id": previous.modpack_project_id, "title": previous.modpack_title,
            "version_id": previous.modpack_version_id,
            "version_number": previous.modpack_version_number,
        }
        now = utc_now()
        server = AdoptedServer(
            server_id=server_id,
            loader=loader,
            game_version=game_version,
            scanned_at=now,
            updates_checked_at=now if checked else previous.updates_checked_at,
            modpack_project_id=link["project_id"],
            modpack_title=link["title"],
            modpack_version_id=link["version_id"],
            modpack_version_number=link["version_number"],
            mods=mods,
        )
        self._store.save(server)
        return server

    async def check_updates(self, server_id: str) -> AdoptedServer:
        """Refresh the newest compatible version of every identified mod from stored hashes.

        Downloads no jars and needs no Crafty access, so it can run in the background.
        Never installs anything.
        """
        async with self._locks(server_id):
            server = self._store.get(server_id)
            identified = [m for m in server.mods if m.state == "identified"]
            if not (server.adopted and identified and server.loader and server.game_version):
                return server
            latest = await self._modrinth.latest_versions(
                sorted({m.sha1 for m in identified}),
                compatible_loaders(server.loader), server.game_version,
            )
            self._store.set_latest_versions(
                server_id,
                {m.file_name: ((latest[m.sha1]["id"], latest[m.sha1].get("version_number"))
                               if m.sha1 in latest else None)
                 for m in identified},
                utc_now(),
            )
            return self._store.get(server_id)
