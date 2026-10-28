import json

import httpx
import pytest

from crafty_complete.mods.models import InstalledMod
from tests.conftest import CRAFTY, log_in, make_client
from tests.test_adoption import FILES, SERVER, sha1
from tests.test_modpack import CreationFakes, build_mrpack, pack_file, wait_for
from tests.test_modpack_link import FABRIC_API

OLD_ONLY = b"old-only-mod"
FABRIC_API_NEW = b"fabric-api-0.92.12"
NEW_MOD = b"brand-new-mod"
CONFIG_V1 = b"pack-config-v1"
CONFIG_V2 = b"pack-config-v2"
SHARED_CONFIG = b"shared-config-unchanged"
USER_EDITED = b"my-own-tweaks"

PACK_V1 = build_mrpack(
    files=[pack_file("mods/fabric-api.jar", FABRIC_API), pack_file("mods/old-only.jar", OLD_ONLY)],
    entries={"overrides/config/pack.toml": CONFIG_V1,
             "overrides/config/shared.toml": SHARED_CONFIG},
)
PACK_V2 = build_mrpack(
    files=[pack_file("mods/fabric-api-0.92.12.jar", FABRIC_API_NEW),
           pack_file("mods/new-mod.jar", NEW_MOD)],
    entries={"overrides/config/pack.toml": CONFIG_V2,
             "overrides/config/shared.toml": SHARED_CONFIG},
)
V2 = "pk-2"


class UpdateFakes(CreationFakes):
    """An adopted server with folders, moves and a running flag, and two pack versions."""

    def __init__(self):
        super().__init__(pack=PACK_V1)
        self.files.update({"config/pack.toml": USER_EDITED,
                           "config/shared.toml": USER_EDITED,
                           "mods/old-only.jar": OLD_ONLY})
        self.subdirs = {"": ["mods", "config"], "mods": [], "config": []}
        self.cdn_extra.update({"pack2.mrpack": PACK_V2, "fabric-api-0.92.12.jar": FABRIC_API_NEW,
                               "new-mod.jar": NEW_MOD})
        self.running = False
        self.moves: list[tuple[str, str]] = []
        self.fail_server_upload_after: int | None = None
        self.server_uploads = 0

    def version(self, vid, number, pack):
        return {"id": vid, "project_id": "PM", "version_number": number,
                "version_type": "release", "loaders": ["fabric"], "game_versions": ["1.20.1"],
                "date_published": "2026-10-20T00:00:00Z", "dependencies": [],
                "files": [{"url": f"https://cdn.modrinth.com/data/PM/versions/{vid}/"
                                  f"{'pack2' if vid == V2 else 'pack'}.mrpack",
                           "filename": "pack.mrpack", "primary": True, "size": len(pack),
                           "hashes": {"sha1": sha1(pack), "sha512": __import__("hashlib")
                                      .sha512(pack).hexdigest()}}]}

    def _modrinth(self, request):
        path = request.url.path
        if path == f"/v2/version/{V2}":
            return httpx.Response(200, json=self.version(V2, "2.0.0", PACK_V2))
        if path == "/v2/project/PM/version":
            return httpx.Response(200, json=[self.version(V2, "2.0.0", PACK_V2),
                                             self.version("pk-1", "1.0.0", PACK_V1)])
        return super()._modrinth(request)

    def _crafty(self, request):
        path = request.url.path
        base = f"/api/v2/servers/{SERVER}"
        if path == f"{base}/stats":
            return httpx.Response(200, json={"status": "ok", "data": {"running": self.running}})
        if path == f"{base}/files" and request.method == "POST":
            # Crafty lists with os.listdir: a folder that does not exist is an error.
            if json.loads(request.content)["path"].strip("/") not in self.subdirs:
                return httpx.Response(500, text="FileNotFoundError")
        if path == f"{base}/files/create" and request.method == "PUT":
            body = json.loads(request.content)
            parent, name = body["parent"].strip("/"), body["name"]
            target = f"{parent}/{name}".strip("/")
            if parent not in self.subdirs:
                return httpx.Response(500, text="FileNotFoundError")
            if target in self.subdirs:
                return httpx.Response(400, json={"status": "error", "error": "FILE EXISTS"})
            self.subdirs = {**self.subdirs, target: [], parent: [*self.subdirs[parent], name]}
            return httpx.Response(200, json={"status": "ok"})
        if path == f"{base}/files/move":
            for item in json.loads(request.content)["file_system_objects"]:
                source, target_dir = item["source_path"], item["target_path"].strip("/")
                assert target_dir in self.subdirs, f"move target {target_dir} missing"
                if source not in self.files:  # Crafty: shutil.move raises, answered as 500
                    return httpx.Response(500, json={"status": "error",
                                                     "error": "FileNotFoundError"})
                destination = f"{target_dir}/{source.rsplit('/', 1)[-1]}"
                self.files[destination] = self.files.pop(source)
                self.moves.append((source, destination))
            return httpx.Response(200, json={"status": "ok"})
        if path == f"{base}/files/upload":
            location = request.headers["location"].strip("/")
            if location not in self.subdirs:
                return httpx.Response(500, text="FileNotFoundError")
            self.server_uploads += 1
            if self.fail_server_upload_after is not None and \
                    self.server_uploads > self.fail_server_upload_after:
                return httpx.Response(507, json={"status": "error", "error": "NO STORAGE SPACE"})
            self.files[f"{location}/{request.headers['fileName']}".strip("/")] = request.content
            return httpx.Response(200, json={"status": "completed"})
        return super()._crafty(request)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "db.sqlite3")


