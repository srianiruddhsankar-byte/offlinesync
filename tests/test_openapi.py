"""Keeps the Swagger page honest: the docs exist, and every documented example really behaves as described."""

from app.docs import EXPECTED_ORDER, RID, SYNC_EXAMPLES


def test_docs_page_and_schema_load(client):
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    spec = spec.json()

    post = spec["paths"]["/records/{record_id}/sync"]["post"]
    examples = post["requestBody"]["content"]["application/json"]["examples"]
    assert set(examples) == set(SYNC_EXAMPLES)
    assert {"200", "201", "404", "409", "422"} <= set(post["responses"])
    assert spec["paths"]["/sync"]["post"]["requestBody"]["content"]["application/json"]["examples"]
    assert spec["paths"]["/records/{record_id}/restore/{version}"]["post"]["requestBody"]["content"][
        "application/json"
    ]["examples"]
    assert {"Sync", "Pull", "Records", "History", "System"} <= {t["name"] for t in spec["tags"]}


def test_every_sync_example_behaves_as_documented(client):
    for key, status, outcome in EXPECTED_ORDER:
        r = client.post(f"/records/{RID}/sync", json=SYNC_EXAMPLES[key]["value"])
        assert (r.status_code, r.json()["outcome"]) == (status, outcome), f"example {key}: {r.json()}"
    # running an example twice is an idempotent replay
    again = client.post(f"/records/{RID}/sync", json=SYNC_EXAMPLES["2_update"]["value"])
    assert again.status_code == 200 and again.json()["replayed"] is True


def test_batch_and_restore_examples_work(client):
    from app.docs import BATCH_EXAMPLES, RESTORE_EXAMPLES

    for key, _status, _outcome in EXPECTED_ORDER[:8]:
        client.post(f"/records/{RID}/sync", json=SYNC_EXAMPLES[key]["value"])
    r = client.post("/sync", json=BATCH_EXAMPLES["create_then_update"]["value"])
    assert [x["status"] for x in r.json()["results"]] == [201, 200]
    r = client.post(f"/records/{RID}/restore/6", json=RESTORE_EXAMPLES["undelete"]["value"])
    assert r.status_code == 200 and r.json()["version"] == 8
    assert client.get(f"/records/{RID}").json()["data"]["deleted"] is False
