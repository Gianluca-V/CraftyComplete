import httpx
import pytest

from tests.test_install import BYTES
from tests.test_modpack import NEW_SERVER, CreationFakes, creation_client, wait_for

EXECUTABLES = {"vanilla": "vanilla.jar", "paper": "paper.jar"}


class Fakes(CreationFakes):
    """Creation fakes with vanilla and Paper in Crafty's catalog."""

    def __init__(self, kind="fabric", **kwargs):
        loader = kind if kind in ("fabric", "forge", "neoforge") else "fabric"
        super().__init__(loader=loader, **kwargs)
        self.kind = kind

    def _crafty(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v2/crafty/JarCache":
            versions = {"1.20.1": {"loader_version": "0.16.9"}}
            types = {
                "vanilla": {"versions": {"1.21.4": {}, "1.20.1": {}}},
                "paper": {"versions": {"1.21.4": {}}},
                "fabric": {"versions": versions},
                "forge-installer": {"versions": {"1.20.1": {"loader_version": "47.4.0"}}},
                "neoforge-installer": {"versions": {}},
                "purpur": {"versions": {"1.21.4": {}}},
            }
            return httpx.Response(200, json={"status": "ok", "data": {
                "mc_java_servers": {"types": types}}})
        executable = EXECUTABLES.get(self.kind)
        if executable and path == f"/api/v2/servers/{NEW_SERVER}" and request.method == "GET":
            return httpx.Response(200, json={"status": "ok", "data": {
                "server_id": NEW_SERVER, "execution_command": f"java -jar {executable} nogui",
                "executable": executable}})
        if (executable and path == f"/api/v2/servers/{NEW_SERVER}/files"
                and request.method == "POST"):
            data = {"root_path": {"local_path": ""}}
            if self.jar_ready:
                data[executable] = {"path": executable, "dir": False}
            return httpx.Response(200, json={"status": "ok", "data": data})
        return super()._crafty(request)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "db.sqlite3")


def create(client, **body):
    payload = {"kind": "fabric", "minecraft": "1.20.1", "name": "My Server", "port": 25570,
               "mem_min": 1, "mem_max": 4, "agree_to_eula": True, **body}
    return client.post("/api/servers/new", json=payload)


def run(fakes, db, tmp_path, **body):
    client = creation_client(fakes, db, tmp_path)
    try:
        response = create(client, **body)
        assert response.status_code == 202, response.json()
        job = wait_for(client, response.json()["job_id"])
        mods = client.get(f"/api/servers/{NEW_SERVER}/mods").json()
    finally:
        client.__exit__(None, None, None)
    return job, mods


# --- catalog -----------------------------------------------------------------------

def test_catalog_offers_what_crafty_can_install(tmp_path, db):
    client = creation_client(Fakes(), db, tmp_path)
    response = client.get("/api/servers/new/options")
    client.__exit__(None, None, None)

    assert response.status_code == 200
    assert response.json() == {"kinds": [
        {"kind": "vanilla", "label": "Vanilla", "modded": False, "versions": ["1.21.4", "1.20.1"]},
        {"kind": "paper", "label": "Paper", "modded": False, "versions": ["1.21.4"]},
        {"kind": "fabric", "label": "Fabric", "modded": True, "versions": ["1.20.1"]},
        {"kind": "forge", "label": "Forge", "modded": True, "versions": ["1.20.1"]},
        {"kind": "neoforge", "label": "NeoForge", "modded": True, "versions": []},
    ]}


# --- vanilla and Paper -------------------------------------------------------------

@pytest.mark.parametrize("kind, minecraft", [("vanilla", "1.20.1"), ("paper", "1.21.4")])
def test_creates_a_server_without_mods(tmp_path, db, kind, minecraft):
    fakes = Fakes(kind=kind)
    job, mods = run(fakes, db, tmp_path, kind=kind, minecraft=minecraft)

    assert job["status"] == "succeeded", job
    assert job["server_id"] == NEW_SERVER
    assert job["warnings"] == []
    assert fakes.created[0]["name"] == "My Server"
    data = fakes.created[0]["minecraft_java_create_data"]["download_jar_create_data"]
    assert data == {"category": "mc_java_servers", "type": kind, "version": minecraft,
                    "mem_min": 1, "mem_max": 4, "server_properties_port": 25570,
                    "agree_to_eula": True}
    # Only modded servers are adopted.
    assert mods["adopted"] is False
    assert fakes.created_folders == []


def test_mods_are_refused_for_a_server_without_a_mod_loader(tmp_path, db):
    fakes = Fakes(kind="paper")
    client = creation_client(fakes, db, tmp_path)
    response = create(client, kind="paper", minecraft="1.21.4", mods=["P3"])
    client.__exit__(None, None, None)

    assert response.status_code == 422
    assert response.json() == {"detail": "Paper servers cannot have mods; choose Fabric, "
                                         "Forge or NeoForge."}
    assert fakes.created == []


def test_version_crafty_cannot_install_is_refused(tmp_path, db):
    fakes = Fakes(kind="paper")
    client = creation_client(fakes, db, tmp_path)
    response = create(client, kind="paper", minecraft="1.20.1")
    client.__exit__(None, None, None)

    assert response.status_code == 422
    assert response.json() == {"detail": "Crafty cannot install Paper for Minecraft 1.20.1."}
    assert fakes.created == []


