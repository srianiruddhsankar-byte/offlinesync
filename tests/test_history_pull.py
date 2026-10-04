import uuid


def make(laptop, rid):
    r, _ = laptop.send(rid, "create", 0, {"title": {"set": "v1"}, "tags": {"add": ["a"]}})
    assert r.status_code == 201


def restore(client, rid, version, base, device="laptop", request_id=None):
    return client.post(
        f"/records/{rid}/restore/{version}",
        json={"request_id": request_id or str(uuid.uuid4()), "device_id": device, "base_version": base},
    )


def test_history_lists_every_applied_change_in_order(client, laptop, phone, rid):
    make(laptop, rid)
    laptop.send(rid, "update", 1, {"title": {"set": "v2"}})
    phone.send(rid, "update", 1, {"status": {"set": "DONE"}})
    h = client.get(f"/records/{rid}/history").json()["history"]
    assert [c["version"] for c in h] == [1, 2, 3]
    assert [c["device_id"] for c in h] == ["laptop", "laptop", "phone"]
    assert [c["op"] for c in h] == ["create", "update", "update"]
    assert h[2]["fields"] == {"status": "DONE"}
    assert client.get(f"/records/{uuid.uuid4()}/history").status_code == 404


def test_version_view_reconstructs_old_state(client, laptop, rid):
    make(laptop, rid)
    laptop.send(rid, "update", 1, {"title": {"set": "v2"}, "views": {"increment": 4}})
    v1 = client.get(f"/records/{rid}/versions/1").json()["data"]
    v2 = client.get(f"/records/{rid}/versions/2").json()["data"]
    assert v1["title"] == "v1" and v1["views"] == 0
    assert v2["title"] == "v2" and v2["views"] == 4
    assert client.get(f"/records/{rid}/versions/9").status_code == 404
    assert client.get(f"/records/{rid}/versions/0").status_code == 404


def test_restore_creates_new_version_and_keeps_history(client, laptop, rid):
    make(laptop, rid)
    laptop.send(rid, "update", 1, {"title": {"set": "v2"}, "tags": {"add": ["b"]}, "views": {"increment": 2}})
    r = restore(client, rid, 1, base=2)
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 3 and body["restored_from"] == 1
    data = client.get(f"/records/{rid}").json()["data"]
    assert data["title"] == "v1" and data["tags"] == ["a"] and data["views"] == 0
    h = client.get(f"/records/{rid}/history").json()["history"]
    assert [c["version"] for c in h] == [1, 2, 3] and h[2]["op"] == "restore"
    assert (
        client.get(f"/records/{rid}/versions/2").json()["data"]["title"] == "v2"
    )  # old version still viewable


def test_restore_stale_base_unknown_version_and_replay(client, laptop, rid):
    make(laptop, rid)
    laptop.send(rid, "update", 1, {"title": {"set": "v2"}})
    stale = restore(client, rid, 1, base=1)
    assert stale.status_code == 409 and stale.json()["reason"] == "STALE_BASE"
    assert restore(client, rid, 7, base=2).status_code == 404
    assert restore(client, str(uuid.uuid4()), 1, base=1).status_code == 404

    rid_req = str(uuid.uuid4())
    first = restore(client, rid, 1, base=2, request_id=rid_req)
    again = restore(client, rid, 1, base=2, request_id=rid_req)
    assert first.status_code == 200 and again.json()["replayed"] is True
    assert client.get(f"/records/{rid}").json()["version"] == 3  # applied once


def test_restore_noop_when_content_already_matches(client, laptop, rid):
    make(laptop, rid)
    r = restore(client, rid, 1, base=1)
    assert r.status_code == 200 and r.json()["noop"] is True
    assert client.get(f"/records/{rid}").json()["version"] == 1


def test_restore_can_undelete_and_does_not_consume_sequence(client, laptop, rid):
    make(laptop, rid)
    laptop.send(rid, "delete", 1)
    assert client.get(f"/records/{rid}").json()["data"]["deleted"] is True
    r = restore(client, rid, 1, base=2)
    assert r.status_code == 200
    assert client.get(f"/records/{rid}").json()["data"]["deleted"] is False
    r2, _ = laptop.send(rid, "update", 3, {"title": {"set": "back"}})  # laptop continues with next sequence
    assert r2.status_code == 200


def test_restore_then_stale_edit_still_conflicts(client, laptop, phone, rid):
    make(laptop, rid)
    laptop.send(rid, "update", 1, {"title": {"set": "v2"}})
    restore(client, rid, 1, base=2, device="laptop")  # title goes back to v1 in version 3
    r, _ = phone.send(rid, "update", 1, {"title": {"set": "phone"}})
    assert r.status_code == 409  # restore counts as a newer change to title


def test_pull_orders_pages_and_filters(client, laptop, phone, rid):
    make(laptop, rid)
    laptop.send(rid, "update", 1, {"title": {"set": "v2"}})
    phone.send(rid, "update", 1, {"status": {"set": "DONE"}})

    full = client.get("/sync/changes").json()
    ids = [c["change_id"] for c in full["changes"]]
    assert ids == sorted(ids) and len(ids) == 3 and full["has_more"] is False
    assert full["next_cursor"] == ids[-1]

    page = client.get("/sync/changes?limit=2").json()
    assert len(page["changes"]) == 2 and page["has_more"] is True
    rest = client.get(f"/sync/changes?since={page['next_cursor']}&limit=2").json()
    assert len(rest["changes"]) == 1 and rest["has_more"] is False

    others = client.get("/sync/changes?exclude_device=laptop").json()["changes"]
    assert [c["device_id"] for c in others] == ["phone"]

    empty = client.get(f"/sync/changes?since={ids[-1]}").json()
    assert empty["changes"] == [] and empty["next_cursor"] == ids[-1]


def test_replaying_pulled_changes_reproduces_server_state(client, laptop, phone, rid):
    make(laptop, rid)
    laptop.send(rid, "update", 1, {"tags": {"add": ["x"]}, "views": {"increment": 3}})
    phone.send(rid, "update", 1, {"title": {"set": "from phone"}})
    restore(client, rid, 2, base=3)
    replica = {}
    for c in client.get("/sync/changes").json()["changes"]:
        replica.update(c["fields"])
    assert replica == client.get(f"/records/{rid}").json()["data"]


def test_pull_validation(client):
    assert client.get("/sync/changes?limit=0").status_code == 422
    assert client.get("/sync/changes?since=-1").status_code == 422
