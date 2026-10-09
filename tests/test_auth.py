"""Optional shared API key: when SYNC_API_KEY is set, every POST route needs a matching X-API-Key."""

import uuid

import pytest
from fastapi.routing import APIRoute

from app.db import connect
from app.main import app, require_api_key

KEY = "s3cret-key"


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv("SYNC_API_KEY", KEY)


def sync_body(rid, seq=1, op="create", base=0):
    return {
        "request_id": str(uuid.uuid4()),
        "device_id": "laptop",
        "sequence": seq,
        "record_id": rid,
        "op": op,
        "base_version": base,
        "changes": {"title": {"set": "x"}},
    }


def restore_body(base=1):
    return {"request_id": str(uuid.uuid4()), "device_id": "viewer", "base_version": base}


def counts():
    conn = connect()
    try:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("records", "change_log", "requests", "devices")
        }
    finally:
        conn.close()


def assert_unauthorized(r):
    assert r.status_code == 401
    body = r.json()
    assert body["outcome"] == "REJECTED" and body["reason"] == "UNAUTHORIZED"


def post_routes(rid):
    """Every write: (path, json body), so a request can be sent to each."""
    return [
        (f"/records/{rid}/sync", sync_body(rid)),
        ("/sync", {"changes": [sync_body(rid)]}),
        (f"/records/{rid}/restore/1", restore_body()),
    ]


# ---------------------------------------------------------------- key unset


def test_without_key_configured_writes_need_no_header(client, rid):
    assert client.post(f"/records/{rid}/sync", json=sync_body(rid)).status_code == 201


def test_empty_key_counts_as_unset(client, monkeypatch, rid):
    monkeypatch.setenv("SYNC_API_KEY", "")
    assert client.post(f"/records/{rid}/sync", json=sync_body(rid)).status_code == 201


def test_without_key_configured_any_header_is_ignored(client, rid):
    r = client.post(f"/records/{rid}/sync", json=sync_body(rid), headers={"X-API-Key": "whatever"})
    assert r.status_code == 201


# ---------------------------------------------------------------- key set


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": ""}, {"X-API-Key": KEY + "x"}])
def test_missing_or_wrong_key_is_401_and_writes_nothing(client, with_key, rid, headers):
    before = counts()
    for path, body in post_routes(rid):
        assert_unauthorized(client.post(path, json=body, headers=headers))
    assert counts() == before


def test_key_header_is_case_sensitive_in_value(client, with_key, rid):
    r = client.post(f"/records/{rid}/sync", json=sync_body(rid), headers={"X-API-Key": KEY.upper()})
    assert_unauthorized(r)


def test_correct_key_allows_every_write(client, with_key, rid):
    h = {"x-api-key": KEY}  # header names are case-insensitive
    assert client.post(f"/records/{rid}/sync", json=sync_body(rid), headers=h).status_code == 201
    update = {**sync_body(rid, 2, "update", 1), "changes": {"title": {"set": "y"}}}
    r = client.post("/sync", json={"changes": [update]}, headers=h)
    assert r.status_code == 200 and r.json()["results"][0]["status"] == 200
    assert client.post(f"/records/{rid}/restore/1", json=restore_body(2), headers=h).status_code == 200


def test_replay_also_needs_the_key(client, with_key, rid):
    body = sync_body(rid)
    assert client.post(f"/records/{rid}/sync", json=body, headers={"X-API-Key": KEY}).status_code == 201
    assert_unauthorized(client.post(f"/records/{rid}/sync", json=body))


def test_reads_stay_open(client, with_key, rid):
    assert (
        client.post(f"/records/{rid}/sync", json=sync_body(rid), headers={"X-API-Key": KEY}).status_code
        == 201
    )
    for path in [
        "/health",
        "/records",
        f"/records/{rid}",
        f"/records/{rid}/history",
        f"/records/{rid}/versions/1",
        "/sync/changes",
        "/demo",
        "/openapi.json",
    ]:
        assert client.get(path).status_code == 200, path


def test_auth_is_checked_before_body_validation(client, with_key, rid):
    """An unauthenticated client gets 401, not details about its payload."""
    assert_unauthorized(client.post(f"/records/{rid}/sync", json={"bogus": True}))
    r = client.post(
        f"/records/{rid}/sync", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert_unauthorized(r)
    assert_unauthorized(client.post("/records/not-a-uuid/sync", json={}))


def test_validation_errors_still_reported_with_the_right_key(client, with_key, rid):
    r = client.post(f"/records/{rid}/sync", json={"bogus": True}, headers={"X-API-Key": KEY})
    assert r.status_code == 422 and r.json()["reason"] == "VALIDATION_ERROR"


def test_non_ascii_key(client, monkeypatch, rid):
    monkeypatch.setenv("SYNC_API_KEY", "clé-✓")
    raw = "clé-✓".encode()
    assert (
        client.post(f"/records/{rid}/sync", json=sync_body(rid), headers={"X-API-Key": raw}).status_code
        == 201
    )
    assert_unauthorized(
        client.post(f"/records/{rid}/sync", json=sync_body(rid), headers={"X-API-Key": b"cle"})
    )


def test_key_is_read_per_request(client, monkeypatch, rid):
    monkeypatch.setenv("SYNC_API_KEY", KEY)
    assert_unauthorized(client.post(f"/records/{rid}/sync", json=sync_body(rid)))
    monkeypatch.delenv("SYNC_API_KEY")
    assert client.post(f"/records/{rid}/sync", json=sync_body(rid)).status_code == 201


# ---------------------------------------------------------------- coverage and docs


def test_every_post_route_is_guarded():
    """A new write route cannot be added without the key check."""
    posts = [r for r in app.routes if isinstance(r, APIRoute) and "POST" in r.methods]
    assert {r.path for r in posts} == {
        "/records/{record_id}/sync",
        "/sync",
        "/records/{record_id}/restore/{version}",
    }
    for route in posts:
        assert any(d.call is require_api_key for d in route.dependant.dependencies), route.path


def test_get_routes_are_not_guarded():
    gets = [r for r in app.routes if isinstance(r, APIRoute) and "GET" in r.methods]
    assert gets
    for route in gets:
        assert not any(d.call is require_api_key for d in route.dependant.dependencies), route.path


def test_openapi_documents_the_key(client):
    spec = client.get("/openapi.json").json()
    schemes = spec["components"]["securitySchemes"]
    assert schemes["APIKeyHeader"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
        "description": "Required on POST routes only when the server sets SYNC_API_KEY.",
    }
    assert spec["paths"]["/sync"]["post"]["security"] == [{"APIKeyHeader": []}]
    assert "security" not in spec["paths"]["/records"]["get"]
    assert "SYNC_API_KEY" in spec["info"]["description"]
