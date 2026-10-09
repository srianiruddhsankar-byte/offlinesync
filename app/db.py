"""SQLite connection helper, schema, and the transaction wrapper."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id          TEXT PRIMARY KEY,
    data        TEXT NOT NULL,
    version     INTEGER NOT NULL,
    field_meta  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS records_by_created ON records (created_at, id);

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


# Ordered upgrades. MIGRATIONS[0] takes version 1 (SCHEMA above) to 2, MIGRATIONS[1] takes 2 to 3,
# and so on; the code's schema version is 1 + len(MIGRATIONS). Append only, never edit or reorder.
# Statements are split on ";", so keep semicolons out of string literals.
MIGRATIONS: list[str] = []

DEFAULT_REQUEST_RETENTION_DAYS = 30


def db_path() -> str:
    return os.environ.get("DB_PATH", "sync.db")


def request_retention_days() -> int:
    """REQUEST_RETENTION_DAYS from the environment (default 30). Must be a positive integer."""
    raw = os.environ.get("REQUEST_RETENTION_DAYS", str(DEFAULT_REQUEST_RETENTION_DAYS))
    try:
        days = int(raw)
    except ValueError:
        days = 0
    if days < 1:
        raise ValueError(f"REQUEST_RETENTION_DAYS must be a positive integer, got {raw!r}")
    return days


def connect() -> sqlite3.Connection:
    # isolation_level=None -> autocommit; we issue BEGIN IMMEDIATE ourselves.
    conn = sqlite3.connect(db_path(), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class SchemaTooNewError(RuntimeError):
    """The database was written by a newer version of the code than this one."""


def _run_script(conn: sqlite3.Connection, sql: str) -> None:
    # executescript() would COMMIT the open transaction, so run statements one by one.
    for statement in sql.split(";"):
        if statement.strip():
            conn.execute(statement)


def init_db() -> None:
    """Create a fresh database at the latest version, or migrate an existing one forward.

    A database created before versioning existed has the version 1 tables and no version row,
    so it is treated as version 1. A database newer than this code is refused untouched.
    """
    latest = 1 + len(MIGRATIONS)
    conn = connect()
    try:
        with transaction(conn):
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version "
                "(id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)"
            )
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            current = row["version"] if row else 1
            if current > latest:
                raise SchemaTooNewError(
                    f"database schema version {current} is newer than this code supports ({latest}); "
                    f"upgrade the app or point DB_PATH at another database"
                )
            if row is None:
                _run_script(conn, SCHEMA)
                conn.execute("INSERT INTO schema_version (id, version) VALUES (1, 1)")
            for version in range(current, latest):
                _run_script(conn, MIGRATIONS[version - 1])
                conn.execute("UPDATE schema_version SET version = ?", (version + 1,))
    finally:
        conn.close()


def purge_expired_requests(retention_days: int | None = None, now: datetime | None = None) -> int:
    """Delete idempotency rows older than the retention period; return how many were deleted.

    Only the `requests` table is touched. records, change_log and devices are never changed, so
    history, pull cursors and per-device sequence numbers are unaffected.

    A retry whose row has expired is NOT replayed: it is processed as a brand-new request. That is
    safe because the rest of the state still guards it:
    - a change that advanced the device's sequence now carries a stale sequence number, so it is
      rejected with 409 STALE_SEQUENCE and nothing is applied twice (counters included);
    - a stored conflict, which did not advance the sequence, is re-evaluated against the current
      record exactly as a new request would be;
    - a restore that wrote a version now has a stale base_version, so it gets 409 STALE_BASE;
    - reusing the request_id with a different payload is no longer reported as REQUEST_ID_REUSED.
    Retention must therefore exceed the longest time a device may keep retrying the same request.
    """
    days = request_retention_days() if retention_days is None else retention_days
    # created_at is datetime.now(UTC).isoformat(); a UTC cutoff in the same format compares as text.
    cutoff = ((now or datetime.now(UTC)).astimezone(UTC) - timedelta(days=days)).isoformat()
    conn = connect()
    try:
        return conn.execute("DELETE FROM requests WHERE created_at < ?", (cutoff,)).rowcount
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
