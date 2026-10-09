"""Integer inputs are bounded to SQLite's signed 64-bit range: out-of-range values are a 422, never a 500."""

import json
import uuid

import pytest

from app.db import connect
from app.merge import SQLITE_INT_MAX, SQLITE_INT_MIN

HUGE = 99999999999999999999


def make_record(laptop, rid):
    r, _ = laptop.send(rid, "create", 0, {"title": {"set": "First"}})
    assert r.status_code == 201


def assert_validation_error(r, field):
    assert r.status_code == 422
    body = r.json()
    assert body["outcome"] == "REJECTED" and body["reason"] == "VALIDATION_ERROR"
    assert any(e["loc"][-1] == field for e in body["errors"])


def sync_body(rid, **overrides):
    body = {
        "request_id": str(uuid.uuid4()),
        "device_id": "laptop",
        "sequence": 2,
        "record_id": rid,
        "op": "update",
        "base_version": 1,
        "changes": {"title": {"set": "x"}},
    }
    return {**body, **overrides}


def restore_body(base_version=1):
    return {"request_id": str(uuid.uuid4()), "device_id": "laptop", "base_version": base_version}


# ---------------------------------------------------------------- request fields


@pytest.mark.parametrize("field", ["sequence", "base_version"])
def test_huge_sync_integer_is_a_validation_error(client, laptop, rid, field):
    make_record(laptop, rid)
    r = client.post(f"/records/{rid}/sync", json=sync_body(rid, **{field: HUGE}))
    assert_validation_error(r, field)


@pytest.mark.parametrize("field", ["sequence", "base_version"])
def test_huge_batch_integer_rejects_the_envelope(client, laptop, rid, field):
    make_record(laptop, rid)
    r = client.post("/sync", json={"changes": [sync_body(rid, **{field: HUGE})]})
    assert_validation_error(r, field)


def test_huge_restore_base_version_is_a_validation_error(client, laptop, rid):
    make_record(laptop, rid)
    r = client.post(f"/records/{rid}/restore/1", json=restore_body(base_version=HUGE))
    assert_validation_error(r, "base_version")


def test_largest_sqlite_integer_is_still_accepted_as_input(client, laptop, rid):
    make_record(laptop, rid)
    r = client.post(f"/records/{rid}/sync", json=sync_body(rid, base_version=SQLITE_INT_MAX))
    assert r.status_code == 422 and r.json()["reason"] == "FUTURE_BASE_VERSION"
    assert client.get(f"/sync/changes?since={SQLITE_INT_MAX}").status_code == 200


# ---------------------------------------------------------------- query and path parameters


@pytest.mark.parametrize("param", ["since", "limit"])
def test_huge_pull_parameter_is_a_validation_error(client, param):
    r = client.get(f"/sync/changes?{param}={HUGE}")
    assert_validation_error(r, param)


def test_huge_version_path_parameter_is_a_validation_error(client, laptop, rid):
    make_record(laptop, rid)
    assert_validation_error(client.get(f"/records/{rid}/versions/{HUGE}"), "version")
    assert_validation_error(client.post(f"/records/{rid}/restore/{HUGE}", json=restore_body()), "version")


# ---------------------------------------------------------------- counter


def test_huge_increment_is_a_validation_error(laptop, rid):
    make_record(laptop, rid)
    r, _ = laptop.send(rid, "update", 1, {"views": {"increment": HUGE}})
    assert r.status_code == 422 and r.json()["reason"] == "VALIDATION_ERROR"


def set_views(rid, views):
    conn = connect()
    try:
        row = conn.execute("SELECT data FROM records WHERE id = ?", (rid,)).fetchone()
        data = json.loads(row["data"])
        data["views"] = views
        conn.execute("UPDATE records SET data = ? WHERE id = ?", (json.dumps(data), rid))
    finally:
        conn.close()


@pytest.mark.parametrize(("start", "increment"), [(SQLITE_INT_MAX, 1), (SQLITE_INT_MIN, -1)])
def test_counter_cannot_leave_sqlite_range(client, laptop, rid, start, increment):
    make_record(laptop, rid)
    set_views(rid, start)
    before = client.get(f"/records/{rid}").json()
    history = client.get(f"/records/{rid}/history").json()

    r, _ = laptop.send(rid, "update", 1, {"views": {"increment": increment}})

    assert r.status_code == 422 and r.json()["reason"] == "VALIDATION_ERROR"
    assert client.get(f"/records/{rid}").json() == before
    assert client.get(f"/records/{rid}/history").json() == history
    assert laptop.seq == 1  # the rejected change did not consume a sequence number


def test_counter_may_reach_the_range_limit_exactly(client, laptop, rid):
    make_record(laptop, rid)
    set_views(rid, SQLITE_INT_MAX - 1)
    r, _ = laptop.send(rid, "update", 1, {"views": {"increment": 1}})
    assert r.status_code == 200
    assert client.get(f"/records/{rid}").json()["data"]["views"] == SQLITE_INT_MAX
