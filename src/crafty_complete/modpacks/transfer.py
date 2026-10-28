"""Writing pack files to a Crafty server: shared by server creation and modpack updates."""

import asyncio
from pathlib import Path, PurePosixPath

from crafty_complete.config import Settings
from crafty_complete.crafty.client import CraftyClient, CraftyError
from crafty_complete.modpacks.jobs import Job

RETRIES = 3
TRANSFER_CONCURRENCY = 4


async def gather(coroutines) -> None:
    """Run concurrently; the first failure cancels the rest and is re-raised as is."""
    try:
        async with asyncio.TaskGroup() as group:
            for coroutine in coroutines:
                group.create_task(coroutine)
    except ExceptionGroup as group_error:
        raise group_error.exceptions[0] from None


def parent_of(relative: str) -> str:
    parent = PurePosixPath(relative).parent
    return str(parent) if parent.parts else ""


class ServerWriter:
    def __init__(self, crafty: CraftyClient, settings: Settings):
        self._crafty = crafty
        self._settings = settings

    async def retry(self, operation):
        """Run a Crafty call, retrying network errors and 5xx-style failures.

        Never used for creating a server, which must not happen twice.
        """
        for attempt in range(1, RETRIES + 1):
            try:
                return await operation()
            except CraftyError as exc:
                if exc.state not in ("unreachable", "error") or attempt == RETRIES:
                    raise
            await asyncio.sleep(self._settings.crafty_retry_seconds * attempt)

    async def create_folders(self, job: Job, token: str, server_id: str,
                             folders: set[str]) -> None:
        """Create folders (and their parents), parents first; existing ones are fine.

        Crafty's upload cannot create folders and its folder creation is not recursive.
        """
        wanted = {parent for folder in folders if folder
                  for parent in (PurePosixPath(folder), *PurePosixPath(folder).parents)
                  if parent.parts}
        job.begin("Creating folders", total=len(wanted))
        for folder in sorted(wanted, key=lambda f: (len(f.parts), str(f))):
            parent = str(folder.parent) if folder.parent.parts else ""
            await self.retry(lambda: self._crafty.create_folder(
                token, server_id, parent, folder.name))
            job.done += 1

    async def upload(self, job: Job, token: str, server_id: str,
                     files: dict[str, Path]) -> None:
        """Upload server path -> local file; their folders must exist."""
        job.begin("Uploading files to the server", total=len(files))
        limit = asyncio.Semaphore(TRANSFER_CONCURRENCY)

        async def send(relative: str, source: Path) -> None:
            async with limit:
                await self.retry(lambda: self._crafty.upload_path(
                    token, server_id, parent_of(relative), PurePosixPath(relative).name,
                    source, self._settings.upload_chunk_bytes))
            job.done += 1

        await gather(send(r, s) for r, s in files.items())

    async def move(self, token: str, server_id: str, source: str, target_folder: str) -> None:
        try:
            await self.retry(lambda: self._crafty.move_file(token, server_id, source,
                                                            target_folder))
        except CraftyError as exc:
            # A retry after an answer that was lost finds the source already gone.
            if exc.state == "unauthorized" or not await self._moved(
                    token, server_id, source, target_folder):
                raise

    async def _moved(self, token: str, server_id: str, source: str, target_folder: str) -> bool:
        name = PurePosixPath(source).name
        try:
            in_source = await self.retry(
                lambda: self._crafty.list_dir(token, server_id, parent_of(source)))
            in_target = await self.retry(
                lambda: self._crafty.list_dir(token, server_id, target_folder))
        except CraftyError:
            return False
        return (all(e.name != name for e in in_source)
                and any(e.name == name and not e.is_dir for e in in_target))

    async def file_names(self, token: str, server_id: str, folder: str) -> set[str]:
        """Server paths of the files in `folder`, built from the names Crafty lists."""
        entries = await self.retry(lambda: self._crafty.list_dir(token, server_id, folder))
        return {f"{folder}/{e.name}" if folder else e.name for e in entries if not e.is_dir}
