"""OpenAPI examples. Everything here only feeds the Swagger page (/docs); no logic.

SYNC_EXAMPLES are written to be run top to bottom on a fresh database. tests/test_openapi.py
actually executes them in order and checks the documented outcomes, so the docs cannot rot.
"""

from typing import Any

RID = "11111111-1111-1111-1111-111111111111"


def _req(
    n: int,
    device: str,
    seq: int,
    op: str,
    base: int,
    changes: dict[str, Any] | None = None,
    partial: bool = False,
) -> dict[str, Any]:
    return {
        "request_id": f"aaaaaaaa-0000-0000-0000-{n:012d}",
        "device_id": device,
        "sequence": seq,
        "record_id": RID,
        "op": op,
        "base_version": base,
        "partial": partial,
        "changes": changes or {},
    }


SYNC_EXAMPLES: dict[str, Any] = {
    "1_create": {
        "summary": "1 · Laptop creates the record",
        "description": "Expect **201 ACCEPTED**, version 1.",
        "value": _req(1, "laptop", 1, "create", 0, {"title": {"set": "Demo"}, "status": {"set": "TODO"}}),
    },
    "2_update": {
        "summary": "2 · Laptop sets status to DONE",
        "description": "Expect **200 ACCEPTED**, version 2.",
        "value": _req(2, "laptop", 2, "update", 1, {"status": {"set": "DONE"}}),
    },
    "3_conflict": {
        "summary": "3 · Phone (still on v1) sets the same field -> CONFLICT",
        "description": "The phone has not seen version 2. Expect **409 CONFLICT** listing base, server and client values. Nothing is written.",
        "value": _req(3, "phone", 1, "update", 1, {"status": {"set": "IN_PROGRESS"}}),
    },
    "4_merge": {
        "summary": "4 · Phone edits a different field -> MERGED",
        "description": "Same stale base, different field. Expect **200 MERGED**, version 3. It reuses sequence 1 because the conflict in step 3 did not consume it.",
        "value": _req(4, "phone", 1, "update", 1, {"title": {"set": "Phone title"}}),
    },
    "5_tags_counter": {
        "summary": "5 · Laptop changes tags, a counter and status",
        "description": "Tags and counters are sent as operations, so they merge instead of conflicting. Expect **200 ACCEPTED**, version 4.",
        "value": _req(
            5,
            "laptop",
            3,
            "update",
            3,
            {"tags": {"add": ["work"]}, "views": {"increment": 3}, "status": {"set": "TODO"}},
        ),
    },
    "6_partial": {
        "summary": "6 · Phone, partial mode: one clean field, one conflicting field",
        "description": "`body` is clean, `status` conflicts with step 5. With `partial: true` the clean field is applied. Expect **200 MERGED**, version 5, `needs_resolution: true`.",
        "value": _req(
            6, "phone", 2, "update", 3, {"body": {"set": "notes"}, "status": {"set": "DONE"}}, partial=True
        ),
    },
    "7_resolve": {
        "summary": "7 · Phone resolves the leftover conflict",
        "description": "Resubmit the dropped field at the latest version with the next sequence. Expect **200 ACCEPTED**, version 6.",
        "value": _req(7, "phone", 3, "update", 5, {"status": {"set": "IN_PROGRESS"}}),
    },
    "8_delete": {
        "summary": "8 · Laptop deletes the record",
        "description": "Soft delete (tombstone). Expect **200 ACCEPTED**, version 7.",
        "value": _req(8, "laptop", 4, "delete", 6),
    },
    "9_out_of_order": {
        "summary": "9 · Out-of-order operation (sequence gap)",
        "description": "The phone skips ahead to sequence 99. Expect **409 REJECTED / OUT_OF_ORDER** with `expected_sequence`. Works at any point.",
        "value": _req(9, "phone", 99, "update", 1, {"title": {"set": "too early"}}),
    },
}

# (example key, expected HTTP status, expected outcome) in the order they must run
EXPECTED_ORDER = [
    ("1_create", 201, "ACCEPTED"),
    ("2_update", 200, "ACCEPTED"),
    ("3_conflict", 409, "CONFLICT"),
    ("4_merge", 200, "MERGED"),
    ("5_tags_counter", 200, "ACCEPTED"),
    ("6_partial", 200, "MERGED"),
    ("7_resolve", 200, "ACCEPTED"),
    ("8_delete", 200, "ACCEPTED"),
    ("9_out_of_order", 409, "REJECTED"),
]

