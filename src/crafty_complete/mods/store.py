import sqlite3
import threading
from dataclasses import fields
from pathlib import Path

from crafty_complete.mods.models import AdoptedServer, InstalledMod

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS adopted_servers (
    server_id    TEXT PRIMARY KEY,
    loader       TEXT,
    game_version TEXT,
    scanned_at   TEXT NOT NULL
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
"""

MOD_COLUMNS = [f.name for f in fields(InstalledMod)]


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
            self._db.executescript(SCHEMA)
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self._db.close()

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
        mods = [
            InstalledMod(**{**{c: r[c] for c in MOD_COLUMNS}, "pinned": bool(r["pinned"])})
            for r in mod_rows
        ]
        return AdoptedServer(
            server_id=server_id, loader=row["loader"], game_version=row["game_version"],
            scanned_at=row["scanned_at"], mods=mods,
        )

    def save(self, server: AdoptedServer) -> None:
        """Replace everything stored for this server in one transaction."""
        placeholders = ", ".join("?" for _ in MOD_COLUMNS)
        with self._lock, self._db:
            self._db.execute("DELETE FROM server_mods WHERE server_id = ?", (server.server_id,))
            self._db.execute(
                "INSERT INTO adopted_servers (server_id, loader, game_version, scanned_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(server_id) DO UPDATE SET "
                "loader = excluded.loader, game_version = excluded.game_version, "
                "scanned_at = excluded.scanned_at",
                (server.server_id, server.loader, server.game_version, server.scanned_at),
            )
            self._db.executemany(
                f"INSERT INTO server_mods (server_id, {', '.join(MOD_COLUMNS)}) "
                f"VALUES (?, {placeholders})",
                [(server.server_id, *(getattr(mod, c) for c in MOD_COLUMNS))
                 for mod in server.mods],
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
