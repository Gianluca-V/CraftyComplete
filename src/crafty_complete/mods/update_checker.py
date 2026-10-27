import asyncio
import logging

from crafty_complete.catalog.models import CatalogError
from crafty_complete.mods.adoption import AdoptionService
from crafty_complete.mods.store import AdoptionStore

logger = logging.getLogger(__name__)


class UpdateChecker:
    """Periodically refresh "update available" for every adopted server. Never installs."""

    def __init__(self, adoption: AdoptionService, store: AdoptionStore, interval_hours: float):
        self._adoption = adoption
        self._store = store
        self._interval_seconds = interval_hours * 3600
        self._task: asyncio.Task | None = None

    async def run_once(self) -> int:
        """Check every adopted server; returns how many were checked successfully."""
        checked = 0
        for server_id in self._store.server_ids():
            try:
                await self._adoption.check_updates(server_id)
            except CatalogError as exc:
                logger.warning("Update check for server %s failed: %s", server_id, exc)
                continue
            checked += 1
        return checked

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                await self.run_once()
            except Exception:  # keep the timer alive whatever happens
                logger.exception("Background update check failed")

    def start(self) -> None:
        if self._interval_seconds > 0 and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
