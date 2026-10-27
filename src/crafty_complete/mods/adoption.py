import asyncio
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from crafty_complete.catalog.models import SERVER_LOADERS
from crafty_complete.catalog.modrinth import RELEASE_VERSION, ModrinthProvider
from crafty_complete.crafty.client import CraftyClient
from crafty_complete.mods.models import AdoptedServer, InstalledMod
from crafty_complete.mods.store import AdoptionStore

MODS_DIR = "mods"
# Parallel downloads from Crafty while hashing jars.
HASH_CONCURRENCY = 4


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

    def __init__(self, crafty: CraftyClient, modrinth: ModrinthProvider, store: AdoptionStore):
        self._crafty = crafty
        self._modrinth = modrinth
        self._store = store

    async def ensure_file_access(self, token: str, server_id: str) -> None:
        """Raise CraftyError unless Crafty lets this user browse the server's files."""
        await self._crafty.list_dir(token, server_id, "")

    async def _hash_jars(self, token: str, server_id: str) -> dict[str, str]:
        root = await self._crafty.list_dir(token, server_id, "")
        if not any(entry.is_dir and entry.name == MODS_DIR for entry in root):
            raise NoModsFolder()
        entries = await self._crafty.list_dir(token, server_id, MODS_DIR)
        jars = [e for e in entries if not e.is_dir and e.name.lower().endswith(".jar")]

        limit = asyncio.Semaphore(HASH_CONCURRENCY)

        async def hash_one(path: str) -> str:
            async with limit:
                return await self._crafty.file_sha1(token, server_id, path)

        hashes = await asyncio.gather(*(hash_one(jar.path) for jar in jars))
        return {jar.name: sha1 for jar, sha1 in zip(jars, hashes)}

    async def scan(self, token: str, server_id: str, loader: str | None = None,
                   game_version: str | None = None) -> AdoptedServer:
        """(Re)scan the server. Nothing is saved unless every step succeeds."""
        previous = self._store.get(server_id)
        hashes = await self._hash_jars(token, server_id)

        versions = await self._modrinth.versions_by_sha1(sorted(set(hashes.values())))
        identified = list(versions.values())
        projects = await self._modrinth.projects(
            sorted({v["project_id"] for v in identified})
        )

        loader = loader or previous.loader or infer_loader(identified)
        game_version = game_version or previous.game_version or infer_game_version(identified)
        latest: dict[str, dict[str, Any]] = {}
        if versions and loader and game_version:
            latest = await self._modrinth.latest_versions(sorted(versions), loader, game_version)

        pinned = {mod.pin_key for mod in previous.mods if mod.pinned}
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

        server = AdoptedServer(
            server_id=server_id,
            loader=loader,
            game_version=game_version,
            scanned_at=datetime.now(UTC).isoformat(timespec="seconds"),
            mods=mods,
        )
        self._store.save(server)
        return server
