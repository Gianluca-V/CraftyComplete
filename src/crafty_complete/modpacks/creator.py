import asyncio
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from crafty_complete.catalog.models import CatalogNotFound
from crafty_complete.catalog.modrinth import MODRINTH_CDN, ModrinthProvider
from crafty_complete.config import Settings
from crafty_complete.crafty.client import CraftyClient
from crafty_complete.mods.adoption import AdoptionService
from crafty_complete.mods.locks import ServerLocks
from crafty_complete.mods.store import AdoptionStore
from crafty_complete.modpacks.jobs import Job, JobFailure
from crafty_complete.servers.provision import NewServer, catalog_versions, create_and_install
from crafty_complete.modpacks.transfer import ServerWriter, parent_of
from crafty_complete.modpacks.packs import (
    PackReader,
    describe_failure,
    file_sha1,
    is_mod_jar,
    sendable,
)

# Crafty's catalog server types for each loader it can install.
CRAFTY_TYPES = {"fabric": "fabric", "forge": "forge-installer", "neoforge": "neoforge-installer"}
LOADER_NAMES = {"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge"}
# The dependency key each loader uses in modrinth.index.json, for messages.
PACK_LOADER_KEYS = {"fabric": "fabric-loader", "forge": "forge", "neoforge": "neoforge"}