def linked_client(fakes, db, tmp_path):
    client = make_client(fakes, crafty_url=CRAFTY, database_path=db, update_check_hours=0,
                         modpack_poll_seconds=0, crafty_retry_seconds=0,
                         modpack_work_dir=str(tmp_path / "work"))
    client.__enter__()
    log_in(client)
    assert client.post(f"/api/servers/{SERVER}/scan",
                       json={"loader": "fabric", "game_version": "1.20.1"}).status_code == 200
    job = wait_for(client, client.post(f"/api/servers/{SERVER}/modpack/link", json={
        "project_id": "PM", "version_id": "pk-1"}).json()["job_id"])
    assert job["status"] == "succeeded", job
    fakes.requests.clear()
    return client


def update(client, **body):
    return client.post(f"/api/servers/{SERVER}/modpack/update", json=body)


def backup_files(fakes):
    return {path: data for path, data in fakes.files.items()
            if path.startswith(".crafty-complete/backups/")}


def test_update_applies_the_new_pack_version(tmp_path, db):
    fakes = UpdateFakes()
    client = linked_client(fakes, db, tmp_path)
    response = update(client, version_id=V2)
    assert response.status_code == 202
    job = wait_for(client, response.json()["job_id"])
    mods = client.get(f"/api/servers/{SERVER}/mods").json()
    stored = client.app.state.adoption_store.get_modpack_files(SERVER)
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    files = fakes.files
    assert files["mods/fabric-api-0.92.12.jar"] == FABRIC_API_NEW
    assert files["mods/new-mod.jar"] == NEW_MOD
    assert "mods/fabric-api.jar" not in files
    assert "mods/old-only.jar" not in files
    # Changed in the pack: replaced. Unchanged in the pack: the user's copy stays.
    assert files["config/pack.toml"] == CONFIG_V2
    assert files["config/shared.toml"] == USER_EDITED
    # The user's own mods are untouched.
    assert files["mods/sodium.jar"] == FILES["mods/sodium.jar"]
    assert files["mods/custom.jar"] == FILES["mods/custom.jar"]

    backup = backup_files(fakes)
    assert sorted(path.split("/", 3)[3] for path in backup) == [
        "config/pack.toml", "mods/fabric-api.jar", "mods/old-only.jar"]
    assert USER_EDITED in backup.values()

    assert job["result"]["added"] == ["mods/fabric-api-0.92.12.jar", "mods/new-mod.jar"]
    assert job["result"]["updated"] == ["config/pack.toml"]
    assert job["result"]["removed"] == ["mods/fabric-api.jar", "mods/old-only.jar"]
    assert job["result"]["own_mods_kept"] == ["custom.jar", "sodium.jar"]
    assert job["result"]["backup_folder"].startswith(".crafty-complete/backups/")
    assert job["result"]["restart_required"] is True

    assert mods["modpack"]["version_id"] == V2
    names = {m["file_name"] for m in mods["mods"]}
    assert {"fabric-api-0.92.12.jar", "new-mod.jar", "sodium.jar", "custom.jar"} <= names
    assert "fabric-api.jar" not in names
    assert stored["config/pack.toml"] == sha1(CONFIG_V2)
    assert "mods/old-only.jar" not in stored