_RECORD: dict[str, Any] = {
    "id": RID,
    "version": 2,
    "data": {"title": "Demo", "body": "", "status": "DONE", "tags": [], "views": 0, "deleted": False},
}

_CONFLICT_ITEM: dict[str, Any] = {
    "field": "status",
    "base_value": "TODO",
    "server_value": "DONE",
    "client_value": "IN_PROGRESS",
    "server_changed_in_version": 2,
    "server_changed_by": "laptop",
}


def _ex(summary: str, value: dict[str, Any]) -> dict[str, Any]:
    return {"summary": summary, "value": value}


SYNC_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "Applied (**ACCEPTED** or **MERGED**), a no-op, or a replay of an earlier request.",
        "content": {
            "application/json": {
                "examples": {
                    "accepted": _ex(
                        "ACCEPTED",
                        {
                            "outcome": "ACCEPTED",
                            "request_id": "aaaaaaaa-0000-0000-0000-000000000002",
                            "record_id": RID,
                            "version": 2,
                            "applied_fields": ["status"],
                            "merged_with": [],
                            "record": _RECORD,
                            "replayed": False,
                        },
                    ),
                    "merged": _ex(
                        "MERGED (different field than a newer change)",
                        {
                            "outcome": "MERGED",
                            "request_id": "aaaaaaaa-0000-0000-0000-000000000004",
                            "record_id": RID,
                            "version": 3,
                            "applied_fields": ["title"],
                            "merged_with": [{"version": 2, "device_id": "laptop", "fields": ["status"]}],
                            "record": {**_RECORD, "version": 3},
                            "replayed": False,
                        },
                    ),
                    "partial": _ex(
                        "MERGED, partial mode with a leftover conflict",
                        {
                            "outcome": "MERGED",
                            "partial": True,
                            "needs_resolution": True,
                            "record_id": RID,
                            "version": 5,
                            "applied_fields": ["body"],
                            "merged_with": [
                                {"version": 4, "device_id": "laptop", "fields": ["status", "tags", "views"]}
                            ],
                            "conflicts": [_CONFLICT_ITEM],
                            "record": {**_RECORD, "version": 5},
                            "replayed": False,
                        },
                    ),
                    "replay": _ex(
                        "Same request_id sent again",
                        {
                            "outcome": "ACCEPTED",
                            "record_id": RID,
                            "version": 2,
                            "applied_fields": ["status"],
                            "merged_with": [],
                            "record": _RECORD,
                            "replayed": True,
                        },
                    ),
                }
            }
        },
    },
    201: {
        "description": "Record created.",
        "content": {
            "application/json": {
                "examples": {
                    "created": _ex(
                        "ACCEPTED (create)",
                        {
                            "outcome": "ACCEPTED",
                            "record_id": RID,
                            "version": 1,
                            "applied_fields": ["status", "title"],
                            "record": {
                                **_RECORD,
                                "version": 1,
                                "data": {**_RECORD["data"], "status": "TODO"},
                            },
                            "replayed": False,
                        },
                    ),
                }
            }
        },
    },
    404: {
        "description": "Unknown record (update or delete for an id that does not exist).",
        "content": {
            "application/json": {
                "examples": {
                    "unknown": _ex(
                        "UNKNOWN_RECORD",
                        {
                            "outcome": "REJECTED",
                            "reason": "UNKNOWN_RECORD",
                            "record_id": RID,
                            "replayed": False,
                        },
                    ),
                }
            }
        },
    },
    409: {
        "description": "**CONFLICT** (needs resolution, nothing written) or a rejected out-of-order / stale sequence.",
        "content": {
            "application/json": {
                "examples": {
                    "field_conflict": _ex(
                        "CONFLICT / FIELD_CONFLICT",
                        {
                            "outcome": "CONFLICT",
                            "reason": "FIELD_CONFLICT",
                            "record_id": RID,
                            "current_version": 2,
                            "conflicts": [_CONFLICT_ITEM],
                            "record": _RECORD,
                            "replayed": False,
                        },
                    ),
                    "out_of_order": _ex(
                        "REJECTED / OUT_OF_ORDER",
                        {
                            "outcome": "REJECTED",
                            "reason": "OUT_OF_ORDER",
                            "expected_sequence": 3,
                            "record_id": RID,
                            "replayed": False,
                        },
                    ),
                }
            }
        },
    },
    422: {
        "description": "Invalid request: bad field, wrong op type, future base_version, or request_id reused with a different payload.",
        "content": {
            "application/json": {
                "examples": {
                    "validation": _ex(
                        "VALIDATION_ERROR",
                        {
                            "outcome": "REJECTED",
                            "reason": "VALIDATION_ERROR",
                            "detail": "'status' must be one of ['DONE', 'IN_PROGRESS', 'TODO']",
                        },
                    ),
                    "future_base": _ex(
                        "FUTURE_BASE_VERSION",
                        {
                            "outcome": "REJECTED",
                            "reason": "FUTURE_BASE_VERSION",
                            "current_version": 2,
                            "record_id": RID,
                            "replayed": False,
                        },
                    ),
                }
            }
        },
    },
}

