"""Reading Modrinth modpacks: shared by server creation, linking and updates."""

import asyncio
import hashlib
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from crafty_complete.catalog.models import CatalogError
from crafty_complete.catalog.modrinth import ModrinthProvider
from crafty_complete.crafty.client import CraftyError
from crafty_complete.mods.adoption import MODS_DIR
from crafty_complete.modpacks.jobs import Job, JobFailure
from crafty_complete.modpacks.mrpack import MrpackError, PackIndex, read_index

MAX_PACK_BYTES = 4 * 1024**3
MAX_FILE_BYTES = 1024**3
DOWNLOAD_CONCURRENCY = 4


def check_hashes(path: str, expected_sha1: str | None, expected_sha512: str | None,
                 actual: tuple[str, str]) -> None:
    if not (expected_sha1 or expected_sha512) or (
        (expected_sha1 and expected_sha1 != actual[0])
        or (expected_sha512 and expected_sha512 != actual[1])
    ):
        raise JobFailure(f"{path} does not match the hash in the modpack.")


def file_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sendable(relative: str) -> bool:
    """Crafty takes the file name in an HTTP header, which must be printable ASCII."""
    return relative.isascii() and relative.isprintable()


def is_mod_jar(relative: str) -> bool:
    path = PurePosixPath(relative)
    return path.parent == PurePosixPath(MODS_DIR) and path.suffix.lower() == ".jar"


def describe_failure(exc: Exception) -> str:
    """A message for the user; unexpected details stay in the log."""
    if isinstance(exc, (JobFailure, MrpackError)):
        return str(exc)
    if isinstance(exc, CraftyError):
        return ("Your Crafty session expired; log in again." if exc.state == "unauthorized"
                else f"Crafty request failed: {exc.message}.")
    if isinstance(exc, CatalogError):
        return f"{exc}."
    return "Unexpected error; see the craftyComplete log for details."


