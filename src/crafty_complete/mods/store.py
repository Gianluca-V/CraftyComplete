import sqlite3
import threading
from dataclasses import fields
from pathlib import Path

from crafty_complete.mods.models import AdoptedServer, InstalledMod

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS adopted_servers (
    server_id          TEXT PRIMARY KEY,
    loader             TEXT,
    game_version       TEXT,
    scanned_at         TEXT NOT NULL,
    updates_checked_at TEXT,
    -- The Modrinth modpack version the server was created from, if any.
    modpack_project_id     TEXT,
    modpack_title          TEXT,
    modpack_version_id     TEXT,
    modpack_version_number TEXT
);
CREATE TABLE IF NOT EXISTS server_mods (
    server_id             TEXT NOT NULL REFERENCES adopted_servers(server_id) ON DELETE CASCADE,
    file_name             TEXT NOT NULL,
    sha1                  TEXT NOT NULL,
    state                 TEXT NOT NULL,
    project_id            TEXT,
    slug                  TEXT,
    title                 TEXT,
    icon_url              TEXT,
    version_id            TEXT,
    version_number        TEXT,
    server_side           TEXT,
    latest_version_id     TEXT,
    latest_version_number TEXT,
    pinned                INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (server_id, file_name)
);
-- The files the linked modpack version put on the server (path relative to the server
-- folder -> SHA-1), so an update can tell pack files from the user's own.
CREATE TABLE IF NOT EXISTS modpack_files (
    server_id TEXT NOT NULL REFERENCES adopted_servers(server_id) ON DELETE CASCADE,
    path      TEXT NOT NULL,
    sha1      TEXT,
    PRIMARY KEY (server_id, path)
);
"""

# Applied in order to databases created by older versions (index = version it upgrades from).
MIGRATIONS = {
    1: ["ALTER TABLE adopted_servers ADD COLUMN updates_checked_at TEXT"],
    2: [f"ALTER TABLE adopted_servers ADD COLUMN {column} TEXT" for column in (
        "modpack_project_id", "modpack_title", "modpack_version_id", "modpack_version_number")],
    3: [],  # adds the modpack_files table, created by SCHEMA
}

SERVER_COLUMNS = ["server_id", "loader", "game_version", "scanned_at", "updates_checked_at",
                  "modpack_project_id", "modpack_title", "modpack_version_id",
                  "modpack_version_number"]

MOD_COLUMNS = [f.name for f in fields(InstalledMod)]


def _mod(row: sqlite3.Row) -> InstalledMod:
    return InstalledMod(**{**{c: row[c] for c in MOD_COLUMNS}, "pinned": bool(row["pinned"])})


class AdoptionStore:
    """SQLite persistence for adopted servers and the mods found on them."""

    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute("PRAGMA foreign_keys = ON")
            current = self._db.execute("PRAGMA user_version").fetchone()[0]
            exists = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'adopted_servers'"
            ).fetchone()
            if exists:
                # A database that has tables but user_version 0 can only be a v1 file whose
                # version pragma was never written, so it migrates from 1.
                for version in range(current or 1, SCHEMA_VERSION):
                    for statement in MIGRATIONS[version]:
                        self._db.execute(statement)
            self._db.executescript(SCHEMA)
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self._db.close()

    def server_ids(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT server_id FROM adopted_servers")]

    def get(self, server_id: str) -> AdoptedServer:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM adopted_servers WHERE server_id = ?", (server_id,)
            ).fetchone()
            if row is None:
                return AdoptedServer(server_id=server_id)
            mod_rows = self._db.execute(
                "SELECT * FROM server_mods WHERE server_id = ?", (server_id,)
            ).fetchall()
        return AdoptedServer(**{c: row[c] for c in SERVER_COLUMNS},
                             mods=[_mod(r) for r in mod_rows])

    def save(self, server: AdoptedServer) -> None:
        """Replace everything stored for this server in one transaction."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM server_mods WHERE server_id = ?", (server.server_id,))
            updates = ", ".join(f"{c} = excluded.{c}" for c in SERVER_COLUMNS[1:])
            self._db.execute(
                f"INSERT INTO adopted_servers ({', '.join(SERVER_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in SERVER_COLUMNS)}) "
                f"ON CONFLICT(server_id) DO UPDATE SET {updates}",
                tuple(getattr(server, c) for c in SERVER_COLUMNS),
            )
            self._insert_mods(server.server_id, server.mods)

    def _insert_mods(self, server_id: str, mods: list[InstalledMod]) -> None:
        placeholders = ", ".join("?" for _ in MOD_COLUMNS)
        self._db.executemany(
            f"INSERT OR REPLACE INTO server_mods (server_id, {', '.join(MOD_COLUMNS)}) "
            f"VALUES (?, {placeholders})",
            [(server_id, *(getattr(mod, c) for c in MOD_COLUMNS)) for mod in mods],
        )

    def put_mod(self, server_id: str, mod: InstalledMod) -> None:
        """Insert one mod, replacing the record with the same file name if there is one."""
        with self._lock, self._db:
            self._insert_mods(server_id, [mod])

    def remove_mod(self, server_id: str, file_name: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM server_mods WHERE server_id = ? AND file_name = ?",
                (server_id, file_name),
            )

    def set_latest_versions(self, server_id: str, latest: dict[str, tuple[str, str] | None],
                            checked_at: str) -> None:
        """Record the newest compatible version per file name (None = no compatible version)."""
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE server_mods SET latest_version_id = ?, latest_version_number = ? "
                "WHERE server_id = ? AND file_name = ?",
                [(*(value or (None, None)), server_id, file_name)
                 for file_name, value in latest.items()],
            )
            self._db.execute(
                "UPDATE adopted_servers SET updates_checked_at = ? WHERE server_id = ?",
                (checked_at, server_id),
            )

    def set_modpack_link(self, server_id: str, link: dict[str, str | None]) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE adopted_servers SET modpack_project_id = ?, modpack_title = ?, "
                "modpack_version_id = ?, modpack_version_number = ? WHERE server_id = ?",
                (link["project_id"], link["title"], link["version_id"],
                 link["version_number"], server_id),
            )

    def get_modpack_files(self, server_id: str) -> dict[str, str | None]:
        with self._lock:
            rows = self._db.execute(
                "SELECT path, sha1 FROM modpack_files WHERE server_id = ?", (server_id,)
            ).fetchall()
        return {row["path"]: row["sha1"] for row in rows}

    def set_modpack_files(self, server_id: str, files: dict[str, str | None]) -> None:
        """Replace the remembered pack files of a server."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM modpack_files WHERE server_id = ?", (server_id,))
            self._db.executemany(
                "INSERT INTO modpack_files (server_id, path, sha1) VALUES (?, ?, ?)",
                [(server_id, path, sha1) for path, sha1 in files.items()],
            )

    def set_pinned(self, server_id: str, file_name: str, pinned: bool) -> InstalledMod | None:
        with self._lock, self._db:
            updated = self._db.execute(
                "UPDATE server_mods SET pinned = ? WHERE server_id = ? AND file_name = ?",
                (int(pinned), server_id, file_name),
            ).rowcount
        if not updated:
            return None
        # A concurrent scan may have dropped the jar right after the update.
        return next((m for m in self.get(server_id).mods if m.file_name == file_name), None)
