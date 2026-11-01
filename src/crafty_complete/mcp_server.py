"""MCP server: control Crafty servers, mods, players and modpacks from an AI agent.

Run it as a local stdio process from the MCP client (`crafty-complete-mcp`). It acts with
the Crafty API token in CRAFTY_COMPLETE_MCP_CRAFTY_TOKEN, so Crafty enforces that user's
permissions, and shares the web app's services and database.

Destructive tools (stopping, restarting or killing a server, removing mods, banning or
kicking players, changing a modpack version) take two calls: the first returns a plan
and a single-use confirm_token, the second repeats the same arguments plus that token.
"""

import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from typing import Annotated, Any, Literal

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from crafty_complete.catalog.models import CatalogError, SearchQuery
from crafty_complete.config import Settings
from crafty_complete.crafty.client import CraftyError
from crafty_complete.modpacks.creator import CreateRequest, CreationRefused
from crafty_complete.modpacks.packs import describe_failure
from crafty_complete.mods.adoption import NoModsFolder
from crafty_complete.mods.installer import ModOperationError
from crafty_complete.players.service import PlayerAction, PlayerError
from crafty_complete.servers.control import PowerAction
from crafty_complete.servers.creator import KINDS, NewServerRequest
from crafty_complete.services import Services, open_services

CONFIRM_SECONDS = 300
# Jobs started from the MCP server belong to this owner (one job at a time, like a user).
JOB_OWNER = "mcp"
NO_TOKEN = ("Set CRAFTY_COMPLETE_MCP_CRAFTY_TOKEN to a Crafty API token "
            "(Crafty panel > your user > API keys).")
EULA_REQUIRED = ("Set accept_minecraft_eula to true only if the user accepts the Minecraft "
                 "EULA (https://aka.ms/MinecraftEULA).")
BAD_CONFIRMATION = ("This confirmation token is invalid, expired or for other arguments; call "
                    "the tool again without confirm_token to get a new plan.")

ServerId = Annotated[str, Field(pattern=r"^[a-z0-9-]{1,64}$",
                                description="Crafty server id (from list_servers)")]
FileName = Annotated[str, Field(min_length=1, max_length=255,
                                description="Jar file name in mods/ (from list_mods)")]
ProjectRef = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$",
                                  description="Modrinth project id or slug")]
VersionId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$",
                                 description="Modrinth version id")]
ConfirmToken = Annotated[str | None, Field(
    default=None, description="Token from the first call's plan; omit it to get the plan")]
ServerName = Annotated[str, Field(min_length=2, max_length=64, pattern=r"^[^/\\#]*$")]
Memory = Annotated[float, Field(gt=0, le=256, description="GB")]
Port = Annotated[int, Field(ge=1, le=65535)]


@dataclass
class Pending:
    tool: str
    arguments: str
    expires: float
    # What the plan resolved (e.g. the modpack version "newest" meant), used on execution.
    resolved: dict[str, Any] = field(default_factory=dict)


@dataclass
class McpState:
    services: Services
    token: str | None
    superuser: bool | None = None


class Confirmations:
    """Single-use tokens tying a confirmation to one tool call with the same arguments."""

    def __init__(self):
        self._pending: dict[str, Pending] = {}

    def issue(self, tool: str, arguments: dict[str, Any], plan: dict[str, Any],
              resolved: dict[str, Any] | None = None) -> dict[str, Any]:
        now = time.monotonic()
        self._pending = {t: p for t, p in self._pending.items() if p.expires > now}
        token = secrets.token_urlsafe(16)
        self._pending[token] = Pending(tool, _canonical(arguments), now + CONFIRM_SECONDS,
                                       resolved or {})
        return {
            "confirmation_required": True,
            "plan": plan,
            "confirm_token": token,
            "expires_in_seconds": CONFIRM_SECONDS,
            "message": f"Show this plan to the user. If they agree, call {tool} again with the "
                       "same arguments and this confirm_token.",
        }

    def redeem(self, tool: str, arguments: dict[str, Any], token: str) -> Pending:
        pending = self._pending.pop(token, None)
        if (pending is None or pending.expires <= time.monotonic() or pending.tool != tool
                or pending.arguments != _canonical(arguments)):
            raise ToolError(BAD_CONFIRMATION)
        return pending


def _canonical(arguments: dict[str, Any]) -> str:
    return json.dumps(arguments, sort_keys=True, default=str)