def test_update_defaults_to_the_newest_compatible_version(tmp_path, db):
    fakes = UpdateFakes()
    client = linked_client(fakes, db, tmp_path)
    job = wait_for(client, update(client).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert job["title"] == "Update A Pack 1.0.0 → 2.0.0"


@pytest.mark.parametrize(("prepare", "status", "detail"), [
    ("running", 409, "Stop the server in Crafty before updating its modpack."),
    ("same", 409, "The server already runs A Pack 1.0.0."),
])
def test_update_refusals(tmp_path, db, prepare, status, detail):
    fakes = UpdateFakes()
    client = linked_client(fakes, db, tmp_path)
    if prepare == "running":
        fakes.running = True
    body = {"version_id": "pk-1"} if prepare == "same" else {"version_id": V2}
    response = update(client, **body)
    client.__exit__(None, None, None)

    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert fakes.moves == []


def test_update_requires_a_linked_server(tmp_path, db):
    fakes = UpdateFakes()
    client = make_client(fakes, crafty_url=CRAFTY, database_path=db, update_check_hours=0)
    client.__enter__()
    log_in(client)
    client.post(f"/api/servers/{SERVER}/scan", json={"loader": "fabric", "game_version": "1.20.1"})
    response = update(client, version_id=V2)
    client.__exit__(None, None, None)

    assert response.status_code == 409
    assert response.json() == {"detail": "Link this server to its modpack first."}


def test_pinned_pack_mod_is_left_alone(tmp_path, db):
    fakes = UpdateFakes()
    client = linked_client(fakes, db, tmp_path)
    client.patch(f"/api/servers/{SERVER}/mods/fabric-api.jar", json={"pinned": True})
    job = wait_for(client, update(client, version_id=V2).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert fakes.files["mods/fabric-api.jar"] == FABRIC_API
    assert "Kept your pinned mods/fabric-api.jar." in job["warnings"]


def test_users_own_copy_of_a_mod_the_pack_now_ships_is_moved_aside(tmp_path, db):
    fakes = UpdateFakes()
    fakes.identified = {
        sha1(FILES["mods/sodium.jar"]): {"id": "so-1", "project_id": "PX", "version_number": "1",
                                         "loaders": ["fabric"], "game_versions": ["1.20.1"]},
        sha1(NEW_MOD): {"id": "so-2", "project_id": "PX", "version_number": "2",
                        "loaders": ["fabric"], "game_versions": ["1.20.1"]},
    }
    client = linked_client(fakes, db, tmp_path)
    job = wait_for(client, update(client, version_id=V2).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert "mods/sodium.jar" not in fakes.files
    assert any(path.endswith("mods/sodium.jar") for path in backup_files(fakes))
    assert ("Moved your own mods/sodium.jar aside: the pack now ships "
            "mods/new-mod.jar for the same project.") in job["warnings"]


def test_bad_download_fails_before_touching_the_server(tmp_path, db):
    fakes = UpdateFakes()
    fakes.cdn_extra["new-mod.jar"] = b"tampered"
    client = linked_client(fakes, db, tmp_path)
    job = wait_for(client, update(client, version_id=V2).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "failed"
    assert job["error"] == "mods/new-mod.jar does not match the hash in the modpack."
    assert fakes.moves == []
    assert fakes.files["mods/fabric-api.jar"] == FABRIC_API


def test_failure_mid_update_points_to_the_backup(tmp_path, db):
    fakes = UpdateFakes()
    fakes.fail_server_upload_after = 1
    client = linked_client(fakes, db, tmp_path)
    job = wait_for(client, update(client, version_id=V2).json()["job_id"])
    stored = client.app.state.adoption_store.get(SERVER)
    client.__exit__(None, None, None)

    assert job["status"] == "failed"
    assert job["error"].startswith("Crafty request failed: NO STORAGE SPACE.")
    assert "The update stopped part-way; the files it replaced or removed are in " \
           ".crafty-complete/backups/" in job["error"]
    assert stored.modpack_version_id == "pk-1"


def test_update_requires_files_permission(tmp_path, db):
    fakes = UpdateFakes()
    client = linked_client(fakes, db, tmp_path)
    fakes.download_error = (400, "NOT_AUTHORIZED")
    response = update(client, version_id=V2)
    client.__exit__(None, None, None)

    assert response.status_code == 403


class ChannelFakes(UpdateFakes):
    """The pack's versions newest first, as Modrinth lists them: (id, number, type)."""

    def __init__(self, listed):
        super().__init__()
        self.listed = listed

    def _modrinth(self, request):
        if request.url.path == "/v2/project/PM/version":
            return httpx.Response(200, json=[
                {**self.version(vid, number, PACK_V2 if vid == V2 else PACK_V1),
                 "version_type": kind} for vid, number, kind in self.listed])
        return super()._modrinth(request)


@pytest.mark.parametrize(("listed", "latest"), [
    # The server runs a pre-release newer than the last release: that release is a downgrade.
    ([("pk-1", "1.0.0", "alpha"), ("pk-0", "0.9.0", "release")], None),
    # Nothing newer at all.
    ([("pk-1", "1.0.0", "release"), ("pk-0", "0.9.0", "release")], None),
    # A newer release wins over a newer beta.
    ([("pk-3", "3.0.0-beta", "beta"), (V2, "2.0.0", "release"), ("pk-1", "1.0.0", "release")],
     V2),
    # On a pre-release with no newer release, the newest newer version is offered.
    ([(V2, "2.0.0-alpha", "alpha"), ("pk-1", "1.0.0", "alpha"), ("pk-0", "0.9.0", "release")],
     V2),
])
def test_only_newer_versions_count_as_updates(tmp_path, db, listed, latest):
    fakes = ChannelFakes(listed)
    client = linked_client(fakes, db, tmp_path)
    status = client.get(f"/api/servers/{SERVER}/modpack").json()
    response = update(client)
    client.__exit__(None, None, None)

    assert status["update_available"] is (latest is not None)
    assert (status["latest"] or {}).get("id") == latest
    if latest is None:
        assert response.status_code == 409
        assert response.json() == {
            "detail": "A Pack 1.0.0 is the newest version for fabric 1.20.1."}
    else:
        assert response.status_code == 202


def test_pinned_pack_mod_whose_file_name_changes_is_not_installed_twice(tmp_path, db):
    fakes = UpdateFakes()
    fakes.identified = {
        sha1(FABRIC_API): {"id": "fa-1", "project_id": "P1", "version_number": "0.83.0",
                           "loaders": ["fabric"], "game_versions": ["1.20.1"]},
        sha1(FABRIC_API_NEW): {"id": "fa-2", "project_id": "P1", "version_number": "0.92.12",
                               "loaders": ["fabric"], "game_versions": ["1.20.1"]},
    }
    client = linked_client(fakes, db, tmp_path)
    client.patch(f"/api/servers/{SERVER}/mods/fabric-api.jar", json={"pinned": True})
    job = wait_for(client, update(client, version_id=V2).json()["job_id"])
    remembered = client.app.state.adoption_store.get_modpack_files(SERVER)
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert fakes.files["mods/fabric-api.jar"] == FABRIC_API
    assert "mods/fabric-api-0.92.12.jar" not in fakes.files
    assert "mods/fabric-api-0.92.12.jar" not in remembered
    assert ("Skipped the pack's mods/fabric-api-0.92.12.jar: you pinned "
            "mods/fabric-api.jar of the same project.") in job["warnings"]


def test_mod_recorded_during_the_update_download_is_kept(tmp_path, db):
    fakes = UpdateFakes()
    client = linked_client(fakes, db, tmp_path)
    store = client.app.state.adoption_store
    added = {}

    def install_during_download():
        if not added:
            added["done"] = True
            fakes.files["mods/distant-horizons.jar"] = b"dh"
            store.put_mod(SERVER, InstalledMod(file_name="distant-horizons.jar",
                                               sha1=sha1(b"dh"), state="unknown"))

    class Hooked(dict):
        def __contains__(self, key):
            if key == "new-mod.jar":
                install_during_download()
            return dict.__contains__(self, key)

    fakes.cdn_extra = Hooked(fakes.cdn_extra)
    job = wait_for(client, update(client, version_id=V2).json()["job_id"])
    names = {m.file_name for m in store.get(SERVER).mods}
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert added
    assert "distant-horizons.jar" in names
    assert fakes.files["mods/distant-horizons.jar"] == b"dh"


def test_server_started_during_the_download_is_not_touched(tmp_path, db):
    fakes = UpdateFakes()
    client = linked_client(fakes, db, tmp_path)

    class Hooked(dict):
        def __contains__(self, key):
            if key == "new-mod.jar":
                fakes.running = True
            return dict.__contains__(self, key)

    fakes.cdn_extra = Hooked(fakes.cdn_extra)
    before = dict(fakes.files)
    job = wait_for(client, update(client, version_id=V2).json()["job_id"])
    linked = client.get(f"/api/servers/{SERVER}/modpack").json()["linked"]
    client.__exit__(None, None, None)

    assert job["status"] == "failed"
    assert job["error"] == "Stop the server in Crafty before updating its modpack."
    assert fakes.moves == []
    assert fakes.files == before
    assert linked["version_id"] == "pk-1"


def test_no_update_is_offered_when_the_linked_version_is_gone():
    from crafty_complete.modpacks.linker import newest_update
    versions = [{"id": "b", "version_type": "release"}, {"id": "a", "version_type": "release"}]
    assert newest_update(versions, "gone") is None
    assert newest_update(versions, None)["id"] == "b"


# ---- Review follow-ups (L32) ----

PACK_V2_KEEPS_OLD_ONLY = build_mrpack(
    files=[pack_file("mods/fabric-api-0.92.12.jar", FABRIC_API_NEW),
           pack_file("mods/old-only.jar", OLD_ONLY),
           pack_file("mods/new-mod.jar", NEW_MOD)],
    entries={"overrides/config/pack.toml": CONFIG_V2,
             "overrides/config/shared.toml": SHARED_CONFIG},
)


class KeepFakes(UpdateFakes):
    """Pack version 2 still ships mods/old-only.jar unchanged."""

    def __init__(self):
        super().__init__()
        self.cdn_extra.update({"pack2.mrpack": PACK_V2_KEEPS_OLD_ONLY, "old-only.jar": OLD_ONLY})

    def _modrinth(self, request):
        path = request.url.path
        if path == f"/v2/version/{V2}":
            return httpx.Response(200, json=self.version(V2, "2.0.0", PACK_V2_KEEPS_OLD_ONLY))
        if path == "/v2/project/PM/version":
            return httpx.Response(200, json=[self.version(V2, "2.0.0", PACK_V2_KEEPS_OLD_ONLY),
                                             self.version("pk-1", "1.0.0", PACK_V1)])
        return super()._modrinth(request)


def test_update_restores_pack_mods_missing_from_the_server(tmp_path, db):
    fakes = KeepFakes()
    del fakes.files["mods/old-only.jar"]
    client = linked_client(fakes, db, tmp_path)
    job = wait_for(client, update(client, version_id=V2).json()["job_id"])
    names = {m["file_name"] for m in client.get(f"/api/servers/{SERVER}/mods").json()["mods"]}
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert fakes.files["mods/old-only.jar"] == OLD_ONLY
    assert job["result"]["restored"] == ["mods/old-only.jar"]
    assert "old-only.jar" in names


def test_update_does_not_restore_a_pack_mod_the_user_replaced(tmp_path, db):
    fakes = KeepFakes()
    del fakes.files["mods/old-only.jar"]
    fakes.files["mods/old-only-fork.jar"] = b"my-fork"
    fakes.identified = {
        sha1(OLD_ONLY): {"id": "oo-1", "project_id": "P9", "version_number": "1",
                         "loaders": ["fabric"], "game_versions": ["1.20.1"]},
        sha1(b"my-fork"): {"id": "oo-f", "project_id": "P9", "version_number": "1-fork",
                           "loaders": ["fabric"], "game_versions": ["1.20.1"]},
    }
    client = linked_client(fakes, db, tmp_path)
    job = wait_for(client, update(client, version_id=V2).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert "mods/old-only.jar" not in fakes.files
    assert fakes.files["mods/old-only-fork.jar"] == b"my-fork"
    assert job["result"]["restored"] == []


def test_a_move_that_succeeded_before_a_timeout_is_not_a_failure(tmp_path, db):
    fakes = UpdateFakes()
    client = linked_client(fakes, db, tmp_path)
    original = fakes._crafty
    failed = []

    def flaky(request):
        response = original(request)
        if request.url.path.endswith("/files/move") and not failed:
            failed.append(True)  # Crafty moved the file, then the answer was lost.
            return httpx.Response(502, text="Bad Gateway")
        return response

    fakes._crafty = flaky
    job = wait_for(client, update(client, version_id=V2).json()["job_id"])
    client.__exit__(None, None, None)

    assert failed
    assert job["status"] == "succeeded", job
    assert sorted(path.split("/", 3)[3] for path in backup_files(fakes)) == [
        "config/pack.toml", "mods/fabric-api.jar", "mods/old-only.jar"]


def test_existing_files_are_found_by_name_whatever_path_crafty_lists(tmp_path, db):
    fakes = UpdateFakes()
    client = linked_client(fakes, db, tmp_path)
    original = fakes._crafty

    def absolute_paths(request):
        response = original(request)
        if request.method == "POST" and request.url.path == f"/api/v2/servers/{SERVER}/files":
            body = response.json()
            for name, entry in body["data"].items():
                if name != "root_path":
                    entry["path"] = f"/var/opt/minecraft/crafty/servers/{SERVER}/{entry['path']}"
            return httpx.Response(200, json=body)
        return response

    fakes._crafty = absolute_paths
    job = wait_for(client, update(client, version_id=V2).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert "mods/fabric-api.jar" not in fakes.files
    assert sorted(path.split("/", 3)[3] for path in backup_files(fakes)) == [
        "config/pack.toml", "mods/fabric-api.jar", "mods/old-only.jar"]


BUSY = "A modpack update is changing this server's files; try again when it finishes."


def test_mod_changes_are_refused_while_an_update_writes_files(tmp_path, db):
    fakes = UpdateFakes()
    client = linked_client(fakes, db, tmp_path)
    locks = client.app.state.locks
    seen = []
    original = fakes._crafty

    def watch(request):
        if request.url.path.endswith("/files/upload"):
            seen.append(locks.busy(SERVER))
        return original(request)

    fakes._crafty = watch
    job = wait_for(client, update(client, version_id=V2).json()["job_id"])
    after = locks.busy(SERVER)

    with locks.marked_busy(SERVER, BUSY):
        refused = [
            client.post(f"/api/servers/{SERVER}/mods", json={"project_id": "abc"}),
            client.post(f"/api/servers/{SERVER}/mods/sodium.jar/update", json={}),
            client.post(f"/api/servers/{SERVER}/update-all", json={}),
            client.post(f"/api/servers/{SERVER}/check-updates", json={}),
            client.post(f"/api/servers/{SERVER}/scan", json={}),
            client.patch(f"/api/servers/{SERVER}/mods/sodium.jar", json={"pinned": True}),
        ]
    readable = client.get(f"/api/servers/{SERVER}/mods").status_code
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert seen and set(seen) == {BUSY}
    assert after is None
    assert [r.status_code for r in refused] == [409] * 6
    assert {r.json()["detail"] for r in refused} == {BUSY}
    assert readable == 200


PACK_V2_NEW_FOLDER = build_mrpack(
    files=[pack_file("mods/fabric-api-0.92.12.jar", FABRIC_API_NEW),
           pack_file("mods/new-mod.jar", NEW_MOD)],
    entries={"overrides/config/pack.toml": CONFIG_V2,
             "overrides/config/shared.toml": SHARED_CONFIG,
             "overrides/config/new-mod/settings.toml": b"new-mod-settings"},
)


class NewFolderFakes(KeepFakes):
    def __init__(self):
        super().__init__()
        self.cdn_extra["pack2.mrpack"] = PACK_V2_NEW_FOLDER

    def _modrinth(self, request):
        path = request.url.path
        if path == f"/v2/version/{V2}":
            return httpx.Response(200, json=self.version(V2, "2.0.0", PACK_V2_NEW_FOLDER))
        if path == "/v2/project/PM/version":
            return httpx.Response(200, json=[self.version(V2, "2.0.0", PACK_V2_NEW_FOLDER),
                                             self.version("pk-1", "1.0.0", PACK_V1)])
        return super()._modrinth(request)


def test_update_adds_files_in_folders_the_server_does_not_have_yet(tmp_path, db):
    fakes = NewFolderFakes()
    client = linked_client(fakes, db, tmp_path)
    job = wait_for(client, update(client, version_id=V2).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert fakes.files["config/new-mod/settings.toml"] == b"new-mod-settings"
