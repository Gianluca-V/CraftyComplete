"""Reading Modrinth modpacks: shared by server creation, linking and updates."""

import asyncio
import hashlib
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

    async def drop_client_only(self, job: Job,
                               hashes: dict[str, str | None]) -> dict[str, str | None]:
        """The pack files without jars whose Modrinth project cannot run on a server.

        The index's `env` is optional and many packs omit it, which the spec reads as
        "needed everywhere"; Modrinth's project data is the reliable source.
        """
        job.begin("Checking which mods run on a server")
        jars = {path: sha1 for path, sha1 in hashes.items() if is_mod_jar(path) and sha1}
        versions = await self._modrinth.versions_by_sha1(sorted(set(jars.values())))
        projects = await self._modrinth.projects(
            sorted({v["project_id"] for v in versions.values()}))
        kept = dict(hashes)
        skipped = []
        for path, sha1 in sorted(jars.items()):
            project = projects.get((versions.get(sha1) or {}).get("project_id"), {})
            if project.get("server_side") == "unsupported":
                del kept[path]
                skipped.append(f"{project.get('title') or path} ({path})")
        if skipped:
            noun = "mod" if len(skipped) == 1 else "mods"
            job.warnings.append(f"Skipped {len(skipped)} client-only {noun} (not needed on a "
                                f"server): {', '.join(skipped)}.")
        return kept
