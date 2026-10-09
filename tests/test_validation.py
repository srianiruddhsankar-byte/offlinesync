"""Validation rules and rejection paths that the scenario tests do not reach.

Every case asserts the exact outcome and that nothing was written.
"""

import asyncio
import uuid

import pytest

from app.db import connect
from app.main import BodySizeLimit
from app.merge import MAX_BODY, MAX_TAG_LEN, MAX_TAGS, MAX_TITLE


def make_record(laptop, rid, **changes):
    r, _ = laptop.send(rid, "create", 0, {"title": {"set": "First"}, **changes})
    assert r.status_code == 201


def counts():
    conn = connect()
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("records", "change_log")}
    finally:
        conn.close()


def assert_rejected(r, status, reason, detail=None):
    assert r.status_code == status
    body = r.json()
    assert body["outcome"] == "REJECTED" and body["reason"] == reason
    if detail is not None:
        assert body["detail"] == detail


# ---------------------------------------------------------------- field validation (merge.validate_changes)


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        ({"title": {"set": 5}}, "'title' must be a string"),
        ({"title": {"set": "x" * (MAX_TITLE + 1)}}, f"'title' exceeds {MAX_TITLE} characters"),
        ({"body": {"set": "x" * (MAX_BODY + 1)}}, f"'body' exceeds {MAX_BODY} characters"),
        ({"tags": {}}, '\'tags\' takes {"add": [...], "remove": [...]}'),
        ({"tags": {"add": ["a"], "replace": ["b"]}}, '\'tags\' takes {"add": [...], "remove": [...]}'),
        ({"tags": {"add": "a"}}, "'tags.add' must be a list of strings"),
        ({"tags": {"remove": ["a", 1]}}, "'tags.remove' must be a list of strings"),
        ({"tags": {"add": [""]}}, f"'tags' items must be 1-{MAX_TAG_LEN} characters"),
        ({"tags": {"add": ["x" * (MAX_TAG_LEN + 1)]}}, f"'tags' items must be 1-{MAX_TAG_LEN} characters"),
    ],
)
def test_invalid_field_values_are_rejected(client, laptop, rid, changes, detail):
    make_record(laptop, rid)
    before = client.get(f"/records/{rid}").json()
    r, _ = laptop.send(rid, "update", 1, changes)
    assert_rejected(r, 422, "VALIDATION_ERROR", detail)
    assert client.get(f"/records/{rid}").json() == before


def test_limits_are_inclusive(laptop, rid):
    make_record(laptop, rid)
    r, _ = laptop.send(
        rid,
        "update",
        1,
        {
            "title": {"set": "x" * MAX_TITLE},
            "body": {"set": "x" * MAX_BODY},
            "tags": {"add": ["x" * MAX_TAG_LEN]},
        },
    )
    assert r.status_code == 200


def test_update_needs_at_least_one_change(client, laptop, rid):
    make_record(laptop, rid)
    r, _ = laptop.send(rid, "update", 1, {})
    assert_rejected(r, 422, "VALIDATION_ERROR", "update needs at least one change")
    assert client.get(f"/records/{rid}").json()["version"] == 1


# ---------------------------------------------------------------- record tag limit (merge.apply_field)


def test_create_over_tag_limit_creates_nothing(client, laptop, rid):
    r, _ = laptop.send(rid, "create", 0, {"tags": {"add": [f"t{i}" for i in range(MAX_TAGS + 1)]}})
    assert_rejected(r, 422, "VALIDATION_ERROR", f"'tags' would exceed {MAX_TAGS} items")
    assert client.get(f"/records/{rid}").status_code == 404
    assert laptop.seq == 0  # the sequence number was not consumed


