"""Read Modrinth modpacks (.mrpack): https://support.modrinth.com/en/articles/8802351"""

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

INDEX_FILE = "modrinth.index.json"
# Later folders win: server-overrides replace files from overrides.
OVERRIDE_FOLDERS = ("overrides/", "server-overrides/")
# Hosts the .mrpack specification allows launchers to download from.
ALLOWED_DOWNLOAD_HOSTS = {"cdn.modrinth.com", "github.com", "raw.githubusercontent.com",
                          "gitlab.com"}
LOADERS = {"fabric-loader": "fabric", "forge": "forge", "neoforge": "neoforge"}
# Bound on what overrides may unpack to, so a hostile archive cannot fill the disk.
MAX_OVERRIDES_BYTES = 4 * 1024**3


class MrpackError(Exception):
    """The modpack cannot be installed; the message is meant for the user."""


@dataclass(frozen=True)
class PackFile:
    path: str  # relative to the server folder
    sha1: str | None
    sha512: str | None
    size: int
    downloads: list[str]


@dataclass(frozen=True)
class PackIndex:
    name: str
    version: str
    minecraft: str
    loader: str
    loader_version: str
    server_files: list[PackFile]
    # server-relative path -> archive entry to copy it from
    overrides: dict[str, str] = field(default_factory=dict)


def safe_relative_path(path: str) -> str:
    """Return `path` if it stays inside the server folder, else raise MrpackError."""
    parts = PurePosixPath(path).parts
    if (not path or "\\" in path or path.startswith("/") or not parts
            or ":" in parts[0] or any(part in ("..", ".") for part in parts)):
        raise MrpackError(f"The modpack contains an unsafe path: {path}")
    return str(PurePosixPath(*parts))


def _server_file(entry: dict) -> PackFile:
    path = safe_relative_path(entry.get("path", ""))
    downloads = entry.get("downloads") or []
    for url in downloads:
        host = urlsplit(url).hostname or ""
        if urlsplit(url).scheme != "https" or host not in ALLOWED_DOWNLOAD_HOSTS:
            raise MrpackError(
                f"{path} is downloaded from a host Modrinth does not allow: {host or url}")
    if not downloads:
        raise MrpackError(f"{path} has no download URL.")
    hashes = entry.get("hashes") or {}
    return PackFile(path=path, sha1=hashes.get("sha1"), sha512=hashes.get("sha512"),
                    size=int(entry.get("fileSize") or 0), downloads=downloads)


def read_index(pack_path: Path) -> PackIndex:
    try:
        with zipfile.ZipFile(pack_path) as archive:
            index = json.loads(archive.read(INDEX_FILE))
            entries = archive.namelist()
            unpacked_sizes = {info.filename: info.file_size for info in archive.infolist()}
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise MrpackError("The downloaded file is not a valid .mrpack.") from exc

    if index.get("formatVersion") != 1:
        raise MrpackError(f"Unsupported modpack format version {index.get('formatVersion')}.")
    if index.get("game") != "minecraft":
        raise MrpackError("This modpack is not for Minecraft.")

    dependencies = index.get("dependencies") or {}
    if "quilt-loader" in dependencies:
        raise MrpackError("Quilt modpacks are not supported yet.")
    found = [(key, value) for key, value in dependencies.items() if key in LOADERS]
    if len(found) != 1 or "minecraft" not in dependencies:
        raise MrpackError("The modpack does not name a Fabric, Forge or NeoForge loader.")
    loader_key, loader_version = found[0]

    server_files = [
        _server_file(entry) for entry in index.get("files") or []
        if (entry.get("env") or {}).get("server", "required") != "unsupported"
    ]
    seen: set[str] = set()
    for pack_file in server_files:
        if pack_file.path in seen:  # two downloads into one file could mix their bytes
            raise MrpackError(f"The modpack lists {pack_file.path} more than once.")
        seen.add(pack_file.path)

    overrides: dict[str, str] = {}
    for folder in OVERRIDE_FOLDERS:
        for name in entries:
            if name.startswith(folder) and not name.endswith("/"):
                try:
                    relative = safe_relative_path(name[len(folder):])
                except MrpackError:
                    raise MrpackError(f"The modpack contains an unsafe path: {name}") from None
                overrides[relative] = name

    if sum(unpacked_sizes[entry] for entry in overrides.values()) > MAX_OVERRIDES_BYTES:
        raise MrpackError(
            f"The modpack's overrides unpack to more than {MAX_OVERRIDES_BYTES} bytes.")

    return PackIndex(
        name=index.get("name") or "Modpack",
        version=index.get("versionId") or "",
        minecraft=dependencies["minecraft"],
        loader=LOADERS[loader_key],
        loader_version=loader_version,
        server_files=server_files,
        overrides=overrides,
    )
