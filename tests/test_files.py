import hashlib
import json
from urllib.parse import unquote

import httpx
import pytest

from tests.conftest import CRAFTY, USER_TOKEN, make_client, make_logged_in_client

SERVER = "9b1968d1-0000-4000-8000-000000000002"
FILES = f"/api/servers/{SERVER}/files"
CONTENT = f"{FILES}/content"

PROPERTIES = b"motd=A Minecraft Server\nonline-mode=true\n"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeCrafty:
    """Fake Crafty file API: list, download, upload."""

    def __init__(self, files=None, forbidden=False):
        self.files: dict[str, bytes] = {"server.properties": PROPERTIES,
                                        "config/sodium.json": b"{}\n"}
        if files is not None:
            self.files = files
        self.forbidden = forbidden
        self.uploads: list[tuple[str, str]] = []
        self.calls = 0

    def folders(self) -> set[str]:
        found = {""}
        for name in self.files:
            parts = name.split("/")[:-1]
            for index in range(1, len(parts) + 1):
                found.add("/".join(parts[:index]))
        return found

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        assert request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
        if self.forbidden:
            return httpx.Response(400, json={"status": "error", "error": "NOT_AUTHORIZED"})
        base = f"/api/v2/servers/{SERVER}"
        path = request.url.path
        if path == f"{base}/files" and request.method == "POST":
            folder = json.loads(request.content)["path"]
            prefix = f"{folder}/" if folder else ""
            data = {"root_path": {"path": folder, "top": folder == ""}}
            for name in self.files:
                if name.startswith(prefix):
                    child = name[len(prefix):].split("/")[0]
                    data[child] = {"path": prefix + child, "dir": "/" in name[len(prefix):]}
            return httpx.Response(200, json={"status": "ok", "data": data})
        if path.startswith(f"{base}/files/") and path.endswith("/download"):
            name = unquote(path[len(f"{base}/files/"):-len("/download")])
            if name not in self.files:
                return httpx.Response(404, json={"status": "error", "error": "File not found"})
            return httpx.Response(200, content=self.files[name])
        if path == f"{base}/files/upload":
            location, name = request.headers["location"], request.headers["fileName"]
            assert location in self.folders()
            key = f"{location}/{name}" if location else name
            self.files[key] = request.content
            self.uploads.append((location, name))
            return httpx.Response(200, json={"status": "completed", "data": {}})
        raise AssertionError(f"unexpected call {request.method} {path}")


@pytest.fixture
def crafty():
    return FakeCrafty()


def client_for(crafty):
    return make_logged_in_client(crafty, crafty_url=CRAFTY, update_check_hours=0)


def save(client, path, content, sha):
    return client.put(CONTENT, json={"path": path, "content": content, "sha256": sha})


# --- browsing --------------------------------------------------------------------

def test_lists_the_server_root_folders_first(crafty):
    crafty.files["eula.txt"] = b"eula=true\n"
    with client_for(crafty) as client:
        response = client.get(FILES)

    assert response.status_code == 200
    assert response.json() == {"path": "", "entries": [
        {"name": "config", "path": "config", "is_dir": True},
        {"name": "eula.txt", "path": "eula.txt", "is_dir": False},
        {"name": "server.properties", "path": "server.properties", "is_dir": False},
    ]}


def test_lists_a_subfolder_with_server_relative_paths(crafty):
    with client_for(crafty) as client:
        response = client.get(FILES, params={"path": "config"})

    assert response.json() == {"path": "config", "entries": [
        {"name": "sodium.json", "path": "config/sodium.json", "is_dir": False}]}


# --- reading ---------------------------------------------------------------------

def test_opens_a_text_file(crafty):
    with client_for(crafty) as client:
        response = client.get(CONTENT, params={"path": "server.properties"})

    assert response.status_code == 200
    assert response.json() == {"path": "server.properties", "content": PROPERTIES.decode(),
                               "sha256": sha256(PROPERTIES), "size": len(PROPERTIES)}


def test_opening_a_missing_file_is_404(crafty):
    with client_for(crafty) as client:
        response = client.get(CONTENT, params={"path": "options.txt"})

    assert response.status_code == 404
    assert response.json()["detail"] == "options.txt does not exist on the server."


@pytest.mark.parametrize("data", [b"PK\x03\x04\x00\x00binary", b"caf\xe9\n"])
def test_binary_or_non_utf8_files_are_refused(crafty, data):
    crafty.files["mods/sodium.jar"] = data
    with client_for(crafty) as client:
        response = client.get(CONTENT, params={"path": "mods/sodium.jar"})

    assert response.status_code == 415
    assert response.json()["detail"] == ("mods/sodium.jar is not a UTF-8 text file, "
                                         "so it cannot be edited here.")


def test_files_over_one_mebibyte_are_refused(crafty):
    crafty.files["logs/latest.log"] = b"x" * (1024 * 1024 + 1)
    with client_for(crafty) as client:
        response = client.get(CONTENT, params={"path": "logs/latest.log"})

    assert response.status_code == 413
    assert response.json()["detail"] == ("logs/latest.log is larger than 1 MiB, "
                                         "so it cannot be edited here.")


# --- saving ----------------------------------------------------------------------

def test_saves_a_root_file(crafty):
    new = "motd=Welcome\nonline-mode=false\n"
    with client_for(crafty) as client:
        response = save(client, "server.properties", new, sha256(PROPERTIES))

    assert response.status_code == 200
    assert response.json() == {"path": "server.properties", "sha256": sha256(new.encode()),
                               "size": len(new.encode())}
    assert crafty.uploads == [("", "server.properties")]
    assert crafty.files["server.properties"] == new.encode()


