"""Creating a server in Crafty and waiting until its jar or loader is installed.

Shared by modpack servers and by vanilla, Paper and modded servers.
"""

import asyncio
import time
from dataclasses import dataclass

from crafty_complete.config import Settings
from crafty_complete.crafty.client import CraftyClient
from crafty_complete.modpacks.jobs import Job, JobFailure
from crafty_complete.modpacks.transfer import ServerWriter

# Crafty's catalog category for Minecraft Java servers (GET /api/v2/crafty/JarCache).
CRAFTY_CATEGORY = "mc_java_servers"


@dataclass(frozen=True)
class NewServer:
    name: str
    port: int
    mem_min: float
    mem_max: float
    # Crafty catalog type (e.g. "paper", "forge-installer") and Minecraft version.
    crafty_type: str
    minecraft: str
    # For messages: "Paper", "Forge", ...
    label: str


def catalog_versions(catalog: dict, crafty_type: str) -> dict:
    """Minecraft version -> details Crafty's catalog lists for one server type."""
    return (catalog.get(CRAFTY_CATEGORY, {}).get("types", {})
            .get(crafty_type, {}).get("versions", {}))


async def create_and_install(crafty: CraftyClient, writer: ServerWriter, settings: Settings,
                             job: Job, token: str, server: NewServer) -> None:
    """Create the server and wait until Crafty has downloaded or installed it."""
    job.begin("Creating the server in Crafty")
    job.server_name = server.name
    job.server_id = await crafty.create_server(token, {
        "name": server.name,
        "monitoring_type": "minecraft_java",
        # Crafty pings the game port to show status; the server runs on Crafty's host.
        "minecraft_java_monitoring_data": {"host": "127.0.0.1", "port": server.port},
        "create_type": "minecraft_java",
        "minecraft_java_create_data": {
            "create_type": "download_jar",
            "download_jar_create_data": {
                "category": CRAFTY_CATEGORY,
                "type": server.crafty_type,
                "version": server.minecraft,
                "mem_min": server.mem_min,
                "mem_max": server.mem_max,
                "server_properties_port": server.port,
                "agree_to_eula": True,
            },
        },
    })

    installer = server.crafty_type.endswith("-installer")
    job.begin(f"Installing {server.label} in Crafty")
    deadline = time.monotonic() + settings.modpack_install_timeout_minutes * 60
    while True:
        stats = await writer.retry(lambda: crafty.server_stats(token, job.server_id))
        if not stats.get("importing"):
            details = await writer.retry(lambda: crafty.get_server(token, job.server_id))
            if not installer:
                # `importing` is also false during the ~3 s Crafty waits before its
                # download starts, so only the jar on disk proves it finished.
                root = await writer.retry(lambda: crafty.list_dir(token, job.server_id, ""))
                if any(e.name == details.get("executable") and not e.is_dir for e in root):
                    return
            # Crafty clears `importing` before its Forge/NeoForge installer runs; the
            # installer rewrites the start command once it has succeeded.
            elif "--installServer" not in (details.get("execution_command") or ""):
                return
        if time.monotonic() >= deadline:
            what = "installer" if installer else "download"
            raise JobFailure(
                f'Crafty created "{server.name}" but its {server.label} {what} did not '
                "finish in time. Check the server's terminal in Crafty, then delete the "
                "server there and try again.", names_server=True)
        await asyncio.sleep(settings.modpack_poll_seconds)
