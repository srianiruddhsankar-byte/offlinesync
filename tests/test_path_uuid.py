"""The {record_id} path parameter is normalized like the body's record_id: any valid UUID spelling
reaches the same record, and a value that is not a UUID is a 422, not a 404."""

import uuid

import pytest


def make_record(laptop, rid):
    r, _ = laptop.send(rid, "create", 0, {"title": {"set": "First"}})
    assert r.status_code == 201


def restore_body(base_version):
    return {"request_id": str(uuid.uuid4()), "device_id": "viewer", "base_version": base_version}


def test_uppercase_path_reads_match_lowercase(client, laptop, rid):
    make_record(laptop, rid)
    laptop.send(rid, "update", 1, {"title": {"set": "Second"}})
    upper = rid.upper()
    for path in ("", "/history", "/versions/1"):
        lower_r = client.get(f"/records/{rid}{path}")
        upper_r = client.get(f"/records/{upper}{path}")
        assert lower_r.status_code == upper_r.status_code == 200
        assert upper_r.json() == lower_r.json()


def test_uppercase_path_sync_create_and_update(client, laptop, rid):
    upper = rid.upper()
    body = {
        "request_id": str(uuid.uuid4()),
        "device_id": "laptop",
        "sequence": 1,
        "record_id": upper,
        "op": "create",
        "base_version": 0,
        "changes": {"title": {"set": "x"}},
    }
    r = client.post(f"/records/{upper}/sync", json=body)
    assert r.status_code == 201 and r.json()["record_id"] == rid

    body = {**body, "request_id": str(uuid.uuid4()), "sequence": 2, "op": "update", "base_version": 1}
    r = client.post(f"/records/{upper}/sync", json={**body, "changes": {"title": {"set": "y"}}})
    assert r.status_code == 200 and r.json()["version"] == 2
    assert client.get(f"/records/{rid}").json()["data"]["title"] == "y"


def test_uppercase_path_restore(client, laptop, rid):
    make_record(laptop, rid)
    laptop.send(rid, "update", 1, {"title": {"set": "Second"}})
    r = client.post(f"/records/{rid.upper()}/restore/1", json=restore_body(2))
    assert r.status_code == 200 and r.json()["record_id"] == rid
    assert client.get(f"/records/{rid}").json()["data"]["title"] == "First"


def test_unknown_uppercase_uuid_is_still_404(client):
    unknown = str(uuid.uuid4()).upper()
    assert client.get(f"/records/{unknown}").status_code == 404
    assert client.get(f"/records/{unknown}/history").status_code == 404
    assert client.get(f"/records/{unknown}/versions/1").status_code == 404
    assert client.post(f"/records/{unknown}/restore/1", json=restore_body(1)).status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", ""),
        ("get", "/history"),
        ("get", "/versions/1"),
        ("post", "/sync"),
        ("post", "/restore/1"),
    ],
)
def test_malformed_path_record_id_is_a_validation_error(client, method, path):
    kwargs = {"json": restore_body(1)} if method == "post" else {}
    r = getattr(client, method)(f"/records/not-a-uuid{path}", **kwargs)
    assert r.status_code == 422
    body = r.json()
    assert body["outcome"] == "REJECTED" and body["reason"] == "VALIDATION_ERROR"
    assert any(e["loc"] == ["path", "record_id"] for e in body["errors"])
