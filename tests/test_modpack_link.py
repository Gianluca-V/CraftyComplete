import httpx
import pytest

from tests.conftest import CRAFTY, log_in, make_client
from tests.test_adoption import FILES, SERVER, sha1
from tests.test_modpack import (
    NEW_SERVER,
    PACK_VERSION_ID,
    CreationFakes,
    build_mrpack,
    pack_file,
    start,
    wait_for,
)

FABRIC_API = FILES["mods/fabric-api.jar"]
PACK_ONLY_MOD = b"pack-only-mod"
CONFIG = b"pack-config-v1"

# A pack whose mods overlap with what the adopted server already has.
LINKED_PACK = build_mrpack(
    files=[
        pack_file("mods/fabric-api.jar", FABRIC_API),
        pack_file("mods/pack-only.jar", PACK_ONLY_MOD),
    ],
    entries={"overrides/config/pack.toml": CONFIG},
)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "db.sqlite3")


def adopted_client(fakes, db, tmp_path, **settings):
    settings.setdefault("update_check_hours", 0)
    settings.setdefault("modpack_poll_seconds", 0)
    settings.setdefault("modpack_work_dir", str(tmp_path / "work"))
    client = make_client(fakes, crafty_url=CRAFTY, database_path=db, **settings)
    client.__enter__()
    log_in(client)
    scan = client.post(f"/api/servers/{SERVER}/scan",
                       json={"loader": "fabric", "game_version": "1.20.1"})
    assert scan.status_code == 200
    return client


def link(client, **body):
    return client.post(f"/api/servers/{SERVER}/modpack/link",
                       json={"project_id": "PM", "version_id": PACK_VERSION_ID, **body})


def test_links_an_adopted_server_to_a_modpack_version(tmp_path, db):
    fakes = CreationFakes(pack=LINKED_PACK)
    client = adopted_client(fakes, db, tmp_path)
    response = link(client)
    assert response.status_code == 202
    job = wait_for(client, response.json()["job_id"])
    mods = client.get(f"/api/servers/{SERVER}/mods").json()
    stored = client.app.state.adoption_store.get_modpack_files(SERVER)
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert job["result"] == {
        "missing": ["mods/pack-only.jar"],
        "own_mods": ["custom.jar", "sodium.jar"],
    }
    assert mods["modpack"] == {"project_id": "PM", "title": "A Pack",
                               "version_id": PACK_VERSION_ID, "version_number": "1.0.0"}
    assert stored == {
        "mods/fabric-api.jar": sha1(FABRIC_API),
        "mods/pack-only.jar": sha1(PACK_ONLY_MOD),
        "config/pack.toml": sha1(CONFIG),
    }
    # Linking downloads only the .mrpack, never the pack's mods.
    assert not [r for r in fakes.requests if r.url.path.endswith("pack-only.jar")]
    assert fakes.created == []


def test_link_skips_client_only_pack_files(tmp_path, db):
    fakes = CreationFakes(pack=LINKED_PACK)
    fakes.identified = {sha1(PACK_ONLY_MOD): {
        "id": "so-9", "project_id": "P2", "version_number": "0.5.0",
        "loaders": ["fabric"], "game_versions": ["1.20.1"]}}
    client = adopted_client(fakes, db, tmp_path)
    job = wait_for(client, link(client).json()["job_id"])
    stored = client.app.state.adoption_store.get_modpack_files(SERVER)
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert "mods/pack-only.jar" not in stored
    assert job["result"]["missing"] == []


def test_link_refuses_another_minecraft_version(tmp_path, db):
    fakes = CreationFakes(pack=LINKED_PACK)
    original = fakes._modrinth

    def other_minecraft(request):
        response = original(request)
        if request.url.path == f"/v2/version/{PACK_VERSION_ID}":
            return httpx.Response(200, json={**response.json(), "game_versions": ["1.19.2"]})
        return response

    fakes._modrinth = other_minecraft
    client = adopted_client(fakes, db, tmp_path)
    response = link(client)
    client.__exit__(None, None, None)

    assert response.status_code == 422
    assert response.json() == {"detail": "This modpack version is for Minecraft 1.19.2 and "
                                         "fabric; the server runs 1.20.1 with fabric."}


def test_link_requires_an_adopted_server(tmp_path, db):
    fakes = CreationFakes(pack=LINKED_PACK)
    client = make_client(fakes, crafty_url=CRAFTY, database_path=db, update_check_hours=0)
    client.__enter__()
    log_in(client)
    response = link(client)
    client.__exit__(None, None, None)

    assert response.status_code == 409
    assert response.json() == {"detail": "Adopt this server first."}


def test_link_requires_files_permission(tmp_path, db):
    fakes = CreationFakes(pack=LINKED_PACK)
    client = adopted_client(fakes, db, tmp_path)
    fakes.download_error = (400, "NOT_AUTHORIZED")
    response = link(client)
    client.__exit__(None, None, None)

    assert response.status_code == 403


def test_modpack_status_reports_a_newer_compatible_version(tmp_path, db):
    fakes = CreationFakes(pack=LINKED_PACK)
    original = fakes._modrinth

    def with_newer(request):
        response = original(request)
        if request.url.path == "/v2/project/PM/version":
            current = response.json()[0]
            newer = {**current, "id": "pk-2", "version_number": "1.1.0",
                     "date_published": "2026-10-20T00:00:00Z"}
            other_mc = {**current, "id": "pk-3", "version_number": "2.0.0",
                        "game_versions": ["1.21.1"]}
            return httpx.Response(200, json=[other_mc, newer, current])
        return response

    fakes._modrinth = with_newer
    client = adopted_client(fakes, db, tmp_path)
    wait_for(client, link(client).json()["job_id"])
    status = client.get(f"/api/servers/{SERVER}/modpack")
    client.__exit__(None, None, None)

    assert status.status_code == 200
    body = status.json()
    assert body["linked"]["version_id"] == PACK_VERSION_ID
    assert body["update_available"] is True
    assert body["latest"] == {"id": "pk-2", "version_number": "1.1.0", "version_type": "release",
                              "date_published": "2026-10-20T00:00:00Z"}
    assert [v["id"] for v in body["versions"]] == ["pk-2", PACK_VERSION_ID]


def test_modpack_status_for_an_unlinked_server(tmp_path, db):
    fakes = CreationFakes(pack=LINKED_PACK)
    client = adopted_client(fakes, db, tmp_path)
    response = client.get(f"/api/servers/{SERVER}/modpack")
    client.__exit__(None, None, None)

    assert response.json() == {"linked": None, "latest": None, "update_available": False,
                               "versions": []}


def test_created_servers_remember_their_pack_files(tmp_path, db):
    fakes = CreationFakes()
    client = make_client(fakes, crafty_url=CRAFTY, database_path=db, update_check_hours=0,
                         modpack_poll_seconds=0, modpack_work_dir=str(tmp_path / "work"))
    client.__enter__()
    log_in(client)
    job = wait_for(client, start(client).json()["job_id"])
    stored = client.app.state.adoption_store.get_modpack_files(NEW_SERVER)
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert set(stored) == {"mods/mod-a.jar", "mods/mod-b.jar", "mods/bundled.jar",
                           "config/a.toml", "config/b.toml"}
    assert stored["config/a.toml"] == sha1(b"from-server-overrides")
