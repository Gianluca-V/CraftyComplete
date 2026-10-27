import hashlib
import io
import json
import time
import zipfile

import httpx
import pytest

from crafty_complete.modpacks.mrpack import MrpackError, read_index
from tests.conftest import CRAFTY, USER_TOKEN, log_in, make_client
from tests.test_install import FakeServices, sha1, sha512

NEW_SERVER = "new-server-1"
MOD_A = b"mod-a-bytes"
MOD_B = b"mod-b-bytes-from-github"
CLIENT_MOD = b"client-only-mod"
OVERRIDE_JAR = b"bundled-jar"


def pack_file(path, data, server="required", host="https://cdn.modrinth.com/data/x/versions/y"):
    return {
        "path": path,
        "hashes": {"sha1": sha1(data), "sha512": sha512(data)},
        "env": {"client": "required", "server": server},
        "downloads": [f"{host}/{path.split('/')[-1]}"],
        "fileSize": len(data),
    }


def build_mrpack(dependencies=None, files=None, entries=None, index_overrides=None) -> bytes:
    index = {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": "1.0.0",
        "name": "Test Pack",
        "files": files if files is not None else [
            pack_file("mods/mod-a.jar", MOD_A),
            pack_file("mods/mod-b.jar", MOD_B, host="https://github.com/owner/repo/releases/download/v1"),
            pack_file("mods/client-only.jar", CLIENT_MOD, server="unsupported"),
        ],
        "dependencies": dependencies or {"minecraft": "1.20.1", "fabric-loader": "0.15.0"},
        **(index_overrides or {}),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("modrinth.index.json", json.dumps(index))
        for name, data in (entries if entries is not None else {
            "overrides/config/a.toml": b"from-overrides",
            "overrides/config/b.toml": b"only-in-overrides",
            "overrides/mods/bundled.jar": OVERRIDE_JAR,
            "server-overrides/config/a.toml": b"from-server-overrides",
            "client-overrides/options.txt": b"client",
        }).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def write_pack(tmp_path, data: bytes):
    path = tmp_path / "pack.mrpack"
    path.write_bytes(data)
    return path


# --- mrpack parsing ------------------------------------------------------------

def test_index_selects_server_files_and_overrides(tmp_path):
    index = read_index(write_pack(tmp_path, build_mrpack()))

    assert index.name == "Test Pack"
    assert index.minecraft == "1.20.1"
    assert index.loader == "fabric"
    assert index.loader_version == "0.15.0"
    assert [f.path for f in index.server_files] == ["mods/mod-a.jar", "mods/mod-b.jar"]
    assert index.overrides == {
        "config/a.toml": "server-overrides/config/a.toml",
        "config/b.toml": "overrides/config/b.toml",
        "mods/bundled.jar": "overrides/mods/bundled.jar",
    }


@pytest.mark.parametrize(("dependencies", "loader"), [
    ({"minecraft": "1.20.1", "forge": "47.2.0"}, "forge"),
    ({"minecraft": "1.20.1", "neoforge": "47.1.99"}, "neoforge"),
])
def test_index_detects_forge_and_neoforge(tmp_path, dependencies, loader):
    index = read_index(write_pack(tmp_path, build_mrpack(dependencies=dependencies)))

    assert index.loader == loader


@pytest.mark.parametrize(("pack", "message"), [
    (build_mrpack(dependencies={"minecraft": "1.20.1", "quilt-loader": "0.26.0"}),
     "Quilt modpacks are not supported yet."),
    (build_mrpack(dependencies={"minecraft": "1.20.1"}),
     "The modpack does not name a Fabric, Forge or NeoForge loader."),
    (build_mrpack(index_overrides={"formatVersion": 2}),
     "Unsupported modpack format version 2."),
    (build_mrpack(index_overrides={"game": "terraria"}), "This modpack is not for Minecraft."),
    (build_mrpack(files=[pack_file("../evil.jar", MOD_A)]),
     "The modpack contains an unsafe path: ../evil.jar"),
    (build_mrpack(files=[pack_file("/etc/passwd", MOD_A)]),
     "The modpack contains an unsafe path: /etc/passwd"),
    (build_mrpack(files=[pack_file("C:/x.jar", MOD_A)]),
     "The modpack contains an unsafe path: C:/x.jar"),
    (build_mrpack(entries={"overrides/../../evil.sh": b"x"}),
     "The modpack contains an unsafe path: overrides/../../evil.sh"),
    (build_mrpack(files=[pack_file("mods/x.jar", MOD_A, host="https://evil.example.com")]),
     "mods/x.jar is downloaded from a host Modrinth does not allow: evil.example.com"),
    (b"not a zip", "The downloaded file is not a valid .mrpack."),
])
def test_index_rejects_bad_packs(tmp_path, pack, message):
    with pytest.raises(MrpackError) as error:
        read_index(write_pack(tmp_path, pack))

    assert str(error.value) == message


# --- fakes ---------------------------------------------------------------------

PACK_VERSION_ID = "pk-1"


class CreationFakes(FakeServices):
    """FakeServices plus Crafty server creation and a downloadable modpack."""

    def __init__(self, pack: bytes | None = None, loader="fabric", **kwargs):
        super().__init__(**kwargs)
        self.pack = pack if pack is not None else build_mrpack()
        self.loader = loader
        self.created: list[dict] = []
        self.stats_polls = 0
        # Crafty sleeps 3 s before marking the server as importing, so the first poll
        # sees importing=False with no jar yet; then importing for this many polls.
        self.importing_polls = 2
        self.jar_ready = False
        self.upload_before_jar = False
        # Folders that exist in the new server (Crafty's upload cannot create them).
        self.folders = {""}
        self.created_folders: list[str] = []
        # Fail the first N requests to these paths with 503, then behave.
        self.flaky: dict[str, int] = {}
        self.installer_polls = 2  # execution_command keeps --installServer this long
        self.install_never_finishes = False
        self.can_create = True
        self.server_files: dict[str, bytes] = {}
        self.chunks: dict[str, dict[int, bytes]] = {}
        self.cdn_extra = {"mod-a.jar": MOD_A, "mod-b.jar": MOD_B}
        # sha1 -> Modrinth version, to override the default identification fake.
        self.identified: dict[str, dict] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host in ("cdn.modrinth.com", "github.com"):
            name = request.url.path.split("/")[-1]
            if name == "pack.mrpack":
                self.requests.append(request)
                return httpx.Response(200, content=self.pack)
            if name in self.cdn_extra:
                self.requests.append(request)
                return httpx.Response(200, content=self.cdn_extra[name])
        return super().__call__(request)

    def _modrinth(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/version_files" and self.identified is not None:
            hashes = json.loads(request.content)["hashes"]
            return httpx.Response(200, json={h: v for h, v in self.identified.items()
                                             if h in hashes})
        version = {
            "id": PACK_VERSION_ID, "project_id": "PM", "version_number": "1.0.0",
            "version_type": "release", "loaders": [self.loader], "game_versions": ["1.20.1"],
            "date_published": "2026-10-01T00:00:00Z", "dependencies": [],
            "files": [{"url": "https://cdn.modrinth.com/data/PM/versions/pk-1/pack.mrpack",
                       "filename": "pack.mrpack", "primary": True, "size": len(self.pack),
                       "hashes": {"sha1": sha1(self.pack), "sha512": sha512(self.pack)}}],
        }
        if path == f"/v2/version/{PACK_VERSION_ID}":
            return httpx.Response(200, json=version)
        if path == "/v2/project/PM/version":
            return httpx.Response(200, json=[version])
        return super()._modrinth(request)

    def _crafty(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for suffix, remaining in list(self.flaky.items()):
            if path.endswith(suffix) and remaining:
                self.flaky[suffix] = remaining - 1
                return httpx.Response(503, text="Service Unavailable")
        if request.headers.get("Authorization") != f"Bearer {USER_TOKEN}":
            return httpx.Response(403, json={"status": "error", "error": "ACCESS_DENIED"})
        if path == "/api/v2/users/@me/permissions":
            return httpx.Response(200, json={"status": "ok", "data": {"permissions": "100"}})
        if path == "/api/v2/crafty/JarCache":
            types = {
                "fabric": {"versions": {"1.20.1": {"loader_version": "0.16.9"}}},
                "forge-installer": {"versions": {"1.20.1": {"loader_version": "47.4.0"}}},
                "neoforge-installer": {"versions": {"1.20.1": {"loader_version": "47.1.106"}}},
            }
            return httpx.Response(200, json={"status": "ok", "data": {
                "mc_java_servers": {"types": types}}})
        if path == "/api/v2/servers" and request.method == "POST":
            if not self.can_create:
                return httpx.Response(400, json={"status": "error", "error": "NOT_AUTHORIZED"})
            self.created.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "ok", "data": {
                "new_server_id": NEW_SERVER, "new_server_uuid": NEW_SERVER}})
        if path == f"/api/v2/servers/{NEW_SERVER}/stats":
            self.stats_polls += 1
            importing = 2 <= self.stats_polls <= 1 + self.importing_polls
            if self.stats_polls > 1 + self.importing_polls and not self.install_never_finishes:
                self.jar_ready = True
            return httpx.Response(200, json={"status": "ok", "data": {"importing": importing}})
        if path == f"/api/v2/servers/{NEW_SERVER}/files" and request.method == "POST":
            folder = json.loads(request.content)["path"]
            data = {"root_path": {"local_path": folder}}
            if folder == "" and self.jar_ready:
                data["fabric.jar"] = {"path": "fabric.jar", "dir": False}
            for name in sorted(self.folders - {""}):
                parent, _, leaf = name.rpartition("/")
                if parent == folder:
                    data[leaf] = {"path": name, "dir": True}
            return httpx.Response(200, json={"status": "ok", "data": data})
        if path == f"/api/v2/servers/{NEW_SERVER}/files/create" and request.method == "PUT":
            body = json.loads(request.content)
            assert body["directory"] is True
            target = f"{body['parent']}/{body['name']}".strip("/")
            if body["parent"].strip("/") not in self.folders:
                return httpx.Response(500, text="FileNotFoundError")  # os.mkdir, not makedirs
            if target in self.folders:
                return httpx.Response(400, json={"status": "error", "error": "FILE EXISTS"})
            self.folders.add(target)
            self.created_folders.append(target)
            return httpx.Response(200, json={"status": "ok"})
        if path == f"/api/v2/servers/{NEW_SERVER}" and request.method == "GET":
            self.installer_polls -= 1
            installing = self.install_never_finishes or self.installer_polls >= 0
            command = ('java -Xms1024M -Xmx4096M -jar "forge-installer-1.20.1.jar" --installServer'
                       if installing and self.loader != "fabric"
                       else "java -Xms1024M -Xmx4096M @libraries/x/unix_args.txt nogui")
            return httpx.Response(200, json={"status": "ok", "data": {
                "server_id": NEW_SERVER, "server_name": "My Pack", "execution_command": command,
                "executable": "fabric.jar" if self.loader == "fabric"
                else "forge-installer-1.20.1.jar"}})
        if path == f"/api/v2/servers/{NEW_SERVER}/files/upload":
            return self._new_server_upload(request)
        if path.startswith(f"/api/v2/servers/{NEW_SERVER}/files/") and path.endswith("/download"):
            return httpx.Response(404, json={"status": "error", "error": "File not found"})
        return super()._crafty(request)

    def _new_server_upload(self, request: httpx.Request) -> httpx.Response:
        headers = request.headers
        location, name = headers["location"], headers["fileName"]
        if location.strip("/") not in self.folders:
            # Crafty checks free disk space on the folder before creating it -> HTTP 500.
            return httpx.Response(500, text="FileNotFoundError")
        if self.loader == "fabric" and not self.jar_ready:
            self.upload_before_jar = True
        target = f"{location}/{name}".lstrip("/")
        if headers.get("chunked") == "true":
            file_id = headers["fileId"]
            if "chunkId" not in headers:
                self.chunks[file_id] = {}
                return httpx.Response(200, json={"status": "ok", "data": {"file-id": file_id}})
            assert headers["chunkHash"] == hashlib.sha256(request.content).hexdigest()
            parts = self.chunks[file_id]
            parts[int(headers["chunkId"])] = request.content
            if len(parts) < int(headers["totalChunks"]):
                return httpx.Response(200, json={"status": "partial"})
            self.server_files[target] = b"".join(parts[i] for i in range(len(parts)))
            return httpx.Response(200, json={"status": "completed"})
        assert "chunked" not in headers
        self.server_files[target] = request.content
        return httpx.Response(200, json={"status": "completed"})


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "db.sqlite3")


