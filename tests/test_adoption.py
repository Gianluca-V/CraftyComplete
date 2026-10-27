import hashlib
import html
import json
from urllib.parse import unquote

import httpx
import pytest

from crafty_complete.crafty.client import PERMISSION_PROBE_PATH
from tests.conftest import CRAFTY, USER_TOKEN, log_in, make_client

SERVER = "abc-123"

FILES = {
    "mods/fabric-api.jar": b"fabric-api-0.83.0",
    "mods/sodium.jar": b"sodium-0.5.0",
    "mods/custom.jar": b"my-own-build",
    "mods/readme.txt": b"not a mod",
}
SUBDIRS = {"": ["mods", "config"], "mods": ["disabled"]}


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


FABRIC_SHA = sha1(FILES["mods/fabric-api.jar"])
SODIUM_SHA = sha1(FILES["mods/sodium.jar"])

VERSIONS = {
    FABRIC_SHA: {"id": "fa-1", "project_id": "P1", "version_number": "0.83.0",
                 "loaders": ["fabric"], "game_versions": ["1.20.1"]},
    SODIUM_SHA: {"id": "so-1", "project_id": "P2", "version_number": "0.5.0",
                 "loaders": ["fabric", "quilt"], "game_versions": ["1.20", "1.20.1"]},
}
LATEST = {
    FABRIC_SHA: {"id": "fa-2", "project_id": "P1", "version_number": "0.92.12"},
    SODIUM_SHA: {"id": "so-1", "project_id": "P2", "version_number": "0.5.0"},
}
PROJECTS = [
    {"id": "P1", "slug": "fabric-api", "title": "Fabric API", "icon_url": "https://cdn/p1.png",
     "client_side": "optional", "server_side": "optional", "project_type": "mod"},
    {"id": "P2", "slug": "sodium", "title": "Sodium", "icon_url": None,
     "client_side": "required", "server_side": "unsupported", "project_type": "mod"},
]


