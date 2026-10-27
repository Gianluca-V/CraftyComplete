import asyncio
import hashlib
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from crafty_complete.catalog.models import CatalogError, CatalogNotFound
from crafty_complete.catalog.modrinth import MODRINTH_CDN, ModrinthProvider
from crafty_complete.config import Settings
from crafty_complete.crafty.client import CraftyClient, CraftyError
from crafty_complete.mods.adoption import MODS_DIR, AdoptionService
from crafty_complete.mods.locks import ServerLocks
from crafty_complete.modpacks.jobs import Job, JobFailure
from crafty_complete.modpacks.mrpack import MrpackError, read_index

# Crafty's catalog category and server types for each loader it can install.
CRAFTY_CATEGORY = "mc_java_servers"
CRAFTY_TYPES = {"fabric": "fabric", "forge": "forge-installer", "neoforge": "neoforge-installer"}
LOADER_NAMES = {"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge"}
# The dependency key each loader uses in modrinth.index.json, for messages.
PACK_LOADER_KEYS = {"fabric": "fabric-loader", "forge": "forge", "neoforge": "neoforge"}
MAX_PACK_BYTES = 4 * 1024**3
MAX_FILE_BYTES = 1024**3
TRANSFER_CONCURRENCY = 4


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


def _check_hashes(path: str, expected_sha1: str | None, expected_sha512: str | None,
                  actual: tuple[str, str]) -> None:
    if not (expected_sha1 or expected_sha512) or (
        (expected_sha1 and expected_sha1 != actual[0])
        or (expected_sha512 and expected_sha512 != actual[1])
    ):
        raise JobFailure(f"{path} does not match the hash in the modpack.")


RETRIES = 3


def _sendable(relative: str) -> bool:
    """Crafty takes the file name in an HTTP header, which must be printable ASCII."""
    return relative.isascii() and relative.isprintable()


def _is_mod_jar(relative: str) -> bool:
    path = PurePosixPath(relative)
    return path.parent == PurePosixPath(MODS_DIR) and path.suffix.lower() == ".jar"