def test_saves_a_file_in_a_subfolder(crafty):
    with client_for(crafty) as client:
        response = save(client, "config/sodium.json", '{"a": 1}\n', sha256(b"{}\n"))

    assert response.status_code == 200
    assert crafty.uploads == [("config", "sodium.json")]
    assert crafty.files["config/sodium.json"] == b'{"a": 1}\n'


def test_saving_keeps_windows_line_endings(crafty):
    crafty.files["options.txt"] = b"a=1\r\nb=2\r\n"
    with client_for(crafty) as client:
        response = save(client, "options.txt", "a=1\nb=3\n", sha256(b"a=1\r\nb=2\r\n"))

    assert response.status_code == 200
    assert crafty.files["options.txt"] == b"a=1\r\nb=3\r\n"
    assert response.json()["sha256"] == sha256(b"a=1\r\nb=3\r\n")


def test_saving_a_file_changed_since_it_was_opened_is_refused(crafty):
    with client_for(crafty) as client:
        response = save(client, "server.properties", "motd=Mine\n", sha256(b"older"))

    assert response.status_code == 409
    assert response.json()["detail"] == ("server.properties changed on the server since you "
                                         "opened it. Open it again to see the new content.")
    assert crafty.uploads == []
    assert crafty.files["server.properties"] == PROPERTIES


def test_saving_a_file_deleted_since_it_was_opened_is_refused(crafty):
    with client_for(crafty) as client:
        response = save(client, "options.txt", "a=1\n", sha256(b"a=1\n"))

    assert response.status_code == 409
    assert response.json()["detail"] == "options.txt no longer exists on the server."
    assert crafty.uploads == []


def test_saving_over_a_binary_file_is_refused(crafty):
    crafty.files["mods/sodium.jar"] = b"PK\x03\x04\x00"
    with client_for(crafty) as client:
        response = save(client, "mods/sodium.jar", "text", sha256(b"PK\x03\x04\x00"))

    assert response.status_code == 415
    assert crafty.uploads == []
    assert crafty.files["mods/sodium.jar"] == b"PK\x03\x04\x00"


def test_saving_content_over_one_mebibyte_is_refused(crafty):
    with client_for(crafty) as client:
        response = save(client, "server.properties", "x" * (1024 * 1024 + 1),
                        sha256(PROPERTIES))

    assert response.status_code == 413
    assert crafty.uploads == []


def test_saving_is_refused_while_a_modpack_update_runs(crafty):
    with client_for(crafty) as client:
        with client.app.state.locks.marked_busy(SERVER, "A modpack update is running."):
            response = save(client, "server.properties", "motd=x\n", sha256(PROPERTIES))

    assert response.status_code == 409
    assert response.json()["detail"] == "A modpack update is running."
    assert crafty.uploads == []


# --- paths and permissions -------------------------------------------------------

BAD_PATHS = ["../server.properties", "config/../../etc/passwd", "/etc/passwd",
             "config\\sodium.json", "config//sodium.json", "./server.properties",
             "config/", "a\x00b", "a\nb", "x" * 513,
             "C:/Windows/win.ini", "C:", "D:x.txt", "a/b:stream",
             ".../x", ".. /x", " server.properties", "server.properties "]


@pytest.mark.parametrize("bad", BAD_PATHS)
def test_unsafe_paths_never_reach_crafty(crafty, bad):
    with client_for(crafty) as client:
        responses = [
            client.get(FILES, params={"path": bad}),
            client.get(CONTENT, params={"path": bad}),
            save(client, bad, "x", sha256(b"x")),
        ]

    assert [r.status_code for r in responses] == [422, 422, 422]
    assert crafty.calls == 0


def test_a_file_with_a_non_ascii_name_opens_but_saving_it_is_refused():
    data = b"key=value\n"
    crafty = FakeCrafty(files={"config/café.toml": data})
    with client_for(crafty) as client:
        opened = client.get(CONTENT, params={"path": "config/café.toml"})
        response = save(client, "config/café.toml", "key=other\n", sha256(data))

    assert opened.status_code == 200
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "config/café.toml cannot be saved: Crafty only accepts uploads to paths "
        "made of ASCII characters.")
    assert crafty.uploads == []
    assert crafty.files["config/café.toml"] == data


def test_opening_or_saving_the_root_is_refused(crafty):
    with client_for(crafty) as client:
        opened = client.get(CONTENT, params={"path": ""})
        saved = save(client, "", "x", sha256(b"x"))

    assert (opened.status_code, saved.status_code) == (422, 422)
    assert crafty.calls == 0


def test_without_crafty_file_permission_is_403():
    crafty = FakeCrafty(forbidden=True)
    with client_for(crafty) as client:
        responses = [client.get(FILES), client.get(CONTENT, params={"path": "server.properties"}),
                     save(client, "server.properties", "x", sha256(PROPERTIES))]

    assert [r.status_code for r in responses] == [403, 403, 403]
    assert {r.json()["detail"] for r in responses} == {
        "You do not have file access to this server in Crafty."}


def test_file_routes_need_a_session(crafty):
    with make_client(crafty, crafty_url=CRAFTY) as client:
        responses = [client.get(FILES), client.get(CONTENT, params={"path": "server.properties"}),
                     save(client, "server.properties", "x", sha256(PROPERTIES))]

    assert [r.status_code for r in responses] == [401, 401, 401]
    assert crafty.calls == 0


def test_server_page_has_the_files_panel(crafty):
    with client_for(crafty) as client:
        page = client.get(f"/servers/{SERVER}")

    assert 'id="files-panel"' in page.text
    assert "server_files.js" in page.text
