import asyncio
from collections import defaultdict


class ServerLocks:
    """One asyncio lock per server, so scans, installs and updates never interleave."""

    def __init__(self):
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def __call__(self, server_id: str) -> asyncio.Lock:
        return self._locks[server_id]
