"""A small SQLite pool with exclusive leases and idle-only reclamation."""

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator


logger = logging.getLogger(__name__)


class SQLiteConnectionPool:
    def __init__(
        self,
        factory: Callable[[], sqlite3.Connection],
        max_size: int = 4,
        idle_timeout: float = 60.0,
        checkout_timeout: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        if max_size < 1 or idle_timeout <= 0 or checkout_timeout <= 0:
            raise ValueError("Pool size and timeouts must be positive")
        self._factory = factory
        self._max_size = max_size
        self._idle_timeout = idle_timeout
        self._checkout_timeout = checkout_timeout
        self._clock = clock
        self._condition = threading.Condition()
        self._idle = []
        self._checked_out = set()
        self._total = 0  # Includes connections still being created or reset.
        self._closed = False
        self._reaper = None

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Always return the lease; callers must explicitly commit any writes."""
        conn = self._acquire()
        try:
            yield conn
        finally:
            self._release(conn)

    def _acquire(self) -> sqlite3.Connection:
        deadline = time.monotonic() + self._checkout_timeout
        while True:
            self.prune_idle()
            with self._condition:
                if self._closed:
                    raise RuntimeError("SQLite connection pool is closed")
                if self._idle:
                    conn, _ = self._idle.pop()
                    self._checked_out.add(conn)
                    return conn
                if self._total < self._max_size:
                    if self._reaper is None:
                        self._reaper = threading.Thread(
                            target=self._reap_idle,
                            name="sqlite-idle-reaper",
                            daemon=True,
                        )
                        self._reaper.start()
                    self._total += 1
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Timed out waiting for a SQLite connection")
                self._condition.wait(remaining)

        # Connecting can touch disk; do not hold the pool lock while doing it.
        try:
            conn = self._factory()
        except BaseException:
            with self._condition:
                self._total -= 1
                self._condition.notify_all()
            raise
        with self._condition:
            if not self._closed:
                self._checked_out.add(conn)
                return conn
            self._total -= 1
            self._condition.notify_all()
        self._close_connections([conn])
        raise RuntimeError("SQLite connection pool is closed")

    def _release(self, conn: sqlite3.Connection):
        reusable = True
        try:
            # Never hand another request an uncommitted transaction or its locks.
            conn.rollback()
        except sqlite3.Error:
            reusable = False
        with self._condition:
            self._checked_out.remove(conn)
            discard = self._closed or not reusable
            if discard:
                self._total -= 1
            else:
                self._idle.append((conn, self._clock()))
            self._condition.notify_all()
        if discard:
            self._close_connections([conn])

    def _expired_locked(self):
        now = self._clock()
        expired = [conn for conn, since in self._idle if now - since >= self._idle_timeout]
        self._idle = [(conn, since) for conn, since in self._idle if now - since < self._idle_timeout]
        self._total -= len(expired)
        if expired:
            self._condition.notify_all()
        return expired

    def prune_idle(self) -> int:
        """Close expired, returned connections; checked-out leases are untouched."""
        with self._condition:
            expired = self._expired_locked()
        self._close_connections(expired)
        return len(expired)

    def _reap_idle(self):
        while True:
            with self._condition:
                if self._closed:
                    return
                expired = self._expired_locked()
                if not expired:
                    wait = None
                    if self._idle:
                        expires_at = min(since for _, since in self._idle) + self._idle_timeout
                        wait = max(0.0, expires_at - self._clock())
                    self._condition.wait(wait)
                    continue
            self._close_connections(expired)

    def close(self):
        """Stop lending and close idle connections; active leases close on return."""
        with self._condition:
            self._closed = True
            idle = [conn for conn, _ in self._idle]
            self._idle.clear()
            self._total -= len(idle)
            self._condition.notify_all()
        self._close_connections(idle)

    @staticmethod
    def _close_connections(connections):
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                logger.exception("Failed to close an idle SQLite connection")
