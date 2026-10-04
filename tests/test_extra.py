"""Edge cases around the HTTP layer: batch handling, replay headers, restore, and error handling."""

import sqlite3
import uuid

from fastapi.testclient import TestClient

from app.main import app


def make_record(laptop, rid):
    r, _ = laptop.send(rid, "create", 0, {"title": {"set": "First"}, "status": {"set": "TODO"}})
    assert r.status_code == 201


def batch_item(rid, device, seq, op, base, changes=None):
    return {
        "request_id": str(uuid.uuid4()),
        "device_id": device,
        "sequence": seq,
        "record_id": rid,
        "op": op,
        "base_version": base,
        "changes": changes or {},
    }


# ---------------------------------------------------------------- idempotency header


def test_replayed_response_carries_idempotent_replay_header(client, laptop, rid):
    make_record(laptop, rid)
    first, body = laptop.send(rid, "update", 1, {"title": {"set": "X"}})
    again = client.post(f"/records/{rid}/sync", json=body)
    assert "Idempotent-Replay" not in first.headers
    assert again.headers["Idempotent-Replay"] == "true"


def test_replayed_already_exists_conflict_is_stable(client, laptop, phone, rid):
    make_record(laptop, rid)
    first, body = phone.send(rid, "create", 0, {"title": {"set": "dup"}})
    again = client.post(f"/records/{rid}/sync", json=body)
    assert first.status_code == again.status_code == 409
    assert again.json()["reason"] == "ALREADY_EXISTS" and again.json()["replayed"] is True
    assert phone.seq == 0  # the conflicted create did not consume the sequence


# ---------------------------------------------------------------- batch endpoint


def test_batch_rejects_empty_and_oversized_envelopes(client, rid):
    assert client.post("/sync", json={"changes": []}).status_code == 422
    too_many = [batch_item(rid, "d", i + 1, "update", 1, {"title": {"set": "x"}}) for i in range(101)]
    assert client.post("/sync", json={"changes": too_many}).status_code == 422


def test_batch_with_one_malformed_item_applies_nothing(client, rid):
    good = batch_item(rid, "d", 1, "create", 0, {"title": {"set": "x"}})
    bad = batch_item(rid, "d", 2, "update", 1, {"title": {"set": "y"}})
    bad["request_id"] = "not-a-uuid"
    r = client.post("/sync", json={"changes": [good, bad]})
    assert r.status_code == 422 and r.json()["reason"] == "VALIDATION_ERROR"
    assert client.get(f"/records/{rid}").status_code == 404


def test_batch_spans_records_and_devices_and_replays(client):
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    items = [
        batch_item(a, "tablet", 1, "create", 0, {"title": {"set": "A"}}),
        batch_item(b, "watch", 1, "create", 0, {"title": {"set": "B"}}),
        batch_item(a, "tablet", 2, "update", 1, {"views": {"increment": 2}}),
    ]
    first = client.post("/sync", json={"changes": items}).json()["results"]
    assert [x["status"] for x in first] == [201, 201, 200]
    second = client.post("/sync", json={"changes": items}).json()["results"]
    assert [x["status"] for x in second] == [201, 201, 200]
    assert all(x["replayed"] for x in second)
    assert client.get(f"/records/{a}").json()["data"]["views"] == 2  # applied once


# ---------------------------------------------------------------- restore interplay


def test_restored_record_can_be_edited_at_the_current_version(client, laptop, phone, rid):
    make_record(laptop, rid)
    laptop.send(rid, "update", 1, {"title": {"set": "v2"}})
    restored = client.post(
        f"/records/{rid}/restore/1",
        json={"request_id": str(uuid.uuid4()), "device_id": "laptop", "base_version": 2},
    )
    assert restored.status_code == 200 and restored.json()["version"] == 3
    r, _ = phone.send(rid, "update", 3, {"title": {"set": "after restore"}})
    assert r.status_code == 200 and r.json()["outcome"] == "ACCEPTED"


# ---------------------------------------------------------------- reads


def test_unknown_record_reads_return_404(client):
    unknown = str(uuid.uuid4())
    assert client.get(f"/records/{unknown}").status_code == 404
    assert client.get(f"/records/{unknown}/history").status_code == 404
    assert client.get(f"/records/{unknown}/versions/1").status_code == 404


def test_list_hides_deleted_records_unless_asked(client, laptop, rid):
    make_record(laptop, rid)
    laptop.send(rid, "delete", 1)
    assert client.get("/records").json()["records"] == []
    listed = client.get("/records", params={"include_deleted": "true"}).json()["records"]
    assert [r["id"] for r in listed] == [rid]


def test_body_and_url_record_id_must_match(client, laptop, rid):
    _, body = laptop.send(rid, "create", 0, {"title": {"set": "x"}})
    other = str(uuid.uuid4())
    r = client.post(f"/records/{other}/sync", json={**body, "request_id": str(uuid.uuid4()), "sequence": 2})
    assert r.status_code == 422


# ---------------------------------------------------------------- error handling


def test_unexpected_error_returns_clean_json_500(db_env, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr("app.main.apply_change", boom)
    with TestClient(app, raise_server_exceptions=False) as c:
        rid = str(uuid.uuid4())
        body = {
            "request_id": str(uuid.uuid4()),
            "device_id": "d",
            "sequence": 1,
            "record_id": rid,
            "op": "create",
            "base_version": 0,
            "changes": {},
        }
        r = c.post(f"/records/{rid}/sync", json=body)
    assert r.status_code == 500
    assert r.json()["reason"] == "INTERNAL_ERROR" and "bug" not in r.text


def test_locked_database_returns_503_with_retry_after(db_env, monkeypatch):
    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("app.main.apply_change", locked)
    with TestClient(app, raise_server_exceptions=False) as c:
        rid = str(uuid.uuid4())
        body = {
            "request_id": str(uuid.uuid4()),
            "device_id": "d",
            "sequence": 1,
            "record_id": rid,
            "op": "create",
            "base_version": 0,
            "changes": {},
        }
        r = c.post(f"/records/{rid}/sync", json=body)
    assert r.status_code == 503
    assert r.json()["reason"] == "DATABASE_BUSY" and r.headers["Retry-After"] == "1"
