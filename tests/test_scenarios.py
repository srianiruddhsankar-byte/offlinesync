import threading
import uuid

from app.db import connect
from app.engine import apply_change


def make_record(laptop, rid, **extra):
    changes = {"title": {"set": "First"}, "status": {"set": "TODO"}}
    changes.update(extra)
    r, _ = laptop.send(rid, "create", 0, changes)
    assert r.status_code == 201
    return r.json()


def test_create_then_update_accepted(client, laptop, rid):
    make_record(laptop, rid)
    r, _ = laptop.send(rid, "update", 1, {"title": {"set": "Second"}})
    assert r.status_code == 200
    assert r.json()["outcome"] == "ACCEPTED"
    assert r.json()["version"] == 2
    assert client.get(f"/records/{rid}").json()["data"]["title"] == "Second"


def test_different_fields_merge(client, laptop, phone, rid):
    make_record(laptop, rid)
    r1, _ = laptop.send(rid, "update", 1, {"title": {"set": "Laptop title"}})
    r2, _ = phone.send(rid, "update", 1, {"status": {"set": "DONE"}})
    assert r1.json()["outcome"] == "ACCEPTED"
    assert r2.status_code == 200
    body = r2.json()
    assert body["outcome"] == "MERGED"
    assert body["version"] == 3
    assert body["merged_with"][0]["device_id"] == "laptop"
    data = client.get(f"/records/{rid}").json()["data"]
    assert data["title"] == "Laptop title" and data["status"] == "DONE"


def test_same_field_conflict_writes_nothing(client, laptop, phone, rid):
    make_record(laptop, rid)
    laptop.send(rid, "update", 1, {"status": {"set": "DONE"}})
    r, _ = phone.send(rid, "update", 1, {"status": {"set": "IN_PROGRESS"}})
    assert r.status_code == 409
    body = r.json()
    assert body["outcome"] == "CONFLICT" and body["reason"] == "FIELD_CONFLICT"
    c = body["conflicts"][0]
    assert (c["field"], c["base_value"], c["server_value"], c["client_value"]) == (
        "status",
        "TODO",
        "DONE",
        "IN_PROGRESS",
    )
    assert c["server_changed_by"] == "laptop"
    rec = client.get(f"/records/{rid}").json()
    assert rec["version"] == 2 and rec["data"]["status"] == "DONE"


def test_same_value_is_not_a_conflict(client, laptop, phone, rid):
    make_record(laptop, rid)
    laptop.send(rid, "update", 1, {"status": {"set": "DONE"}})
    r, _ = phone.send(rid, "update", 1, {"status": {"set": "DONE"}})
    assert r.status_code == 200
    assert r.json().get("noop") is True
    assert client.get(f"/records/{rid}").json()["version"] == 2


def test_same_device_queued_edits_do_not_self_conflict(client, laptop, rid):
    make_record(laptop, rid)
    r1, _ = laptop.send(rid, "update", 1, {"title": {"set": "A"}})
    r2, _ = laptop.send(rid, "update", 1, {"title": {"set": "B"}})  # still based on v1
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json()["outcome"] == "ACCEPTED"
    assert client.get(f"/records/{rid}").json()["data"]["title"] == "B"


def test_tags_merge_and_remove_not_resurrected(client, laptop, phone, rid):
    make_record(laptop, rid, tags={"add": ["a", "b"]})
    laptop.send(rid, "update", 1, {"tags": {"add": ["work"]}})
    r, _ = phone.send(rid, "update", 1, {"tags": {"add": ["urgent"], "remove": ["b"]}})
    assert r.status_code == 200
    tags = client.get(f"/records/{rid}").json()["data"]["tags"]
    assert tags == ["a", "urgent", "work"]


def test_counter_deltas_sum(client, laptop, phone, rid):
    make_record(laptop, rid)
    laptop.send(rid, "update", 1, {"views": {"increment": 3}})
    phone.send(rid, "update", 1, {"views": {"increment": 5}})
    assert client.get(f"/records/{rid}").json()["data"]["views"] == 8