def _describe(exc: Exception) -> str:
    if isinstance(exc, CraftyError):
        if exc.state == "unauthorized":
            return "Crafty rejected CRAFTY_COMPLETE_MCP_CRAFTY_TOKEN; create a new API token in Crafty."
        if exc.state == "forbidden":
            return "Crafty denied this action for the token's user."
        if exc.state == "not_found":
            return "Not found in Crafty."
        if exc.state == "unreachable":
            return "Crafty is unreachable."
        return f"Crafty request failed: {exc.message}"
    if isinstance(exc, (ModOperationError, PlayerError, CreationRefused)):
        return exc.detail
    if isinstance(exc, NoModsFolder):
        return "This server has no mods folder."
    return str(exc)


def create_mcp_server(settings: Settings | None = None,
                      transport: httpx.AsyncBaseTransport | None = None) -> MCPServer:
    """Build the MCP server. `transport` lets tests replace all outbound HTTP."""
    settings = settings or Settings()
    confirmations = Confirmations()

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[McpState]:
        async with open_services(settings, transport) as services:
            yield McpState(services, settings.mcp_crafty_token)

    mcp = MCPServer(
        "craftyComplete",
        instructions=(
            "Control Crafty Controller 4 Minecraft servers: status and power, Modrinth mods "
            "(with dependencies), players, new servers and modpacks. Tools that stop or kill "
            "servers, remove mods, ban or kick players or change a modpack version first "
            "return a plan and a confirm_token: show the plan to the user and call again with "
            "the token only if they agree. Long work (creating servers, modpack updates) "
            "returns a job_id to follow with job_status."),
        lifespan=lifespan,
    )

    def state(ctx: Context) -> McpState:
        current: McpState = ctx.request_context.lifespan_context
        if not current.token:
            raise ToolError(NO_TOKEN)
        if current.services.crafty is None:
            raise ToolError("Set CRAFTY_COMPLETE_CRAFTY_URL to the Crafty base URL.")
        return current

    async def guarded(operation: Awaitable[Any]) -> Any:
        try:
            return await operation
        except ToolError:
            raise
        except (CraftyError, CatalogError, ModOperationError, PlayerError, CreationRefused,
                NoModsFolder) as exc:
            raise ToolError(_describe(exc)) from exc

    def not_busy(current: McpState, server_id: str) -> None:
        reason = current.services.locks.busy(server_id)
        if reason:
            raise ToolError(reason)

    async def superuser(current: McpState) -> bool:
        if current.superuser is None:
            me = await guarded(current.services.crafty.get_me(current.token))
            current.superuser = me.superuser
        return current.superuser

    def start_job(current: McpState, title: str, work, describe) -> dict[str, Any]:
        jobs = current.services.jobs
        if jobs.running(JOB_OWNER):
            raise ToolError("A job is already running; follow it with job_status first.")
        started = jobs.start(JOB_OWNER, title, work, describe)
        return {"job_id": started.id, "title": title,
                "message": "Follow it with job_status until status is not 'running'."}

    # --- servers -------------------------------------------------------------------

    @mcp.tool()
    async def list_servers(ctx: Context) -> dict[str, Any]:
        """Servers the token's Crafty user can see, with whether each is running."""
        current = state(ctx)
        servers = await guarded(current.services.crafty.list_servers(current.token))
        result = []
        for item in servers:
            entry = {"server_id": item["server_id"], "server_name": item["server_name"],
                     "type": item.get("type")}
            try:
                status = await current.services.control.status(current.token, item["server_id"])
                entry.update(state=status["state"], players_online=status["players_online"])
            except CraftyError:
                entry.update(state="unknown")
            result.append(entry)
        return {"servers": result}

    @mcp.tool()
    async def server_status(server_id: ServerId, ctx: Context) -> dict[str, Any]:
        """State (running, stopped, starting, updating, crashed), players, CPU and memory."""
        current = state(ctx)
        return await guarded(current.services.control.status(current.token, server_id))

    @mcp.tool()
    async def server_power(server_id: ServerId,
                           action: Literal["start", "stop", "restart", "kill"], ctx: Context,
                           confirm_token: ConfirmToken = None) -> dict[str, Any]:
        """Start a server, or stop, restart or kill it (these three need confirmation)."""
        current = state(ctx)
        power = PowerAction(action)
        if power is PowerAction.START:
            not_busy(current, server_id)
        if power is not PowerAction.START:
            arguments = {"server_id": server_id, "action": action}
            if confirm_token is None:
                status = await guarded(current.services.control.status(current.token, server_id))
                return confirmations.issue("server_power", arguments, {
                    "action": action, "server_id": server_id, "state": status["state"],
                    "players_online": status["players"]})
            confirmations.redeem("server_power", arguments, confirm_token)
        await guarded(current.services.control.power(current.token, server_id, power))
        return {"action": action, "sent": True}

    # --- mods ----------------------------------------------------------------------

    @mcp.tool()
    async def adopt_server(server_id: ServerId, ctx: Context,
                           loader: Literal["fabric", "forge", "neoforge", "quilt"] | None = None,
                           game_version: str | None = None) -> dict[str, Any]:
        """Identify the jars in mods/ on Modrinth (adopt on first call, rescan later).

        Loader and Minecraft version are detected unless given."""
        current = state(ctx)
        not_busy(current, server_id)
        server = await guarded(current.services.adoption.scan(
            current.token, server_id, loader, game_version))
        return server.to_dict()

    @mcp.tool()
    async def list_mods(server_id: ServerId, ctx: Context) -> dict[str, Any]:
        """Adopted mods with version, update availability, pin and dependency flags."""
        current = state(ctx)
        await guarded(current.services.adoption.ensure_file_access(current.token, server_id))
        return current.services.adoption_store.get(server_id).to_dict()

    @mcp.tool()
    async def search_mods(query: str, ctx: Context,
                          project_type: Literal["mod", "modpack"] = "mod",
                          loader: Literal["fabric", "forge", "neoforge", "quilt"] | None = None,
                          game_version: str | None = None,
                          limit: Annotated[int, Field(ge=1, le=50)] = 10) -> dict[str, Any]:
        """Search Modrinth for server-compatible mods or modpacks."""
        current: McpState = ctx.request_context.lifespan_context
        page = await guarded(current.services.catalog.search(SearchQuery(
            text=query, project_type=project_type, loaders=(loader,) if loader else (),
            game_version=game_version, page_size=limit)))
        return asdict(page)

    @mcp.tool()
    async def install_mod(server_id: ServerId, project: ProjectRef,
                          ctx: Context) -> dict[str, Any]:
        """Install a Modrinth mod on an adopted server, with its missing required dependencies."""
        current = state(ctx)
        not_busy(current, server_id)
        return await guarded(current.services.installer.install(current.token, server_id, project))

    @mcp.tool()
    async def update_mods(server_id: ServerId, ctx: Context,
                          file_name: FileName | None = None) -> dict[str, Any]:
        """Update one mod to its newest version, or every unpinned mod with an update."""
        current = state(ctx)
        not_busy(current, server_id)
        installer = current.services.installer
        if file_name:
            return await guarded(installer.update(current.token, server_id, file_name))
        return await guarded(installer.update_all(current.token, server_id))

    @mcp.tool()
    async def check_mod_updates(server_id: ServerId, ctx: Context) -> dict[str, Any]:
        """Refresh which adopted mods have updates. Installs nothing."""
        current = state(ctx)
        adoption = current.services.adoption
        await guarded(adoption.ensure_file_access(current.token, server_id))
        return (await guarded(adoption.check_updates(server_id))).to_dict()

    @mcp.tool()
    async def mod_versions(server_id: ServerId, file_name: FileName,
                           ctx: Context) -> dict[str, Any]:
        """Versions of an installed mod that fit the server's loader and Minecraft version."""
        current = state(ctx)
        return await guarded(current.services.installer.versions(
            current.token, server_id, file_name))

    @mcp.tool()
    async def set_mod_version(server_id: ServerId, file_name: FileName, version_id: VersionId,
                              ctx: Context) -> dict[str, Any]:
        """Install a chosen version of a mod. It is not pinned; update_mods can move it."""
        current = state(ctx)
        not_busy(current, server_id)
        return await guarded(current.services.installer.set_version(
            current.token, server_id, file_name, version_id))

    @mcp.tool()
    async def pin_mod(server_id: ServerId, file_name: FileName, pinned: bool,
                      ctx: Context) -> dict[str, Any]:
        """Pin a mod (never updated) or unpin it."""
        current = state(ctx)
        not_busy(current, server_id)
        services = current.services
        await guarded(services.adoption.ensure_file_access(current.token, server_id))
        async with services.locks(server_id):
            mod = services.adoption_store.set_pinned(server_id, file_name, pinned)
        if mod is None:
            raise ToolError("No adopted mod with that file name.")
        return mod.to_dict()

    @mcp.tool()
    async def remove_mod(server_id: ServerId, file_name: FileName, ctx: Context,
                         remove_dependencies: list[FileName] | None = None, force: bool = False,
                         confirm_token: ConfirmToken = None) -> dict[str, Any]:
        """Remove a mod (needs confirmation). The plan lists mods that require it and unused
        dependencies; pass those to remove in remove_dependencies, and force=true to remove a
        mod other mods require."""
        current = state(ctx)
        not_busy(current, server_id)
        installer = current.services.installer
        dependencies = list(remove_dependencies or [])
        arguments = {"server_id": server_id, "file_name": file_name,
                     "remove_dependencies": dependencies, "force": force}
        if confirm_token is None:
            plan = await guarded(installer.removal_plan(current.token, server_id, file_name))
            return confirmations.issue("remove_mod", arguments, {
                **plan, "remove_dependencies": dependencies, "force": force})
        confirmations.redeem("remove_mod", arguments, confirm_token)
        return await guarded(installer.remove(current.token, server_id, file_name, force,
                                              dependencies))

    # --- players -------------------------------------------------------------------

    @mcp.tool()
    async def list_players(server_id: ServerId, ctx: Context) -> dict[str, Any]:
        """Players online, whitelist, operators and banned players."""
        current = state(ctx)
        return await guarded(current.services.players.players(current.token, server_id))

    @mcp.tool()
    async def manage_player(server_id: ServerId,
                            action: Literal["whitelist_add", "whitelist_remove", "op", "deop",
                                            "ban", "pardon", "kick"],
                            name: Annotated[str, Field(max_length=16)], ctx: Context,
                            reason: Annotated[str | None, Field(max_length=200)] = None,
                            confirm_token: ConfirmToken = None) -> dict[str, Any]:
        """Whitelist, op, ban or kick a Minecraft player (ban and kick need confirmation).

        A running server gets a console command; a stopped one gets its player files edited."""
        current = state(ctx)
        not_busy(current, server_id)
        players = current.services.players
        player_action = PlayerAction(action)
        if player_action in (PlayerAction.BAN, PlayerAction.KICK):
            arguments = {"server_id": server_id, "action": action, "name": name,
                         "reason": reason}
            if confirm_token is None:
                status = await guarded(current.services.control.status(current.token, server_id))
                return confirmations.issue("manage_player", arguments, {
                    "action": action, "name": name, "reason": reason,
                    "server_id": server_id, "server_running": status["running"]})
            confirmations.redeem("manage_player", arguments, confirm_token)
        return await guarded(players.apply(current.token, server_id, player_action, name, reason))

    # --- new servers and modpacks ----------------------------------------------------

    @mcp.tool()
    async def new_server_options(ctx: Context) -> dict[str, Any]:
        """Kinds of server (vanilla, paper, fabric, forge, neoforge) and the Minecraft
        versions this Crafty can install for each."""
        current = state(ctx)
        return await guarded(current.services.server_creator.options(current.token))

    @mcp.tool()
    async def create_server(kind: Literal[tuple(KINDS)], minecraft: str, name: ServerName,
                            accept_minecraft_eula: bool, ctx: Context, port: Port = 25565,
                            mem_min: Memory = 2, mem_max: Memory = 4,
                            mods: list[ProjectRef] | None = None) -> dict[str, Any]:
        """Create a server; for fabric/forge/neoforge, also install Modrinth mods with their
        dependencies. Only set accept_minecraft_eula if the user accepts the Minecraft EULA.
        Returns a job_id."""
        current = state(ctx)
        if not accept_minecraft_eula:
            raise ToolError(EULA_REQUIRED)
        if mem_min > mem_max:
            raise ToolError("Minimum memory cannot exceed maximum memory.")
        creator = current.services.server_creator
        request = NewServerRequest(kind=kind, minecraft=minecraft, name=name, port=port,
                                   mem_min=mem_min, mem_max=mem_max, mods=tuple(mods or ()))
        prepared = await guarded(creator.prepare(current.token, await superuser(current),
                                                 request))
        token = current.token
        holder: dict = {}
        result = start_job(current, f"{prepared.kind.label} {minecraft}: {name}",
                           lambda job: creator.run(job, token, prepared),
                           lambda exc: creator.describe_error(holder["job"], exc))
        holder["job"] = current.services.jobs.get(result["job_id"], JOB_OWNER)
        return result

    @mcp.tool()
    async def modpack_versions(project: ProjectRef, ctx: Context) -> dict[str, Any]:
        """Versions of a Modrinth modpack, with loaders and Minecraft versions."""
        current: McpState = ctx.request_context.lifespan_context
        catalog = current.services.catalog
        found = await guarded(catalog.project(project))
        versions = await guarded(catalog.project_versions(found["id"]))
        return {"project_id": found["id"], "title": found["title"], "versions": [{
            "id": v["id"], "version_number": v.get("version_number"),
            "version_type": v.get("version_type"), "loaders": v.get("loaders") or [],
            "game_versions": v.get("game_versions") or []} for v in versions]}

    @mcp.tool()
    async def create_modpack_server(project: ProjectRef, name: ServerName,
                                    accept_minecraft_eula: bool, ctx: Context,
                                    version_id: VersionId | None = None, port: Port = 25565,
                                    mem_min: Memory = 2, mem_max: Memory = 6) -> dict[str, Any]:
        """Create a server from a Fabric, Forge or NeoForge Modrinth modpack (default: its
        newest release). Only set accept_minecraft_eula if the user accepts the Minecraft
        EULA. Returns a job_id."""
        current = state(ctx)
        if not accept_minecraft_eula:
            raise ToolError(EULA_REQUIRED)
        if mem_min > mem_max:
            raise ToolError("Minimum memory cannot exceed maximum memory.")
        creator = current.services.modpack_creator
        request = CreateRequest(project_id=project, version_id=version_id, name=name,
                                port=port, mem_min=mem_min, mem_max=mem_max)
        prepared = await guarded(creator.prepare(current.token, await superuser(current),
                                                 request))
        token = current.token
        holder: dict = {}
        result = start_job(
            current, f"{prepared.project['title']} {prepared.version.get('version_number')}",
            lambda job: creator.run(job, token, prepared, request),
            lambda exc: creator.describe_error(holder["job"], exc))
        holder["job"] = current.services.jobs.get(result["job_id"], JOB_OWNER)
        return result

    @mcp.tool()
    async def modpack_status(server_id: ServerId, ctx: Context) -> dict[str, Any]:
        """The modpack version an adopted server is linked to and the newest compatible one."""
        current = state(ctx)
        return await guarded(current.services.modpack_linker.status(current.token, server_id))

    @mcp.tool()
    async def link_modpack(server_id: ServerId, project: ProjectRef, version_id: VersionId,
                           ctx: Context) -> dict[str, Any]:
        """Record which modpack version an adopted server runs. Returns a job_id."""
        current = state(ctx)
        linker = current.services.modpack_linker
        target = await guarded(linker.prepare(current.token, server_id, project, version_id))
        return start_job(
            current, f"Link {target.project['title']} {target.version.get('version_number')}",
            lambda job: linker.link(job, server_id, target), describe_failure)

    @mcp.tool()
    async def update_modpack(server_id: ServerId, ctx: Context,
                             version_id: VersionId | None = None,
                             confirm_token: ConfirmToken = None) -> dict[str, Any]:
        """Move a stopped, linked server to another version of its modpack (default: the
        newest). Needs confirmation; replaced files go to a backup folder. Returns a job_id."""
        current = state(ctx)
        updater = current.services.modpack_updater
        arguments = {"server_id": server_id, "version_id": version_id}
        if confirm_token is None:
            server, target = await guarded(updater.prepare(current.token, server_id, version_id))
            return confirmations.issue("update_modpack", arguments, {
                "server_id": server_id, "modpack": server.modpack_title,
                "from_version": server.modpack_version_number,
                "to_version": target.version.get("version_number"),
                "to_version_id": target.version["id"],
                "note": "The pack's files are replaced, mods you added are kept, and replaced "
                        "files are moved to .crafty-complete/backups/ on the server."},
                resolved={"version_id": target.version["id"]})
        pending = confirmations.redeem("update_modpack", arguments, confirm_token)
        server, target = await guarded(updater.prepare(
            current.token, server_id, pending.resolved["version_id"]))
        token = current.token
        return start_job(
            current, f"Update {server.modpack_title} {server.modpack_version_number} → "
                     f"{target.version.get('version_number')}",
            lambda job: updater.run(job, token, server_id, target), describe_failure)

    @mcp.tool()
    async def job_status(job_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")],
                         ctx: Context) -> dict[str, Any]:
        """Progress, warnings, errors and result of a job this MCP server started."""
        current: McpState = ctx.request_context.lifespan_context
        job = current.services.jobs.get(job_id, JOB_OWNER)
        if job is None:
            raise ToolError("No such job (jobs live only as long as this MCP server).")
        return job.to_dict()

    return mcp


def main() -> None:
    # Every outbound request would otherwise be logged to the MCP client's stderr.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    create_mcp_server().run("stdio")


if __name__ == "__main__":
    main()
