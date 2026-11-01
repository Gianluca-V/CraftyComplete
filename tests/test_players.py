import hashlib
import json
import uuid
from urllib.parse import unquote

import httpx
import pytest

from tests.conftest import CRAFTY, USER_TOKEN, make_logged_in_client

SERVER = "9b1968d1-0000-4000-8000-000000000001"
PLAYERS = f"/api/servers/{SERVER}/players"

STEVE = {"id": "8667ba71b85a4004af54457a9734eed7", "name": "Steve"}
STEVE_UUID = "8667ba71-b85a-4004-af54-457a9734eed7"
ALEX = {"id": "ec561538f3fd461daff5086b22154bce", "name": "Alex"}
ALEX_UUID = "ec561538-f3fd-461d-aff5-086b22154bce"


def offline_uuid(name: str) -> str:
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode()).digest())
    digest[6] = digest[6] & 0x0F | 0x30
    digest[8] = digest[8] & 0x3F | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


class FakeWorld:
    """Fake Crafty (stats, stdin, files) and fake Mojang profiles."""

    def __init__(self, running=False, files=None, online=None):
        self.running = running
        self.online = online or []
        self.files: dict[str, bytes] = {"server.properties": b"online-mode=true\n"}
        self.files.update(files or {})
        self.commands: list[str] = []
        self.uploads: list[str] = []
        self.mojang_down = False
        self.stdin_error = None
        self.upload_error = None

    def json_file(self, name):
        return json.loads(self.files[name])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.mojang.com":
            if self.mojang_down:
                return httpx.Response(503)
            name = request.url.path.rsplit("/", 1)[1].lower()
            found = next((p for p in (STEVE, ALEX) if p["name"].lower() == name), None)
            return httpx.Response(200, json=found) if found else httpx.Response(404)
        assert request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
        base = f"/api/v2/servers/{SERVER}"
        path = request.url.path
        if path == f"{base}/stats":
            return httpx.Response(200, json={"status": "ok", "data": {
                "running": self.running, "online": len(self.online), "max": 20,
                "players": str(self.online)}})
        if path == f"{base}/stdin" and request.method == "POST":
            if self.stdin_error:
                return httpx.Response(400, json={"status": "error", "error": self.stdin_error})
            self.commands.append(request.content.decode())
            return httpx.Response(200, json={"status": "ok"})
        if path.startswith(f"{base}/files/") and path.endswith("/download"):
            name = unquote(path[len(f"{base}/files/"):-len("/download")])
            if name not in self.files:
                return httpx.Response(404, json={"status": "error", "error": "File not found"})
            return httpx.Response(200, content=self.files[name])
        if path == f"{base}/files/upload":
            if self.upload_error:
                return httpx.Response(400, json={"status": "error", "error": self.upload_error})
            location, name = request.headers["location"], request.headers["fileName"]
            key = f"{location}/{name}" if location else name
            self.files[key] = request.content
            self.uploads.append(key)
            return httpx.Response(200, json={"status": "completed", "data": {}})
        raise AssertionError(f"unexpected call {request.method} {path}")


@pytest.fixture
def world():
    return FakeWorld()


def client_for(world):
    return make_logged_in_client(world, crafty_url=CRAFTY, update_check_hours=0)


def act(client, action, name, reason=None):
    body = {"action": action, "name": name}
    if reason is not None:
        body["reason"] = reason
    return client.post(PLAYERS, json=body)


# --- listing --------------------------------------------------------------------

