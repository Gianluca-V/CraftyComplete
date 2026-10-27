import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

SESSION_COOKIE = "cc_session"


@dataclass
class Session:
    id: str
    # The Crafty JWT never leaves the server: the browser only holds the opaque session id.
    crafty_token: str
    user_id: str
    username: str
    superuser: bool
    created_at: float
    last_seen: float


class SessionStore:
    """In-memory sessions with idle and absolute expiry.

    Crafty tokens never expire on their own, so they are kept in memory only:
    a restart logs everyone out instead of leaving long-lived tokens on disk.
    """

    def __init__(self, idle_seconds: float, max_seconds: float,
                 clock: Callable[[], float] = time.monotonic):
        self._idle_seconds = idle_seconds
        self._max_seconds = max_seconds
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, crafty_token: str, user_id: str, username: str,
               superuser: bool) -> Session:
        now = self._clock()
        session = Session(
            id=secrets.token_urlsafe(32),
            crafty_token=crafty_token,
            user_id=user_id,
            username=username,
            superuser=superuser,
            created_at=now,
            last_seen=now,
        )
        with self._lock:
            self._sessions[session.id] = session
        return session

    def get(self, session_id: str | None) -> Session | None:
        """Return the live session and refresh its idle timer, or None."""
        if not session_id:
            return None
        now = self._clock()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if (now - session.last_seen > self._idle_seconds
                    or now - session.created_at > self._max_seconds):
                del self._sessions[session_id]
                return None
            session.last_seen = now
            return session

    def delete(self, session_id: str | None) -> None:
        with self._lock:
            self._sessions.pop(session_id or "", None)

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)
