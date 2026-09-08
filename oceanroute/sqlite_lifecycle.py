"""Short process-wide guard for managed SQLite connection open/close.

SQLite 3.51.0/3.51.1's Unix WAL VFS can invert the inode/global mutex order
when one thread opens a database as another closes it. SQLite 3.51.2 fixes this
upstream: https://sqlite.org/releaselog/3_51_2.html and
https://sqlite.org/forum/info/7497f8f6052ed70a763672edffb414ec6c6834037cf2d7d4e5b486284fcc6b7a

Both stores share this gate, including different database files because the
VFS mutex is global to the SQLite library. Only connect and explicit close
are serialized: transactions, WAL reads, commit/rollback and cross-process
revision conflicts retain their existing SQLite locking semantics. This
does not guard unrelated libraries/raw SQLite users or inherited connections
after fork; those must use a fixed native SQLite or manage their own lifecycle.
"""
from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from threading import RLock


_lifecycle_gate = RLock()


@contextmanager
def managed_connection(path, *, foreign_keys=False, timeout=15):
    with _lifecycle_gate:
        connection = sqlite3.connect(path, timeout=timeout)
    # Include initialization in the cleanup scope: a failed PRAGMA must not
    # leak an unclosed connection whose eventual GC bypasses the close gate.
    try:
        connection.row_factory = sqlite3.Row
        if foreign_keys:
            connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        with _lifecycle_gate:
            connection.close()
