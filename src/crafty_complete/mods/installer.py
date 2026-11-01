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
# Planned jars are held in memory until every hash is verified, so bound the batch.
MAX_TOTAL_DOWNLOAD_BYTES = 512 * 1024 * 1024
RESCAN_HINT = " If an earlier attempt timed out, rescan the server and try again."


def _size(n: int) -> str:
    return f"{n // (1024 * 1024)} MiB" if n >= 1024 * 1024 else f"{n} bytes"


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
    """Install, update, pick versions of and remove Modrinth mods on adopted Crafty servers."""

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
            missing_version = False
            if dep.get("version_id"):
                try:
                    dep_version = await self._modrinth.version(dep["version_id"])
                except CatalogNotFound:
                    missing_version = True
            project_id = dep.get("project_id") or (dep_version or {}).get("project_id")
            if not project_id or project_id in installed or project_id in planned:
                continue
            if len(planned) >= MAX_PLANNED_PROJECTS:
                raise ModOperationError(422, "The dependency tree is too large to install.")
            project = await self._modrinth.project(project_id)
            if missing_version:
                outcome.skipped.append(Skipped(
                    project_id=project["id"], title=project["title"],
                    reason=f"Modrinth no longer has the required version {dep['version_id']}; "
                           "install it yourself."))
                continue
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
                     existing: set[str], replaces: InstalledMod | None = None,
                     latest: dict[str, Any] | None = None) -> InstalledMod:
        """Upload one jar (replacing `replaces` if given) and record it.

        `latest` is the newest compatible version to record, when it is not this one.

        The new jar is uploaded and recorded before the old one is deleted, so a failure
        never leaves the mod missing; if the delete fails, both jars stay recorded and a
        retry only has to delete the old one.
        """
        name = item.file["filename"]
        old_name = replaces.file_name if replaces else None
        project, version = item.project, item.version
        latest = latest or version
        already_uploaded = False
        if name != old_name and name in existing:
            recorded = next((m for m in self._store.get(server.server_id).mods
                             if m.file_name == name), None)
            if not (recorded and recorded.project_id == project["id"]
                    and recorded.version_id == version["id"]):
                raise ModOperationError(409, f"{MODS_DIR}/{name} already exists.{RESCAN_HINT}")
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
            latest_version_id=latest["id"],
            latest_version_number=latest.get("version_number"),
            dependency=replaces.dependency if replaces else item.dependency,
        )
        # Same name: this overwrites the old record. Different name: the old record is only
        # removed once its jar is gone.
        self._store.put_mod(server.server_id, mod)
        existing.add(name)
        if old_name and old_name != name:
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
        total = sum(item.file.get("size") or 0 for item in downloadable)
        if total > MAX_TOTAL_DOWNLOAD_BYTES:
            raise ModOperationError(422, f"This install would download more than "
                                         f"{_size(MAX_TOTAL_DOWNLOAD_BYTES)}; install the mods "
                                         "one by one.")
        payloads = [await self._download(item) for item in downloadable]

        existing = {e.name for e in await self._crafty.list_dir(token, server.server_id, MODS_DIR)}
        done = []
        for item, data in zip(downloadable, payloads):
            try:
                await self._write(token, server, item, data, existing)
            except (CraftyError, ModOperationError) as exc:
                if not done or (isinstance(exc, CraftyError) and exc.state == "unauthorized"):
                    raise
                message = exc.detail if isinstance(exc, ModOperationError) else (
                    f"Crafty request failed: {exc.message}")
                already = ", ".join(f"{d.title} {d.version_number} ({d.file_name})" for d in done)
                raise ModOperationError(
                    502, f"{message}. Already installed before the failure: {already}.") from exc
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
        await self._replace(token, server, mod, version, outcome)

    async def _replace(self, token: str, server: AdoptedServer, mod: InstalledMod,
                       version: dict[str, Any], outcome: Outcome,
                       latest: dict[str, Any] | None = None) -> None:
        """Swap `mod`'s jar for `version`, installing that version's missing dependencies."""
        title = mod.title or mod.file_name
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
        new = await self._write(token, server, item, data, existing, replaces=mod, latest=latest)
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

    # --- pick a version -------------------------------------------------------

    def _mod(self, server: AdoptedServer, file_name: str) -> InstalledMod:
        mod = next((m for m in server.mods if m.file_name == file_name), None)
        if mod is None:
            raise ModOperationError(404, "No adopted mod with that file name.")
        return mod

    async def _versions_of(self, server: AdoptedServer,
                           mod: InstalledMod) -> list[dict[str, Any]]:
        if mod.state != "identified":
            raise ModOperationError(
                409, f"{mod.file_name} is not on Modrinth, so its version cannot be changed.")
        return await self._modrinth.compatible_versions(
            mod.project_id, compatible_loaders(server.loader), server.game_version)

    async def versions(self, token: str, server_id: str, file_name: str) -> dict:
        """Versions of an installed mod that fit the server's loader and Minecraft version."""
        server = await self._adopted(token, server_id)
        mod = self._mod(server, file_name)
        versions = await self._versions_of(server, mod)
        return {
            "current_version_id": mod.version_id,
            "versions": [{"id": v["id"], "version_number": v.get("version_number"),
                          "version_type": v.get("version_type"),
                          "date_published": v.get("date_published")} for v in versions],
        }

    async def set_version(self, token: str, server_id: str, file_name: str,
                          version_id: str) -> dict:
        """Install a chosen version of a mod. It is not pinned, so "Update all" can move it."""
        async with self._locks(server_id):
            server = await self._adopted(token, server_id)
            mod = self._mod(server, file_name)
            title = mod.title or mod.file_name
            versions = await self._versions_of(server, mod)
            if mod.pinned:
                raise ModOperationError(409, f"{title} is pinned; unpin it to change its version.")
            version = next((v for v in versions if v["id"] == version_id), None)
            if version is None:
                raise ModOperationError(422, f"{title} has no version {version_id} for "
                                             f"{server.loader} {server.game_version}.")
            if version_id == mod.version_id:
                raise ModOperationError(
                    409, f"{title} {mod.version_number} is already installed.")
            outcome = Outcome()
            await self._replace(token, server, mod, version, outcome,
                                latest=_choose_version(versions))
            return outcome.to_dict(self._store.get(server_id))

    # --- remove ---------------------------------------------------------------

    async def _requirements(self, server: AdoptedServer) -> dict[str, set[str]]:
        """For each installed mod, the installed mods its version requires (by file name)."""
        by_project = {m.project_id: m.file_name for m in server.mods if m.project_id}
        by_version = {m.version_id: m.file_name for m in server.mods if m.version_id}
        versions = await self._modrinth.versions(sorted(by_version)) if by_version else {}
        needs: dict[str, set[str]] = {m.file_name: set() for m in server.mods}
        for mod in server.mods:
            for dep in (versions.get(mod.version_id) or {}).get("dependencies") or []:
                if dep.get("dependency_type") != "required":
                    continue
                target = by_project.get(dep.get("project_id")) or by_version.get(
                    dep.get("version_id"))
                if target and target != mod.file_name:
                    needs[mod.file_name].add(target)
        return needs

    async def _plan_removal(self, server: AdoptedServer, mod: InstalledMod,
                            needs: dict[str, set[str]]) -> dict:
        titles = {m.file_name: m.title or m.file_name for m in server.mods}
        required_by = sorted(f for f, deps in needs.items() if mod.file_name in deps)
        # Mods installed as dependencies of what leaves that nothing staying requires,
        # followed transitively. Mods the user added themselves are never offered.
        installed_as_dependency = {m.file_name for m in server.mods if m.dependency}
        leaving = {mod.file_name}
        unused: list[str] = []
        changed = True
        while changed:
            changed = False
            needed = set().union(*(needs[f] for f in leaving))
            for candidate in sorted(needed & installed_as_dependency - leaving):
                staying = set(needs) - leaving - {candidate}
                if not any(candidate in needs[f] for f in staying):
                    leaving.add(candidate)
                    unused.append(candidate)
                    changed = True
        warnings = []
        if f"{MODS_DIR}/{mod.file_name}" in self._store.get_modpack_files(server.server_id):
            warnings.append(f"{titles[mod.file_name]} is part of the linked modpack; "
                            "a modpack update adds it back.")
        return {
            "file_name": mod.file_name,
            "title": titles[mod.file_name],
            "required_by": [{"file_name": f, "title": titles[f]} for f in required_by],
            "unused_dependencies": [{"file_name": f, "title": titles[f]} for f in unused],
            "warnings": warnings,
        }

    async def removal_plan(self, token: str, server_id: str, file_name: str) -> dict:
        """What removing a mod affects: mods that require it and dependencies left unused."""
        server = await self._adopted(token, server_id)
        return await self._plan_removal(server, self._mod(server, file_name),
                                        await self._requirements(server))

    async def remove(self, token: str, server_id: str, file_name: str, force: bool,
                     remove_dependencies: list[str]) -> dict:
        """Delete a mod and the unused dependencies the user confirmed.

        A mod other mods require is only removed with `force`. The plan is computed again
        under the server lock, so a dependency that became needed meanwhile is refused.
        """
        async with self._locks(server_id):
            server = await self._adopted(token, server_id)
            mod = self._mod(server, file_name)
            needs = await self._requirements(server)
            plan = await self._plan_removal(server, mod, needs)
            if plan["required_by"] and not force:
                names = ", ".join(m["title"] for m in plan["required_by"])
                raise ModOperationError(
                    409, f"{plan['title']} is required by {names}. Confirm to remove it anyway.")
            unused = {m["file_name"] for m in plan["unused_dependencies"]}
            for name in remove_dependencies:
                if name not in unused:
                    raise ModOperationError(409, f"{name} is not an unused dependency of "
                                                 f"{plan['title']}; review the removal again.")
            titles = {m.file_name: m.title or m.file_name for m in server.mods}
            # The plan assumes every offered dependency leaves; a subset must not strand
            # a mod that stays.
            leaving = {file_name, *remove_dependencies}
            for name in remove_dependencies:
                users = sorted(f for f in set(needs) - leaving if name in needs[f])
                if users:
                    others = ", ".join(titles[f] for f in users)
                    raise ModOperationError(409, f"{titles[name]} is still required by {others}; "
                                                 f"remove {others} too, or keep {titles[name]}.")
            removed = []
            for name in [file_name, *dict.fromkeys(remove_dependencies)]:
                try:
                    await self._crafty.delete_file(token, server_id, f"{MODS_DIR}/{name}")
                except CraftyError as exc:
                    if exc.state == "unauthorized":
                        raise
                    done = ", ".join(r["file_name"] for r in removed)
                    already = f" Already removed: {done}." if removed else ""
                    raise ModOperationError(
                        502, f"Crafty could not delete {name} ({exc.message}).{already}") from exc
                self._store.remove_mod(server_id, name)
                removed.append({"file_name": name, "title": titles[name]})
            return {
                "removed": removed,
                "warnings": plan["warnings"],
                "restart_required": True,
                "server": self._store.get(server_id).to_dict(),
            }
