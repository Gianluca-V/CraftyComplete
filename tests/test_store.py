import sqlite3

from crafty_complete.mods.store import SCHEMA_VERSION, AdoptionStore

# The schema written by version 1 (T4), before `updates_checked_at` existed.
V1_SCHEMA = """
CREATE TABLE adopted_servers (
    server_id TEXT PRIMARY KEY, loader TEXT, game_version TEXT, scanned_at TEXT NOT NULL
);
CREATE TABLE server_mods (
    server_id TEXT NOT NULL REFERENCES adopted_servers(server_id) ON DELETE CASCADE,
    file_name TEXT NOT NULL, sha1 TEXT NOT NULL, state TEXT NOT NULL, project_id TEXT,
    slug TEXT, title TEXT, icon_url TEXT, version_id TEXT, version_number TEXT,
    server_side TEXT, latest_version_id TEXT, latest_version_number TEXT,
    pinned INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (server_id, file_name)
);
PRAGMA user_version = 1;
"""


def test_version_1_database_is_migrated_in_place(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(V1_SCHEMA)
    db.execute("INSERT INTO adopted_servers VALUES ('abc', 'fabric', '1.20.1', '2026-10-01')")
    db.execute(
        "INSERT INTO server_mods (server_id, file_name, sha1, state, project_id, version_id, "
        "pinned) VALUES ('abc', 'a.jar', 'deadbeef', 'identified', 'P1', 'v1', 1)"
    )
    db.commit()
    db.close()

    store = AdoptionStore(str(path))
    server = store.get("abc")
    store.set_latest_versions("abc", {"a.jar": ("v2", "2.0")}, "2026-10-27T00:00:00+00:00")
    refreshed = store.get("abc")
    store.close()

    assert server.loader == "fabric"
    assert server.updates_checked_at is None
    assert server.mods[0].pinned is True
    assert server.mods[0].dependency is False
    assert refreshed.updates_checked_at == "2026-10-27T00:00:00+00:00"
    assert refreshed.mods[0].latest_version_id == "v2"
    version = sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION


def test_reopening_a_current_database_changes_nothing(tmp_path):
    path = str(tmp_path / "db.sqlite3")
    AdoptionStore(path).close()
    store = AdoptionStore(path)
    assert store.server_ids() == []
    store.close()


def test_version_2_database_gains_the_modpack_link(tmp_path):
    path = tmp_path / "v2.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(V1_SCHEMA.replace("PRAGMA user_version = 1;", ""))
    db.execute("ALTER TABLE adopted_servers ADD COLUMN updates_checked_at TEXT")
    db.execute("PRAGMA user_version = 2")
    db.execute("INSERT INTO adopted_servers VALUES ('abc', 'forge', '1.20.1', 't0', 't1')")
    db.commit()
    db.close()

    store = AdoptionStore(str(path))
    server = store.get("abc")
    store.set_modpack_files("abc", {"mods/a.jar": "deadbeef"})
    pack_files = store.get_modpack_files("abc")
    store.close()

    assert pack_files == {"mods/a.jar": "deadbeef"}
    assert server.updates_checked_at == "t1"
    assert server.modpack_project_id is None
    assert server.to_dict()["modpack"] is None
