"""Cursor paging for GET /records and GET /records/{id}/history."""

import uuid

import pytest

from app.db import connect
from app.merge import MAX_PAGE, SQLITE_INT_MAX


def create(client, device, seq, rid=None):
    rid = rid or str(uuid.uuid4())
    body = {
        "request_id": str(uuid.uuid4()),
        "device_id": device,
        "sequence": seq,
        "record_id": rid,
        "op": "create",
        "base_version": 0,
        "changes": {"title": {"set": f"r{seq}"}},
    }
    assert client.post(f"/records/{rid}/sync", json=body).status_code == 201
    return rid


def make_records(client, n):
    """n records, created through the batch endpoint 100 at a time."""
    rids = []
    for start in range(0, n, 100):
        changes = []
        for seq in range(start + 1, min(start + 100, n) + 1):
            rid = str(uuid.uuid4())
            rids.append(rid)
            changes.append(
                {
                    "request_id": str(uuid.uuid4()),
                    "device_id": "bulk",
                    "sequence": seq,
                    "record_id": rid,
                    "op": "create",
                    "base_version": 0,
                    "changes": {"title": {"set": f"r{seq}"}},
                }
            )
        results = client.post("/sync", json={"changes": changes}).json()["results"]
        assert all(r["status"] == 201 for r in results)
    return rids


def walk(client, url, limit, **params):
    """Follow next_cursor until has_more is false; return every page."""
    pages, cursor = [], None
    while True:
        query = {"limit": limit, **params}
        if cursor is not None:
            query["after"] = cursor
        page = client.get(url, params=query).json()
        pages.append(page)
        if not page["has_more"]:
            return pages
        cursor = page["next_cursor"]


def delete(laptop, rid):
    r, _ = laptop.send(rid, "delete", 1)
    assert r.status_code == 200


# ---------------------------------------------------------------- records


def test_unpaged_response_keeps_existing_keys(client, laptop, rid):
    laptop.send(rid, "create", 0, {"title": {"set": "x"}})
    body = client.get("/records").json()
    assert [r["id"] for r in body["records"]] == [rid]
    assert set(body["records"][0]) == {"id", "version", "data"}
    assert body["next_cursor"] == rid and body["has_more"] is False


def test_default_page_is_100(client):
    make_records(client, 101)
    body = client.get("/records").json()
    assert len(body["records"]) == 100 and body["has_more"] is True


@pytest.mark.parametrize("limit", [1, 7, 100, MAX_PAGE])
def test_paging_records_returns_each_exactly_once(client, limit):
    rids = make_records(client, 250)
    pages = walk(client, "/records", limit)
    seen = [r["id"] for p in pages for r in p["records"]]
    assert len(seen) == len(set(seen)) == 250
    assert set(seen) == set(rids)
    assert all(len(p["records"]) == limit for p in pages[:-1])
    unpaged = client.get("/records", params={"limit": MAX_PAGE}).json()["records"]
    assert seen == [r["id"] for r in unpaged]  # paging does not change the order


def test_order_is_created_at_then_id(client):
    make_records(client, 150)
    records = walk(client, "/records", MAX_PAGE)[0]["records"]
    conn = connect()
    try:
        created = dict(conn.execute("SELECT id, created_at FROM records").fetchall())
    finally:
        conn.close()
    keys = [(created[r["id"]], r["id"]) for r in records]
    assert keys == sorted(keys)


def test_paging_is_stable_when_timestamps_tie(client):
    rids = make_records(client, 40)
    conn = connect()
    try:
        conn.execute("UPDATE records SET created_at = '2026-01-01T00:00:00+00:00'")
    finally:
        conn.close()
    seen = [r["id"] for p in walk(client, "/records", 3) for r in p["records"]]
    assert seen == sorted(rids)  # ties are broken by id, so no record is skipped or repeated


def test_paging_honors_include_deleted(client, laptop):
    rids = [create(client, "laptop", seq) for seq in range(1, 31)]
    laptop.seq = 30
    deleted = set(rids[::3])  # includes the first record, so a cursor can name a deleted row
    for rid in deleted:
        delete(laptop, rid)

    live = [r["id"] for p in walk(client, "/records", 4) for r in p["records"]]
    assert live == [r for r in rids if r not in deleted]

    everything = walk(client, "/records", 4, include_deleted="true")
    seen = [r["id"] for p in everything for r in p["records"]]
    assert seen == rids
    assert {r["id"] for p in everything for r in p["records"] if r["data"]["deleted"]} == deleted