def test_duplicate_request_replays_without_reapplying(client, laptop, rid):
    make_record(laptop, rid)
    r1, body = laptop.send(rid, "update", 1, {"views": {"increment": 3}})
    r2 = client.post(f"/records/{rid}/sync", json=body)
    assert r2.status_code == r1.status_code
    assert r2.json()["replayed"] is True
    rec = client.get(f"/records/{rid}").json()
    assert rec["version"] == 2 and rec["data"]["views"] == 3


def test_replayed_conflict_stays_conflict(client, laptop, phone, rid):
    make_record(laptop, rid)
    laptop.send(rid, "update", 1, {"status": {"set": "DONE"}})
    r1, body = phone.send(rid, "update", 1, {"status": {"set": "TODO"}})
    r1b = client.post(f"/records/{rid}/sync", json=body)
    assert r1.status_code == 409 and r1b.status_code == 409 and r1b.json()["replayed"] is True


def test_request_id_reuse_with_different_payload(client, laptop, rid):
    make_record(laptop, rid)
    r1, body = laptop.send(rid, "update", 1, {"title": {"set": "X"}})
    body["changes"] = {"title": {"set": "Y"}}
    r2 = client.post(f"/records/{rid}/sync", json=body)
    assert r2.status_code == 422 and r2.json()["reason"] == "REQUEST_ID_REUSED"


def test_out_of_order_then_success_after_gap_filled(client, laptop, rid):
    make_record(laptop, rid)
    r, body = laptop.send(rid, "update", 1, {"title": {"set": "third"}}, sequence=3)
    assert r.status_code == 409 and r.json()["reason"] == "OUT_OF_ORDER"
    assert r.json()["expected_sequence"] == 2
    laptop.send(rid, "update", 1, {"body": {"set": "second"}})  # sequence 2
    r2 = client.post(f"/records/{rid}/sync", json=body)  # same payload, sequence 3 now valid
    assert r2.status_code == 200


def test_stale_sequence_rejected(client, laptop, rid):
    make_record(laptop, rid)
    r, _ = laptop.send(rid, "update", 1, {"title": {"set": "x"}}, sequence=1)
    assert r.status_code == 409 and r.json()["reason"] == "STALE_SEQUENCE"


def test_invalid_requests_do_not_change_data(client, laptop, rid):
    make_record(laptop, rid)
    before = client.get(f"/records/{rid}").json()
    cases = [
        {"unknown": {"set": "x"}},
        {"title": {"increment": 1}},
        {"views": {"set": 5}},
        {"status": {"set": "NOPE"}},
        {"tags": {"add": ["a"], "remove": ["a"]}},
    ]
    for changes in cases:
        r, _ = laptop.send(rid, "update", 1, changes)
        assert r.status_code == 422, changes
    r, _ = laptop.send(rid, "update", 99, {"title": {"set": "x"}})
    assert r.status_code == 422 and r.json()["reason"] == "FUTURE_BASE_VERSION"
    r, _ = laptop.send(str(uuid.uuid4()), "update", 1, {"title": {"set": "x"}})
    assert r.status_code == 404
    assert client.get(f"/records/{rid}").json() == before


def test_create_existing_id_conflicts(client, laptop, phone, rid):
    make_record(laptop, rid)
    r, _ = phone.send(rid, "create", 0, {"title": {"set": "dup"}})
    assert r.status_code == 409 and r.json()["reason"] == "ALREADY_EXISTS"


def test_delete_vs_edit_conflicts_both_ways(client, laptop, phone, rid):
    make_record(laptop, rid)
    laptop.send(rid, "update", 1, {"title": {"set": "edited"}})
    r, _ = phone.send(rid, "delete", 1)
    assert r.status_code == 409 and r.json()["reason"] == "EDITED_SINCE_BASE"

    rid2 = str(uuid.uuid4())
    make_record(laptop, rid2)
    r, _ = laptop.send(rid2, "delete", 1)
    assert r.status_code == 200
    r, _ = phone.send(rid2, "update", 1, {"title": {"set": "late edit"}})
    assert r.status_code == 409 and r.json()["reason"] == "RECORD_DELETED"


