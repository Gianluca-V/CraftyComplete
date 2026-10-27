import hashlib
import json

import httpx
import pytest

from tests.conftest import CRAFTY, log_in, make_client
from tests.test_adoption import FABRIC_SHA, SERVER, SODIUM_SHA, World, by_file, sha1

CDN = "https://cdn.modrinth.com/data"


def sha512(data: bytes) -> str:
    return hashlib.sha512(data).hexdigest()


def version(vid, project_id, number, file_name, data, loaders=("fabric",),
            game_versions=("1.20.1",), deps=(), version_type="release", url=None):
    return {
        "id": vid, "project_id": project_id, "version_number": number,
        "version_type": version_type, "loaders": list(loaders),
        "game_versions": list(game_versions), "dependencies": list(deps),
        "files": [{
            "url": url or f"{CDN}/{project_id}/versions/{vid}/{file_name}",
            "filename": file_name, "primary": True, "size": len(data),
            "hashes": {"sha1": sha1(data), "sha512": sha512(data)},
        }],
    }


def dep(project_id, kind="required", version_id=None):
    return {"project_id": project_id, "version_id": version_id, "dependency_type": kind}


BYTES = {
    "fa-2": b"fabric-api-0.92.12",
    "li-1": b"lithium-0.11.1",
    "li-2": b"lithium-0.11.2",
    "cl-1": b"cloth-config-11",
    "zo-1": b"zoomify",
    "fo-1": b"forge-only",
    "ex-1": b"external",
    "br-1": b"breaks-sodium",
    "cd-1": b"client-dep-user",
    "qu-1": b"fabric-only-for-quilt",
}

PROJECTS = {
    "P1": {"id": "P1", "slug": "fabric-api", "title": "Fabric API", "server_side": "optional",
           "client_side": "optional", "project_type": "mod", "icon_url": None},
    "P2": {"id": "P2", "slug": "sodium", "title": "Sodium", "server_side": "unsupported",
           "client_side": "required", "project_type": "mod", "icon_url": None},
    "P3": {"id": "P3", "slug": "lithium", "title": "Lithium", "server_side": "required",
           "client_side": "optional", "project_type": "mod", "icon_url": "https://cdn/li.png"},
    "P4": {"id": "P4", "slug": "cloth-config", "title": "Cloth Config", "server_side": "required",
           "client_side": "required", "project_type": "mod", "icon_url": None},
    "P5": {"id": "P5", "slug": "optional-thing", "title": "Optional", "server_side": "required",
           "client_side": "required", "project_type": "mod", "icon_url": None},
    "P6": {"id": "P6", "slug": "zoomify", "title": "Zoomify", "server_side": "unsupported",
           "client_side": "required", "project_type": "mod", "icon_url": None},
    "P7": {"id": "P7", "slug": "forge-only", "title": "Forge Only", "server_side": "required",
           "client_side": "required", "project_type": "mod", "icon_url": None},
    "P8": {"id": "P8", "slug": "external", "title": "External", "server_side": "required",
           "client_side": "required", "project_type": "mod", "icon_url": None},
    "P9": {"id": "P9", "slug": "breaks", "title": "Breaks Sodium", "server_side": "required",
           "client_side": "required", "project_type": "mod", "icon_url": None},
    "PA": {"id": "PA", "slug": "client-dep-user", "title": "Needs Zoomify",
           "server_side": "required", "client_side": "required", "project_type": "mod",
           "icon_url": None},
    "PB": {"id": "PB", "slug": "fabric-only", "title": "Fabric Only", "server_side": "required",
           "client_side": "required", "project_type": "mod", "icon_url": None},
    "PM": {"id": "PM", "slug": "a-pack", "title": "A Pack", "server_side": "required",
           "client_side": "required", "project_type": "modpack", "icon_url": None},
}

ALL_VERSIONS = [
    version("fa-2", "P1", "0.92.12", "fabric-api-0.92.12.jar", BYTES["fa-2"]),
    version("li-2", "P3", "0.11.2", "lithium-0.11.2.jar", BYTES["li-2"],
            deps=[dep("P1"), dep("P4"), dep("P5", "optional"), dep("PX", "embedded")]),
    version("li-3b", "P3", "0.12.0-beta", "lithium-0.12.0-beta.jar", b"beta",
            version_type="beta"),
    version("li-1", "P3", "0.11.1", "lithium-0.11.1.jar", BYTES["li-1"]),
    version("cl-1", "P4", "11.1", "cloth-config-11.1.jar", BYTES["cl-1"]),
    version("zo-1", "P6", "2.0", "zoomify-2.0.jar", BYTES["zo-1"]),
    version("fo-1", "P7", "1.0", "forge-only-1.0.jar", BYTES["fo-1"], loaders=("forge",)),
    version("ex-1", "P8", "1.0", "external-1.0.jar", BYTES["ex-1"],
            url="https://example.com/external-1.0.jar"),
    version("br-1", "P9", "1.0", "breaks-1.0.jar", BYTES["br-1"], deps=[dep("P2", "incompatible")]),
    version("cd-1", "PA", "1.0", "client-dep-user-1.0.jar", BYTES["cd-1"], deps=[dep("P6")]),
    version("qu-1", "PB", "1.0", "fabric-only-1.0.jar", BYTES["qu-1"]),
]
VERSIONS_BY_ID = {v["id"]: v for v in ALL_VERSIONS}


