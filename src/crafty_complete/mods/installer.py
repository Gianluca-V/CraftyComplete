import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

from crafty_complete.catalog.models import CatalogError, CatalogNotFound, compatible_loaders
from crafty_complete.catalog.modrinth import MODRINTH_CDN, ModrinthProvider
from crafty_complete.crafty.client import CraftyClient, CraftyError
from crafty_complete.mods.adoption import MODS_DIR, AdoptionService
from crafty_complete.mods.locks import ServerLocks
from crafty_complete.mods.models import AdoptedServer, InstalledMod
from crafty_complete.mods.store import AdoptionStore

# Safety net against dependency cycles or absurd trees.
MAX_PLANNED_PROJECTS = 50


class ModOperationError(Exception):
    """A refusal or failure the user should read, with its HTTP status."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class Installed:
    project_id: str
    title: str
    version_number: str
    file_name: str
    dependency: bool


@dataclass
class Skipped:
    project_id: str
    title: str
    reason: str
    file_name: str | None = None
    url: str | None = None


@dataclass
class Updated:
    project_id: str
    title: str
    from_version: str | None
    to_version: str
    old_file_name: str
    file_name: str


@dataclass
class Failed:
    project_id: str | None
    title: str
    file_name: str
    reason: str


@dataclass
class Outcome:
    installed: list[Installed] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    updated: list[Updated] = field(default_factory=list)
    failed: list[Failed] = field(default_factory=list)

    def to_dict(self, server: AdoptedServer) -> dict:
        return {
            "installed": [asdict(i) for i in self.installed],
            "skipped": [asdict(s) for s in self.skipped],
            "updated": [asdict(u) for u in self.updated],
            "failed": [asdict(f) for f in self.failed],
            # Loaders only pick up new jars on start; craftyComplete never restarts servers.
            "restart_required": bool(self.installed or self.updated),
            "server": server.to_dict(),
        }


@dataclass
class Planned:
    project: dict[str, Any]
    version: dict[str, Any]
    file: dict[str, Any]
    dependency: bool


def _primary_file(version: dict[str, Any]) -> dict[str, Any]:
    files = version.get("files") or []
    if not files:
        raise ModOperationError(502, f"Modrinth version {version.get('id')} has no files.")
    return next((f for f in files if f.get("primary")), files[0])


def _choose_version(versions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Newest release, or the newest version of any type when there is no release."""
    return next((v for v in versions if v.get("version_type") == "release"),
                versions[0] if versions else None)


def _safe_file_name(name: str) -> bool:
    """A plain jar name Crafty can receive: it travels in an (ASCII-only) HTTP header."""
    return (bool(name) and name.lower().endswith(".jar") and "/" not in name
            and "\\" not in name and ":" not in name and not name.startswith(".")
            and len(name) <= 255 and name.isascii() and name.isprintable())


UNSAFE_NAME_REASON = ("Its file name cannot be sent to Crafty; download it yourself and "
                      "upload it to mods/.")


