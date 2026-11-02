import asyncio
import json
import time

import httpx
import pytest
from mcp import Client

from crafty_complete.config import Settings
from crafty_complete.mcp_server import create_mcp_server
from tests.conftest import CRAFTY
from tests.test_control import SERVER as CONTROL_SERVER
from tests.test_control import FakeCrafty
from tests.test_create_server import Fakes as WebCreationFakes
from tests.test_files import PROPERTIES
from tests.test_files import SERVER as FILES_SERVER
from tests.test_files import FakeCrafty as FilesCrafty
from tests.test_files import sha256 as files_sha256
from tests.test_install import BYTES
from tests.test_players import SERVER as PLAYERS_SERVER
from tests.test_players import FakeWorld
from tests.test_remove_and_version import Services as ModFakes
from tests.test_adoption import SERVER as MOD_SERVER

TOKEN = "user-jwt"


class CreationFakes(WebCreationFakes):
    """The MCP server learns who the token belongs to from Crafty (the web app has a session)."""

    def _crafty(self, request):
        if request.url.path == "/api/v2/users/@me":
            return httpx.Response(200, json={"status": "ok", "data": {
                "user_id": 1, "username": "agent", "superuser": False}})
        return super()._crafty(request)


def mcp_server(fakes, tmp_path, token=TOKEN, **settings):
    settings.setdefault("update_check_hours", 0)
    settings.setdefault("modpack_poll_seconds", 0)
    settings.setdefault("modpack_work_dir", str(tmp_path / "work"))
    return create_mcp_server(
        Settings(_env_file=None, crafty_url=CRAFTY, mcp_crafty_token=token,
                 database_path=str(tmp_path / "db.sqlite3"), **settings),
        transport=_transport(fakes))


def _transport(fakes):
    return httpx.MockTransport(fakes)


def run(server, steps):
    """Run `steps(call)` inside one MCP session; `call(tool, **args)` returns the data."""

    async def main():
        async with Client(server) as client:
            async def call(tool, **args):
                result = await client.call_tool(tool, args)
                text = " ".join(getattr(c, "text", "") for c in result.content)
                if result.is_error:
                    # The SDK prefixes "Error executing tool <name>: " to the message.
                    return {"error": text.split(": ", 1)[1] if text.startswith("Error executing")
                            else text}
                return json.loads(text)
            return await steps(call)

    return asyncio.run(main())


# --- surface -----------------------------------------------------------------------

def test_tools_cover_every_feature(tmp_path):
    server = mcp_server(FakeCrafty(), tmp_path)

    async def names():
        async with Client(server) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert asyncio.run(names()) == {
        "list_servers", "server_status", "server_power",
        "adopt_server", "list_mods", "search_mods", "install_mod", "update_mods",
        "check_mod_updates", "mod_versions", "set_mod_version", "pin_mod", "remove_mod",
        "list_players", "manage_player", "list_files", "read_file", "write_file",
        "new_server_options", "create_server", "modpack_versions", "create_modpack_server",
        "modpack_status", "link_modpack", "update_modpack", "job_status",
    }


def test_without_a_token_every_tool_explains_how_to_configure_it(tmp_path):
    server = mcp_server(FakeCrafty(), tmp_path, token=None)

    result = run(server, lambda call: call("server_status", server_id=CONTROL_SERVER))

    assert result == {"error": "Set CRAFTY_COMPLETE_MCP_CRAFTY_TOKEN to a Crafty API token "
                               "(Crafty panel > your user > API keys)."}


def test_a_rejected_token_is_explained(tmp_path):
    def handler(request):
        return httpx.Response(403, json={"status": "error", "error": "ACCESS_DENIED"})

    server = mcp_server(handler, tmp_path, token="revoked")
    result = run(server, lambda call: call("server_status", server_id=CONTROL_SERVER))

    assert result == {"error": "Crafty rejected CRAFTY_COMPLETE_MCP_CRAFTY_TOKEN; create a new "
                               "API token in Crafty."}


# --- status and power ----------------------------------------------------------------

def test_server_status(tmp_path):
    server = mcp_server(FakeCrafty(), tmp_path)

    status = run(server, lambda call: call("server_status", server_id=CONTROL_SERVER))

    assert status["state"] == "running"
    assert status["players"] == ["Steve", "Alex"]
    assert status["cpu_percent"] == 12.5


def test_start_runs_at_once(tmp_path):
    fakes = FakeCrafty()
    server = mcp_server(fakes, tmp_path)

    result = run(server, lambda call: call("server_power", server_id=CONTROL_SERVER,
                                           action="start"))

    assert result == {"action": "start", "sent": True}
    assert fakes.actions == ["start_server"]