def test_list_reads_the_player_files_and_who_is_online():
    world = FakeWorld(running=True, online=["Steve"], files={
        "whitelist.json": json.dumps([{"uuid": STEVE_UUID, "name": "Steve"}]).encode(),
        "ops.json": json.dumps([{"uuid": ALEX_UUID, "name": "Alex", "level": 4,
                                 "bypassesPlayerLimit": False}]).encode(),
        "banned-players.json": json.dumps([{
            "uuid": "x", "name": "Griefer", "created": "2026-10-01 10:00:00 +0000",
            "source": "Server", "expires": "forever", "reason": "Griefing"}]).encode(),
    })
    with client_for(world) as client:
        response = client.get(PLAYERS)

    assert response.status_code == 200
    assert response.json() == {
        "running": True,
        "online": ["Steve"],
        "whitelist": [{"name": "Steve", "uuid": STEVE_UUID}],
        "ops": [{"name": "Alex", "uuid": ALEX_UUID, "level": 4}],
        "banned": [{"name": "Griefer", "uuid": "x", "reason": "Griefing",
                    "created": "2026-10-01 10:00:00 +0000", "expires": "forever"}],
    }


def test_missing_player_files_are_empty_lists(world):
    with client_for(world) as client:
        body = client.get(PLAYERS).json()

    assert body == {"running": False, "online": [], "whitelist": [], "ops": [], "banned": []}


def test_a_broken_player_file_is_reported(world):
    world.files["ops.json"] = b"{not json"
    with client_for(world) as client:
        response = client.get(PLAYERS)

    assert response.status_code == 502
    assert response.json() == {"detail": "ops.json on the server is not valid JSON; "
                                         "fix or delete it in Crafty first."}


# --- running server: console commands ---------------------------------------------

@pytest.mark.parametrize("action, reason, command", [
    ("whitelist_add", None, "whitelist add Steve"),
    ("whitelist_remove", None, "whitelist remove Steve"),
    ("op", None, "op Steve"),
    ("deop", None, "deop Steve"),
    ("ban", None, "ban Steve"),
    ("ban", "Griefing spawn", "ban Steve Griefing spawn"),
    ("pardon", None, "pardon Steve"),
    ("kick", "Be back soon", "kick Steve Be back soon"),
])
def test_running_server_gets_a_console_command(action, reason, command):
    world = FakeWorld(running=True)
    with client_for(world) as client:
        response = act(client, action, "Steve", reason)

    assert response.status_code == 200
    assert response.json() == {"action": action, "name": "Steve", "applied": "command",
                               "changed": True}
    assert world.commands == [command]
    assert world.uploads == []


@pytest.mark.parametrize("name", ["Steve; stop", "a b", "x" * 17, "", "Ste\nve"])
def test_invalid_player_names_never_reach_crafty(name):
    world = FakeWorld(running=True)
    with client_for(world) as client:
        response = act(client, "op", name)

    assert response.status_code == 422
    assert world.commands == []


def test_reason_cannot_carry_a_second_command():
    world = FakeWorld(running=True)
    with client_for(world) as client:
        response = act(client, "ban", "Steve", "bye\nstop")

    assert response.status_code == 422
    assert world.commands == []


def test_command_without_the_crafty_permission_is_explained():
    world = FakeWorld(running=True)
    world.stdin_error = "NOT_AUTHORIZED"
    with client_for(world) as client:
        response = act(client, "op", "Steve")

    assert response.status_code == 403
    assert response.json() == {
        "detail": "You do not have permission to control this server in Crafty."}


# --- stopped server: player files -------------------------------------------------

def test_stopped_server_whitelist_add_writes_the_file_with_the_mojang_uuid(world):
    with client_for(world) as client:
        response = act(client, "whitelist_add", "steve")

    assert response.status_code == 200
    assert response.json() == {"action": "whitelist_add", "name": "Steve", "applied": "file",
                               "changed": True}
    assert world.json_file("whitelist.json") == [{"uuid": STEVE_UUID, "name": "Steve"}]
    assert world.commands == []


def test_adding_twice_does_not_duplicate(world):
    world.files["whitelist.json"] = json.dumps([{"uuid": STEVE_UUID, "name": "Steve"}]).encode()
    with client_for(world) as client:
        response = act(client, "whitelist_add", "Steve")

    assert response.json()["changed"] is False
    assert world.uploads == []


