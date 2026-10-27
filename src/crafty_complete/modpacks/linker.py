import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crafty_complete.catalog.models import CatalogNotFound
from crafty_complete.catalog.modrinth import MODRINTH_CDN, ModrinthProvider
from crafty_complete.config import Settings
from crafty_complete.mods.adoption import AdoptionService
from crafty_complete.mods.locks import ServerLocks
from crafty_complete.mods.models import AdoptedServer
from crafty_complete.mods.store import AdoptionStore
from crafty_complete.modpacks.creator import CreationRefused
from crafty_complete.modpacks.jobs import Job, JobFailure
from crafty_complete.modpacks.packs import PackReader, is_mod_jar, sendable


@dataclass(frozen=True)
class LinkTarget:
    project: dict[str, Any]
    version: dict[str, Any]
    pack_file: dict[str, Any]

    @property
    def link(self) -> dict[str, str | None]:
        return {"project_id": self.project["id"], "title": self.project["title"],
                "version_id": self.version["id"],
                "version_number": self.version.get("version_number")}


def version_summary(version: dict[str, Any]) -> dict[str, Any]:
    return {"id": version["id"], "version_number": version.get("version_number"),
            "version_type": version.get("version_type"),
            "date_published": version.get("date_published")}


def newest_update(versions: list[dict[str, Any]],
                  current_id: str | None) -> dict[str, Any] | None:
    """The version to offer as an update, from `versions` listed newest first.

    Only versions newer than the current one count; with the current one no longer listed,
    nothing is offered. A release wins; with no newer release,
    a server already on a pre-release is offered the newest newer pre-release.
    """
    ids = [v["id"] for v in versions]
    if current_id is not None and current_id not in ids:
        return None  # an unlisted or deleted version: "newer" cannot be told apart
    newer = versions[:ids.index(current_id)] if current_id in ids else versions
    release = next((v for v in newer if v.get("version_type") == "release"), None)
    current = versions[ids.index(current_id)] if current_id in ids else None
    if release or not newer or (current and current.get("version_type") == "release"):
        return release
    return newer[0]


class ModpackLinker:
    """Tie an adopted server to the Modrinth modpack version it runs.

    Linking downloads only the .mrpack: the index and the overrides give the path and
    SHA-1 of every file the pack puts on a server, which later updates compare against.
    """

    def __init__(self, modrinth: ModrinthProvider, adoption: AdoptionService,
                 store: AdoptionStore, locks: ServerLocks, settings: Settings):
        self._modrinth = modrinth
        self._adoption = adoption
        self._store = store
        self._locks = locks
        self._settings = settings
        self._packs = PackReader(modrinth)

    async def adopted(self, token: str, server_id: str) -> AdoptedServer:
        await self._adoption.ensure_file_access(token, server_id)
        server = self._store.get(server_id)
        if not server.adopted:
            raise CreationRefused(409, "Adopt this server first.")
        if not (server.loader and server.game_version):
            raise CreationRefused(409, "Set the server's loader and Minecraft version, "
                                       "then rescan.")
        return server

    def compatible(self, version: dict[str, Any], server: AdoptedServer) -> bool:
        return (server.loader in (version.get("loaders") or [])
                and server.game_version in (version.get("game_versions") or []))

    async def compatible_versions(self, project_id: str,
                                  server: AdoptedServer) -> list[dict[str, Any]]:
        """Versions of the pack for the server's loader and Minecraft version, newest first."""
        return [v for v in await self._modrinth.project_versions(project_id)
                if self.compatible(v, server)]

    async def prepare(self, token: str, server_id: str, project_id: str,
                      version_id: str) -> LinkTarget:
        server = await self.adopted(token, server_id)
        try:
            project = await self._modrinth.project(project_id)
            version = await self._modrinth.version(version_id)
        except CatalogNotFound as exc:
            raise CreationRefused(404, "No such Modrinth modpack version.") from exc
        if project.get("project_type") != "modpack":
            raise CreationRefused(422, f"{project['title']} is not a modpack.")
        if version.get("project_id") != project["id"]:
            raise CreationRefused(422, "That version belongs to another project.")
        if not self.compatible(version, server):
            raise CreationRefused(422, (
                f"This modpack version is for Minecraft "
                f"{', '.join(version.get('game_versions') or [])} and "
                f"{', '.join(version.get('loaders') or [])}; the server runs "
                f"{server.game_version} with {server.loader}."))
        files = version.get("files") or []
        pack_file = next((f for f in files if f.get("primary")), files[0] if files else None)
        if not pack_file or not str(pack_file.get("url", "")).startswith(MODRINTH_CDN):
            raise CreationRefused(422, "The modpack file is not hosted on Modrinth.")
        return LinkTarget(project, version, pack_file)

    async def pack_files(self, job: Job, target: LinkTarget,
                         server: AdoptedServer, workdir: Path) -> tuple[Path, Any, dict]:
        """Download a version's .mrpack and return (path, index, server path -> SHA-1)."""
        pack_path, index = await self._packs.fetch(job, target.pack_file, workdir)
        if (index.loader, index.minecraft) != (server.loader, server.game_version):
            raise JobFailure(f"The modpack file is for {index.loader} {index.minecraft}; "
                             f"the server runs {server.loader} {server.game_version}.")
        kept = await self._packs.drop_client_only(
            job, await self._packs.file_hashes(index, pack_path))
        return pack_path, index, {p: h for p, h in kept.items() if sendable(p)}

    def workdir(self) -> Path:
        base = Path(self._settings.modpack_work_dir or tempfile.gettempdir())
        base.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="modpack-", dir=base))

    async def link(self, job: Job, server_id: str, target: LinkTarget) -> None:
        workdir = self.workdir()
        try:
            server = self._store.get(server_id)
            _, _, files = await self.pack_files(job, target, server, workdir)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        job.begin("Saving the link")
        async with self._locks(server_id):
            self._store.set_modpack_link(server_id, target.link)
            self._store.set_modpack_files(server_id, files)
            server = self._store.get(server_id)

        on_server = {m.sha1 for m in server.mods}
        in_pack = {sha1 for sha1 in files.values() if sha1}
        job.result = {
            # Pack mods the server does not have (they arrive with the next update).
            "missing": sorted(p for p, h in files.items() if is_mod_jar(p) and h not in on_server),
            # Mods the user added on top of the pack; updates never touch them.
            "own_mods": sorted(m.file_name for m in server.mods if m.sha1 not in in_pack),
        }
