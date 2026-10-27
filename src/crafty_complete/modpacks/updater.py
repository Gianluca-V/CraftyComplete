import re
import shutil
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from crafty_complete.catalog.modrinth import ModrinthProvider
from crafty_complete.config import Settings
from crafty_complete.crafty.client import CraftyClient
from crafty_complete.mods.adoption import AdoptionService
from crafty_complete.mods.locks import ServerLocks
from crafty_complete.mods.models import AdoptedServer
from crafty_complete.mods.store import AdoptionStore
from crafty_complete.modpacks.creator import CreationRefused
from crafty_complete.modpacks.jobs import Job, JobFailure
from crafty_complete.modpacks.linker import LinkTarget, ModpackLinker, newest_update
from crafty_complete.modpacks.packs import PackReader, describe_failure, is_mod_jar
from crafty_complete.modpacks.transfer import ServerWriter, parent_of

BACKUP_ROOT = ".crafty-complete/backups"


def _slug(text: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9.+-]+", "_", text or "unknown")[:40]


class ModpackUpdater:
    """Move a linked server to another version of its modpack, CurseForge-style.

    Files the pack changed are replaced, files it dropped from mods/ are removed, files it
    left alone keep the server's copy (so config edits survive), and files that never came
    from the pack (the user's own mods) are not touched. Every replaced or removed file is
    moved to a backup folder on the server instead of being deleted.
    """

    def __init__(self, crafty: CraftyClient, modrinth: ModrinthProvider,
                 linker: ModpackLinker, adoption: AdoptionService, store: AdoptionStore,
                 locks: ServerLocks, settings: Settings):
        self._crafty = crafty
        self._modrinth = modrinth
        self._linker = linker
        self._adoption = adoption
        self._store = store
        self._locks = locks
        self._packs = PackReader(modrinth)
        self._writer = ServerWriter(crafty, settings)

    async def prepare(self, token: str, server_id: str,
                      version_id: str | None) -> tuple[AdoptedServer, LinkTarget]:
        server = await self._linker.adopted(token, server_id)
        if not server.modpack_project_id:
            raise CreationRefused(409, "Link this server to its modpack first.")
        if version_id is None:
            versions = await self._linker.compatible_versions(server.modpack_project_id, server)
            newest = newest_update(versions, server.modpack_version_id)
            if newest is None:
                raise CreationRefused(409, f"{server.modpack_title} "
                                           f"{server.modpack_version_number} is the newest "
                                           f"version for {server.loader} {server.game_version}.")
            version_id = newest["id"]
        if version_id == server.modpack_version_id:
            raise CreationRefused(409, f"The server already runs {server.modpack_title} "
                                       f"{server.modpack_version_number}.")
        target = await self._linker.prepare(token, server_id, server.modpack_project_id,
                                            version_id)
        stats = await self._crafty.server_stats(token, server_id)
        if stats.get("running"):
            raise CreationRefused(409, "Stop the server in Crafty before updating its modpack.")
        return server, target

    async def run(self, job: Job, token: str, server_id: str, target: LinkTarget) -> None:
        workdir = self._linker.workdir()
        try:
            await self._update(job, token, server_id, target, workdir)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def _update(self, job: Job, token: str, server_id: str, target: LinkTarget,
                      workdir: Path) -> None:
        server = self._store.get(server_id)
        old = self._store.get_modpack_files(server_id)
        pack_path, index, new = await self._linker.pack_files(job, target, server, workdir)

        added = sorted(p for p in new if p not in old)
        changed = sorted(p for p in new if p in old and new[p] != old[p])
        removed = sorted(p for p in old if p not in new and is_mod_jar(p))

        # Pinned mods are the user's decision: leave them exactly as they are.
        pinned = {f"mods/{m.file_name}" for m in server.mods if m.pinned}
        kept_pinned = sorted(pinned & set(changed + removed + added))
        for path in kept_pinned:
            job.warnings.append(f"Kept your pinned {path}.")
        added = [p for p in added if p not in pinned]
        changed = [p for p in changed if p not in pinned]
        removed = [p for p in removed if p not in pinned]

        # A mod the user added themselves that the pack now ships would load twice.
        displaced = await self._displaced_own_mods(job, server, old, new, added + changed)

        files = await self._packs.fetch_files(job, index, pack_path, set(added + changed),
                                              workdir)
        backup = (f"{BACKUP_ROOT}/{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-"
                  f"{_slug(server.modpack_version_number)}-to-"
                  f"{_slug(target.version.get('version_number'))}")

        async with self._locks(server_id):
            moved = await self._apply(job, token, server_id, files,
                                      changed + removed + displaced + added, backup)

            hashes = {m.file_name: m.sha1 for m in server.mods
                      if f"mods/{m.file_name}" not in moved}
            hashes.update({PurePosixPath(p).name: new[p] for p in files if is_mod_jar(p)})
            remembered = dict(new)
            for path in kept_pinned:  # still the pack's file, at the old version
                if path in old:
                    remembered[path] = old[path]
                else:
                    remembered.pop(path, None)
            self._store.set_modpack_link(server_id, target.link)
            self._store.set_modpack_files(server_id, remembered)
            job.begin("Identifying mods")
            await self._adoption.record(server_id, hashes)

        displaced_names = {PurePosixPath(p).name for p in displaced}
        old_hashes = set(old.values())
        job.result = {
            "added": added,
            "updated": changed,
            "removed": removed,
            "own_mods_kept": sorted(m.file_name for m in server.mods
                                    if m.sha1 not in old_hashes
                                    and m.file_name not in displaced_names
                                    and f"mods/{m.file_name}" not in new),
            "backup_folder": backup,
            "restart_required": True,
        }

    async def _displaced_own_mods(self, job: Job, server: AdoptedServer, old: dict,
                                  new: dict, incoming: list[str]) -> list[str]:
        new_jars = {p: new[p] for p in incoming if is_mod_jar(p) and new[p]}
        versions = await self._modrinth.versions_by_sha1(sorted(set(new_jars.values())))
        incoming_by_project = {versions[h]["project_id"]: p for p, h in new_jars.items()
                               if h in versions}
        old_hashes = set(old.values())
        displaced = []
        for mod in server.mods:
            path = f"mods/{mod.file_name}"
            if (mod.sha1 not in old_hashes and not mod.pinned and path not in new
                    and mod.project_id in incoming_by_project):
                displaced.append(path)
                job.warnings.append(
                    f"Moved your own {path} aside: the pack now ships "
                    f"{incoming_by_project[mod.project_id]} for the same project.")
        return sorted(displaced)

    async def _apply(self, job: Job, token: str, server_id: str, files: dict[str, Path],
                     candidates: list[str], backup: str) -> set[str]:
        """Move existing `candidates` into the backup, then upload `files`."""
        await self._writer.create_folders(
            job, token, server_id,
            {parent_of(p) for p in files} | {f"{backup}/{parent_of(p)}".rstrip("/")
                                             for p in candidates})
        existing: set[str] = set()
        for folder in sorted({parent_of(p) for p in candidates}):
            entries = await self._writer.retry(
                lambda: self._crafty.list_dir(token, server_id, folder))
            existing |= {e.path for e in entries if not e.is_dir}

        to_move = [p for p in candidates if p in existing]
        moved: set[str] = set()
        try:
            job.begin("Moving replaced files to the backup", total=len(to_move))
            for path in to_move:
                await self._writer.move(token, server_id, path,
                                        f"{backup}/{parent_of(path)}".rstrip("/"))
                moved.add(path)
                job.done += 1
            await self._writer.upload(job, token, server_id, files)
        except Exception as exc:
            if not moved:
                raise
            raise JobFailure(
                f"{describe_failure(exc)} The update stopped part-way; the files it replaced "
                f"or removed are in {backup} on the server. Move them back, or restore a "
                "Crafty backup, before starting the server.") from exc
        return moved
