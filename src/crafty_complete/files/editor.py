import hashlib
import re
from typing import Any

from crafty_complete.crafty.client import CraftyClient, CraftyError
from crafty_complete.mods.locks import ServerLocks

FILES_FORBIDDEN = "You do not have file access to this server in Crafty."
MAX_TEXT_BYTES = 1024 * 1024
MAX_PATH_LENGTH = 512
# Server-relative paths only: no control characters or backslashes.
UNSAFE_PATH = re.compile(r"[\x00-\x1f\x7f\\]")


class FileError(Exception):
    """A refusal or failure the user should read, with its HTTP status."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def check_path(path: str, folder: bool = False) -> str:
    """`path` if it is a clean server-relative path; "" (the root) only for a folder."""
    if path == "" and folder:
        return path
    parts = path.split("/")
    if (not path or len(path) > MAX_PATH_LENGTH or UNSAFE_PATH.search(path)
            or any(part in ("", ".", "..") for part in parts)):
        raise FileError(422, f"{path[:80]!r} is not a valid path inside the server folder.")
    return path


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(path: str, data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is None or "\x00" in text:
        raise FileError(415, f"{path} is not a UTF-8 text file, so it cannot be edited here.")
    return text


def _too_large(path: str) -> FileError:
    return FileError(413, f"{path} is larger than 1 MiB, so it cannot be edited here.")


class FileEditor:
    """Browse a server's folders and edit its text files, shared by the web routes and the MCP."""

    def __init__(self, crafty: CraftyClient, locks: ServerLocks):
        self._crafty = crafty
        self._locks = locks

    async def _call(self, operation):
        try:
            return await operation
        except CraftyError as exc:
            if exc.state == "forbidden":
                raise FileError(403, FILES_FORBIDDEN) from exc
            raise

    async def _read(self, token: str, server_id: str, path: str) -> bytes | None:
        try:
            # read_file stops downloading as soon as the file passes max_bytes.
            return await self._call(self._crafty.read_file(token, server_id, path,
                                                           max_bytes=MAX_TEXT_BYTES))
        except CraftyError as exc:
            if exc.state == "error" and "is larger than" in exc.message:
                raise _too_large(path) from exc
            raise

    async def list(self, token: str, server_id: str, path: str = "") -> dict[str, Any]:
        path = check_path(path, folder=True)
        entries = await self._call(self._crafty.list_dir(token, server_id, path))
        prefix = f"{path}/" if path else ""
        listed = [{"name": e.name, "path": prefix + e.name, "is_dir": e.is_dir}
                  for e in entries if e.name and "/" not in e.name]
        listed.sort(key=lambda e: (not e["is_dir"], e["name"].casefold()))
        return {"path": path, "entries": listed}

    async def read(self, token: str, server_id: str, path: str) -> dict[str, Any]:
        path = check_path(path)
        data = await self._read(token, server_id, path)
        if data is None:
            raise FileError(404, f"{path} does not exist on the server.")
        return {"path": path, "content": _text(path, data), "sha256": _digest(data),
                "size": len(data)}

    async def write(self, token: str, server_id: str, path: str, content: str,
                    sha256: str) -> dict[str, Any]:
        """Replace a text file, unless it changed since it was read with hash `sha256`."""
        path = check_path(path)
        if "\x00" in content:
            raise FileError(422, "The content must be text without NUL characters.")
        async with self._locks(server_id):
            current = await self._read(token, server_id, path)
            if current is None:
                raise FileError(409, f"{path} no longer exists on the server.")
            _text(path, current)
            if _digest(current) != sha256:
                raise FileError(409, f"{path} changed on the server since you opened it. "
                                     "Open it again to see the new content.")
            # Browsers send textarea content with "\n" only; keep the file's line endings.
            if b"\r\n" in current:
                content = content.replace("\r\n", "\n").replace("\n", "\r\n")
            data = content.encode("utf-8")
            if len(data) > MAX_TEXT_BYTES:
                raise _too_large(path)
            folder, _, name = path.rpartition("/")
            await self._call(self._crafty.upload_file(token, server_id, folder, name, data))
        return {"path": path, "sha256": _digest(data), "size": len(data)}
