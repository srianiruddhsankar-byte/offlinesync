"""Idempotency retention: expired `requests` rows are purged, nothing else is touched,
and a retry after expiry is handled as a new request."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import connect, purge_expired_requests, request_retention_days
from app.main import app

NOW = datetime.now(UTC)


def query(sql, params=()):
    conn = connect()
    try:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def backdate(request_id, days):
    conn = connect()
    try:
        when = (NOW - timedelta(days=days)).isoformat()
        conn.execute("UPDATE requests SET created_at = ? WHERE request_id = ?", (when, request_id))
    finally:
        conn.close()


def request_ids():
    return {r[0] for r in query("SELECT request_id FROM requests")}


def snapshot():
    return {
        table: query(f"SELECT * FROM {table} ORDER BY 1") for table in ("records", "change_log", "devices")
    }


# ---------------------------------------------------------------- purge


def test_purge_deletes_only_old_request_rows(laptop, rid):
    _, old = laptop.send(rid, "create", 0, {"title": {"set": "a"}})
    _, edge = laptop.send(rid, "update", 1, {"title": {"set": "b"}})
    _, fresh = laptop.send(rid, "update", 2, {"title": {"set": "c"}})
    backdate(old["request_id"], 31)
    backdate(edge["request_id"], 29)
    before = snapshot()

    assert purge_expired_requests(retention_days=30, now=NOW) == 1

    assert request_ids() == {edge["request_id"], fresh["request_id"]}
    assert snapshot() == before  # records, change_log and devices are untouched


def test_purge_with_nothing_expired_deletes_nothing(laptop, rid):
    laptop.send(rid, "create", 0, {"title": {"set": "a"}})
    before = request_ids()
    assert purge_expired_requests(retention_days=30, now=NOW) == 0
    assert request_ids() == before


def test_purge_everything_old_leaves_history_and_reads_intact(client, laptop, rid):
    laptop.send(rid, "create", 0, {"title": {"set": "a"}, "views": {"increment": 1}})
    laptop.send(rid, "update", 1, {"views": {"increment": 2}})
    record = client.get(f"/records/{rid}").json()
    history = client.get(f"/records/{rid}/history").json()
    changes = client.get("/sync/changes").json()
    for request_id in request_ids():
        backdate(request_id, 365)
    before = snapshot()

    assert purge_expired_requests(retention_days=30, now=NOW) == 2

    assert request_ids() == set()
    assert snapshot() == before
    assert client.get(f"/records/{rid}").json() == record
    assert client.get(f"/records/{rid}/history").json() == history
    assert client.get("/sync/changes").json() == changes


def test_purge_uses_the_environment_by_default(monkeypatch, laptop, rid):
    _, body = laptop.send(rid, "create", 0, {"title": {"set": "a"}})
    backdate(body["request_id"], 8)
    monkeypatch.setenv("REQUEST_RETENTION_DAYS", "10")
    assert purge_expired_requests() == 0
    monkeypatch.setenv("REQUEST_RETENTION_DAYS", "7")
    assert purge_expired_requests() == 1


def test_startup_purges_expired_rows(db_env):
    with TestClient(app) as c:
        rid = str(uuid.uuid4())
        old = {
            "request_id": str(uuid.uuid4()),
            "device_id": "laptop",
            "sequence": 1,
            "record_id": rid,
            "op": "create",
            "base_version": 0,
            "changes": {"title": {"set": "a"}},
        }
        assert c.post(f"/records/{rid}/sync", json=old).status_code == 201
    backdate(old["request_id"], 31)

    with TestClient(app):  # entering the client runs the lifespan, as a server restart would
        pass
    assert request_ids() == set()


# ---------------------------------------------------------------- configuration


def test_retention_defaults_to_30_days(monkeypatch):
    monkeypatch.delenv("REQUEST_RETENTION_DAYS", raising=False)
    assert request_retention_days() == 30


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "1.5", ""])
def test_invalid_retention_is_rejected(monkeypatch, raw):
    monkeypatch.setenv("REQUEST_RETENTION_DAYS", raw)
    with pytest.raises(ValueError, match="REQUEST_RETENTION_DAYS"):
        request_retention_days()


# ---------------------------------------------------------------- retries after expiry


def test_retry_before_expiry_is_replayed(client, laptop, rid):
    laptop.send(rid, "create", 0, {"title": {"set": "a"}})
    _, body = laptop.send(rid, "update", 1, {"views": {"increment": 5}})
    backdate(body["request_id"], 29)
    purge_expired_requests(retention_days=30, now=NOW)

    r = client.post(f"/records/{rid}/sync", json=body)
    assert r.status_code == 200 and r.json()["replayed"] is True


def test_retry_after_expiry_is_a_new_request_and_applies_nothing(client, laptop, rid):
    laptop.send(rid, "create", 0, {"title": {"set": "a"}})
    _, body = laptop.send(rid, "update", 1, {"views": {"increment": 5}})
    backdate(body["request_id"], 31)
    purge_expired_requests(retention_days=30, now=NOW)
    before = snapshot()

    r = client.post(f"/records/{rid}/sync", json=body)

    assert r.status_code == 409
    assert r.json()["reason"] == "STALE_SEQUENCE" and r.json()["replayed"] is False
    assert "Idempotent-Replay" not in r.headers
    assert snapshot() == before
    assert client.get(f"/records/{rid}").json()["data"]["views"] == 5  # not incremented twice


def test_retry_of_expired_restore_is_a_new_request(client, laptop, rid):
    laptop.send(rid, "create", 0, {"title": {"set": "a"}})
    laptop.send(rid, "update", 1, {"title": {"set": "b"}})
    body = {"request_id": str(uuid.uuid4()), "device_id": "viewer", "base_version": 2}
    assert client.post(f"/records/{rid}/restore/1", json=body).status_code == 200
    backdate(body["request_id"], 31)
    purge_expired_requests(retention_days=30, now=NOW)
    before = snapshot()

    r = client.post(f"/records/{rid}/restore/1", json=body)

    assert r.status_code == 409 and r.json()["reason"] == "STALE_BASE"
    assert snapshot() == before


def test_expired_request_id_can_be_reused_with_a_new_payload(client, laptop, rid):
    _, body = laptop.send(rid, "create", 0, {"title": {"set": "a"}})
    backdate(body["request_id"], 31)
    reused = {**body, "sequence": 2, "op": "update", "base_version": 1, "changes": {"title": {"set": "b"}}}
    assert client.post(f"/records/{rid}/sync", json=reused).json()["reason"] == "REQUEST_ID_REUSED"

    purge_expired_requests(retention_days=30, now=NOW)

    r = client.post(f"/records/{rid}/sync", json=reused)
    assert r.status_code == 200 and r.json()["outcome"] == "ACCEPTED"