def test_default_mode_is_all_or_nothing(client, laptop, phone, rid):
    make_record(laptop, rid)
    laptop.send(rid, "update", 1, {"status": {"set": "DONE"}})
    r, _ = phone.send(rid, "update", 1, {"title": {"set": "clean"}, "status": {"set": "IN_PROGRESS"}})
    assert r.status_code == 409
    assert client.get(f"/records/{rid}").json()["data"]["title"] == "First"
    assert phone.seq == 0  # conflict did not advance the sequence


def test_partial_mode_applies_clean_fields(client, laptop, phone, rid):
    make_record(laptop, rid)
    laptop.send(rid, "update", 1, {"status": {"set": "DONE"}})
    r, _ = phone.send(
        rid, "update", 1, {"title": {"set": "clean"}, "status": {"set": "IN_PROGRESS"}}, partial=True
    )
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "MERGED" and body["partial"] and body["needs_resolution"]
    assert body["applied_fields"] == ["title"]
    assert body["conflicts"][0]["field"] == "status"
    data = client.get(f"/records/{rid}").json()["data"]
    assert data["title"] == "clean" and data["status"] == "DONE"
    # follow-up resolution uses the next sequence and the latest version
    r2, _ = phone.send(rid, "update", body["version"], {"status": {"set": "IN_PROGRESS"}})
    assert r2.status_code == 200


def test_partial_with_no_clean_field_is_plain_conflict(client, laptop, phone, rid):
    make_record(laptop, rid)
    laptop.send(rid, "update", 1, {"status": {"set": "DONE"}})
    r, _ = phone.send(rid, "update", 1, {"status": {"set": "IN_PROGRESS"}}, partial=True)
    assert r.status_code == 409 and phone.seq == 0


def test_batch_endpoint_mixed_results(client, rid):
    def item(seq, op, base, changes, dev="d1"):
        return {
            "request_id": str(uuid.uuid4()),
            "device_id": dev,
            "sequence": seq,
            "record_id": rid,
            "op": op,
            "base_version": base,
            "changes": changes,
        }

    r = client.post(
        "/sync",
        json={
            "changes": [
                item(1, "create", 0, {"title": {"set": "b"}}),
                item(2, "update", 1, {"title": {"set": "c"}}),
                item(4, "update", 2, {"title": {"set": "gap"}}),
            ]
        },
    )
    assert r.status_code == 200
    results = r.json()["results"]
    assert [x["status"] for x in results] == [201, 200, 409]
    assert results[2]["reason"] == "OUT_OF_ORDER"


def test_malformed_envelope_is_422(client, rid):
    r = client.post(f"/records/{rid}/sync", json={"device_id": "x"})
    assert r.status_code == 422 and r.json()["reason"] == "VALIDATION_ERROR"


def test_concurrent_conflicting_requests_exactly_one_wins(db_env):
    rid = str(uuid.uuid4())

    def req(dev, seq, op, base, changes):
        return {
            "request_id": str(uuid.uuid4()),
            "device_id": dev,
            "sequence": seq,
            "record_id": rid,
            "op": op,
            "base_version": base,
            "partial": False,
            "changes": changes,
        }

    conn = connect()
    apply_change(conn, req("seed", 1, "create", 0, {"title": {"set": "t"}}))
    conn.close()

    barrier = threading.Barrier(2)
    results = {}

    def worker(dev, value):
        c = connect()
        barrier.wait()
        results[dev] = apply_change(c, req(dev, 1, "update", 1, {"status": {"set": value}}))[0]
        c.close()

    threads = [threading.Thread(target=worker, args=(d, v)) for d, v in (("a", "DONE"), ("b", "IN_PROGRESS"))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results.values()) == [200, 409]