def test_stop_needs_a_confirmation_token(tmp_path):
    fakes = FakeCrafty()
    server = mcp_server(fakes, tmp_path)

    async def steps(call):
        plan = await call("server_power", server_id=CONTROL_SERVER, action="stop")
        sent_before = list(fakes.actions)
        done = await call("server_power", server_id=CONTROL_SERVER, action="stop",
                          confirm_token=plan["confirm_token"])
        reused = await call("server_power", server_id=CONTROL_SERVER, action="stop",
                            confirm_token=plan["confirm_token"])
        return plan, sent_before, done, reused

    plan, sent_before, done, reused = run(server, steps)

    assert plan["confirmation_required"] is True
    assert plan["plan"] == {"action": "stop", "server_id": CONTROL_SERVER,
                            "state": "running", "players": ["Steve", "Alex"]}
    assert plan["expires_in_seconds"] == 300
    assert sent_before == []
    assert done == {"action": "stop", "sent": True}
    assert fakes.actions == ["stop_server"]
    assert reused == {"error": "This confirmation token is invalid, expired or for other "
                               "arguments; call the tool again without confirm_token to get a "
                               "new plan."}


def test_a_token_does_not_confirm_other_arguments(tmp_path):
    fakes = FakeCrafty()
    server = mcp_server(fakes, tmp_path)

    async def steps(call):
        plan = await call("server_power", server_id=CONTROL_SERVER, action="stop")
        return await call("server_power", server_id=CONTROL_SERVER, action="kill",
                          confirm_token=plan["confirm_token"])

    result = run(server, steps)

    assert "invalid, expired or for other arguments" in result["error"]
    assert fakes.actions == []


def test_an_expired_token_is_refused(tmp_path, monkeypatch):
    fakes = FakeCrafty()
    server = mcp_server(fakes, tmp_path)
    now = [1000.0]
    monkeypatch.setattr("crafty_complete.mcp_server.time.monotonic", lambda: now[0])

    async def steps(call):
        plan = await call("server_power", server_id=CONTROL_SERVER, action="restart")
        now[0] += 301
        return await call("server_power", server_id=CONTROL_SERVER, action="restart",
                          confirm_token=plan["confirm_token"])

    assert "expired" in run(server, steps)["error"]
    assert fakes.actions == []


def test_invalid_server_ids_are_refused(tmp_path):
    fakes = FakeCrafty()
    server = mcp_server(fakes, tmp_path)

    result = run(server, lambda call: call("server_status", server_id="../etc"))

    assert "error" in result


# --- mods ----------------------------------------------------------------------------

def test_adopt_install_and_remove_a_mod_with_confirmation(tmp_path):
    fakes = ModFakes()
    server = mcp_server(fakes, tmp_path)

    async def steps(call):
        adopted = await call("adopt_server", server_id=MOD_SERVER)
        installed = await call("install_mod", server_id=MOD_SERVER, project="lithium")
        plan = await call("remove_mod", server_id=MOD_SERVER, file_name="lithium-0.11.2.jar",
                          remove_dependencies=["cloth-config-11.1.jar"])
        still_there = "mods/lithium-0.11.2.jar" in fakes.files
        removed = await call("remove_mod", server_id=MOD_SERVER, file_name="lithium-0.11.2.jar",
                             remove_dependencies=["cloth-config-11.1.jar"],
                             confirm_token=plan["confirm_token"])
        return adopted, installed, plan, still_there, removed

    adopted, installed, plan, still_there, removed = run(server, steps)

    assert adopted["adopted"] is True
    assert [m["file_name"] for m in installed["installed"]] == [
        "lithium-0.11.2.jar", "cloth-config-11.1.jar"]
    assert plan["confirmation_required"] is True
    assert plan["plan"]["unused_dependencies"] == [
        {"file_name": "cloth-config-11.1.jar", "title": "Cloth Config"}]
    assert plan["plan"]["remove_dependencies"] == ["cloth-config-11.1.jar"]
    assert still_there is True
    assert [m["file_name"] for m in removed["removed"]] == [
        "lithium-0.11.2.jar", "cloth-config-11.1.jar"]
    assert "mods/cloth-config-11.1.jar" not in fakes.files