class ModInstaller:
    """Install and update Modrinth mods on adopted Crafty servers."""

    def __init__(self, crafty: CraftyClient, modrinth: ModrinthProvider, store: AdoptionStore,
                 adoption: AdoptionService, locks: ServerLocks):
        self._crafty = crafty
        self._modrinth = modrinth
        self._store = store
        self._adoption = adoption
        self._locks = locks

    # --- helpers ------------------------------------------------------------

    async def _adopted(self, token: str, server_id: str) -> AdoptedServer:
        await self._adoption.ensure_file_access(token, server_id)
        server = self._store.get(server_id)
        if not server.adopted:
            raise ModOperationError(409, "Adopt this server first.")
        if not (server.loader and server.game_version):
            raise ModOperationError(
                409, "Set the server's loader and Minecraft version, then rescan.")
        return server

    async def _compatible_version(self, project: dict[str, Any],
                                  server: AdoptedServer) -> dict[str, Any] | None:
        versions = await self._modrinth.compatible_versions(
            project["id"], compatible_loaders(server.loader), server.game_version
        )
        return _choose_version(versions)

    async def _plan_dependencies(self, version: dict[str, Any], server: AdoptedServer,
                                 planned: dict[str, Planned], outcome: Outcome) -> None:
        """Add missing required dependencies of `version` (recursively) to `planned`."""
        installed = {m.project_id for m in server.mods if m.project_id}
        for dep in version.get("dependencies") or []:
            if dep.get("dependency_type") != "required":
                continue
            dep_version = None
            if dep.get("version_id"):
                dep_version = await self._modrinth.version(dep["version_id"])
            project_id = dep.get("project_id") or (dep_version or {}).get("project_id")
            if not project_id or project_id in installed or project_id in planned:
                continue
            if len(planned) >= MAX_PLANNED_PROJECTS:
                raise ModOperationError(422, "The dependency tree is too large to install.")
            project = await self._modrinth.project(project_id)
            if project.get("server_side") == "unsupported":
                outcome.skipped.append(Skipped(
                    project_id=project["id"], title=project["title"],
                    reason="Client-only dependency; not needed on the server."))
                continue
            dep_version = dep_version or await self._compatible_version(project, server)
            if dep_version is None:
                outcome.skipped.append(Skipped(
                    project_id=project["id"], title=project["title"],
                    reason=f"No version for {server.loader} {server.game_version}; "
                           "install it yourself."))
                continue
            planned[project["id"]] = Planned(project, dep_version, _primary_file(dep_version),
                                             dependency=True)
            await self._plan_dependencies(dep_version, server, planned, outcome)

    def _check_incompatibilities(self, planned: dict[str, Planned],
                                 server: AdoptedServer) -> None:
        present = {m.project_id: m.title or m.file_name for m in server.mods if m.project_id}
        present.update({pid: p.project["title"] for pid, p in planned.items()})
        for item in planned.values():
            for dep in item.version.get("dependencies") or []:
                other = dep.get("project_id")
                if dep.get("dependency_type") == "incompatible" and other in present:
                    raise ModOperationError(
                        409, f"{item.project['title']} is incompatible with {present[other]}.")

    async def _download(self, item: Planned) -> bytes:
        data = await self._modrinth.download(item.file["url"])
        hashes = item.file.get("hashes") or {}
        expected = {"sha1": hashes.get("sha1"), "sha512": hashes.get("sha512")}
        if not any(expected.values()) or any(
            value and getattr(hashlib, algo)(data).hexdigest() != value
            for algo, value in expected.items()
        ):
            raise ModOperationError(
                502, f"{item.file['filename']} from Modrinth does not match its published hash.")
        return data

    async def _write(self, token: str, server: AdoptedServer, item: Planned, data: bytes,
                     existing: set[str], replaces: InstalledMod | None = None) -> InstalledMod:
        """Upload one jar (replacing `replaces` if given) and record it.

        The new jar is uploaded and recorded before the old one is deleted, so a failure
        never leaves the mod missing; if the delete fails, both jars stay recorded and a
        retry only has to delete the old one.
        """
        name = item.file["filename"]
        old_name = replaces.file_name if replaces else None
        project, version = item.project, item.version
        already_uploaded = False
        if name != old_name and name in existing:
            recorded = next((m for m in self._store.get(server.server_id).mods
                             if m.file_name == name), None)
            if not (recorded and recorded.project_id == project["id"]
                    and recorded.version_id == version["id"]):
                raise ModOperationError(409, f"{MODS_DIR}/{name} already exists.")
            already_uploaded = True  # left behind by an update whose delete failed
        if not already_uploaded:
            await self._crafty.upload_file(token, server.server_id, MODS_DIR, name, data)
        mod = InstalledMod(
            file_name=name,
            sha1=item.file["hashes"].get("sha1") or hashlib.sha1(data).hexdigest(),
            state="identified",
            project_id=project["id"],
            slug=project.get("slug"),
            title=project.get("title"),
            icon_url=project.get("icon_url"),
            version_id=version["id"],
            version_number=version.get("version_number"),
            server_side=project.get("server_side"),
            latest_version_id=version["id"],
            latest_version_number=version.get("version_number"),
        )
        same_name = old_name == name
        self._store.put_mod(server.server_id, mod, replaces=old_name if same_name else None)
        existing.add(name)
        if old_name and not same_name:
            try:
                await self._crafty.delete_file(
                    token, server.server_id, f"{MODS_DIR}/{old_name}")
            except CraftyError as exc:
                if exc.state == "unauthorized":
                    raise
                raise ModOperationError(502, (
                    f"{project['title']} {version.get('version_number')} was uploaded as "
                    f"{name}, but Crafty could not delete the old {old_name} ({exc.message}). "
                    f"Stop the server and update again, or delete {MODS_DIR}/{old_name} "
                    "yourself before restarting.")) from exc
            self._store.remove_mod(server.server_id, old_name)
        return mod

    async def _install_planned(self, token: str, server: AdoptedServer,
                               planned: list[Planned], outcome: Outcome) -> None:
        """Download every planned jar first (any bad hash aborts), then upload them.

        Dependencies are uploaded before the mods that need them (planning order reversed),
        so a failure part-way never leaves a mod without its dependencies.
        """
        downloadable = []
        for item in reversed(planned):
            reason = None
            if not item.file.get("url", "").startswith(MODRINTH_CDN):
                reason = "Not hosted on Modrinth; download it yourself and upload it to mods/."
            elif not _safe_file_name(item.file.get("filename", "")):
                reason = UNSAFE_NAME_REASON
            if reason:
                outcome.skipped.append(Skipped(
                    project_id=item.project["id"], title=item.project["title"],
                    file_name=item.file.get("filename"), url=item.file.get("url"),
                    reason=reason))
                continue
            downloadable.append(item)
        payloads = [await self._download(item) for item in downloadable]

        existing = {e.name for e in await self._crafty.list_dir(token, server.server_id, MODS_DIR)}
        done = []
        for item, data in zip(downloadable, payloads):
            await self._write(token, server, item, data, existing)
            done.append(Installed(
                project_id=item.project["id"], title=item.project["title"],
                version_number=item.version.get("version_number"),
                file_name=item.file["filename"], dependency=item.dependency))
        # Report in planning order: the requested mod first, then what it pulled in.
        outcome.installed.extend(reversed(done))

    # --- install ------------------------------------------------------------

    async def install(self, token: str, server_id: str, project_ref: str) -> dict:
        async with self._locks(server_id):
            server = await self._adopted(token, server_id)
            try:
                project = await self._modrinth.project(project_ref)
            except CatalogNotFound as exc:
                raise ModOperationError(404, f"Modrinth has no project '{project_ref}'.") from exc
            title = project["title"]
            if project.get("project_type") != "mod":
                kind = project.get("project_type", "something else")
                raise ModOperationError(422, f"{title} is a {kind}, not a mod.")
            if project.get("server_side") == "unsupported":
                raise ModOperationError(
                    422, f"{title} only runs on clients; it cannot be installed on a server.")
            current = next((m for m in server.mods if m.project_id == project["id"]), None)
            if current:
                raise ModOperationError(
                    409, f"{title} is already installed ({current.file_name}).")

            version = await self._compatible_version(project, server)
            if version is None:
                raise ModOperationError(
                    422, f"{title} has no version for {server.loader} {server.game_version}.")

            outcome = Outcome()
            planned = {project["id"]: Planned(project, version, _primary_file(version), False)}
            await self._plan_dependencies(version, server, planned, outcome)
            self._check_incompatibilities(planned, server)
            await self._install_planned(token, server, list(planned.values()), outcome)
            return outcome.to_dict(self._store.get(server_id))

    # --- update -------------------------------------------------------------

    async def _update_one(self, token: str, server: AdoptedServer, mod: InstalledMod,
                          outcome: Outcome) -> None:
        title = mod.title or mod.file_name
        if mod.state != "identified":
            raise ModOperationError(
                409, f"{mod.file_name} is not on Modrinth, so it cannot be updated.")
        if mod.pinned:
            raise ModOperationError(409, f"{title} is pinned; unpin it to update.")
        if not mod.update_available:
            raise ModOperationError(409, f"{title} has no update available.")
        try:
            version = await self._modrinth.version(mod.latest_version_id)
        except CatalogNotFound as exc:
            raise ModOperationError(
                502, f"Modrinth has no version {mod.latest_version_id}.") from exc
        project = {"id": mod.project_id, "title": title, "slug": mod.slug,
                   "icon_url": mod.icon_url, "server_side": mod.server_side}
        item = Planned(project, version, _primary_file(version), dependency=False)
        if not item.file.get("url", "").startswith(MODRINTH_CDN):
            raise ModOperationError(
                502, f"The new {title} is not hosted on Modrinth; update it by hand.")
        if not _safe_file_name(item.file.get("filename", "")):
            raise ModOperationError(502, f"The new {title} has a file name Crafty cannot accept "
                                         f"({item.file.get('filename')!r}); update it by hand.")

        planned_deps: dict[str, Planned] = {}
        await self._plan_dependencies(version, server, planned_deps, outcome)
        self._check_incompatibilities({mod.project_id: item, **planned_deps}, server)

        data = await self._download(item)
        await self._install_planned(token, server, list(planned_deps.values()), outcome)
        existing = {e.name for e in await self._crafty.list_dir(token, server.server_id, MODS_DIR)}
        new = await self._write(token, server, item, data, existing, replaces=mod)
        outcome.updated.append(Updated(
            project_id=mod.project_id, title=title, from_version=mod.version_number,
            to_version=new.version_number, old_file_name=mod.file_name,
            file_name=new.file_name))

    async def update(self, token: str, server_id: str, file_name: str) -> dict:
        async with self._locks(server_id):
            server = await self._adopted(token, server_id)
            mod = next((m for m in server.mods if m.file_name == file_name), None)
            if mod is None:
                raise ModOperationError(404, "No adopted mod with that file name.")
            outcome = Outcome()
            await self._update_one(token, server, mod, outcome)
            return outcome.to_dict(self._store.get(server_id))

    async def update_all(self, token: str, server_id: str) -> dict:
        """Update every unpinned mod with an update; one failure does not stop the rest."""
        async with self._locks(server_id):
            server = await self._adopted(token, server_id)
            outcome = Outcome()
            for mod in sorted(server.mods, key=lambda m: m.file_name.casefold()):
                if not (mod.update_available and not mod.pinned):
                    continue
                try:
                    await self._update_one(token, self._store.get(server_id), mod, outcome)
                except ModOperationError as exc:
                    reason = exc.detail
                except CatalogError as exc:
                    reason = str(exc)
                except CraftyError as exc:
                    if exc.state == "unauthorized":
                        raise
                    reason = f"Crafty: {exc.message}"
                else:
                    continue
                outcome.failed.append(Failed(project_id=mod.project_id,
                                             title=mod.title or mod.file_name,
                                             file_name=mod.file_name, reason=reason))
            return outcome.to_dict(self._store.get(server_id))