# --- modded ------------------------------------------------------------------------

def test_creates_a_fabric_server_with_mods_and_their_dependencies(tmp_path, db):
    fakes = Fakes()
    job, mods = run(fakes, db, tmp_path, mods=["lithium"])

    assert job["status"] == "succeeded", job
    assert job["warnings"] == []
    assert job["result"]["installed"] == [
        {"project_id": "P3", "title": "Lithium", "version_number": "0.11.2",
         "file_name": "lithium-0.11.2.jar", "dependency": False},
        {"project_id": "P1", "title": "Fabric API", "version_number": "0.92.12",
         "file_name": "fabric-api-0.92.12.jar", "dependency": True},
        {"project_id": "P4", "title": "Cloth Config", "version_number": "11.1",
         "file_name": "cloth-config-11.1.jar", "dependency": True},
    ]
    assert fakes.server_files["mods/lithium-0.11.2.jar"] == BYTES["li-2"]
    assert fakes.server_files["mods/cloth-config-11.1.jar"] == BYTES["cl-1"]
    assert "mods" in fakes.created_folders
    assert mods["adopted"] is True
    assert (mods["loader"], mods["game_version"]) == ("fabric", "1.20.1")
    assert {m["file_name"]: m["dependency"] for m in mods["mods"]} == {
        "lithium-0.11.2.jar": False, "fabric-api-0.92.12.jar": True,
        "cloth-config-11.1.jar": True}


def test_a_modded_server_without_mods_is_still_adopted(tmp_path, db):
    job, mods = run(Fakes(), db, tmp_path)

    assert job["status"] == "succeeded", job
    assert job["result"]["installed"] == []
    assert mods["adopted"] is True
    assert mods["mods"] == []


@pytest.mark.parametrize("mod, detail", [
    ("zoomify", "Zoomify only runs on clients; it cannot be installed on a server."),
    ("forge-only", "Forge Only has no version for fabric 1.20.1."),
    ("a-pack", "A Pack is a modpack, not a mod."),
    ("nope", "Modrinth has no project 'nope'."),
])
def test_mods_are_checked_before_anything_is_created(tmp_path, db, mod, detail):
    fakes = Fakes()
    client = creation_client(fakes, db, tmp_path)
    response = create(client, mods=["lithium", mod])
    client.__exit__(None, None, None)

    assert response.status_code in (404, 422)
    assert response.json() == {"detail": detail}
    assert fakes.created == []


def test_a_mod_that_fails_after_creation_becomes_a_warning(tmp_path, db):
    fakes = Fakes()
    fakes.cdn_override["li-2"] = b"tampered"
    job, mods = run(fakes, db, tmp_path, mods=["lithium"])

    assert job["status"] == "succeeded", job
    assert job["warnings"] == [
        "Could not install Lithium: lithium-0.11.2.jar from Modrinth does not match its "
        "published hash. Install it from the server page."]
    assert mods["adopted"] is True


def test_the_same_mod_twice_is_installed_once(tmp_path, db):
    job, _ = run(Fakes(), db, tmp_path, mods=["lithium", "P3"])

    assert [m["project_id"] for m in job["result"]["installed"]] == ["P3", "P1", "P4"]


# --- refusals ----------------------------------------------------------------------

def test_eula_must_be_accepted(tmp_path, db):
    fakes = Fakes()
    client = creation_client(fakes, db, tmp_path)
    response = create(client, agree_to_eula=False)
    client.__exit__(None, None, None)

    assert response.status_code == 422
    assert "EULA" in response.json()["detail"]
    assert fakes.created == []


def test_needs_the_crafty_server_creation_permission(tmp_path, db):
    fakes = Fakes()
    original = fakes._crafty

    def no_permission(request):
        if request.url.path == "/api/v2/users/@me/permissions":
            return httpx.Response(200, json={"status": "ok", "data": {"permissions": "000"}})
        return original(request)

    fakes._crafty = no_permission
    client = creation_client(fakes, db, tmp_path)
    response = create(client)
    client.__exit__(None, None, None)

    assert response.status_code == 403
    assert response.json() == {"detail": "Your Crafty account cannot create servers."}


def test_unknown_kind_is_rejected(tmp_path, db):
    client = creation_client(Fakes(), db, tmp_path)
    response = create(client, kind="bukkit")
    client.__exit__(None, None, None)

    assert response.status_code == 422


@pytest.mark.parametrize("order", [["lithium", "fabric-api"], ["fabric-api", "lithium"]])
def test_a_chosen_mod_is_never_marked_as_a_dependency(tmp_path, db, order):
    """Fabric API is also a dependency of Lithium; the user chose it, so it is theirs."""
    job, mods = run(Fakes(), db, tmp_path, mods=order)

    assert job["status"] == "succeeded", job
    assert {m["file_name"]: m["dependency"] for m in mods["mods"]} == {
        "lithium-0.11.2.jar": False, "fabric-api-0.92.12.jar": False,
        "cloth-config-11.1.jar": True}


def test_duplicates_do_not_count_towards_the_mod_limit(tmp_path, db):
    job, _ = run(Fakes(), db, tmp_path, mods=["lithium"] * 51)

    assert job["status"] == "succeeded", job