def test_set_mod_version_and_update_back(tmp_path):
    fakes = ModFakes()
    server = mcp_server(fakes, tmp_path)

    async def steps(call):
        await call("adopt_server", server_id=MOD_SERVER)
        await call("install_mod", server_id=MOD_SERVER, project="lithium")
        versions = await call("mod_versions", server_id=MOD_SERVER, file_name="lithium-0.11.2.jar")
        await call("set_mod_version", server_id=MOD_SERVER, file_name="lithium-0.11.2.jar",
                   version_id="li-1")
        return versions, await call("update_mods", server_id=MOD_SERVER)

    versions, updated = run(server, steps)

    assert [v["id"] for v in versions["versions"]] == ["li-2", "li-3b", "li-1"]
    # Update all also moves the adopted Fabric API 0.83.0 to its newest version.
    assert {u["title"]: u["to_version"] for u in updated["updated"]} == {
        "Lithium": "0.11.2", "Fabric API": "0.92.12"}
    assert fakes.files["mods/lithium-0.11.2.jar"] == BYTES["li-2"]


def test_mod_refusals_reach_the_agent_as_errors(tmp_path):
    server = mcp_server(ModFakes(), tmp_path)

    async def steps(call):
        await call("adopt_server", server_id=MOD_SERVER)
        return await call("install_mod", server_id=MOD_SERVER, project="zoomify")

    assert run(server, steps) == {
        "error": "Zoomify only runs on clients; it cannot be installed on a server."}


# --- players -------------------------------------------------------------------------

def test_whitelisting_runs_at_once_and_banning_needs_confirmation(tmp_path):
    world = FakeWorld(running=True)
    server = mcp_server(world, tmp_path)

    async def steps(call):
        added = await call("manage_player", server_id=PLAYERS_SERVER, action="whitelist_add",
                           name="Steve")
        plan = await call("manage_player", server_id=PLAYERS_SERVER, action="ban", name="Alex",
                          reason="Griefing")
        commands_before = list(world.commands)
        banned = await call("manage_player", server_id=PLAYERS_SERVER, action="ban",
                            name="Alex", reason="Griefing", confirm_token=plan["confirm_token"])
        return added, plan, commands_before, banned

    added, plan, commands_before, banned = run(server, steps)

    assert added["applied"] == "command"
    assert plan["plan"] == {"action": "ban", "name": "Alex", "reason": "Griefing",
                            "server_id": PLAYERS_SERVER, "server_running": True}
    assert commands_before == ["whitelist add Steve"]
    assert banned["changed"] is True
    assert world.commands == ["whitelist add Steve", "ban Alex Griefing"]


def test_player_names_are_checked_by_the_service(tmp_path):
    world = FakeWorld(running=True)
    server = mcp_server(world, tmp_path)

    result = run(server, lambda call: call("manage_player", server_id=PLAYERS_SERVER,
                                           action="op", name="Steve\nstop"))

    assert "error" in result
    assert world.commands == []


# --- files -----------------------------------------------------------------------------

def test_browse_read_and_edit_a_file(tmp_path):
    crafty = FilesCrafty()
    server = mcp_server(crafty, tmp_path)

    async def steps(call):
        listed = await call("list_files", server_id=FILES_SERVER, path="config")
        opened = await call("read_file", server_id=FILES_SERVER, path="server.properties")
        saved = await call("write_file", server_id=FILES_SERVER, path="server.properties",
                           content="motd=Agent\n", sha256=opened["sha256"])
        return listed, opened, saved

    listed, opened, saved = run(server, steps)

    assert listed["entries"] == [
        {"name": "sodium.json", "path": "config/sodium.json", "is_dir": False}]
    assert opened["content"] == PROPERTIES.decode()
    assert saved["sha256"] == files_sha256(b"motd=Agent\n")
    assert crafty.files["server.properties"] == b"motd=Agent\n"


def test_write_file_refuses_stale_content_and_unsafe_paths(tmp_path):
    crafty = FilesCrafty()
    server = mcp_server(crafty, tmp_path)

    async def steps(call):
        stale = await call("write_file", server_id=FILES_SERVER, path="server.properties",
                           content="motd=x\n", sha256="0" * 64)
        escape = await call("read_file", server_id=FILES_SERVER, path="../etc/passwd")
        return stale, escape

    stale, escape = run(server, steps)

    assert stale == {"error": "server.properties changed on the server since you opened it. "
                              "Open it again to see the new content."}
    assert escape == {"error": "'../etc/passwd' is not a valid path inside the server folder."}
    assert crafty.uploads == []
    assert crafty.files["server.properties"] == PROPERTIES


# --- creating servers ------------------------------------------------------------------

