import json

import httpx
import pytest

from tests.conftest import CRAFTY, make_logged_in_client

SERVER = "9b1968d1-0000-4000-8000-000000000001"

STATS = {
    "running": True,
    "online": 2,
    "max": 20,
    "players": "['Steve', 'Alex']",
    "cpu": 12.5,
    "mem": "1.6GB",
    "mem_percent": 20.1,
    "started": "2026-11-01 10:00:00",
    "version": "1.20.1",
    "world_name": "world",
    "world_size": "250.0MB",
    "server_port": 25565,
    "crashed": False,
    "waiting_start": False,
    "updating": False,
}


class FakeCrafty:
    def __init__(self, stats=None, action_status=200, action_error=None):
        self.stats = STATS if stats is None else stats
        self.action_status = action_status
        self.action_error = action_error
        self.actions: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/api/v2/servers/{SERVER}/stats":
            return httpx.Response(200, json={"status": "ok", "data": self.stats})
        if path.startswith(f"/api/v2/servers/{SERVER}/action/") and request.method == "POST":
            self.actions.append(path.rsplit("/", 1)[1])
            if self.action_status >= 400:
                return httpx.Response(self.action_status,
                                      json={"status": "error", "error": self.action_error})
            return httpx.Response(200, json={"status": "ok"})
        if path == "/api/v2/servers":
            return httpx.Response(200, json={"status": "ok", "data": [
                {"server_id": SERVER, "server_name": "Survival", "type": "minecraft-java"}]})
        return httpx.Response(404, json={"status": "error", "error": "NOT_FOUND"})


def client_for(fake):
    return make_logged_in_client(fake, crafty_url=CRAFTY)


def test_status_reports_power_players_and_resources():
    with client_for(FakeCrafty()) as client:
        response = client.get(f"/api/servers/{SERVER}/status")

    assert response.status_code == 200
    assert response.json() == {
        "state": "running",
        "running": True,
        "players_online": 2,
        "max_players": 20,
        "players": ["Steve", "Alex"],
        "cpu_percent": 12.5,
        "memory": "1.6GB",
        "memory_percent": 20.1,
        "started": "2026-11-01 10:00:00",
        "version": "1.20.1",
        "world_size": "250.0MB",
        "port": 25565,
    }


@pytest.mark.parametrize("flags, state", [
    ({"running": False}, "stopped"),
    ({"running": False, "crashed": True}, "crashed"),
    ({"running": False, "waiting_start": True}, "starting"),
    ({"running": False, "updating": True}, "updating"),
])
def test_status_names_the_state_of_a_server_that_is_not_running(flags, state):
    stats = {**STATS, "online": 0, "players": "[]", **flags}
    with client_for(FakeCrafty(stats=stats)) as client:
        body = client.get(f"/api/servers/{SERVER}/status").json()

    assert body["state"] == state
    assert body["running"] is False
    assert body["players"] == []


def test_status_accepts_players_sent_as_a_list_and_ignores_garbage():
    with client_for(FakeCrafty(stats={**STATS, "players": ["Steve"]})) as client:
        assert client.get(f"/api/servers/{SERVER}/status").json()["players"] == ["Steve"]
    with client_for(FakeCrafty(stats={**STATS, "players": "__import__('os')"})) as client:
        assert client.get(f"/api/servers/{SERVER}/status").json()["players"] == []


@pytest.mark.parametrize("action, crafty_action", [
    ("start", "start_server"),
    ("stop", "stop_server"),
    ("restart", "restart_server"),
    ("kill", "kill_server"),
])
def test_power_actions_are_sent_to_crafty(action, crafty_action):
    fake = FakeCrafty()
    with client_for(fake) as client:
        response = client.post(f"/api/servers/{SERVER}/power", json={"action": action})

    assert response.status_code == 200
    assert response.json() == {"action": action, "sent": True}
    assert fake.actions == [crafty_action]


def test_unknown_power_action_is_rejected_before_crafty():
    fake = FakeCrafty()
    with client_for(fake) as client:
        response = client.post(f"/api/servers/{SERVER}/power", json={"action": "delete"})

    assert response.status_code == 422
    assert fake.actions == []


def test_power_without_the_crafty_permission_is_explained():
    fake = FakeCrafty(action_status=400, action_error="NOT_AUTHORIZED")
    with client_for(fake) as client:
        response = client.post(f"/api/servers/{SERVER}/power", json={"action": "start"})

    assert response.status_code == 403
    assert response.json() == {
        "detail": "You do not have permission to control this server in Crafty."}


def test_starting_is_refused_while_a_modpack_update_changes_the_server():
    fake = FakeCrafty()
    with client_for(fake) as client:
        with client.app.state.locks.marked_busy(SERVER, "busy"):
            start = client.post(f"/api/servers/{SERVER}/power", json={"action": "start"})
            stop = client.post(f"/api/servers/{SERVER}/power", json={"action": "stop"})

    assert start.status_code == 409
    assert stop.status_code == 200
    assert fake.actions == ["stop_server"]


def test_power_needs_a_json_body():
    with client_for(FakeCrafty()) as client:
        response = client.post(f"/api/servers/{SERVER}/power", content=json.dumps({"action": "start"}),
                               headers={"content-type": "text/plain"})

    assert response.status_code == 422