def test_update_over_tag_limit_changes_nothing(client, laptop, rid):
    make_record(laptop, rid, tags={"add": [f"t{i}" for i in range(MAX_TAGS - 1)]})
    before = client.get(f"/records/{rid}").json()
    r, _ = laptop.send(rid, "update", 1, {"tags": {"add": ["new1", "new2"]}})
    assert_rejected(r, 422, "VALIDATION_ERROR", f"'tags' would exceed {MAX_TAGS} items")
    assert client.get(f"/records/{rid}").json() == before
    # adding one more fills the record exactly, and a swap at the limit is fine
    assert laptop.send(rid, "update", 1, {"tags": {"add": ["new1"]}})[0].status_code == 200
    assert laptop.send(rid, "update", 2, {"tags": {"add": ["new2"], "remove": ["t0"]}})[0].status_code == 200


# ---------------------------------------------------------------- base_version rules (SyncRequest)


@pytest.mark.parametrize(
    ("op", "base", "msg"),
    [
        ("create", 1, "Value error, create requires base_version 0"),
        ("update", 0, "Value error, update requires base_version >= 1"),
        ("delete", 0, "Value error, delete requires base_version >= 1"),
    ],
)
def test_base_version_must_fit_the_op(client, laptop, rid, op, base, msg):
    if op != "create":
        make_record(laptop, rid)
    before = counts()
    changes = {"title": {"set": "x"}} if op != "delete" else None
    r, _ = laptop.send(rid, op, base, changes, sequence=laptop.seq + 1)
    assert r.status_code == 422 and r.json()["reason"] == "VALIDATION_ERROR"
    assert [e["msg"] for e in r.json()["errors"]] == [msg]
    assert counts() == before


# ---------------------------------------------------------------- delete and restore rejections


def test_delete_of_unknown_record_is_404(client, laptop, rid):
    r, _ = laptop.send(rid, "delete", 1)
    assert_rejected(r, 404, "UNKNOWN_RECORD")
    assert laptop.seq == 0


def test_delete_with_future_base_version_is_rejected(client, laptop, rid):
    make_record(laptop, rid)
    r, _ = laptop.send(rid, "delete", 5)
    assert_rejected(r, 422, "FUTURE_BASE_VERSION")
    assert r.json()["current_version"] == 1
    assert client.get(f"/records/{rid}").json()["data"]["deleted"] is False


def test_restore_with_future_base_version_is_rejected(client, laptop, rid):
    make_record(laptop, rid)
    laptop.send(rid, "update", 1, {"title": {"set": "Second"}})
    before = counts()
    body = {"request_id": str(uuid.uuid4()), "device_id": "viewer", "base_version": 3}
    r = client.post(f"/records/{rid}/restore/1", json=body)
    assert_rejected(r, 422, "FUTURE_BASE_VERSION")
    assert r.json()["current_version"] == 2
    assert counts() == before
    # the rejection is not stored: the same request_id works once the base is right
    r = client.post(f"/records/{rid}/restore/1", json={**body, "base_version": 2})
    assert r.status_code == 200 and r.json()["replayed"] is False


# ---------------------------------------------------------------- body size middleware internals


def run_middleware(incoming):
    """Run BodySizeLimit around a recording app; return (what the app received, what was sent)."""
    seen, sent = [], []

    async def inner(scope, receive, send):
        seen.append(await receive())
        seen.append(await receive())

    async def receive():
        return incoming.pop(0)

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/sync", "headers": []}
    asyncio.run(BodySizeLimit(inner)(scope, receive, send))
    return seen, sent


def test_client_disconnect_while_reading_body_stops_quietly():
    seen, sent = run_middleware(
        [{"type": "http.request", "body": b"{", "more_body": True}, {"type": "http.disconnect"}]
    )
    assert seen == [] and sent == []  # the app never runs on a half-received body


def test_buffered_body_is_replayed_once_then_reads_pass_through():
    seen, _ = run_middleware(
        [
            {"type": "http.request", "body": b'{"a":', "more_body": True},
            {"type": "http.request", "body": b"1}", "more_body": False},
            {"type": "http.disconnect"},
        ]
    )
    assert seen == [
        {"type": "http.request", "body": b'{"a":1}', "more_body": False},
        {"type": "http.disconnect"},  # later reads reach the real connection
    ]