class PackReader:
    def __init__(self, modrinth: ModrinthProvider):
        self._modrinth = modrinth

    async def download_files(self, job: Job, index: PackIndex, paths: set[str],
                             workdir: Path) -> dict[str, Path]:
        """Download the pack's server files at `paths`, verified. Server path -> local file."""
        wanted = [f for f in index.server_files + index.client_files if f.path in paths]
        files: dict[str, Path] = {}
        job.begin("Downloading mods", total=len(wanted))
        limit = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

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
            check_hashes(pack_file.path, pack_file.sha1, pack_file.sha512, actual)
            files[pack_file.path] = target
            job.done += 1

        try:  # the first failure cancels the other downloads
            async with asyncio.TaskGroup() as group:
                for pack_file in wanted:
                    group.create_task(fetch(pack_file))
        except ExceptionGroup as group_error:
            raise group_error.exceptions[0] from None
        return files

    async def extract_overrides(self, job: Job, index: PackIndex, pack_path: Path,
                                paths: set[str], workdir: Path) -> dict[str, Path]:
        """Unpack the pack's overrides at `paths`. Server path -> local file."""
        wanted = {r: e for r, e in index.overrides.items() if r in paths}
        job.begin("Unpacking overrides", total=len(wanted))
        files: dict[str, Path] = {}

        def extract() -> None:
            with zipfile.ZipFile(pack_path) as archive:
                for relative, entry in wanted.items():
                    target = workdir / "overrides" / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as source, target.open("wb") as handle:
                        shutil.copyfileobj(source, handle)
                    files[relative] = target
                    job.done += 1

        await asyncio.to_thread(extract)
        return files

    async def fetch_files(self, job: Job, index: PackIndex, pack_path: Path,
                          paths: set[str], workdir: Path) -> dict[str, Path]:
        """The pack's files at `paths`, from downloads or overrides (overrides win)."""
        downloads = await self.download_files(
            job, index, {p for p in paths if p not in index.overrides}, workdir)
        return {**downloads,
                **await self.extract_overrides(job, index, pack_path, paths, workdir)}

    async def fetch(self, job: Job, pack_file: dict[str, Any],
                    workdir: Path) -> tuple[Path, PackIndex]:
        """Download a version's .mrpack (hash-checked) and read its index."""
        job.begin("Downloading the modpack")
        pack_path = workdir / "pack.mrpack"
        actual = await self._modrinth.download_to_file(pack_file["url"], pack_path,
                                                       MAX_PACK_BYTES)
        hashes = pack_file.get("hashes") or {}
        check_hashes(pack_file.get("filename", "The modpack"), hashes.get("sha1"),
                     hashes.get("sha512"), actual)
        return pack_path, await asyncio.to_thread(read_index, pack_path)

    async def file_hashes(self, index: PackIndex, pack_path: Path) -> dict[str, str | None]:
        """Server path -> SHA-1 of every file the pack puts on a server (overrides win)."""
        hashes: dict[str, str | None] = {f.path: f.sha1 for f in index.server_files}

        def hash_overrides() -> None:
            with zipfile.ZipFile(pack_path) as archive:
                for relative, entry in index.overrides.items():
                    digest = hashlib.sha1()
                    with archive.open(entry) as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            digest.update(chunk)
                    hashes[relative] = digest.hexdigest()

        await asyncio.to_thread(hash_overrides)
        return hashes

    async def drop_client_only(self, job: Job, index: PackIndex,
                               hashes: dict[str, str | None]) -> dict[str, str | None]:
        """The pack files without jars that cannot run on a server.

        The index's `env` is optional and many packs omit it, which the spec reads as
        "needed everywhere"; Modrinth's project data is the reliable source. Both can call a
        library client-only although server mods require it (Prominence II marks athena so),
        so a client-only jar that a kept mod requires on Modrinth is kept, transitively.
        """
        job.begin("Checking which mods run on a server")
        client_jars = {f.path: f.sha1 for f in index.client_files
                       if is_mod_jar(f.path) and f.sha1 and f.path not in hashes}
        jars = {path: sha1 for path, sha1 in hashes.items() if is_mod_jar(path) and sha1}
        versions = await self._modrinth.versions_by_sha1(
            sorted(set(jars.values()) | set(client_jars.values())))
        projects = await self._modrinth.projects(
            sorted({v["project_id"] for v in versions.values()}))

        def project_of(sha1: str) -> dict:
            return projects.get((versions.get(sha1) or {}).get("project_id"), {})

        excluded = dict(client_jars)
        excluded.update({path: sha1 for path, sha1 in jars.items()
                         if project_of(sha1).get("server_side") == "unsupported"})
        required = self._required_libraries(
            {sha1 for path, sha1 in jars.items() if path not in excluded}, excluded, versions)

        kept = {path: sha1 for path, sha1 in hashes.items()
                if path not in excluded or path in required}
        kept.update({path: client_jars[path] for path in sorted(required) if path in client_jars})
        label = {path: f"{project_of(sha1).get('title') or path} ({path})"
                 for path, sha1 in excluded.items()}
        skipped = [label[p] for p in sorted(excluded) if p in jars and p not in required]
        if skipped:
            noun = "mod" if len(skipped) == 1 else "mods"
            job.warnings.append(f"Skipped {len(skipped)} client-only {noun} (not needed on a "
                                f"server): {', '.join(skipped)}.")
        if required:
            noun, pronoun = ("mod", "it") if len(required) == 1 else ("mods", "them")
            job.warnings.append(
                f"Kept {len(required)} {noun} marked client-only because server mods require "
                f"{pronoun}: {', '.join(label[p] for p in required)}.")
        return kept

    @staticmethod
    def _required_libraries(kept_sha1s: set[str], excluded: dict[str, str],
                            versions: dict[str, dict]) -> list[str]:
        """Excluded jars a kept jar requires on Modrinth, directly or through each other."""
        project_by_version = {v.get("id"): v.get("project_id") for v in versions.values()}
        excluded_by_project: dict[str, list[str]] = {}
        for path, sha1 in sorted(excluded.items()):
            if sha1 in versions:
                excluded_by_project.setdefault(versions[sha1]["project_id"], []).append(path)
        required: list[str] = []
        pending = sorted(kept_sha1s)
        while pending:
            for dependency in (versions.get(pending.pop(0)) or {}).get("dependencies") or []:
                if dependency.get("dependency_type") != "required":
                    continue
                project_id = (dependency.get("project_id")
                              or project_by_version.get(dependency.get("version_id")))
                for path in excluded_by_project.pop(project_id, []):
                    required.append(path)
                    pending.append(excluded[path])
        return required