class World:
    """Fake Crafty (files API) and fake Modrinth (version lookups) in one transport."""

    def __init__(self, files=None, subdirs=None):
        self.files = dict(FILES if files is None else files)
        self.subdirs = SUBDIRS if subdirs is None else subdirs
        self.requests: list[httpx.Request] = []
        self.files_error: tuple[int, str] | None = None
        # Crafty lets BACKUP-only users list files but only FILES users download them.
        self.download_error: tuple[int, str] | None = None
        self.modrinth_down = False
        self.on_version_files = None

    def crafty_calls(self, kind: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "crafty.local" and kind in r.url.path
                and PERMISSION_PROBE_PATH not in r.url.path]

    def permission_probes(self) -> list[httpx.Request]:
        return [r for r in self.requests if PERMISSION_PROBE_PATH in r.url.path]

    def modrinth_calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "api.modrinth.com"
                and r.url.path == path]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.modrinth.com":
            return self._modrinth(request)
        return self._crafty(request)

    def _crafty(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != f"Bearer {USER_TOKEN}":
            return httpx.Response(403, json={"status": "error", "error": "ACCESS_DENIED"})
        prefix = f"/api/v2/servers/{SERVER}/files"
        if not request.url.path.startswith(prefix):
            raise AssertionError(f"unexpected Crafty call {request.url.path}")
        if self.files_error:
            status, error = self.files_error
            return httpx.Response(status, json={"status": "error", "error": error})

        if request.method == "POST" and request.url.path == prefix:
            folder = json.loads(request.content)["path"]
            data = {"root_path": {"local_path": folder, "top": folder == ""}}
            for name in self.subdirs.get(folder, []):
                data[name] = {"path": f"{folder}/{name}".lstrip("/"), "dir": True}
            for path in self.files:
                parent, _, name = path.rpartition("/")
                if parent == folder:
                    data[name] = {"path": path, "dir": False}
            return httpx.Response(200, json={"status": "ok", "data": data})

        if request.method == "GET" and request.url.path.endswith("/download"):
            if self.download_error:
                status, error = self.download_error
                return httpx.Response(status, json={"status": "error", "error": error})
            raw = request.url.raw_path.decode().split("?")[0]
            encoded = raw[len(prefix) + 1:-len("/download")]
            assert "/" not in encoded, "file path must be URL-encoded as one segment"
            # Crafty URL-decodes the path argument, then applies html.unescape to it.
            path = html.unescape(unquote(encoded))
            if path not in self.files:
                return httpx.Response(404, json={"status": "error", "error": "File not found"})
            return httpx.Response(200, content=self.files[path])
        raise AssertionError(f"unexpected Crafty call {request.method} {request.url.path}")

    def _modrinth(self, request: httpx.Request) -> httpx.Response:
        if self.modrinth_down:
            return httpx.Response(503)
        path = request.url.path
        if path == "/v2/version_files":
            if self.on_version_files:
                self.on_version_files()
            hashes = json.loads(request.content)["hashes"]
            return httpx.Response(200, json={h: VERSIONS[h] for h in hashes if h in VERSIONS})
        if path == "/v2/version_files/update":
            hashes = json.loads(request.content)["hashes"]
            return httpx.Response(200, json={h: LATEST[h] for h in hashes if h in LATEST})
        if path == "/v2/projects":
            ids = json.loads(request.url.params["ids"])
            return httpx.Response(200, json=[p for p in PROJECTS if p["id"] in ids])
        raise AssertionError(f"unexpected Modrinth call {path}")


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def db(tmp_path) -> str:
    return str(tmp_path / "crafty_complete.sqlite3")


def client_for(world: World, db: str, login: bool = True):
    client = make_client(world, crafty_url=CRAFTY, database_path=db)
    client.__enter__()
    if login:
        log_in(client)
    return client


def scan(client, **body):
    return client.post(f"/api/servers/{SERVER}/scan", json=body)


def by_file(body: dict) -> dict[str, dict]:
    return {mod["file_name"]: mod for mod in body["mods"]}


# --- adopting -----------------------------------------------------------------

def test_scan_identifies_mods_and_flags_updates(world, db):
    client = client_for(world, db)
    response = scan(client)
    client.__exit__(None, None, None)

    assert response.status_code == 200
    body = response.json()
    assert body["server_id"] == SERVER
    assert body["adopted"] is True
    assert body["loader"] == "fabric"
    assert body["game_version"] == "1.20.1"
    assert body["scanned_at"]
    assert body["counts"] == {"identified": 2, "unknown": 1, "updates": 1, "pinned": 0}

    mods = by_file(body)
    assert list(mods) == ["custom.jar", "fabric-api.jar", "sodium.jar"]
    assert mods["fabric-api.jar"] == {
        "file_name": "fabric-api.jar",
        "sha1": FABRIC_SHA,
        "state": "identified",
        "project_id": "P1",
        "slug": "fabric-api",
        "title": "Fabric API",
        "icon_url": "https://cdn/p1.png",
        "version_id": "fa-1",
        "version_number": "0.83.0",
        "server_side": "optional",
        "latest_version_id": "fa-2",
        "latest_version_number": "0.92.12",
        "update_available": True,
        "pinned": False,
    }
    assert mods["sodium.jar"]["update_available"] is False
    assert mods["sodium.jar"]["server_side"] == "unsupported"
    assert mods["custom.jar"]["state"] == "unknown"
    assert mods["custom.jar"]["title"] is None
    assert mods["custom.jar"]["update_available"] is False


def test_scan_reads_files_through_crafty_with_the_users_token(world, db):
    client = client_for(world, db)
    scan(client)
    client.__exit__(None, None, None)

    crafty = [r for r in world.requests if r.url.host == "crafty.local"]
    assert all(r.headers["Authorization"] == f"Bearer {USER_TOKEN}" for r in crafty)
    listed = [json.loads(r.content)["path"] for r in world.crafty_calls("/files")
              if r.method == "POST"]
    assert listed == ["", "mods"]
    downloads = sorted(r.url.raw_path.decode() for r in world.crafty_calls("/download"))
    assert downloads == [
        f"/api/v2/servers/{SERVER}/files/mods%2Fcustom.jar/download",
        f"/api/v2/servers/{SERVER}/files/mods%2Ffabric-api.jar/download",
        f"/api/v2/servers/{SERVER}/files/mods%2Fsodium.jar/download",
    ]


def test_scan_checks_updates_for_the_inferred_loader_and_version(world, db):
    client = client_for(world, db)
    scan(client)
    client.__exit__(None, None, None)

    update = json.loads(world.modrinth_calls("/v2/version_files/update")[0].content)
    assert update["algorithm"] == "sha1"
    assert sorted(update["hashes"]) == sorted([FABRIC_SHA, SODIUM_SHA])
    assert update["loaders"] == ["fabric"]
    assert update["game_versions"] == ["1.20.1"]


def test_scan_accepts_loader_and_version_overrides(world, db):
    client = client_for(world, db)
    response = scan(client, loader="quilt", game_version="1.20")
    client.__exit__(None, None, None)

    assert response.json()["loader"] == "quilt"
    assert response.json()["game_version"] == "1.20"
    update = json.loads(world.modrinth_calls("/v2/version_files/update")[0].content)
    assert update["loaders"] == ["quilt"]
    assert update["game_versions"] == ["1.20"]


def test_rescan_keeps_the_previous_loader_and_version(world, db):
    client = client_for(world, db)
    scan(client, loader="quilt", game_version="1.20")
    response = scan(client)
    client.__exit__(None, None, None)

    assert response.json()["loader"] == "quilt"
    assert response.json()["game_version"] == "1.20"


@pytest.mark.parametrize("body", [{"loader": "bukkit"}, {"game_version": "x" * 65}])
def test_scan_rejects_invalid_overrides(body, world, db):
    client = client_for(world, db)
    response = scan(client, **body)
    client.__exit__(None, None, None)

    assert response.status_code == 422
    assert world.requests == []


def test_scan_without_identified_mods_skips_update_check(db):
    world = World(files={"mods/custom.jar": b"my-own-build"})
    client = client_for(world, db)
    body = scan(client).json()
    client.__exit__(None, None, None)

    assert body["loader"] is None
    assert body["game_version"] is None
    assert body["counts"] == {"identified": 0, "unknown": 1, "updates": 0, "pinned": 0}
    assert world.modrinth_calls("/v2/version_files/update") == []


def test_scan_reports_server_without_mods_folder(db):
    world = World(files={}, subdirs={"": ["world"]})
    client = client_for(world, db)
    response = scan(client)
    client.__exit__(None, None, None)

    assert response.status_code == 404
    assert response.json() == {"detail": "This server has no mods folder."}


# --- persistence and pinning ---------------------------------------------------

def test_adoption_is_persisted_without_rescanning(world, db):
    first = client_for(world, db)
    scanned = scan(first).json()
    first.__exit__(None, None, None)

    world.requests.clear()
    second = client_for(world, db)
    stored = second.get(f"/api/servers/{SERVER}/mods")
    second.__exit__(None, None, None)

    assert stored.status_code == 200
    assert stored.json() == scanned
    assert world.crafty_calls("/download") == []
    assert len(world.permission_probes()) == 1
    assert [r for r in world.requests if r.url.host == "api.modrinth.com"] == []


def test_unadopted_server_reports_no_mods(world, db):
    client = client_for(world, db)
    response = client.get(f"/api/servers/{SERVER}/mods")
    client.__exit__(None, None, None)

    assert response.status_code == 200
    assert response.json() == {"server_id": SERVER, "adopted": False, "loader": None,
                               "game_version": None, "scanned_at": None, "mods": [],
                               "counts": {"identified": 0, "unknown": 0, "updates": 0,
                                          "pinned": 0}}


def test_pinned_mod_is_excluded_from_updates_and_survives_rescan(world, db):
    client = client_for(world, db)
    scan(client)
    pinned = client.patch(f"/api/servers/{SERVER}/mods/fabric-api.jar", json={"pinned": True})
    rescanned = scan(client).json()
    client.__exit__(None, None, None)

    assert pinned.status_code == 200
    assert pinned.json()["pinned"] is True
    mod = by_file(rescanned)["fabric-api.jar"]
    assert mod["pinned"] is True
    assert mod["update_available"] is True
    assert rescanned["counts"]["updates"] == 0
    assert rescanned["counts"]["pinned"] == 1


def test_unpinning_restores_the_update_count(world, db):
    client = client_for(world, db)
    scan(client)
    client.patch(f"/api/servers/{SERVER}/mods/fabric-api.jar", json={"pinned": True})
    client.patch(f"/api/servers/{SERVER}/mods/fabric-api.jar", json={"pinned": False})
    body = client.get(f"/api/servers/{SERVER}/mods").json()
    client.__exit__(None, None, None)

    assert body["counts"]["updates"] == 1
    assert by_file(body)["fabric-api.jar"]["pinned"] is False


def test_pinning_unknown_file_is_not_found(world, db):
    client = client_for(world, db)
    scan(client)
    response = client.patch(f"/api/servers/{SERVER}/mods/nope.jar", json={"pinned": True})
    client.__exit__(None, None, None)

    assert response.status_code == 404
    assert response.json() == {"detail": "No adopted mod with that file name."}


def test_removed_jar_disappears_on_rescan(world, db):
    client = client_for(world, db)
    scan(client)
    del world.files["mods/custom.jar"]
    body = scan(client).json()
    client.__exit__(None, None, None)

    assert "custom.jar" not in by_file(body)


# --- authorization and failures ---------------------------------------------------

@pytest.mark.parametrize("call", ["scan", "get", "pin"])
def test_missing_files_permission_is_forbidden_but_keeps_session(call, world, db):
    client = client_for(world, db)
    scan(client)
    world.files_error = (400, "NOT_AUTHORIZED")
    if call == "scan":
        response = scan(client)
    elif call == "get":
        response = client.get(f"/api/servers/{SERVER}/mods")
    else:
        response = client.patch(f"/api/servers/{SERVER}/mods/sodium.jar", json={"pinned": True})
    sessions_left = client.app.state.sessions.count()
    client.__exit__(None, None, None)

    assert response.status_code == 403
    assert response.json() == {
        "detail": "You do not have file access to this server in Crafty."
    }
    assert sessions_left == 1


@pytest.mark.parametrize("call", ["scan", "get", "pin"])
def test_backup_only_user_cannot_use_adoption(call, world, db):
    """Crafty lets BACKUP-only users list files; adoption needs FILES (download) access."""
    client = client_for(world, db)
    scan(client)
    world.download_error = (400, "NOT_AUTHORIZED")
    if call == "scan":
        response = scan(client)
    elif call == "get":
        response = client.get(f"/api/servers/{SERVER}/mods")
    else:
        response = client.patch(f"/api/servers/{SERVER}/mods/sodium.jar", json={"pinned": True})
    stored = client.app.state.adoption_store.get(SERVER)
    client.__exit__(None, None, None)

    assert response.status_code == 403
    assert response.json() == {
        "detail": "You do not have file access to this server in Crafty."
    }
    assert "fabric-api.jar" not in response.text
    assert not any(mod.pinned for mod in stored.mods)


def test_backup_only_user_cannot_adopt_an_empty_mods_folder(db):
    world = World(files={}, subdirs={"": ["mods"]})
    world.download_error = (400, "NOT_AUTHORIZED")
    client = client_for(world, db)
    response = scan(client)
    stored = client.app.state.adoption_store.get(SERVER)
    client.__exit__(None, None, None)

    assert response.status_code == 403
    assert stored.adopted is False


def test_scan_handles_html_entities_in_file_names(db):
    """Crafty html-unescapes download paths, so `&` must reach it as `&amp;`."""
    world = World(files={"mods/rock&stone.jar": b"rock", "mods/a&amp;b.jar": b"amp"})
    client = client_for(world, db)
    response = scan(client)
    client.__exit__(None, None, None)

    assert response.status_code == 200
    assert by_file(response.json())["rock&stone.jar"]["sha1"] == sha1(b"rock")
    assert by_file(response.json())["a&amp;b.jar"]["sha1"] == sha1(b"amp")


def test_pin_made_during_a_scan_is_kept(world, db):
    client = client_for(world, db)
    scan(client)
    store = client.app.state.adoption_store
    world.on_version_files = lambda: store.set_pinned(SERVER, "sodium.jar", True)
    body = scan(client).json()
    stored = store.get(SERVER).to_dict()
    client.__exit__(None, None, None)

    assert by_file(body)["sodium.jar"]["pinned"] is True
    assert by_file(stored)["sodium.jar"]["pinned"] is True


def test_forbidden_user_cannot_read_stored_adoption(world, db):
    client = client_for(world, db)
    scan(client)
    world.files_error = (400, "NOT_AUTHORIZED")
    response = client.get(f"/api/servers/{SERVER}/mods")
    client.__exit__(None, None, None)

    assert "fabric-api.jar" not in response.text


def test_revoked_token_ends_session(world, db):
    client = client_for(world, db)
    world.files_error = (403, "ACCESS_DENIED")
    response = scan(client)
    sessions_left = client.app.state.sessions.count()
    client.__exit__(None, None, None)

    assert response.status_code == 401
    assert sessions_left == 0


def test_modrinth_failure_aborts_scan_without_saving(world, db):
    client = client_for(world, db)
    world.modrinth_down = True
    response = scan(client)
    world.modrinth_down = False
    stored = client.get(f"/api/servers/{SERVER}/mods").json()
    client.__exit__(None, None, None)

    assert response.status_code == 502
    assert response.json()["detail"].startswith("Modrinth request failed")
    assert stored["adopted"] is False


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", f"/api/servers/{SERVER}/scan"), ("GET", f"/api/servers/{SERVER}/mods"),
     ("PATCH", f"/api/servers/{SERVER}/mods/x.jar")],
)
def test_adoption_api_requires_session(method, path, world, db):
    client = client_for(world, db, login=False)
    response = client.request(method, path, json={"pinned": True})
    client.__exit__(None, None, None)

    assert response.status_code == 401
    assert world.requests == []


def test_server_id_must_look_like_a_crafty_id(world, db):
    client = client_for(world, db)
    response = client.get("/api/servers/..%2F..%2Fetc/mods")
    client.__exit__(None, None, None)

    assert response.status_code in (404, 422)
    assert world.requests == []


# --- pages ------------------------------------------------------------------------

def test_server_pages_render_for_logged_in_users(world, db):
    client = client_for(world, db)
    servers_page = client.get("/servers")
    detail_page = client.get(f"/servers/{SERVER}")
    client.__exit__(None, None, None)

    assert servers_page.status_code == 200
    assert 'id="server-list"' in servers_page.text
    assert detail_page.status_code == 200
    assert 'id="mod-table"' in detail_page.text
    assert f'data-server-id="{SERVER}"' in detail_page.text


def test_server_pages_redirect_to_login(world, db):
    client = client_for(world, db, login=False)
    response = client.get(f"/servers/{SERVER}", follow_redirects=False)
    client.__exit__(None, None, None)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