def test_cursor_naming_a_deleted_record_still_works(client, laptop):
    first, second = create(client, "laptop", 1), create(client, "laptop", 2)
    laptop.seq = 2
    delete(laptop, first)
    body = client.get("/records", params={"after": first}).json()
    assert [r["id"] for r in body["records"]] == [second]


def test_records_created_after_a_page_appear_on_later_pages(client):
    make_records(client, 5)
    page = client.get("/records", params={"limit": 5}).json()
    assert page["has_more"] is False
    late = create(client, "late", 1)
    after = client.get("/records", params={"after": page["next_cursor"]}).json()
    assert [r["id"] for r in after["records"]] == [late]


def test_empty_page_keeps_the_cursor(client):
    rid = create(client, "laptop", 1)
    body = client.get("/records", params={"after": rid}).json()
    assert body == {"records": [], "next_cursor": rid, "has_more": False}
    assert client.get("/records").json()["next_cursor"] == rid
    assert client.get("/records", params={"include_deleted": "true", "limit": 1}).json()["has_more"] is False


def test_cursor_accepts_any_uuid_spelling(client):
    first, second = create(client, "laptop", 1), create(client, "laptop", 2)
    body = client.get("/records", params={"after": first.upper()}).json()
    assert [r["id"] for r in body["records"]] == [second]


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": MAX_PAGE + 1}, {"after": "not-a-uuid"}, {"after": str(uuid.uuid4())}],
)
def test_records_paging_bounds_are_validation_errors(client, params):
    r = client.get("/records", params=params)
    assert r.status_code == 422
    body = r.json()
    assert body["outcome"] == "REJECTED" and body["reason"] == "VALIDATION_ERROR"
    assert body["errors"][0]["loc"][-1] == next(iter(params))


# ---------------------------------------------------------------- history


def make_versions(laptop, rid, n):
    laptop.send(rid, "create", 0, {"title": {"set": "v1"}})
    for v in range(1, n):
        r, _ = laptop.send(rid, "update", v, {"title": {"set": f"v{v + 1}"}})
        assert r.status_code == 200


def test_history_keeps_existing_keys(client, laptop, rid):
    make_versions(laptop, rid, 3)
    body = client.get(f"/records/{rid}/history").json()
    assert body["record_id"] == rid and [c["version"] for c in body["history"]] == [1, 2, 3]
    assert body["next_cursor"] == 3 and body["has_more"] is False


@pytest.mark.parametrize("limit", [1, 4, 25])
def test_paging_history_returns_each_version_exactly_once(client, laptop, rid, limit):
    make_versions(laptop, rid, 25)
    pages = walk(client, f"/records/{rid}/history", limit)
    versions = [c["version"] for p in pages for c in p["history"]]
    assert versions == list(range(1, 26))
    assert [p["next_cursor"] for p in pages] == [p["history"][-1]["version"] for p in pages]


def test_history_default_page_is_100(client, laptop, rid):
    make_versions(laptop, rid, 101)
    body = client.get(f"/records/{rid}/history").json()
    assert len(body["history"]) == 100 and body["has_more"] is True and body["next_cursor"] == 100
    rest = client.get(f"/records/{rid}/history", params={"after": 100}).json()
    assert [c["version"] for c in rest["history"]] == [101] and rest["has_more"] is False


def test_history_after_last_version_is_empty(client, laptop, rid):
    make_versions(laptop, rid, 2)
    body = client.get(f"/records/{rid}/history", params={"after": 2}).json()
    assert body == {"record_id": rid, "history": [], "next_cursor": 2, "has_more": False}


def test_history_of_unknown_record_is_still_404(client):
    assert client.get(f"/records/{uuid.uuid4()}/history", params={"after": 1}).status_code == 404


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": MAX_PAGE + 1}, {"after": -1}, {"after": SQLITE_INT_MAX + 1}]
)
def test_history_paging_bounds_are_validation_errors(client, laptop, rid, params):
    laptop.send(rid, "create", 0, {"title": {"set": "x"}})
    r = client.get(f"/records/{rid}/history", params=params)
    assert r.status_code == 422 and r.json()["reason"] == "VALIDATION_ERROR"


def test_demo_page_follows_the_history_cursor(client, laptop, rid):
    """Mirror of demo.html refresh(): page with limit=500 until has_more is false."""
    make_versions(laptop, rid, 3)
    page = client.get("/demo").text
    assert "history?after=${after}&limit=500" in page and "more=p.has_more" in page
    hist, after, more = [], 0, True
    while more:
        p = client.get(f"/records/{rid}/history?after={after}&limit=500").json()
        hist += p["history"]
        after, more = p["next_cursor"], p["has_more"]
    assert [c["version"] for c in hist] == [1, 2, 3]