def creation_client(fakes, db, tmp_path, **settings):
    settings.setdefault("update_check_hours", 0)
    settings.setdefault("modpack_poll_seconds", 0)
    settings.setdefault("modpack_work_dir", str(tmp_path / "work"))
    client = make_client(fakes, crafty_url=CRAFTY, database_path=db, **settings)
    client.__enter__()
    log_in(client)
    return client


def start(client, **body):
    payload = {"project_id": "PM", "version_id": PACK_VERSION_ID, "name": "My Pack",
               "port": 25570, "mem_min": 1, "mem_max": 4, "agree_to_eula": True, **body}
    return client.post("/api/modpacks/servers", json=payload)


def wait_for(client, job_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


# --- creating a server ------------------------------------------------------------

def test_creates_fabric_server_from_modpack(tmp_path, db):
    fakes = CreationFakes()
    client = creation_client(fakes, db, tmp_path)
    response = start(client)
    assert response.status_code == 202
    job = wait_for(client, response.json()["job_id"])
    mods = client.get(f"/api/servers/{NEW_SERVER}/mods")
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert job["server_id"] == NEW_SERVER
    assert job["error"] is None
    assert job["warnings"] == [
        "The pack asks for fabric-loader 0.15.0; Crafty installs 0.16.9 for Minecraft 1.20.1."]

    assert fakes.created == [{
        "name": "My Pack",
        "monitoring_type": "minecraft_java",
        "minecraft_java_monitoring_data": {"host": "127.0.0.1", "port": 25570},
        "create_type": "minecraft_java",
        "minecraft_java_create_data": {
            "create_type": "download_jar",
            "download_jar_create_data": {
                "category": "mc_java_servers", "type": "fabric", "version": "1.20.1",
                "mem_min": 1, "mem_max": 4, "server_properties_port": 25570,
                "agree_to_eula": True,
            },
        },
    }]
    assert fakes.server_files == {
        "mods/mod-a.jar": MOD_A,
        "mods/mod-b.jar": MOD_B,
        "mods/bundled.jar": OVERRIDE_JAR,
        "config/a.toml": b"from-server-overrides",
        "config/b.toml": b"only-in-overrides",
    }

    body = mods.json()
    assert body["adopted"] is True
    assert body["loader"] == "fabric"
    assert body["game_version"] == "1.20.1"
    assert body["modpack"] == {"project_id": "PM", "title": "A Pack",
                               "version_id": PACK_VERSION_ID, "version_number": "1.0.0"}
    assert sorted(m["file_name"] for m in body["mods"]) == [
        "bundled.jar", "mod-a.jar", "mod-b.jar"]


def test_creates_forge_server_after_crafty_installer_finishes(tmp_path, db):
    fakes = CreationFakes(pack=build_mrpack(dependencies={"minecraft": "1.20.1",
                                                          "forge": "47.4.0"}), loader="forge")
    client = creation_client(fakes, db, tmp_path)
    job = wait_for(client, start(client).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert job["warnings"] == []
    assert fakes.created[0]["minecraft_java_create_data"]["download_jar_create_data"][
        "type"] == "forge-installer"
    # Files are only uploaded after the installer has rewritten the start command.
    assert fakes.installer_polls < 0
    assert "mods/mod-a.jar" in fakes.server_files


def test_installer_that_never_finishes_fails_naming_the_server(tmp_path, db):
    fakes = CreationFakes(pack=build_mrpack(dependencies={"minecraft": "1.20.1",
                                                          "neoforge": "47.1.106"}),
                          loader="neoforge")
    fakes.install_never_finishes = True
    client = creation_client(fakes, db, tmp_path, modpack_install_timeout_minutes=0.001)
    job = wait_for(client, start(client).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "failed"
    assert job["server_id"] == NEW_SERVER
    assert job["error"] == (
        "Crafty created \"My Pack\" but its NeoForge installer did not finish in time. "
        "Check the server's terminal in Crafty, then delete the server there and try again.")
    assert fakes.server_files == {}


def test_folders_are_created_parents_first_before_uploading(tmp_path, db):
    fakes = CreationFakes(pack=build_mrpack(entries={
        "overrides/config/deep/nested/c.toml": b"deep", "overrides/config/a.toml": b"a"}))
    client = creation_client(fakes, db, tmp_path)
    job = wait_for(client, start(client).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert fakes.server_files["config/deep/nested/c.toml"] == b"deep"
    order = fakes.created_folders
    assert order.index("config") < order.index("config/deep") < order.index("config/deep/nested")
    assert "mods" in order


def test_existing_folders_are_reused(tmp_path, db):
    fakes = CreationFakes()
    fakes.folders |= {"mods", "config"}
    client = creation_client(fakes, db, tmp_path)
    job = wait_for(client, start(client).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert "mods/mod-a.jar" in fakes.server_files


def test_fabric_wait_covers_crafty_start_delay(tmp_path, db):
    """Crafty reports importing=False for about 3 s before its download starts."""
    fakes = CreationFakes()
    client = creation_client(fakes, db, tmp_path)
    job = wait_for(client, start(client).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert fakes.upload_before_jar is False
    assert fakes.stats_polls >= 4


def test_fabric_download_that_never_finishes_fails(tmp_path, db):
    fakes = CreationFakes()
    fakes.install_never_finishes = True
    client = creation_client(fakes, db, tmp_path, modpack_install_timeout_minutes=0.001)
    job = wait_for(client, start(client).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "failed"
    assert job["error"].startswith('Crafty created "My Pack" but its Fabric download did not')
    assert fakes.server_files == {}


def test_duplicate_paths_are_rejected(tmp_path):
    pack = build_mrpack(files=[pack_file("mods/a.jar", MOD_A), pack_file("mods/a.jar", MOD_B)])
    with pytest.raises(MrpackError) as error:
        read_index(write_pack(tmp_path, pack))

    assert str(error.value) == "The modpack lists mods/a.jar more than once."


def test_oversized_overrides_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("crafty_complete.modpacks.mrpack.MAX_OVERRIDES_BYTES", 10)
    with pytest.raises(MrpackError) as error:
        read_index(write_pack(tmp_path, build_mrpack()))

    assert str(error.value) == "The modpack's overrides unpack to more than 10 bytes."


def test_unexpected_errors_do_not_leak_details(tmp_path, db, monkeypatch):
    async def boom(*args, **kwargs):
        raise OSError("[Errno 2] C:/secret/path/work")

    fakes = CreationFakes()
    client = creation_client(fakes, db, tmp_path)
    monkeypatch.setattr(client.app.state.catalog, "download_to_file", boom)
    job = wait_for(client, start(client).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "failed"
    assert job["error"] == "Unexpected error; see the craftyComplete log for details."


def test_api_prefers_the_newest_release(tmp_path, db):
    fakes = CreationFakes()
    client = creation_client(fakes, db, tmp_path)
    original = fakes._modrinth

    def with_beta(request):
        if request.url.path == "/v2/project/PM/version":
            release = original(request).json()[0]
            beta = {**release, "id": "pk-beta", "version_number": "2.0-beta",
                    "version_type": "beta"}
            return httpx.Response(200, json=[beta, release])
        return original(request)

    fakes._modrinth = with_beta
    response = start(client, version_id=None)
    job = wait_for(client, response.json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert job["title"] == "A Pack 1.0.0"


def test_transient_crafty_errors_are_retried(tmp_path, db):
    fakes = CreationFakes()
    fakes.flaky = {"/stats": 2, "/files/upload": 2, "/files/create": 1}
    client = creation_client(fakes, db, tmp_path, crafty_retry_seconds=0)
    job = wait_for(client, start(client).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert "mods/mod-a.jar" in fakes.server_files


def test_failure_after_creation_names_the_server(tmp_path, db):
    fakes = CreationFakes()
    fakes.flaky = {"/files/upload": 100}
    client = creation_client(fakes, db, tmp_path, crafty_retry_seconds=0)
    job = wait_for(client, start(client).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "failed"
    assert job["error"] == (
        'Crafty request failed: HTTP 503. The server "My Pack" was created in Crafty but is '
        "not complete; delete it there before trying again.")


def test_one_running_job_per_user(tmp_path, db):
    fakes = CreationFakes()
    fakes.install_never_finishes = True
    client = creation_client(fakes, db, tmp_path, modpack_poll_seconds=0.05)
    first = start(client)
    second = start(client, name="Another")
    client.__exit__(None, None, None)

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json() == {"detail": "You already have a modpack job running; "
                                       "wait for it to finish."}


def test_oversized_server_file_total_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("crafty_complete.modpacks.mrpack.MAX_SERVER_FILES_BYTES", 5)
    with pytest.raises(MrpackError) as error:
        read_index(write_pack(tmp_path, build_mrpack()))

    assert str(error.value) == "The modpack's server files add up to more than 5 bytes."


def test_oversized_index_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("crafty_complete.modpacks.mrpack.MAX_INDEX_BYTES", 10)
    with pytest.raises(MrpackError) as error:
        read_index(write_pack(tmp_path, build_mrpack()))

    assert str(error.value) == "The modpack's index is larger than 10 bytes."


def test_picks_a_minecraft_version_crafty_supports(tmp_path, db):
    fakes = CreationFakes()
    original = fakes._modrinth

    def two_versions(request):
        response = original(request)
        if request.url.path == f"/v2/version/{PACK_VERSION_ID}":
            body = response.json()
            body["game_versions"] = ["1.20", "1.20.1"]
            return httpx.Response(200, json=body)
        return response

    fakes._modrinth = two_versions
    client = creation_client(fakes, db, tmp_path)
    job = wait_for(client, start(client).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert fakes.created[0]["minecraft_java_create_data"]["download_jar_create_data"][
        "version"] == "1.20.1"


def test_job_interrupted_by_shutdown_is_marked_failed(tmp_path, db):
    fakes = CreationFakes()
    fakes.install_never_finishes = True
    client = creation_client(fakes, db, tmp_path, modpack_poll_seconds=0.05)
    job_id = start(client).json()["job_id"]
    time.sleep(0.3)
    jobs = client.app.state.jobs
    client.__exit__(None, None, None)
    job = jobs.get(job_id, "1")

    assert job.status == "failed"
    assert job.error.startswith("craftyComplete stopped while this job was running.")


def test_client_only_mods_are_skipped_even_without_env(tmp_path, db):
    """Many packs declare no `env`; Modrinth's project data still says what is client-only."""
    fakes = CreationFakes()
    fakes.identified = {
        sha1(MOD_A): {"id": "fa-1", "project_id": "P1", "version_number": "0.83.0",
                      "loaders": ["fabric"], "game_versions": ["1.20.1"]},
        sha1(MOD_B): {"id": "so-1", "project_id": "P2", "version_number": "0.5.0",
                      "loaders": ["fabric"], "game_versions": ["1.20.1"]},
    }
    client = creation_client(fakes, db, tmp_path)
    job = wait_for(client, start(client).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert "mods/mod-b.jar" not in fakes.server_files
    assert "mods/mod-a.jar" in fakes.server_files
    assert ("Skipped 1 client-only mod (not needed on a server): Sodium (mods/mod-b.jar)."
            in job["warnings"])


def test_large_files_use_chunked_upload(tmp_path, db):
    fakes = CreationFakes()
    client = creation_client(fakes, db, tmp_path, upload_chunk_bytes=4)
    job = wait_for(client, start(client).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded", job
    assert fakes.server_files["mods/mod-b.jar"] == MOD_B


def test_bad_file_hash_fails_before_creating_anything(tmp_path, db):
    fakes = CreationFakes()
    fakes.cdn_extra["mod-a.jar"] = b"tampered"
    client = creation_client(fakes, db, tmp_path)
    job = wait_for(client, start(client).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "failed"
    assert job["server_id"] is None
    assert job["error"] == "mods/mod-a.jar does not match the hash in the modpack."
    assert fakes.created == []


def test_work_directory_is_removed_after_the_job(tmp_path, db):
    fakes = CreationFakes()
    client = creation_client(fakes, db, tmp_path)
    wait_for(client, start(client).json()["job_id"])
    client.__exit__(None, None, None)

    assert list((tmp_path / "work").iterdir()) == []


def test_non_ascii_override_is_skipped_with_a_warning(tmp_path, db):
    fakes = CreationFakes(pack=build_mrpack(entries={"overrides/config/señal.toml": b"x"}))
    client = creation_client(fakes, db, tmp_path)
    job = wait_for(client, start(client).json()["job_id"])
    client.__exit__(None, None, None)

    assert job["status"] == "succeeded"
    assert ("config/señal.toml could not be sent to Crafty (non-ASCII name); "
            "upload it yourself.") in job["warnings"]


# --- validation before the job starts -------------------------------------------

@pytest.mark.parametrize(("body", "status", "detail"), [
    ({"agree_to_eula": False}, 422,
     "You must accept the Minecraft EULA (https://aka.ms/MinecraftEULA) to create a server."),
    ({"project_id": "P1"}, 422, "Fabric API is a mod, not a modpack."),
    ({"mem_min": 8, "mem_max": 4}, 422, "Minimum memory cannot exceed maximum memory."),
])
def test_rejects_invalid_requests(tmp_path, db, body, status, detail):
    fakes = CreationFakes()
    client = creation_client(fakes, db, tmp_path)
    response = start(client, **body)
    client.__exit__(None, None, None)

    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert fakes.created == []


def test_rejects_quilt_packs_before_downloading(tmp_path, db):
    fakes = CreationFakes(loader="quilt")
    client = creation_client(fakes, db, tmp_path)
    response = start(client)
    client.__exit__(None, None, None)

    assert response.status_code == 422
    assert response.json() == {"detail": "Quilt modpacks are not supported yet."}
    assert not [r for r in fakes.requests if r.url.path.endswith(".mrpack")]


def test_rejects_minecraft_versions_crafty_cannot_install(tmp_path, db):
    fakes = CreationFakes()
    client = creation_client(fakes, db, tmp_path)
    original = fakes._crafty

    def no_catalog(request):
        if request.url.path == "/api/v2/crafty/JarCache":
            return httpx.Response(200, json={"status": "ok", "data": {
                "mc_java_servers": {"types": {"fabric": {"versions": {}}}}}})
        return original(request)

    fakes._crafty = no_catalog
    response = start(client)
    client.__exit__(None, None, None)

    assert response.status_code == 422
    assert response.json() == {"detail": "Crafty cannot install Fabric for Minecraft 1.20.1."}


def test_requires_crafty_server_creation_permission(tmp_path, db):
    fakes = CreationFakes()
    client = creation_client(fakes, db, tmp_path)
    original = fakes._crafty

    def no_permission(request):
        if request.url.path == "/api/v2/users/@me/permissions":
            return httpx.Response(200, json={"status": "ok", "data": {"permissions": "000"}})
        return original(request)

    fakes._crafty = no_permission
    client.app.state.sessions.get(client.cookies.get("cc_session")).superuser = False
    response = start(client)
    client.__exit__(None, None, None)

    assert response.status_code == 403
    assert response.json() == {"detail": "Your Crafty account cannot create servers."}


# --- jobs, versions and pages ------------------------------------------------------

def test_jobs_are_private_to_their_owner(tmp_path, db):
    fakes = CreationFakes()
    client = creation_client(fakes, db, tmp_path)
    job_id = start(client).json()["job_id"]
    wait_for(client, job_id)
    other = client.app.state.sessions.create(crafty_token=USER_TOKEN, user_id="2",
                                             username="alex", superuser=False)
    client.cookies.set("cc_session", other.id)
    response = client.get(f"/api/jobs/{job_id}")
    client.__exit__(None, None, None)

    assert response.status_code == 404


def test_lists_modpack_versions(tmp_path, db):
    fakes = CreationFakes()
    client = creation_client(fakes, db, tmp_path)
    response = client.get("/api/modpacks/PM/versions")
    client.__exit__(None, None, None)

    assert response.status_code == 200
    assert response.json() == {"project_id": "PM", "title": "A Pack", "versions": [{
        "id": PACK_VERSION_ID, "version_number": "1.0.0", "version_type": "release",
        "loaders": ["fabric"],
        "game_versions": ["1.20.1"], "date_published": "2026-10-01T00:00:00Z",
        "supported": True}]}


def test_create_page_renders(tmp_path, db):
    fakes = CreationFakes()
    client = creation_client(fakes, db, tmp_path)
    page = client.get("/modpacks/PM/create")
    client.__exit__(None, None, None)

    assert page.status_code == 200
    assert 'id="create-form"' in page.text
    assert 'name="agree_to_eula"' in page.text
    assert 'data-project="PM"' in page.text
