from dataclasses import dataclass, field
from typing import Any

from crafty_complete.catalog.models import CatalogError
from crafty_complete.config import Settings
from crafty_complete.crafty.client import CraftyClient, CraftyError
from crafty_complete.modpacks.creator import CreationRefused
from crafty_complete.modpacks.jobs import Job
from crafty_complete.modpacks.packs import describe_failure
from crafty_complete.modpacks.transfer import ServerWriter
from crafty_complete.mods.adoption import MODS_DIR, AdoptionService
from crafty_complete.mods.installer import ModInstaller, ModOperationError
from crafty_complete.mods.locks import ServerLocks
from crafty_complete.mods.store import AdoptionStore
from crafty_complete.servers.provision import NewServer, catalog_versions, create_and_install


@dataclass(frozen=True)
class Kind:
    kind: str
    label: str
    # Crafty catalog type.
    crafty_type: str
    # Mod loaders take Modrinth mods and are adopted; vanilla and Paper are not.
    modded: bool


KINDS = {k.kind: k for k in (
    Kind("vanilla", "Vanilla", "vanilla", False),
    Kind("paper", "Paper", "paper", False),
    Kind("fabric", "Fabric", "fabric", True),
    Kind("forge", "Forge", "forge-installer", True),
    Kind("neoforge", "NeoForge", "neoforge-installer", True),
)}
MAX_MODS = 50


@dataclass(frozen=True)
class NewServerRequest:
    kind: str
    minecraft: str
    name: str
    port: int
    mem_min: float
    mem_max: float
    mods: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class PreparedServer:
    kind: Kind
    request: NewServerRequest
    # Modrinth projects to install, checked against the loader and Minecraft version.
    mods: tuple[dict[str, Any], ...]


class ServerCreator:
    """Create vanilla, Paper and mod-loader servers in Crafty, shared by the web and the MCP.

    A mod-loader server is adopted right away and gets the chosen Modrinth mods with
    their required dependencies, through the same installer as the server page.
    """

    def __init__(self, crafty: CraftyClient, adoption: AdoptionService,
                 installer: ModInstaller, store: AdoptionStore, locks: ServerLocks,
                 settings: Settings):
        self._crafty = crafty
        self._adoption = adoption
        self._installer = installer
        self._store = store
        self._locks = locks
        self._settings = settings
        self._writer = ServerWriter(crafty, settings)

    async def options(self, token: str) -> dict[str, Any]:
        """The kinds of server and Minecraft versions this Crafty can install."""
        catalog = await self._crafty.jar_catalog(token)
        return {"kinds": [
            {"kind": k.kind, "label": k.label, "modded": k.modded,
             "versions": list(catalog_versions(catalog, k.crafty_type))}
            for k in KINDS.values()
        ]}

    async def prepare(self, token: str, superuser: bool,
                      request: NewServerRequest) -> PreparedServer:
        """Every check that can fail fast, before anything is created."""
        permissions = await self._crafty.get_my_permissions(token, superuser)
        if not permissions["server_creation"]:
            raise CreationRefused(403, "Your Crafty account cannot create servers.")
        kind = KINDS[request.kind]
        if request.mods and not kind.modded:
            raise CreationRefused(422, f"{kind.label} servers cannot have mods; choose Fabric, "
                                       "Forge or NeoForge.")
        refs = list(dict.fromkeys(request.mods))
        if len(refs) > MAX_MODS:
            raise CreationRefused(422, f"Choose at most {MAX_MODS} mods.")
        catalog = await self._crafty.jar_catalog(token)
        if request.minecraft not in catalog_versions(catalog, kind.crafty_type):
            raise CreationRefused(422, f"Crafty cannot install {kind.label} for Minecraft "
                                       f"{request.minecraft}.")
        mods: dict[str, dict[str, Any]] = {}
        for ref in refs:
            try:
                project = await self._installer.check_installable(
                    ref, request.kind, request.minecraft)
            except ModOperationError as exc:
                raise CreationRefused(exc.status_code, exc.detail) from exc
            mods.setdefault(project["id"], project)
        return PreparedServer(kind, request, tuple(mods.values()))

    def describe_error(self, job: Job, exc: Exception) -> str:
        message = describe_failure(exc)
        if job.server_id and not getattr(exc, "names_server", False):
            message += (f' The server "{job.server_name}" was created in Crafty but is not '
                        "complete; delete it there before trying again.")
        return message

    async def run(self, job: Job, token: str, prepared: PreparedServer) -> None:
        kind, request = prepared.kind, prepared.request
        await create_and_install(self._crafty, self._writer, self._settings, job, token,
                                 NewServer(request.name, request.port, request.mem_min,
                                           request.mem_max, kind.crafty_type,
                                           request.minecraft, kind.label))
        if not kind.modded:
            job.result = {"installed": []}
            return
        # Mod loaders only create mods/ on their first start; uploads need it now.
        await self._writer.create_folders(job, token, job.server_id, {MODS_DIR})
        job.begin("Adopting the server")
        async with self._locks(job.server_id):
            await self._adoption.record(job.server_id, {}, request.kind, request.minecraft)
        installed: list[dict] = []
        job.begin("Installing mods", total=len(prepared.mods))
        for project in prepared.mods:
            already = {item["project_id"] for item in installed}
            if project["id"] not in already:
                installed.extend(await self._install(job, token, project))
            job.done += 1
        # A chosen mod that an earlier choice pulled in as a dependency is the user's own.
        async with self._locks(job.server_id):
            self._store.mark_chosen(job.server_id, [project["id"] for project in prepared.mods])
        for item in installed:
            if any(item["project_id"] == project["id"] for project in prepared.mods):
                item["dependency"] = False
        job.result = {"installed": installed}

    async def _install(self, job: Job, token: str, project: dict[str, Any]) -> list[dict]:
        """Install one chosen mod; a failure is a warning, since the server already exists."""
        try:
            outcome = await self._installer.install(token, job.server_id, project["id"])
        except ModOperationError as exc:
            if exc.status_code == 409 and "already installed" in exc.detail:
                return []  # pulled in as a dependency of an earlier choice
            reason = exc.detail
        except CatalogError as exc:
            reason = str(exc)
        except CraftyError as exc:
            if exc.state == "unauthorized":
                raise
            reason = f"Crafty: {exc.message}"
        else:
            for skipped in outcome["skipped"]:
                job.warnings.append(f"Skipped {skipped['title']}: {skipped['reason']}")
            return outcome["installed"]
        job.warnings.append(f"Could not install {project['title']}: {reason.rstrip('.')}. "
                            "Install it from the server page.")
        return []