def _sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ModpackServerCreator:
    """Create a Crafty server from a Modrinth modpack (.mrpack).

    Phase 1 downloads and verifies everything without touching Crafty. Phase 2 asks Crafty
    to create the server and install its loader. Phase 3 uploads the pack's files and
    records the server as adopted, linked to the modpack version.
    """

    def __init__(self, crafty: CraftyClient, modrinth: ModrinthProvider,
                 adoption: AdoptionService, locks: ServerLocks, settings: Settings):
        self._crafty = crafty
        self._modrinth = modrinth
        self._adoption = adoption
        self._locks = locks
        self._settings = settings

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
        crafty_versions = (catalog.get(CRAFTY_CATEGORY, {}).get("types", {})
                           .get(CRAFTY_TYPES[loader], {}).get("versions", {}))
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
        if isinstance(exc, (JobFailure, MrpackError)):
            message = str(exc)
        elif isinstance(exc, CraftyError):
            message = ("Your Crafty session expired; log in again." if exc.state == "unauthorized"
                       else f"Crafty request failed: {exc.message}.")
        elif isinstance(exc, CatalogError):
            message = f"{exc}."
        else:  # details (e.g. local paths) stay in the log, which JobRegistry writes
            message = "Unexpected error; see the craftyComplete log for details."
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
            files = await self._download(job, prepared, workdir)
            await self._create_in_crafty(job, token, prepared, request)
            await self._upload(job, token, files)
            await self._record(job, prepared, files)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def _download(self, job: Job, prepared: Prepared, workdir: Path) -> dict[str, Path]:
        """Phase 1: the .mrpack and every server file, verified. Returns server path -> file."""
        job.begin("Downloading the modpack")
        pack_path = workdir / "pack.mrpack"
        actual = await self._modrinth.download_to_file(
            prepared.pack_file["url"], pack_path, MAX_PACK_BYTES)
        hashes = prepared.pack_file.get("hashes") or {}
        _check_hashes(prepared.pack_file.get("filename", "The modpack"),
                      hashes.get("sha1"), hashes.get("sha512"), actual)

        index = await asyncio.to_thread(read_index, pack_path)
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

        files: dict[str, Path] = {}
        job.begin("Downloading mods", total=len(index.server_files))
        limit = asyncio.Semaphore(TRANSFER_CONCURRENCY)

        async def fetch(pack_file) -> None:
            target = workdir / "files" / pack_file.path
            async with limit:
                last_error: Exception | None = None
                for url in pack_file.downloads:
                    try:
                        actual = await self._modrinth.download_to_file(url, target, MAX_FILE_BYTES)
                        break
                    except CatalogError as exc:
                        last_error = exc
                else:
                    raise JobFailure(f"Could not download {pack_file.path}: {last_error}")
            _check_hashes(pack_file.path, pack_file.sha1, pack_file.sha512, actual)
            files[pack_file.path] = target
            job.done += 1

        await self._gather(fetch(f) for f in index.server_files)

        job.begin("Unpacking overrides", total=len(index.overrides))

        def extract() -> None:
            with zipfile.ZipFile(pack_path) as archive:
                for relative, entry in index.overrides.items():
                    target = workdir / "overrides" / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as source, target.open("wb") as handle:
                        shutil.copyfileobj(source, handle)
                    files[relative] = target  # overrides replace downloaded files
                    job.done += 1

        await asyncio.to_thread(extract)
        pack_path.unlink(missing_ok=True)
        await self._drop_client_only(job, files)
        return files

    async def _drop_client_only(self, job: Job, files: dict[str, Path]) -> None:
        """Remove jars whose Modrinth project cannot run on a server.

        The index's `env` is optional and many packs omit it, which the spec reads as
        "needed everywhere"; Modrinth's project data is the reliable source.
        """
        job.begin("Checking which mods run on a server")
        jars = {relative: await asyncio.to_thread(_sha1, source)
                for relative, source in files.items() if _is_mod_jar(relative)}
        versions = await self._modrinth.versions_by_sha1(sorted(set(jars.values())))
        projects = await self._modrinth.projects(
            sorted({v["project_id"] for v in versions.values()}))
        skipped = []
        for relative, sha1 in sorted(jars.items()):
            project = projects.get((versions.get(sha1) or {}).get("project_id"), {})
            if project.get("server_side") == "unsupported":
                del files[relative]
                skipped.append(f"{project.get('title') or relative} ({relative})")
        if skipped:
            noun = "mod" if len(skipped) == 1 else "mods"
            job.warnings.append(f"Skipped {len(skipped)} client-only {noun} (not needed on a "
                                f"server): {', '.join(skipped)}.")

    async def _create_in_crafty(self, job: Job, token: str, prepared: Prepared,
                                request: CreateRequest) -> None:
        """Phase 2: create the server and wait until Crafty has installed the loader."""
        loader_name = LOADER_NAMES[prepared.loader]
        job.begin("Creating the server in Crafty")
        job.server_name = request.name
        job.server_id = await self._crafty.create_server(token, {
            "name": request.name,
            "monitoring_type": "minecraft_java",
            # Crafty pings the game port to show status; the server runs on Crafty's host.
            "minecraft_java_monitoring_data": {"host": "127.0.0.1", "port": request.port},
            "create_type": "minecraft_java",
            "minecraft_java_create_data": {
                "create_type": "download_jar",
                "download_jar_create_data": {
                    "category": CRAFTY_CATEGORY,
                    "type": CRAFTY_TYPES[prepared.loader],
                    "version": prepared.minecraft,
                    "mem_min": request.mem_min,
                    "mem_max": request.mem_max,
                    "server_properties_port": request.port,
                    "agree_to_eula": True,
                },
            },
        })

        job.begin(f"Installing {loader_name} in Crafty")
        deadline = time.monotonic() + self._settings.modpack_install_timeout_minutes * 60
        while True:
            stats = await self._retry(lambda: self._crafty.server_stats(token, job.server_id))
            if not stats.get("importing"):
                server = await self._retry(lambda: self._crafty.get_server(token, job.server_id))
                if prepared.loader == "fabric":
                    # `importing` is also false during the ~3 s Crafty waits before its
                    # download starts, so only the jar on disk proves it finished.
                    root = await self._retry(
                        lambda: self._crafty.list_dir(token, job.server_id, ""))
                    if any(e.name == server.get("executable") and not e.is_dir for e in root):
                        return
                # Crafty clears `importing` before its Forge/NeoForge installer runs; the
                # installer rewrites the start command once it has succeeded.
                elif "--installServer" not in (server.get("execution_command") or ""):
                    return
            if time.monotonic() >= deadline:
                what = "download" if prepared.loader == "fabric" else "installer"
                raise JobFailure(
                    f'Crafty created "{request.name}" but its {loader_name} {what} did not '
                    "finish in time. Check the server's terminal in Crafty, then delete the "
                    "server there and try again.", names_server=True)
            await asyncio.sleep(self._settings.modpack_poll_seconds)

    async def _upload(self, job: Job, token: str, files: dict[str, Path]) -> None:
        """Phase 3a: send every file to the new server through Crafty."""
        sendable = {}
        for relative, source in files.items():
            if _sendable(relative):
                sendable[relative] = source
            else:
                job.warnings.append(f"{relative} could not be sent to Crafty (non-ASCII name); "
                                    "upload it yourself.")
        # Crafty's upload cannot create folders, and its folder creation is not recursive:
        # create every needed folder, parents first.
        folders = {parent for relative in sendable
                   for parent in PurePosixPath(relative).parents if parent.parts}
        job.begin("Creating folders", total=len(folders))
        for folder in sorted(folders, key=lambda f: (len(f.parts), str(f))):
            parent = str(folder.parent) if folder.parent.parts else ""
            await self._retry(lambda: self._crafty.create_folder(
                token, job.server_id, parent, folder.name))
            job.done += 1

        job.begin("Uploading files to the server", total=len(sendable))
        limit = asyncio.Semaphore(TRANSFER_CONCURRENCY)

        async def send(relative: str, source: Path) -> None:
            path = PurePosixPath(relative)
            async with limit:
                await self._retry(lambda: self._crafty.upload_path(
                    token, job.server_id, str(path.parent) if path.parent.parts else "",
                    path.name, source, self._settings.upload_chunk_bytes))
            job.done += 1

        await self._gather(send(r, s) for r, s in sendable.items())

    async def _record(self, job: Job, prepared: Prepared, files: dict[str, Path]) -> None:
        """Phase 3b: remember the server as adopted, linked to its modpack version."""
        job.begin("Identifying mods")
        hashes = {
            PurePosixPath(relative).name: await asyncio.to_thread(_sha1, source)
            for relative, source in files.items()
            if _is_mod_jar(relative) and _sendable(relative)
        }
        async with self._locks(job.server_id):
            await self._adoption.record(
                job.server_id, hashes, prepared.loader, prepared.minecraft,
                modpack={"project_id": prepared.project["id"],
                         "title": prepared.project["title"],
                         "version_id": prepared.version["id"],
                         "version_number": prepared.version.get("version_number")})

    async def _retry(self, operation):
        """Run a Crafty call, retrying network errors and 5xx-style failures.

        Never used for creating the server itself, which must not happen twice.
        """
        for attempt in range(1, RETRIES + 1):
            try:
                return await operation()
            except CraftyError as exc:
                if exc.state not in ("unreachable", "error") or attempt == RETRIES:
                    raise
            await asyncio.sleep(self._settings.crafty_retry_seconds * attempt)

    @staticmethod
    async def _gather(coroutines) -> None:
        """Run concurrently; the first failure cancels the rest and is re-raised as is."""
        try:
            async with asyncio.TaskGroup() as group:
                for coroutine in coroutines:
                    group.create_task(coroutine)
        except ExceptionGroup as group_error:
            raise group_error.exceptions[0] from None
