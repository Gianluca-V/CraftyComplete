import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager


class ServerLocks:
    """One asyncio lock per server, so scans, installs and updates never interleave."""

    def __init__(self):
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._busy: dict[str, str] = {}

    def __call__(self, server_id: str) -> asyncio.Lock:
        return self._locks[server_id]

    def busy(self, server_id: str) -> str | None:
        """Why mod changes on this server are refused right now, if they are."""
        return self._busy.get(server_id)

    @contextmanager
    def marked_busy(self, server_id: str, reason: str) -> Iterator[None]:
        self._busy[server_id] = reason
        try:
            yield
        finally:
            self._busy.pop(server_id, None)

    @asynccontextmanager
    async def exclusive(self, server_id: str, reason: str) -> AsyncIterator[None]:
        """Hold the server's lock for a long operation; meanwhile requests that would
        wait for it are refused with `reason` instead of hanging."""
        async with self._locks[server_id]:
            with self.marked_busy(server_id, reason):
                yield
