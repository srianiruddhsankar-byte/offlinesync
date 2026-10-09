"""Schema versioning: fresh, existing and newer databases, and ordered migrations."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "schema.db"
    monkeypatch.setenv("DB_PATH", str(path))
    return path


def query(sql):
    conn = db.connect()
    try:
        return [tuple(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def tables():
    return {r[0] for r in query("SELECT name FROM sqlite_master WHERE type = 'table'")}


def set_version(version):
    conn = db.connect()
    try:
        conn.execute("UPDATE schema_version SET version = ?", (version,))
    finally:
        conn.close()


def test_fresh_database_starts_at_version_1(db_file):
    db.init_db()
    assert query("SELECT id, version FROM schema_version") == [(1, 1)]
    assert {"records", "change_log", "requests", "devices", "schema_version"} <= tables()


def test_init_is_idempotent(db_file):
    db.init_db()
    db.init_db()
    assert query("SELECT version FROM schema_version") == [(1,)]


def test_schema_version_holds_a_single_row(db_file):
    db.init_db()
    with pytest.raises(sqlite3.IntegrityError):
        query("INSERT INTO schema_version (id, version) VALUES (2, 1)")


def test_database_from_before_versioning_is_adopted_as_version_1(db_file):
    conn = sqlite3.connect(db_file)  # the old init_db: tables only, no schema_version
    conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO records VALUES ('r1', '{}', 3, '{}', 't', 't')")
    conn.commit()
    conn.close()

    db.init_db()

    assert query("SELECT version FROM schema_version") == [(1,)]
    assert query("SELECT id, version FROM records") == [("r1", 3)]


def test_existing_versioned_database_keeps_its_data(db_file):
    db.init_db()
    conn = db.connect()
    try:
        conn.execute("INSERT INTO devices VALUES ('laptop', 4, 't', 't')")
    finally:
        conn.close()

    db.init_db()

    assert query("SELECT device_id, last_sequence FROM devices") == [("laptop", 4)]
    assert query("SELECT version FROM schema_version") == [(1,)]


def test_newer_database_fails_fast_and_is_left_untouched(db_file):
    db.init_db()
    set_version(2)
    before = query("SELECT type, name, sql FROM sqlite_master ORDER BY name")

    with pytest.raises(
        db.SchemaTooNewError, match="schema version 2 is newer than this code supports \\(1\\)"
    ):
        db.init_db()

    assert query("SELECT version FROM schema_version") == [(2,)]
    assert query("SELECT type, name, sql FROM sqlite_master ORDER BY name") == before


def test_newer_database_stops_the_server_starting(db_file):
    db.init_db()
    set_version(99)
    with pytest.raises(db.SchemaTooNewError), TestClient(app):
        pass


# ---------------------------------------------------------------- migrations


def test_migrations_run_in_order_once(db_file, monkeypatch):
    db.init_db()
    monkeypatch.setattr(
        db,
        "MIGRATIONS",
        [
            "CREATE TABLE m (step INTEGER); INSERT INTO m VALUES (2)",
            "INSERT INTO m VALUES (3)",
        ],
    )
    db.init_db()
    db.init_db()
    assert query("SELECT step FROM m") == [(2,), (3,)]
    assert query("SELECT version FROM schema_version") == [(3,)]


def test_fresh_database_runs_every_migration(db_file, monkeypatch):
    monkeypatch.setattr(db, "MIGRATIONS", ["CREATE TABLE m (step INTEGER)"])
    db.init_db()
    assert "m" in tables()
    assert query("SELECT version FROM schema_version") == [(2,)]


def test_failed_migration_rolls_back_completely(db_file, monkeypatch):
    db.init_db()
    monkeypatch.setattr(db, "MIGRATIONS", ["CREATE TABLE m (step INTEGER)", "INSERT INTO nowhere VALUES (1)"])
    with pytest.raises(sqlite3.OperationalError):
        db.init_db()
    assert "m" not in tables()
    assert query("SELECT version FROM schema_version") == [(1,)]