BATCH_RID = "22222222-2222-2222-2222-222222222222"

BATCH_EXAMPLES: dict[str, Any] = {
    "create_then_update": {
        "summary": "A tablet creates a record and edits it, offline",
        "description": "Items are applied in order, each with its own result. Expect statuses 201 then 200.",
        "value": {
            "changes": [
                {
                    "request_id": "cccccccc-0000-0000-0000-000000000001",
                    "device_id": "tablet",
                    "sequence": 1,
                    "record_id": BATCH_RID,
                    "op": "create",
                    "base_version": 0,
                    "changes": {"title": {"set": "Batch demo"}},
                },
                {
                    "request_id": "cccccccc-0000-0000-0000-000000000002",
                    "device_id": "tablet",
                    "sequence": 2,
                    "record_id": BATCH_RID,
                    "op": "update",
                    "base_version": 1,
                    "changes": {"views": {"increment": 2}},
                },
            ]
        },
    },
}

RESTORE_EXAMPLES: dict[str, Any] = {
    "undelete": {
        "summary": "Restore (use version 6 in the URL after running sync examples 1-8)",
        "description": "Writes version 6's content as a **new** version (8) and un-deletes the record. History is never rewritten.",
        "value": {
            "request_id": "bbbbbbbb-0000-0000-0000-000000000001",
            "device_id": "laptop",
            "base_version": 7,
        },
    },
}

APP_DESCRIPTION = """
Synchronizes changes from multiple devices and **detects conflicts instead of silently overwriting data**.

### Try it in 4 steps
1. Open **POST /records/{record_id}/sync** and click **Try it out**. (The fields stay locked until you do.)
2. The `record_id` box is pre-filled. Keep it as is.
3. Above the body box, open the **Examples** dropdown and pick **1 · Laptop creates the record**, then press **Execute**.
4. Repeat with examples **2, 3, 4, ...** in order and read each response. Run any example twice to see an idempotent replay.

To start over, stop the server, delete `sync.db`, and start it again.

### Authentication (optional)
If the server sets `SYNC_API_KEY`, every POST route needs the header `X-API-Key` with that value;
otherwise it answers **401** `UNAUTHORIZED` and writes nothing. Click **Authorize** above to send it.
Reads stay open. When `SYNC_API_KEY` is unset, as on the public demo, no key is needed.

### Outcomes
| Outcome | Meaning | HTTP |
|---|---|---|
| `ACCEPTED` | Applied cleanly | 200 / 201 |
| `MERGED` | Applied on top of newer changes to other fields | 200 |
| `CONFLICT` | Nothing written; the client must resolve | 409 |
| `REJECTED` | Invalid, unknown, out-of-order or unauthorized | 401 / 404 / 409 / 422 |
"""

TAGS_METADATA = [
    {"name": "Sync", "description": "Submit changes from a device. This is the core of the system."},
    {"name": "Pull", "description": "Fetch what changed on the server since a device last synced."},
    {"name": "Records", "description": "Read current state."},
    {"name": "History", "description": "Change history, past versions and restore."},
    {"name": "System", "description": "Health check."},
]