def test_stopped_server_op_and_deop(world):
    with client_for(world) as client:
        act(client, "op", "Alex")
        ops = world.json_file("ops.json")
        response = act(client, "deop", "alex")

    assert ops == [{"uuid": ALEX_UUID, "name": "Alex", "level": 4,
                    "bypassesPlayerLimit": False}]
    assert response.json()["changed"] is True
    assert world.json_file("ops.json") == []


def test_stopped_server_ban_and_pardon(world):
    world.files["whitelist.json"] = json.dumps([{"uuid": STEVE_UUID, "name": "Steve"}]).encode()
    with client_for(world) as client:
        act(client, "ban", "Steve", "Griefing")
        banned = world.json_file("banned-players.json")
        pardon = act(client, "pardon", "STEVE")

    assert len(banned) == 1
    entry = banned[0]
    assert {k: entry[k] for k in ("uuid", "name", "source", "expires", "reason")} == {
        "uuid": STEVE_UUID, "name": "Steve", "source": "craftyComplete",
        "expires": "forever", "reason": "Griefing"}
    assert len(entry["created"]) == len("2026-11-01 10:00:00 +0000")
    assert pardon.json()["changed"] is True
    assert world.json_file("banned-players.json") == []
    # Banning does not touch the whitelist.
    assert world.json_file("whitelist.json") == [{"uuid": STEVE_UUID, "name": "Steve"}]


def test_removing_someone_not_listed_changes_nothing(world):
    with client_for(world) as client:
        response = act(client, "whitelist_remove", "Steve")

    assert response.status_code == 200
    assert response.json()["changed"] is False
    assert world.uploads == []


def test_kick_needs_a_running_server(world):
    with client_for(world) as client:
        response = act(client, "kick", "Steve")

    assert response.status_code == 409
    assert response.json() == {"detail": "Players can only be kicked while the server is running."}


def test_unknown_minecraft_account_is_refused(world):
    with client_for(world) as client:
        response = act(client, "whitelist_add", "Nobody")

    assert response.status_code == 404
    assert response.json() == {"detail": "There is no Minecraft account named Nobody."}
    assert world.uploads == []


def test_mojang_unreachable_is_reported(world):
    world.mojang_down = True
    with client_for(world) as client:
        response = act(client, "op", "Steve")

    assert response.status_code == 502
    assert response.json() == {"detail": "Mojang could not be asked for Steve's UUID; "
                                         "try again later."}


def test_offline_mode_server_uses_offline_uuids_without_mojang(world):
    world.files["server.properties"] = b"motd=Hi\nonline-mode=false\n"
    world.mojang_down = True
    with client_for(world) as client:
        response = act(client, "whitelist_add", "Builder_01")

    assert response.status_code == 200
    assert world.json_file("whitelist.json") == [
        {"uuid": offline_uuid("Builder_01"), "name": "Builder_01"}]


def test_removing_from_a_file_needs_no_mojang_lookup(world):
    world.files["whitelist.json"] = json.dumps([{"uuid": STEVE_UUID, "name": "Steve"}]).encode()
    world.mojang_down = True
    with client_for(world) as client:
        response = act(client, "whitelist_remove", "steve")

    assert response.json()["changed"] is True
    assert world.json_file("whitelist.json") == []


def test_writing_without_file_access_is_explained(world):
    world.upload_error = "NOT_AUTHORIZED"
    with client_for(world) as client:
        response = act(client, "op", "Steve")

    assert response.status_code == 403
    assert response.json() == {"detail": "You do not have file access to this server in Crafty."}


def test_player_changes_are_refused_during_a_modpack_update(world):
    with client_for(world) as client:
        with client.app.state.locks.marked_busy(SERVER, "busy"):
            response = act(client, "op", "Steve")

    assert response.status_code == 409
    assert world.uploads == []
