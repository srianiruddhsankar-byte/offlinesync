"""SQLite connection helper, schema, and the transaction wrapper."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id          TEXT PRIMARY KEY,
    data        TEXT NOT NULL,
    version     INTEGER NOT NULL,
    field_meta  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_log (
    change_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id    TEXT NOT NULL,
    version      INTEGER NOT NULL,
    device_id    TEXT NOT NULL,
    request_id   TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    op           TEXT NOT NULL,
    ops          TEXT NOT NULL,
    after        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (record_id, version)
);

CREATE TABLE IF NOT EXISTS requests (
    request_id   TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    device_id    TEXT NOT NULL,
    status_code  INTEGER NOT NULL,
    response     TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    device_id     TEXT PRIMARY KEY,
    last_sequence INTEGER NOT NULL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
"""


def db_path() -> str:
    return os.environ.get("DB_PATH", "sync.db")


def connect() -> sqlite3.Connection:
    # isolation_level=None -> autocommit; we issue BEGIN IMMEDIATE ourselves.
    conn = sqlite3.connect(db_path(), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """BEGIN IMMEDIATE takes the write lock up front, so concurrent syncs serialize."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
