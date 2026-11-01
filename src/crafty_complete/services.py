"""The application services, built once and shared by the web app and the MCP server."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from crafty_complete.catalog.modrinth import ModrinthProvider
from crafty_complete.config import Settings
from crafty_complete.crafty.client import CookielessTransport, CraftyClient
from crafty_complete.modpacks.creator import ModpackServerCreator
from crafty_complete.modpacks.jobs import JobRegistry
from crafty_complete.modpacks.linker import ModpackLinker
from crafty_complete.modpacks.updater import ModpackUpdater
from crafty_complete.mods.adoption import AdoptionService
from crafty_complete.mods.installer import ModInstaller
from crafty_complete.mods.locks import ServerLocks
from crafty_complete.mods.store import AdoptionStore
from crafty_complete.mods.update_checker import UpdateChecker
from crafty_complete.players.service import PlayerService
from crafty_complete.servers.control import ServerControl
from crafty_complete.servers.creator import ServerCreator


@dataclass
class Services:
    catalog: ModrinthProvider
    # None while CRAFTY_COMPLETE_CRAFTY_URL is not set.
    crafty: CraftyClient | None
    adoption_store: AdoptionStore
    locks: ServerLocks
    adoption: AdoptionService
    installer: ModInstaller
    server_creator: ServerCreator
    update_checker: UpdateChecker
    modpack_creator: ModpackServerCreator
    modpack_linker: ModpackLinker
    modpack_updater: ModpackUpdater
    control: ServerControl
    players: PlayerService
    jobs: JobRegistry


@asynccontextmanager
async def open_services(settings: Settings,
                       transport: httpx.AsyncBaseTransport | None = None
                       ) -> AsyncIterator[Services]:
    """Build every service; on exit, stop running jobs and close connections.

    `transport` lets tests replace all outbound HTTP.
    """
    modrinth_http = httpx.AsyncClient(
        base_url=settings.modrinth_url,
        headers={"User-Agent": settings.user_agent},
        timeout=settings.http_timeout_seconds,
        transport=transport,
    )
    mojang_http = httpx.AsyncClient(
        base_url=settings.mojang_url,
        headers={"User-Agent": settings.user_agent},
        timeout=settings.http_timeout_seconds,
        transport=transport,
    )
    crafty_http = None
    crafty = None
    if settings.crafty_url:
        crafty_http = httpx.AsyncClient(
            base_url=settings.crafty_url.rstrip("/"),
            timeout=settings.http_timeout_seconds,
            transport=CookielessTransport(
                transport or httpx.AsyncHTTPTransport(verify=settings.crafty_verify_tls)
            ),
        )
        crafty = CraftyClient(crafty_http)

    catalog = ModrinthProvider(modrinth_http)
    store = AdoptionStore(settings.database_path)
    locks = ServerLocks()
    # Callers refuse to use these while Crafty is not configured; the background update
    # check only needs Modrinth and the store.
    adoption = AdoptionService(crafty, catalog, store, locks)
    installer = ModInstaller(crafty, catalog, store, adoption, locks)
    linker = ModpackLinker(catalog, adoption, store, locks, settings)
    services = Services(
        catalog=catalog,
        crafty=crafty,
        adoption_store=store,
        locks=locks,
        adoption=adoption,
        installer=installer,
        server_creator=ServerCreator(crafty, adoption, installer, store, locks, settings),
        update_checker=UpdateChecker(adoption, store, settings.update_check_hours),
        modpack_creator=ModpackServerCreator(crafty, catalog, adoption, store, locks, settings),
        modpack_linker=linker,
        modpack_updater=ModpackUpdater(crafty, catalog, linker, adoption, store, locks,
                                       settings),
        control=ServerControl(crafty),
        players=PlayerService(crafty, mojang_http, locks),
        jobs=JobRegistry(),
    )
    try:
        yield services
    finally:
        await services.jobs.shutdown()
        store.close()
        await modrinth_http.aclose()
        await mojang_http.aclose()
        if crafty_http:
            await crafty_http.aclose()