class Store(World):
    """World plus Crafty uploads/deletes and Modrinth projects, versions and CDN."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cdn_override: dict[str, bytes] = {}
        self.upload_error: tuple[int, str] | None = None
        self.latest = {FABRIC_SHA: {"id": "fa-2", "project_id": "P1",
                                    "version_number": "0.92.12"},
                       SODIUM_SHA: {"id": "so-1", "project_id": "P2", "version_number": "0.5.0"}}

    def uploads(self):
        return [r for r in self.requests if r.url.path.endswith("/files/upload")]

    def deletes(self):
        return [json.loads(r.content)["file_system_objects"] for r in self.requests
                if r.method == "DELETE"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.modrinth.com" or request.url.host == "example.com":
            self.requests.append(request)
            vid = request.url.path.split("/")[-2]
            data = self.cdn_override.get(vid, BYTES.get(vid))
            return httpx.Response(200, content=data) if data else httpx.Response(404)
        return super().__call__(request)

    def _crafty(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/api/v2/servers/{SERVER}/files/upload":
            if self.upload_error:
                status, error = self.upload_error
                return httpx.Response(status, json={"status": "error", "error": error})
            assert "chunked" not in request.headers, "Crafty treats any chunked header as true"
            assert request.headers["fileId"]
            assert int(request.headers["fileSize"]) == len(request.content)
            location, name = request.headers["location"], request.headers["fileName"]
            self.files[f"{location}/{name}"] = request.content
            return httpx.Response(200, json={"status": "completed",
                                             "data": {"message": "File uploaded successfully"}})
        if request.method == "DELETE" and path == f"/api/v2/servers/{SERVER}/files":
            for item in json.loads(request.content)["file_system_objects"]:
                self.files.pop(item["filename"], None)
            return httpx.Response(200, json={"status": "ok"})
        return super()._crafty(request)

    def _modrinth(self, request: httpx.Request) -> httpx.Response:
        if self.modrinth_down:
            return httpx.Response(503)
        path = request.url.path
        if path == "/v2/version_files/update":
            hashes = json.loads(request.content)["hashes"]
            return httpx.Response(200, json={h: self.latest[h] for h in hashes
                                             if h in self.latest})
        parts = path.split("/")
        if len(parts) == 4 and parts[2] == "project":
            project = PROJECTS.get(parts[3]) or next(
                (p for p in PROJECTS.values() if p["slug"] == parts[3]), None)
            return httpx.Response(200, json=project) if project else httpx.Response(404)
        if len(parts) == 5 and parts[2] == "project" and parts[4] == "version":
            loaders = json.loads(request.url.params["loaders"])
            game_versions = json.loads(request.url.params["game_versions"])
            found = [v for v in ALL_VERSIONS if v["project_id"] == parts[3]
                     and set(v["loaders"]) & set(loaders)
                     and set(v["game_versions"]) & set(game_versions)]
            return httpx.Response(200, json=found)
        if len(parts) == 4 and parts[2] == "version":
            found = VERSIONS_BY_ID.get(parts[3])
            return httpx.Response(200, json=found) if found else httpx.Response(404)
        return super()._modrinth(request)


@pytest.fixture
def store():
    return Store()


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "db.sqlite3")


class Session:
    """Logged-in client on an adopted server."""

    def __init__(self, world: Store, db: str, adopt=True, **settings):
        settings.setdefault("update_check_hours", 0)
        self.client = make_client(world, crafty_url=CRAFTY, database_path=db, **settings)
        self.client.__enter__()
        log_in(self.client)
        if adopt:
            assert self.post(f"/api/servers/{SERVER}/scan", {}).status_code == 200
        world.requests.clear()

    def post(self, path, body=None):
        return self.client.post(path, json={} if body is None else body)

    def mods(self):
        return by_file(self.client.get(f"/api/servers/{SERVER}/mods").json())

    def close(self):
        self.client.__exit__(None, None, None)


@pytest.fixture
def session(store, db):
    s = Session(store, db)
    yield s
    s.close()


def install(session, project_id):
    return session.post(f"/api/servers/{SERVER}/mods", {"project_id": project_id})


# --- install ------------------------------------------------------------------

def test_install_adds_mod_and_missing_required_dependencies(session, store):
    response = install(session, "P3")

    assert response.status_code == 200
    body = response.json()
    assert body["installed"] == [
        {"project_id": "P3", "title": "Lithium", "version_number": "0.11.2",
         "file_name": "lithium-0.11.2.jar", "dependency": False},
        {"project_id": "P4", "title": "Cloth Config", "version_number": "11.1",
         "file_name": "cloth-config-11.1.jar", "dependency": True},
    ]
    assert body["skipped"] == []
    assert body["restart_required"] is True
    assert store.files["mods/lithium-0.11.2.jar"] == BYTES["li-2"]
    assert store.files["mods/cloth-config-11.1.jar"] == BYTES["cl-1"]

    mods = by_file(body["server"])
    assert mods["lithium-0.11.2.jar"]["state"] == "identified"
    assert mods["lithium-0.11.2.jar"]["version_id"] == "li-2"
    assert mods["lithium-0.11.2.jar"]["title"] == "Lithium"
    assert mods["lithium-0.11.2.jar"]["sha1"] == sha1(BYTES["li-2"])
    assert mods["cloth-config-11.1.jar"]["project_id"] == "P4"
    assert session.mods().keys() == mods.keys()


def test_install_uploads_through_crafty_with_crafty_headers(session, store):
    install(session, "P4")

    upload = store.uploads()[0]
    assert upload.headers["Authorization"] == "Bearer user-jwt"
    assert upload.headers["location"] == "mods"
    assert upload.headers["fileName"] == "cloth-config-11.1.jar"


def test_install_picks_newest_release_for_the_servers_loader_and_version(session, store):
    body = install(session, "P3").json()

    assert body["installed"][0]["version_number"] == "0.11.2"
    query = [r for r in store.requests if r.url.path == "/v2/project/P3/version"][0]
    assert json.loads(query.url.params["loaders"]) == ["fabric"]
    assert json.loads(query.url.params["game_versions"]) == ["1.20.1"]


def test_install_accepts_project_slug(session, store):
    assert install(session, "cloth-config").json()["installed"][0]["project_id"] == "P4"


def test_quilt_server_can_install_fabric_mods(store, db):
    s = Session(store, db, adopt=False)
    s.post(f"/api/servers/{SERVER}/scan", {"loader": "quilt", "game_version": "1.20.1"})
    store.requests.clear()
    response = install(s, "PB")
    query = [r for r in store.requests if r.url.path == "/v2/project/PB/version"][0]
    s.close()

    assert response.status_code == 200
    assert json.loads(query.url.params["loaders"]) == ["quilt", "fabric"]


def test_install_rejects_client_only_mod(session, store):
    response = install(session, "P6")

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Zoomify only runs on clients; it cannot be installed on a server."
    }
    assert store.uploads() == []


def test_install_rejects_modpacks(session, store):
    response = install(session, "PM")

    assert response.status_code == 422
    assert response.json() == {"detail": "A Pack is a modpack, not a mod."}


def test_install_reports_no_compatible_version(session, store):
    response = install(session, "P7")

    assert response.status_code == 422
    assert response.json() == {"detail": "Forge Only has no version for fabric 1.20.1."}


def test_install_rejects_already_installed_project(session, store):
    response = install(session, "P1")

    assert response.status_code == 409
    assert response.json() == {"detail": "Fabric API is already installed (fabric-api.jar)."}


def test_install_refuses_mods_incompatible_with_installed_ones(session, store):
    response = install(session, "P9")

    assert response.status_code == 409
    assert response.json() == {"detail": "Breaks Sodium is incompatible with Sodium."}
    assert store.uploads() == []


def test_install_skips_files_not_hosted_by_modrinth(session, store):
    response = install(session, "P8")

    assert response.status_code == 200
    assert response.json()["installed"] == []
    assert response.json()["skipped"] == [{
        "project_id": "P8", "title": "External", "file_name": "external-1.0.jar",
        "reason": "Not hosted on Modrinth; download it yourself and upload it to mods/.",
        "url": "https://example.com/external-1.0.jar",
    }]
    assert store.uploads() == []


def test_install_skips_client_only_dependency(session, store):
    body = install(session, "PA").json()

    assert [m["project_id"] for m in body["installed"]] == ["PA"]
    assert body["skipped"] == [{
        "project_id": "P6", "title": "Zoomify", "file_name": None, "url": None,
        "reason": "Client-only dependency; not needed on the server.",
    }]


def test_install_rejects_download_with_wrong_hash(session, store):
    store.cdn_override["cl-1"] = b"tampered"
    response = install(session, "P4")

    assert response.status_code == 502
    assert response.json() == {
        "detail": "cloth-config-11.1.jar from Modrinth does not match its published hash."
    }
    assert store.uploads() == []
    assert "cloth-config-11.1.jar" not in session.mods()


def test_install_refuses_to_overwrite_a_different_file(store, db):
    store.files["mods/cloth-config-11.1.jar"] = b"someone else's jar"
    s = Session(store, db)
    response = install(s, "P4")
    s.close()

    assert response.status_code == 409
    assert response.json() == {"detail": "mods/cloth-config-11.1.jar already exists."}
    assert store.files["mods/cloth-config-11.1.jar"] == b"someone else's jar"


def test_install_requires_adoption(store, db):
    s = Session(store, db, adopt=False)
    response = install(s, "P4")
    s.close()

    assert response.status_code == 409
    assert response.json() == {"detail": "Adopt this server first."}


@pytest.mark.parametrize("project_id", ["", "../x", "a" * 65, "a b"])
def test_install_validates_project_id(project_id, session, store):
    response = install(session, project_id)

    assert response.status_code == 422
    assert store.requests == []


def test_failed_upload_saves_nothing(session, store):
    store.upload_error = (500, "OSERROR")
    response = install(session, "P4")

    assert response.status_code == 502
    assert "cloth-config-11.1.jar" not in session.mods()


# --- update -------------------------------------------------------------------

def update(session, file_name):
    return session.post(f"/api/servers/{SERVER}/mods/{file_name}/update")


def test_update_replaces_the_jar(session, store):
    response = update(session, "fabric-api.jar")

    assert response.status_code == 200
    body = response.json()
    assert body["updated"] == [{
        "project_id": "P1", "title": "Fabric API", "from_version": "0.83.0",
        "to_version": "0.92.12", "old_file_name": "fabric-api.jar",
        "file_name": "fabric-api-0.92.12.jar",
    }]
    assert body["failed"] == []
    assert body["restart_required"] is True
    assert store.files["mods/fabric-api-0.92.12.jar"] == BYTES["fa-2"]
    assert "mods/fabric-api.jar" not in store.files

    mods = by_file(body["server"])
    assert "fabric-api.jar" not in mods
    new = mods["fabric-api-0.92.12.jar"]
    assert new["version_id"] == "fa-2"
    assert new["update_available"] is False
    assert body["server"]["counts"]["updates"] == 0


def test_update_uploads_before_deleting(session, store):
    update(session, "fabric-api.jar")

    order = [("upload" if r.url.path.endswith("/upload") else "delete")
             for r in store.requests
             if r.url.path.endswith("/upload") or r.method == "DELETE"]
    assert order == ["upload", "delete"]
    assert store.deletes() == [[{"filename": "mods/fabric-api.jar"}]]


def test_update_with_same_file_name_does_not_delete(store, db):
    store.files["mods/fabric-api-0.92.12.jar"] = store.files.pop("mods/fabric-api.jar")
    s = Session(store, db)
    response = update(s, "fabric-api-0.92.12.jar")
    s.close()

    assert response.status_code == 200
    assert store.deletes() == []
    assert store.files["mods/fabric-api-0.92.12.jar"] == BYTES["fa-2"]


def test_failed_update_keeps_the_old_jar(session, store):
    store.upload_error = (507, "NO STORAGE SPACE")
    response = update(session, "fabric-api.jar")

    assert response.status_code == 502
    assert store.files["mods/fabric-api.jar"] == b"fabric-api-0.83.0"
    assert store.deletes() == []
    assert session.mods()["fabric-api.jar"]["version_id"] == "fa-1"


@pytest.mark.parametrize(
    ("file_name", "prepare", "status", "detail"),
    [("nope.jar", None, 404, "No adopted mod with that file name."),
     ("sodium.jar", None, 409, "Sodium has no update available."),
     ("custom.jar", None, 409, "custom.jar is not on Modrinth, so it cannot be updated."),
     ("fabric-api.jar", "pin", 409, "Fabric API is pinned; unpin it to update.")],
)
def test_update_refusals(file_name, prepare, status, detail, session, store):
    if prepare == "pin":
        session.client.patch(f"/api/servers/{SERVER}/mods/{file_name}", json={"pinned": True})
    response = update(session, file_name)

    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert store.uploads() == []


def test_update_installs_new_required_dependencies(session, store):
    install(session, "P3")
    # Pretend the installed Lithium is the older li-1 with li-2 available.
    session.client.app.state.adoption_store.update_mod(
        SERVER, "lithium-0.11.2.jar", version_id="li-1", version_number="0.11.1",
        latest_version_id="li-2", latest_version_number="0.11.2")
    store.files.pop("mods/cloth-config-11.1.jar")
    session.client.app.state.adoption_store.remove_mod(SERVER, "cloth-config-11.1.jar")

    body = update(session, "lithium-0.11.2.jar").json()

    assert [m["project_id"] for m in body["installed"]] == ["P4"]


# --- update all ---------------------------------------------------------------

def test_update_all_updates_every_unpinned_mod_and_reports_failures(store, db):
    store.latest[SODIUM_SHA] = {"id": "so-2", "project_id": "P2", "version_number": "0.6.0"}
    s = Session(store, db)
    response = s.post(f"/api/servers/{SERVER}/update-all")
    s.close()

    body = response.json()
    assert response.status_code == 200
    assert [u["project_id"] for u in body["updated"]] == ["P1"]
    assert body["failed"] == [{"project_id": "P2", "title": "Sodium", "file_name": "sodium.jar",
                               "reason": "Modrinth has no version so-2."}]
    assert by_file(body["server"])["sodium.jar"]["version_id"] == "so-1"


def test_update_all_skips_pinned_mods(session, store):
    session.client.patch(f"/api/servers/{SERVER}/mods/fabric-api.jar", json={"pinned": True})
    body = session.post(f"/api/servers/{SERVER}/update-all").json()

    assert body["updated"] == []
    assert store.uploads() == []


# --- update checks ------------------------------------------------------------

def test_check_updates_refreshes_latest_versions_without_downloading_jars(session, store):
    store.latest[SODIUM_SHA] = {"id": "so-2", "project_id": "P2", "version_number": "0.6.0"}
    response = session.post(f"/api/servers/{SERVER}/check-updates")

    assert response.status_code == 200
    body = response.json()
    assert by_file(body)["sodium.jar"]["update_available"] is True
    assert by_file(body)["sodium.jar"]["latest_version_number"] == "0.6.0"
    assert body["counts"]["updates"] == 2
    assert body["updates_checked_at"]
    assert store.crafty_calls("/download") == []


def test_background_checker_refreshes_every_adopted_server(session, store):
    store.latest[SODIUM_SHA] = {"id": "so-2", "project_id": "P2", "version_number": "0.6.0"}
    checked = session.client.portal.call(session.client.app.state.update_checker.run_once)

    assert checked == 1
    assert session.mods()["sodium.jar"]["update_available"] is True
    assert [r for r in store.requests if r.url.host == "crafty.local"
            and "permission-check" not in r.url.path] == []


def test_update_checker_survives_modrinth_outage(session, store):
    store.modrinth_down = True
    checked = session.client.portal.call(session.client.app.state.update_checker.run_once)

    assert checked == 0
    assert session.mods()["fabric-api.jar"]["latest_version_id"] == "fa-2"


def test_scan_reports_when_updates_were_checked(session):
    body = session.client.get(f"/api/servers/{SERVER}/mods").json()

    assert body["updates_checked_at"] == body["scanned_at"]


# --- authorization and CSRF ---------------------------------------------------

@pytest.mark.parametrize("path", [
    f"/api/servers/{SERVER}/mods", f"/api/servers/{SERVER}/mods/fabric-api.jar/update",
    f"/api/servers/{SERVER}/update-all", f"/api/servers/{SERVER}/check-updates",
])
def test_mutations_need_files_permission(path, session, store):
    store.download_error = (400, "NOT_AUTHORIZED")
    response = session.post(path, {"project_id": "P4"})

    assert response.status_code == 403
    assert store.uploads() == []


@pytest.mark.parametrize("path", [
    f"/api/servers/{SERVER}/mods/fabric-api.jar/update",
    f"/api/servers/{SERVER}/update-all", f"/api/servers/{SERVER}/check-updates",
])
def test_bodyless_mutations_require_json(path, session, store):
    response = session.client.post(path, data={"x": "1"})

    assert response.status_code == 415
    assert store.uploads() == []


def test_server_page_offers_update_all(session):
    page = session.client.get(f"/servers/{SERVER}").text

    assert 'id="update-all"' in page
    assert 'id="check-updates"' in page


def test_browse_page_offers_install_target(session):
    page = session.client.get("/").text

    assert 'id="target-server"' in page