class CreationRefused(Exception):
    """The request cannot start; carries an HTTP status for the API."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class CreateRequest:
    project_id: str
    version_id: str | None
    name: str
    port: int
    mem_min: float
    mem_max: float


@dataclass(frozen=True)
class Prepared:
    project: dict[str, Any]
    version: dict[str, Any]
    loader: str
    minecraft: str
    crafty_loader_version: str | None
    pack_file: dict[str, Any]


class ModpackServerCreator:
    """Create a Crafty server from a Modrinth modpack (.mrpack).

    Phase 1 downloads and verifies everything without touching Crafty. Phase 2 asks Crafty
    to create the server and install its loader. Phase 3 uploads the pack's files and
    records the server as adopted, linked to the modpack version.
    """

    def __init__(self, crafty: CraftyClient, modrinth: ModrinthProvider,
                 adoption: AdoptionService, store: AdoptionStore, locks: ServerLocks,
                 settings: Settings):
        self._crafty = crafty
        self._modrinth = modrinth
        self._adoption = adoption
        self._store = store
        self._locks = locks
        self._settings = settings
        self._packs = PackReader(modrinth)
        self._writer = ServerWriter(crafty, settings)

    # --- before the job ------------------------------------------------------------

    async def prepare(self, token: str, superuser: bool, request: CreateRequest) -> Prepared:
        """Every check that can fail fast, before anything is downloaded or created."""
        permissions = await self._crafty.get_my_permissions(token, superuser)
        if not permissions["server_creation"]:
            raise CreationRefused(403, "Your Crafty account cannot create servers.")
        try:
            project = await self._modrinth.project(request.project_id)
        except CatalogNotFound as exc:
            raise CreationRefused(
                404, f"Modrinth has no project '{request.project_id}'.") from exc
        if project.get("project_type") != "modpack":
            kind = project.get("project_type", "something else")
            raise CreationRefused(422, f"{project['title']} is a {kind}, not a modpack.")

        if request.version_id:
            try:
                version = await self._modrinth.version(request.version_id)
            except CatalogNotFound as exc:
                raise CreationRefused(404, "That modpack version does not exist.") from exc
            if version.get("project_id") != project["id"]:
                raise CreationRefused(422, "That version belongs to another project.")
        else:
            versions = [v for v in await self._modrinth.project_versions(project["id"])
                        if set(v.get("loaders") or []) & set(CRAFTY_TYPES)]
            if not versions:
                raise CreationRefused(422, f"{project['title']} has no Fabric, Forge or "
                                           "NeoForge version.")
            version = next((v for v in versions if v.get("version_type") == "release"),
                           versions[0])

        loaders = [l for l in version.get("loaders") or [] if l in CRAFTY_TYPES]
        if not loaders:
            if "quilt" in (version.get("loaders") or []):
                raise CreationRefused(422, "Quilt modpacks are not supported yet.")
            raise CreationRefused(422, "The modpack does not use Fabric, Forge or NeoForge.")
        loader = loaders[0]

        catalog = await self._crafty.jar_catalog(token)
        crafty_versions = catalog_versions(catalog, CRAFTY_TYPES[loader])
        game_versions = version.get("game_versions") or [None]
        # A version may list several Minecraft versions; use one Crafty can install.
        minecraft = next((gv for gv in game_versions if gv in crafty_versions),
                         game_versions[0])
        if minecraft not in crafty_versions:
            raise CreationRefused(422, f"Crafty cannot install {LOADER_NAMES[loader]} for "
                                       f"Minecraft {minecraft}.")

        files = version.get("files") or []
        pack_file = next((f for f in files if f.get("primary")), files[0] if files else None)
        if not pack_file or not str(pack_file.get("url", "")).startswith(MODRINTH_CDN):
            raise CreationRefused(422, "The modpack file is not hosted on Modrinth.")
        return Prepared(project, version, loader, minecraft,
                        crafty_versions[minecraft].get("loader_version"), pack_file)

    def describe_error(self, job: Job, exc: Exception) -> str:
        message = describe_failure(exc)
        if job.server_id and not getattr(exc, "names_server", False):
            message += (f' The server "{job.server_name}" was created in Crafty but is not '
                        "complete; delete it there before trying again.")
        return message

    # --- the job -------------------------------------------------------------------

    async def run(self, job: Job, token: str, prepared: Prepared, request: CreateRequest) -> None:
        base = Path(self._settings.modpack_work_dir or tempfile.gettempdir())
        base.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="modpack-", dir=base))
        try:
            files, pack_hashes = await self._download(job, prepared, workdir)
            await self._create_in_crafty(job, token, prepared, request)
            await self._upload(job, token, files)
            await self._record(job, prepared, files, pack_hashes)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def _download(self, job: Job, prepared: Prepared,
                        workdir: Path) -> tuple[dict[str, Path], dict[str, str | None]]:
        """Phase 1: the .mrpack and every server file it keeps, verified.

        Returns server path -> local file, and server path -> SHA-1 of the pack's files.
        """
        pack_path, index = await self._packs.fetch(job, prepared.pack_file, workdir)
        if (index.loader, index.minecraft) != (prepared.loader, prepared.minecraft):
            raise JobFailure(
                f"The modpack file is for {LOADER_NAMES[index.loader]} {index.minecraft}, "
                f"but Modrinth lists it for {LOADER_NAMES[prepared.loader]} "
                f"{prepared.minecraft}.")
        if prepared.crafty_loader_version and index.loader_version != prepared.crafty_loader_version:
            job.warnings.append(
                f"The pack asks for {PACK_LOADER_KEYS[index.loader]} {index.loader_version}; "
                f"Crafty installs {prepared.crafty_loader_version} for Minecraft "
                f"{index.minecraft}.")

        # Decide what to keep before downloading, so client-only mods are never fetched.
        kept = await self._packs.drop_client_only(
            job, index, await self._packs.file_hashes(index, pack_path))
        files = await self._packs.fetch_files(job, index, pack_path, set(kept), workdir)
        pack_path.unlink(missing_ok=True)
        return files, kept

    async def _create_in_crafty(self, job: Job, token: str, prepared: Prepared,
                                request: CreateRequest) -> None:
        """Phase 2: create the server and wait until Crafty has installed the loader."""
        await create_and_install(self._crafty, self._writer, self._settings, job, token,
                                 NewServer(request.name, request.port, request.mem_min,
                                           request.mem_max, CRAFTY_TYPES[prepared.loader],
                                           prepared.minecraft, LOADER_NAMES[prepared.loader]))

    async def _upload(self, job: Job, token: str, files: dict[str, Path]) -> None:
        """Phase 3a: send every file to the new server through Crafty."""
        to_send = {}
        for relative, source in files.items():
            if sendable(relative):
                to_send[relative] = source
            else:
                job.warnings.append(f"{relative} could not be sent to Crafty (non-ASCII name); "
                                    "upload it yourself.")
        await self._writer.create_folders(job, token, job.server_id,
                                          {parent_of(r) for r in to_send})
        await self._writer.upload(job, token, job.server_id, to_send)

    async def _record(self, job: Job, prepared: Prepared, files: dict[str, Path],
                      pack_hashes: dict[str, str | None]) -> None:
        """Phase 3b: remember the server as adopted, linked to its modpack version."""
        job.begin("Identifying mods")
        hashes = {
            PurePosixPath(relative).name: await asyncio.to_thread(file_sha1, source)
            for relative, source in files.items()
            if is_mod_jar(relative) and sendable(relative)
        }
        async with self._locks(job.server_id):
            await self._adoption.record(
                job.server_id, hashes, prepared.loader, prepared.minecraft,
                modpack={"project_id": prepared.project["id"],
                         "title": prepared.project["title"],
                         "version_id": prepared.version["id"],
                         "version_number": prepared.version.get("version_number")})
            self._store.set_modpack_files(
                job.server_id, {p: h for p, h in pack_hashes.items() if sendable(p)})
