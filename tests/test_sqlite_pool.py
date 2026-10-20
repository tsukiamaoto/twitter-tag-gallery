"""Pool lifecycle and concurrent ownership, using disposable SQLite databases."""

import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from sqlite_pool import SQLiteConnectionPool


class TrackedConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closed = threading.Event()

    def close(self):
        super().close()
        self.closed.set()


class SQLitePoolTests(unittest.TestCase):
    def make_pool(self, **kwargs):
        created = []

        def factory():
            conn = sqlite3.connect(":memory:", check_same_thread=False, factory=TrackedConnection)
            conn.execute("CREATE TABLE sample (value INTEGER)")
            created.append(conn)
            return conn

        pool = SQLiteConnectionPool(factory, **kwargs)
        self.addCleanup(pool.close)
        return pool, created

    def test_exception_returns_connection_and_rolls_back_transaction(self):
        pool, created = self.make_pool(max_size=1)
        with self.assertRaisesRegex(ValueError, "request failed"):
            with pool.connection() as first:
                first.execute("INSERT INTO sample VALUES (123)")
                raise ValueError("request failed")
        with pool.connection() as second:
            self.assertIs(second, first)
            self.assertEqual(second.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 0)
            self.assertFalse(second.in_transaction)
        self.assertEqual(len(created), 1)

    def test_sql_error_and_base_exception_also_return_the_lease(self):
        pool, _ = self.make_pool(max_size=1)
        for error in (sqlite3.OperationalError("SQL failed"), KeyboardInterrupt()):
            with self.subTest(error=type(error)), self.assertRaises(type(error)):
                with pool.connection():
                    raise error
            with pool.connection() as conn:
                self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)

    def test_sixty_seconds_is_measured_from_return_not_checkout(self):
        now = [0.0]
        pool, created = self.make_pool(clock=lambda: now[0])
        with pool.connection() as conn:
            now[0] = 1000.0
            self.assertEqual(pool.prune_idle(), 0)
            self.assertFalse(conn.closed.is_set())
        now[0] = 1059.9
        self.assertEqual(pool.prune_idle(), 0)
        self.assertFalse(conn.closed.is_set())
        now[0] = 1060.0
        pool.prune_idle()
        self.assertTrue(conn.closed.wait(1))
        with pool.connection() as replacement:
            self.assertIsNot(replacement, conn)
        self.assertEqual(len(created), 2)

    def test_reaper_closes_idle_connection_without_a_new_request(self):
        pool, _ = self.make_pool(idle_timeout=0.05)
        with pool.connection() as conn:
            pass
        self.assertTrue(conn.closed.wait(2), "idle reaper did not run")

    def test_reaper_never_closes_an_active_connection(self):
        pool, _ = self.make_pool(idle_timeout=0.05)
        with pool.connection() as active:
            with pool.connection() as idle:
                pass
            self.assertTrue(idle.closed.wait(2))
            pool.prune_idle()
            self.assertFalse(active.closed.is_set())
            self.assertEqual(active.execute("SELECT 42").fetchone()[0], 42)

    def test_close_drains_active_connection_only_when_it_is_returned(self):
        pool, _ = self.make_pool()
        with pool.connection() as active:
            with pool.connection() as idle:
                pass
            pool.close()
            self.assertTrue(idle.closed.is_set())
            self.assertFalse(active.closed.is_set())
            self.assertEqual(active.execute("SELECT 1").fetchone()[0], 1)
            with self.assertRaisesRegex(RuntimeError, "closed"):
                with pool.connection():
                    pass
        self.assertTrue(active.closed.is_set())

    def test_waiting_request_gets_connection_only_after_return(self):
        pool, created = self.make_pool(max_size=1)
        waiting = threading.Event()
        acquired = threading.Event()

        def request():
            waiting.set()
            with pool.connection() as conn:
                acquired.set()
                return conn.execute("SELECT 7").fetchone()[0]

        with ThreadPoolExecutor(max_workers=1) as executor:
            with pool.connection() as conn:
                future = executor.submit(request)
                self.assertTrue(waiting.wait(1))
                self.assertFalse(acquired.wait(0.05))
                self.assertFalse(conn.closed.is_set())
            self.assertEqual(future.result(timeout=2), 7)
        self.assertEqual(len(created), 1)

    def test_checkout_timeout_does_not_steal_an_active_connection(self):
        pool, _ = self.make_pool(max_size=1, checkout_timeout=0.05)
        with pool.connection() as conn:
            with self.assertRaises(TimeoutError):
                with pool.connection():
                    pass
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
            self.assertFalse(conn.closed.is_set())

    def test_failed_connection_creation_does_not_exhaust_pool(self):
        attempts = []

        def factory():
            attempts.append(1)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("cannot open")
            return sqlite3.connect(":memory:", check_same_thread=False)

        pool = SQLiteConnectionPool(factory, max_size=1)
        self.addCleanup(pool.close)
        with self.assertRaises(sqlite3.OperationalError):
            with pool.connection():
                pass
        with pool.connection() as conn:
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)

    def test_failed_reset_discards_connection_instead_of_reusing_it(self):
        pool, created = self.make_pool(max_size=1)
        with pool.connection() as conn:
            conn.close()
        with pool.connection() as replacement:
            self.assertIsNot(replacement, conn)
            self.assertEqual(replacement.execute("SELECT 1").fetchone()[0], 1)
        self.assertEqual(len(created), 2)


if __name__ == "__main__":
    unittest.main()