def test_create_a_fabric_server_and_follow_the_job(tmp_path):
    fakes = CreationFakes()
    server = mcp_server(fakes, tmp_path)

    async def steps(call):
        options = await call("new_server_options")
        started = await call("create_server", kind="fabric", minecraft="1.20.1",
                             name="Agent Server", accept_minecraft_eula=True, mods=["lithium"])
        deadline = time.monotonic() + 10
        while True:
            job = await call("job_status", job_id=started["job_id"])
            if job["status"] != "running" or time.monotonic() > deadline:
                return options, job
            await asyncio.sleep(0.02)

    options, job = run(server, steps)

    assert [k["kind"] for k in options["kinds"]] == [
        "vanilla", "paper", "fabric", "forge", "neoforge"]
    assert job["status"] == "succeeded", job
    assert [m["project_id"] for m in job["result"]["installed"]] == ["P3", "P1", "P4"]
    assert fakes.created[0]["name"] == "Agent Server"


def test_creating_needs_the_eula_to_be_accepted(tmp_path):
    fakes = CreationFakes()
    server = mcp_server(fakes, tmp_path)

    result = run(server, lambda call: call("create_server", kind="vanilla", minecraft="1.21.4",
                                           name="No EULA", accept_minecraft_eula=False))

    assert "EULA" in result["error"]
    assert fakes.created == []


# --- modpacks ------------------------------------------------------------------------

def test_update_modpack_needs_confirmation_and_then_runs(tmp_path):
    from tests.test_modpack_update import V2, UpdateFakes, linked_client

    fakes = UpdateFakes()
    db = str(tmp_path / "db.sqlite3")
    # Link the server through the web app; the MCP server shares the same database.
    linked_client(fakes, db, tmp_path).__exit__(None, None, None)
    server = mcp_server(fakes, tmp_path, crafty_retry_seconds=0)

    async def steps(call):
        status = await call("modpack_status", server_id=MOD_SERVER)
        plan = await call("update_modpack", server_id=MOD_SERVER, version_id=V2)
        moved_before = backup_count(fakes)
        started = await call("update_modpack", server_id=MOD_SERVER, version_id=V2,
                             confirm_token=plan["confirm_token"])
        deadline = time.monotonic() + 10
        while True:
            job = await call("job_status", job_id=started["job_id"])
            if job["status"] != "running" or time.monotonic() > deadline:
                return status, plan, moved_before, job
            await asyncio.sleep(0.02)

    status, plan, moved_before, job = run(server, steps)

    assert status["update_available"] is True
    assert plan["plan"]["from_version"] == "1.0.0"
    assert plan["plan"]["to_version_id"] == V2
    assert moved_before == 0
    assert job["status"] == "succeeded", job


def backup_count(fakes):
    return sum(path.startswith(".crafty-complete/backups/") for path in fakes.files)


# --- busy servers ----------------------------------------------------------------------

@pytest.mark.parametrize("action", ["start", "restart"])
def test_start_and_restart_are_refused_while_the_server_is_busy(tmp_path, monkeypatch, action):
    """Same rule as the web route (NEEDS_IDLE_SERVER): a busy server must not start."""
    from crafty_complete.mods.locks import ServerLocks

    fakes = FakeCrafty()
    server = mcp_server(fakes, tmp_path)
    monkeypatch.setattr(ServerLocks, "busy", lambda self, sid: "A modpack update is running.")

    async def steps(call):
        first = await call("server_power", server_id=CONTROL_SERVER, action=action)
        if "confirm_token" not in first:
            return first
        return await call("server_power", server_id=CONTROL_SERVER, action=action,
                          confirm_token=first["confirm_token"])

    assert run(server, steps) == {"error": "A modpack update is running."}
    assert fakes.actions == []


def test_stop_is_allowed_while_the_server_is_busy(tmp_path, monkeypatch):
    from crafty_complete.mods.locks import ServerLocks

    fakes = FakeCrafty()
    server = mcp_server(fakes, tmp_path)
    monkeypatch.setattr(ServerLocks, "busy", lambda self, sid: "A modpack update is running.")

    async def steps(call):
        plan = await call("server_power", server_id=CONTROL_SERVER, action="stop")
        return await call("server_power", server_id=CONTROL_SERVER, action="stop",
                          confirm_token=plan["confirm_token"])

    assert run(server, steps) == {"action": "stop", "sent": True}


def test_check_mod_updates_is_refused_while_the_server_is_busy(tmp_path, monkeypatch):
    from crafty_complete.mods.locks import ServerLocks

    server = mcp_server(ModFakes(), tmp_path)
    monkeypatch.setattr(ServerLocks, "busy", lambda self, sid: "A modpack update is running.")

    result = run(server, lambda call: call("check_mod_updates", server_id=MOD_SERVER))

    assert result == {"error": "A modpack update is running."}
