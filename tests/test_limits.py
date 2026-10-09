"""Payload limits: per-request tag list caps and the POST body size limit."""

import asyncio
import json
import uuid

import pytest

from app.main import app
from app.merge import MAX_BODY, MAX_REQUEST_BYTES, MAX_TAG_LEN, MAX_TAG_OPS


def make_record(laptop, rid):
    r, _ = laptop.send(rid, "create", 0, {"title": {"set": "First"}})
    assert r.status_code == 201


def tags(n, prefix="t"):
    return [f"{prefix}{i}" for i in range(n)]


def sync_body(rid, seq, **changes):
    return {
        "request_id": str(uuid.uuid4()),
        "device_id": "laptop",
        "sequence": seq,
        "record_id": rid,
        "op": "update",
        "base_version": 1,
        "changes": changes or {"title": {"set": "x"}},
    }


def assert_too_large(r):
    assert r.status_code == 413
    body = r.json()
    assert body["outcome"] == "REJECTED" and body["reason"] == "PAYLOAD_TOO_LARGE"


# ---------------------------------------------------------------- tag list caps


@pytest.mark.parametrize("key", ["add", "remove"])
def test_tag_list_over_cap_is_a_validation_error(client, laptop, rid, key):
    make_record(laptop, rid)
    r, _ = laptop.send(rid, "update", 1, {"tags": {key: tags(MAX_TAG_OPS + 1)}})
    assert r.status_code == 422
    body = r.json()
    assert body["reason"] == "VALIDATION_ERROR"
    assert body["detail"] == f"'tags.{key}' has {MAX_TAG_OPS + 1} items; at most {MAX_TAG_OPS} per request"
    assert client.get(f"/records/{rid}").json()["version"] == 1


def test_tag_lists_at_cap_are_accepted(laptop, rid):
    make_record(laptop, rid)
    # 50 removes of absent tags plus 20 adds stays within MAX_TAGS on the record itself.
    r, _ = laptop.send(rid, "update", 1, {"tags": {"add": tags(20), "remove": tags(MAX_TAG_OPS, "r")}})
    assert r.status_code == 200
    r, _ = laptop.send(rid, "update", 2, {"tags": {"remove": tags(MAX_TAG_OPS)}})
    assert r.status_code == 200


def test_tag_list_cap_applies_in_batch(client, rid):
    item = sync_body(rid, 1, tags={"add": tags(MAX_TAG_OPS + 1)})
    item.update(op="create", base_version=0)
    r = client.post("/sync", json={"changes": [item]})
    assert r.status_code == 200
    assert r.json()["results"][0]["status"] == 422
    assert r.json()["results"][0]["reason"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------- body size limit


def padded(body, size):
    """JSON for body, padded with trailing whitespace to exactly size bytes."""
    raw = json.dumps(body).encode()
    return raw + b" " * (size - len(raw))


def test_body_at_limit_is_accepted(client, laptop, rid):
    make_record(laptop, rid)
    content = padded(sync_body(rid, 2), MAX_REQUEST_BYTES)
    r = client.post(f"/records/{rid}/sync", content=content, headers={"Content-Type": "application/json"})
    assert r.status_code == 200


def test_body_over_limit_is_413(client, laptop, rid):
    make_record(laptop, rid)
    content = padded(sync_body(rid, 2), MAX_REQUEST_BYTES + 1)
    r = client.post(f"/records/{rid}/sync", content=content, headers={"Content-Type": "application/json"})
    assert_too_large(r)
    assert client.get(f"/records/{rid}").json()["version"] == 1


@pytest.mark.parametrize(
    "path", ["/sync", f"/records/{uuid.uuid4()}/sync", f"/records/{uuid.uuid4()}/restore/1"]
)
def test_every_post_route_is_limited(client, path):
    r = client.post(
        path, content=b"x" * (MAX_REQUEST_BYTES + 1), headers={"Content-Type": "application/json"}
    )
    assert_too_large(r)


def test_limit_holds_without_content_length(client, rid):
    def chunks():  # a generator body is sent chunked, with no Content-Length header
        for _ in range(5):
            yield b" " * (MAX_REQUEST_BYTES // 4)

    r = client.post(f"/records/{rid}/sync", content=chunks(), headers={"Content-Type": "application/json"})
    assert_too_large(r)


def call_asgi(headers, chunks):
    """Drive the app directly so the Content-Length header can disagree with the real body."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/sync",
        "raw_path": b"/sync",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json"), *headers],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    incoming = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)
    ]
    sent = []

    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(body)


def test_limit_holds_when_content_length_understates_the_body(db_env):
    status, body = call_asgi([(b"content-length", b"10")], [b" " * MAX_REQUEST_BYTES, b" " * 10])
    assert status == 413 and body["reason"] == "PAYLOAD_TOO_LARGE"


def test_declared_content_length_over_limit_is_rejected_up_front(db_env):
    status, body = call_asgi([(b"content-length", str(MAX_REQUEST_BYTES + 1).encode())], [b"{}"])
    assert status == 413 and body["reason"] == "PAYLOAD_TOO_LARGE"


def test_get_routes_are_not_limited(client):
    assert client.get("/health").status_code == 200


# ---------------------------------------------------------------- normal traffic still fits


def test_largest_valid_single_request_fits(client, laptop, rid):
    make_record(laptop, rid)
    big_tag = "😀" * MAX_TAG_LEN
    r, _ = laptop.send(
        rid,
        "update",
        1,
        {
            "body": {"set": "😀" * MAX_BODY},
            "tags": {"add": [f"{i:02d}" + big_tag[2:] for i in range(20)]},
        },
    )
    assert r.status_code == 200


def test_full_batch_of_100_items_still_works(client, rid):
    changes = [
        sync_body(rid, i + 1, title={"set": f"v{i}"}, tags={"add": [f"t{i % 10}"]}) for i in range(100)
    ]
    changes[0].update(op="create", base_version=0)
    for i, item in enumerate(changes[1:], start=1):
        item["base_version"] = i
    r = client.post("/sync", json={"changes": changes})
    assert r.status_code == 200
    assert [res["status"] for res in r.json()["results"]] == [201] + [200] * 99
    assert client.get(f"/records/{rid}").json()["version"] == 100
