# Offline Sync Conflict API

A backend that synchronizes changes from multiple devices and **detects conflicts instead of silently overwriting data**.

Several devices can edit the same record while offline and sync later. Every change says which version of the record it was based on, so the server can tell exactly what each device had seen. Changes that don't collide are merged, changes that do are refused with enough information for the client to resolve them, and retries, duplicates, out-of-order delivery and concurrent requests can't corrupt the data.

- **Stack:** Python 3.11, FastAPI, SQLite (WAL), pytest
- **Live demo:** _add the deployed URL here_
- **Design spec:** [design.md](design.md)

## Contents

1. [Quick start](#quick-start)
2. [How synchronization works](#how-synchronization-works)
3. [How changes are tracked](#how-changes-are-tracked)
4. [How conflicts are detected](#how-conflicts-are-detected)
5. [Conflict-resolution strategy](#conflict-resolution-strategy)
6. [Conflicting and non-conflicting scenarios](#conflicting-and-non-conflicting-scenarios)
7. [API reference and examples](#api-reference-and-examples)
8. [Edge cases handled](#edge-cases-handled)
9. [Testing](#testing)
10. [Design decisions](#design-decisions)
11. [Assumptions and known limits](#assumptions-and-known-limits)
12. [Project layout](#project-layout)

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

pytest                             # 49 tests
ruff check . && mypy               # lint and type check
python demo.py                     # a narrated walkthrough, no server needed
python -m tests.test_chaos         # the large randomized stress run
uvicorn app.main:app --reload      # API at http://localhost:8000
```

Open **http://localhost:8000/demo** for a visual demo: two devices (Laptop and Phone) edit the same record offline, and you can watch changes merge, conflicts appear with a resolve card, and retries replay safely. The Swagger UI is at `/docs`.

`/docs` has a dropdown of ready-made requests (numbered 1 to 9) for the sync endpoint. Run them in order against a fresh database to see every outcome. To start over, stop the server and delete `sync.db`.

## How synchronization works

```
  Laptop ──┐                              ┌────────────────────────┐
           ├── POST /records/{id}/sync ─▶ │      Sync engine       │
  Phone  ──┘   base_version, sequence,    │  (one DB transaction)  │
               request_id, changes        └───────────┬────────────┘
                                                      │ writes
                         ┌────────────────────────────┼───────────────────────────┐
                         ▼                            ▼                           ▼
                 records                        change_log                    requests
         current state + field_meta     every applied change (history     original answer per
                                        and the feed for pull)            request_id (idempotency)

  Laptop ◀── GET /sync/changes?since=N ── everything that happened after change N
```

- A device **pushes** changes, each tagged with the record version it was based on.
- The server **applies** a change only if it can do so without losing anyone's data, and says exactly what happened (`ACCEPTED`, `MERGED`, `CONFLICT` or `REJECTED`).
- A device **pulls** with a cursor to learn what other devices did.
- The server never guesses on the client's behalf: conflicts are returned, not auto-resolved.

## How changes are tracked

| Concept | Purpose |
|---|---|
| `device_id` | Which device made the change. Self-declared, no authentication. |
| `base_version` | The record version the device last saw when it made the edit. This is how the server knows what the device was looking at. |
| `version` | Server-assigned, +1 for every applied change to a record. |
| `sequence` | Per-device operation counter (1, 2, 3, ...). `base_version` says what a device *saw*; `sequence` says the *order of the device's own operations*. |
| `request_id` | Client-chosen UUID. Makes retries safe. |
| `field_meta` | For each field of a record, the version and device that last wrote it. Used for fast conflict checks. |
| `change_log` | Every applied change with the values it produced. The source for history, version viewing, restore and pull. |

Record IDs are client-generated UUIDs, so a device can create a record while completely offline.

**Record fields** (fixed schema, anything else is a 422):

| Field | Type | Merge behaviour | How it is submitted |
|---|---|---|---|
| `title`, `body` | string | scalar | `{"set": "text"}` |
| `status` | `TODO` / `IN_PROGRESS` / `DONE` | scalar | `{"set": "DONE"}` |
| `tags` | set of strings | set | `{"add": [...], "remove": [...]}` |
| `views` | integer | counter | `{"increment": 3}` |

Sets and counters are submitted as **operations, never final values**. That is what lets two offline devices both add tags or both increment a counter without anyone losing data.

## How conflicts are detected

For an `update`, each field in the change is checked independently.

A **scalar** field conflicts only if **all three** are true:

1. it was last written **after** the device's `base_version`, and
2. it was last written by a **different device**, and
3. the current value **differs** from the value the device wants to set.

Everything else is safe:

- **Same device:** a device never conflicts with its own earlier edits (this is what lets a device send several queued edits to one field).
- **Same value:** if both sides arrived at the same value there is nothing to resolve.
- **Sets and counters never conflict.** They are applied to the current value: `(current ∪ add) − remove` and `current + increment`. A tag removed by one device is not brought back by another device's stale add.
- **Delete vs. edit:** a `delete` conflicts if another device changed any field after the delete's `base_version`, and an `update` of a deleted record conflicts (`RECORD_DELETED`).

Outcome labels:

- `ACCEPTED`: the change applied and no other device changed the record since its `base_version`.
- `MERGED`: it applied on top of newer changes made by other devices to different fields.
- `CONFLICT`: at least one scalar field collided. Nothing is written.
- `REJECTED`: the request itself was invalid, unknown, or out of order. Nothing is written.

## Conflict-resolution strategy

**The server is stateless about conflicts.** A conflict response contains, for every colliding field, the value at the device's base version, the server's current value, the value the device tried to set, and who changed it. The client decides per field (keep the server's, keep its own, or something new) and submits an ordinary update with `base_version` set to the `current_version` from the conflict response and a new `request_id`.

**Default: all-or-nothing.** If any field of a change conflicts, nothing in that change is applied, including its clean fields. A change either happened or it didn't, so a client never has to reason about a half-applied operation. Nothing is lost because the client still holds the full change. A conflicted request does not consume its `sequence` number, so the resolution reuses the same sequence.

**Optional: `"partial": true`.** Applies the clean fields immediately and returns the conflicting ones with `needs_resolution: true`. This preserves valid work right away at the cost of a two-step outcome. The partial apply wrote a version, so it consumes its sequence, and the follow-up for the dropped fields uses the next sequence. If no clean field remains it behaves like a normal `CONFLICT`.

**Restore.** Any earlier version can be restored. This writes the old content as a **new** version, so history is never rewritten, and it can un-delete a record.

## Conflicting and non-conflicting scenarios

Both start from version 1 (`status: TODO`), with the laptop and phone both offline.

| | Non-conflicting | Conflicting |
|---|---|---|
| Laptop | sets `status = DONE` | sets `status = DONE` |
| Phone | sets `title = "Phone title"` | sets `status = IN_PROGRESS` |
| Result for the second device to sync | `MERGED`, both edits kept | `409 CONFLICT`, nothing written |
| Why | different fields | same field, different value, written by another device after the phone's base version |

More cases that are **not** conflicts: both devices add different tags (union), both increment a counter (deltas add up), both set the same field to the same value, and one device sending several of its own edits to one field.

## API reference and examples

| Method and path | Purpose |
|---|---|
| `POST /records/{id}/sync` | Apply one change. HTTP status follows the outcome. |
| `POST /sync` | Apply up to 100 changes in order. Returns 200 with a result per item. |
| `GET /sync/changes?since=&limit=&exclude_device=` | Pull changes after a cursor. |
| `GET /records/{id}` | Current state and version. |
| `GET /records` | List records (`include_deleted=true` to include tombstones). |
| `GET /records/{id}/history` | Every applied change, in order. |
| `GET /records/{id}/versions/{v}` | The record as it was at version `v`. |
| `POST /records/{id}/restore/{v}` | Restore version `v` as a new version. |
| `GET /health` | Liveness. |

**Status codes:** `200` applied or replayed, `201` created, `404` unknown record or version, `409` conflict / out-of-order / stale sequence / stale restore, `422` validation error / future `base_version` / `request_id` reused with a different payload.

**Rejection reasons:** `VALIDATION_ERROR`, `UNKNOWN_RECORD`, `UNKNOWN_VERSION`, `FUTURE_BASE_VERSION`, `OUT_OF_ORDER` (includes `expected_sequence`), `STALE_SEQUENCE`, `REQUEST_ID_REUSED`. **Conflict reasons:** `FIELD_CONFLICT`, `RECORD_DELETED`, `EDITED_SINCE_BASE`, `ALREADY_EXISTS`, `STALE_BASE`.

The examples below are real output from this code, using the same requests as the Swagger examples (record `11111111-1111-1111-1111-111111111111`).

### Conflict: the phone edits `status`, which the laptop already changed

The phone is still on version 1. The laptop set `status = DONE` in version 2.

```json
{
  "request_id": "aaaaaaaa-0000-0000-0000-000000000003",
  "device_id": "phone",
  "sequence": 1,
  "record_id": "11111111-1111-1111-1111-111111111111",
  "op": "update",
  "base_version": 1,
  "partial": false,
  "changes": {
    "status": {
      "set": "IN_PROGRESS"
    }
  }
}
```

```
HTTP 409
{
  "outcome": "CONFLICT",
  "reason": "FIELD_CONFLICT",
  "record_id": "11111111-1111-1111-1111-111111111111",
  "current_version": 2,
  "conflicts": [
    {
      "field": "status",
      "base_value": "TODO",
      "server_value": "DONE",
      "client_value": "IN_PROGRESS",
      "server_changed_in_version": 2,
      "server_changed_by": "laptop"
    }
  ],
  "record": {
    "id": "11111111-1111-1111-1111-111111111111",
    "version": 2,
    "data": {
      "title": "Demo",
      "body": "",
      "status": "DONE",
      "tags": [],
      "views": 0,
      "deleted": false
    }
  },
  "request_id": "aaaaaaaa-0000-0000-0000-000000000003",
  "replayed": false
}
```

Nothing was written. The response carries everything needed to resolve it.

### Merge: the phone edits a different field instead

```json
{
  "request_id": "aaaaaaaa-0000-0000-0000-000000000004",
  "device_id": "phone",
  "sequence": 1,
  "record_id": "11111111-1111-1111-1111-111111111111",
  "op": "update",
  "base_version": 1,
  "partial": false,
  "changes": {
    "title": {
      "set": "Phone title"
    }
  }
}
```

```
HTTP 200
{
  "outcome": "MERGED",
  "record_id": "11111111-1111-1111-1111-111111111111",
  "version": 3,
  "applied_fields": [
    "title"
  ],
  "merged_with": [
    {
      "version": 2,
      "device_id": "laptop",
      "fields": [
        "status"
      ]
    }
  ],
  "record": {
    "id": "11111111-1111-1111-1111-111111111111",
    "version": 3,
    "data": {
      "title": "Phone title",
      "body": "",
      "status": "DONE",
      "tags": [],
      "views": 0,
      "deleted": false
    }
  },
  "request_id": "aaaaaaaa-0000-0000-0000-000000000004",
  "replayed": false
}
```

### Retry: the same request sent again

The first response was lost, so the phone retries with the same `request_id`. The server returns the original answer and applies nothing twice (`"replayed": true`, version unchanged) and adds an `Idempotent-Replay: true` response header.

```
HTTP 200
{
  "outcome": "MERGED",
  "record_id": "11111111-1111-1111-1111-111111111111",
  "version": 3,
  "applied_fields": [
    "title"
  ],
  "merged_with": [
    {
      "version": 2,
      "device_id": "laptop",
      "fields": [
        "status"
      ]
    }
  ],
  "record": {
    "id": "11111111-1111-1111-1111-111111111111",
    "version": 3,
    "data": {
      "title": "Phone title",
      "body": "",
      "status": "DONE",
      "tags": [],
      "views": 0,
      "deleted": false
    }
  },
  "request_id": "aaaaaaaa-0000-0000-0000-000000000004",
  "replayed": true
}
```

### Partial mode: one clean field, one conflicting field

`body` is clean, `status` conflicts. With `partial: true`:

```json
{
  "request_id": "aaaaaaaa-0000-0000-0000-000000000006",
  "device_id": "phone",
  "sequence": 2,
  "record_id": "11111111-1111-1111-1111-111111111111",
  "op": "update",
  "base_version": 3,
  "partial": true,
  "changes": {
    "body": {
      "set": "notes"
    },
    "status": {
      "set": "DONE"
    }
  }
}
```

```
HTTP 200
{
  "outcome": "MERGED",
  "record_id": "11111111-1111-1111-1111-111111111111",
  "version": 5,
  "applied_fields": [
    "body"
  ],
  "merged_with": [
    {
      "version": 4,
      "device_id": "laptop",
      "fields": [
        "status",
        "tags",
        "views"
      ]
    }
  ],
  "record": {
    "id": "11111111-1111-1111-1111-111111111111",
    "version": 5,
    "data": {
      "title": "Phone title",
      "body": "notes",
      "status": "TODO",
      "tags": [
        "work"
      ],
      "views": 3,
      "deleted": false
    }
  },
  "partial": true,
  "needs_resolution": true,
  "conflicts": [
    {
      "field": "status",
      "base_value": "DONE",
      "server_value": "TODO",
      "client_value": "DONE",
      "server_changed_in_version": 4,
      "server_changed_by": "laptop"
    }
  ],
  "request_id": "aaaaaaaa-0000-0000-0000-000000000006",
  "replayed": false
}
```

### Out-of-order: the device skips a sequence number

```
HTTP 409
{
  "outcome": "REJECTED",
  "reason": "OUT_OF_ORDER",
  "request_id": "aaaaaaaa-0000-0000-0000-000000000009",
  "record_id": "11111111-1111-1111-1111-111111111111",
  "replayed": false,
  "expected_sequence": 3
}
```

### Pull: what changed since a cursor

`GET /sync/changes?since=0&limit=2`. Use `next_cursor` as the next `since`. The cursor is a server-assigned id, never a timestamp, because client clocks can't be trusted.

```json
{
  "changes": [
    {
      "change_id": 1,
      "record_id": "11111111-1111-1111-1111-111111111111",
      "version": 1,
      "device_id": "laptop",
      "op": "create",
      "base_version": 0,
      "request_id": "aaaaaaaa-0000-0000-0000-000000000001",
      "submitted": {
        "title": {
          "set": "Demo"
        },
        "status": {
          "set": "TODO"
        }
      },
      "fields": {
        "title": "Demo",
        "body": "",
        "status": "TODO",
        "tags": [],
        "views": 0,
        "deleted": false
      }
    },
    {
      "change_id": 2,
      "record_id": "11111111-1111-1111-1111-111111111111",
      "version": 2,
      "device_id": "laptop",
      "op": "update",
      "base_version": 1,
      "request_id": "aaaaaaaa-0000-0000-0000-000000000002",
      "submitted": {
        "status": {
          "set": "DONE"
        }
      },
      "fields": {
        "status": "DONE"
      }
    }
  ],
  "next_cursor": 2,
  "has_more": true
}
```

### History

`GET /records/{id}/history` (first three entries shown).

```json
{
  "record_id": "11111111-1111-1111-1111-111111111111",
  "history": [
    {
      "change_id": 1,
      "record_id": "11111111-1111-1111-1111-111111111111",
      "version": 1,
      "device_id": "laptop",
      "op": "create",
      "base_version": 0,
      "request_id": "aaaaaaaa-0000-0000-0000-000000000001",
      "submitted": {
        "title": {
          "set": "Demo"
        },
        "status": {
          "set": "TODO"
        }
      },
      "fields": {
        "title": "Demo",
        "body": "",
        "status": "TODO",
        "tags": [],
        "views": 0,
        "deleted": false
      }
    },
    {
      "change_id": 2,
      "record_id": "11111111-1111-1111-1111-111111111111",
      "version": 2,
      "device_id": "laptop",
      "op": "update",
      "base_version": 1,
      "request_id": "aaaaaaaa-0000-0000-0000-000000000002",
      "submitted": {
        "status": {
          "set": "DONE"
        }
      },
      "fields": {
        "status": "DONE"
      }
    },
    {
      "change_id": 3,
      "record_id": "11111111-1111-1111-1111-111111111111",
      "version": 3,
      "device_id": "phone",
      "op": "update",
      "base_version": 1,
      "request_id": "aaaaaaaa-0000-0000-0000-000000000004",
      "submitted": {
        "title": {
          "set": "Phone title"
        }
      },
      "fields": {
        "title": "Phone title"
      }
    }
  ]
}
```

### Restore

`POST /records/{id}/restore/6` brings back version 6's content, including un-deleting the record, as a new version.

```json
{
  "request_id": "bbbbbbbb-0000-0000-0000-000000000001",
  "device_id": "laptop",
  "base_version": 7
}
```

```
HTTP 200
{
  "outcome": "ACCEPTED",
  "record_id": "11111111-1111-1111-1111-111111111111",
  "version": 8,
  "restored_from": 6,
  "applied_fields": [
    "deleted"
  ],
  "record": {
    "id": "11111111-1111-1111-1111-111111111111",
    "version": 8,
    "data": {
      "title": "Phone title",
      "body": "notes",
      "status": "IN_PROGRESS",
      "tags": [
        "work"
      ],
      "views": 3,
      "deleted": false
    }
  },
  "request_id": "bbbbbbbb-0000-0000-0000-000000000001",
  "replayed": false
}
```

## Edge cases handled

| Case from the brief | How it is handled | Covered by |
|---|---|---|
| Requests arriving close together | Each sync runs in one `BEGIN IMMEDIATE` transaction, so writes serialize. Two conflicting requests at once: exactly one wins. | `test_concurrent_conflicting_requests_exactly_one_wins`, chaos run with 4 threads |
| Changes arriving out of order | Per-device `sequence`. A gap is rejected with `OUT_OF_ORDER` and the expected number; nothing changes. | `test_out_of_order_then_success_after_gap_filled`, chaos injection |
| Same request sent more than once | `request_id` store returns the original response (including a replayed `409`). A reused id with a different payload is a 422. | `test_duplicate_request_replays_without_reapplying`, `test_request_id_reuse_with_different_payload` |
| Device updating outdated data | `base_version` plus per-field `field_meta` finds exactly which fields changed since the device last looked. | `test_same_field_conflict_writes_nothing` |
| Two devices modifying the same data | Different fields merge; the same field conflicts. | `test_different_fields_merge`, `test_same_field_conflict_writes_nothing` |
| A newer change overwritten by an old one | Impossible by construction (conflict check) and verified after every chaos run by replaying history. | chaos invariants |
| Invalid or unknown requests | Fixed schema, op/field type checks, limits. Validation happens before any write. | `test_invalid_requests_do_not_change_data` |
| A failure mid-request | All writes for a change happen in one transaction, so it rolls back as a whole. | by design |

## Testing

```bash
pytest                         # 49 tests, a few seconds
pytest tests/test_chaos.py -s  # chaos test with a per-seed summary
python -m tests.test_chaos     # big run, prints the headline result
```

- **Scenario tests (21):** one or more for every edge case above, plus create/update/delete, set and counter merging, partial mode, and the batch endpoint.
- **History, pull and restore tests (10):** ordering, paging, `exclude_device`, version reconstruction, restore (including undelete and stale base), and that replaying pulled changes reproduces the server state.
- **API docs tests (3):** the Swagger page loads, and every documented example is executed in order and must behave exactly as its description says.
- **HTTP-layer tests (11):** batch limits and malformed items, replayed responses and the `Idempotent-Replay` header, editing after a restore, unknown-record reads, deleted-record listing, and error handling (an unexpected error returns clean JSON with a 500, a locked database returns 503 with `Retry-After`).
- **Chaos tests (4):** simulated devices keep local copies and queues, make random edits, creates, deletes and restores, and resolve conflicts at random, while the simulated network **duplicates requests, loses responses and reorders operations**. One run uses four real threads sharing the database. Afterwards the test checks:
  1. versions are contiguous (no gaps) for every record;
  2. folding the change log reproduces the current data;
  3. `field_meta` matches what the history implies;
  4. no request was applied twice;
  5. no stale scalar write overwrote a newer change from a different device;
  6. an **independent reference model** (separate code, fed only by what the server accepted) agrees with the server at every version of every record, and with every response body;
  7. counters equal the exact sum of accepted increments (concurrent run);
  8. after a full sync, every device's copy equals the server.

Latest large run (`python -m tests.test_chaos`): **more than 15,000 randomized operations across 5 seeds plus a 4-thread concurrent run, 0 invariant violations.** The seeded runs are reproducible; the exact total varies a little because the threaded run depends on timing.

To confirm the chaos test can actually fail, the engine was deliberately broken (conflict detection switched off, then counters changed from adding to overwriting) and the test caught both.

## Code quality

- **Linting and formatting:** `ruff check .` and `ruff format --check .` (configured in `pyproject.toml`).
- **Type checking:** `mypy` with `disallow_untyped_defs`; every function in `app/` is annotated.
- **CI:** `.github/workflows/ci.yml` runs lint, format check, type check and the full test suite on every push.
- **Structure:** `engine.py` has no HTTP in it. Small single-purpose functions (conflict detection, effective changes, one `_write_version` used by update, delete and restore) keep each sync path short.
- **Errors and logging:** every sync is logged (device, record, outcome, version, replayed). Unexpected errors return clean JSON with a 500 and are logged with the traceback; a locked database returns a 503 with `Retry-After`.

### Docker

```bash
docker build -t sync-api .
docker run -p 8000:8000 -v sync-data:/data sync-api   # data persists in the sync-data volume
```

## Design decisions

- **Per-field conflict detection** instead of whole-record versioning. With one record version, any two concurrent edits would conflict. Per-field tracking lets edits to different fields merge, which is what "preserve valid changes wherever possible" asks for.
- **Operations for sets and counters.** Sending the final value of a counter makes concurrent increments impossible to merge safely; sending `increment: 3` makes them commutative.
- **Stateless resolution.** No pending-conflict table to expire or clean up. The conflict response is self-contained and the resolution is an ordinary update.
- **All-or-nothing by default, partial as opt-in.** Atomic changes are easy to reason about; partial mode is there for clients that want to keep clean work immediately.
- **Both `base_version` and `sequence`.** They answer different questions. Without sequence numbers the server could not tell an out-of-order delivery from a merely stale one, and could not safely let a device's own queued edits pass.
- **Final answers are stored, rejections are not.** `ACCEPTED`, `MERGED` and `CONFLICT` are stored by `request_id`, so a retry gets the identical answer. `REJECTED` responses change nothing, so they are re-evaluated on retry, which means an out-of-order request succeeds once the missing operation arrives.
- **History as the source of truth.** Every applied change stores the values it produced, so any past version can be reconstructed, restored, and checked independently.
- **Soft delete.** A tombstone keeps the record's history and makes delete-vs-edit an ordinary, detectable conflict.
- **Server-assigned cursor for pull.** Never a timestamp.
- **SQLite.** The brief doesn't evaluate technology; SQLite with `BEGIN IMMEDIATE` makes the concurrency story simple and exact.

## Assumptions and known limits

- `device_id` is self-declared. There is no authentication, by design: the brief doesn't ask for it.
- One SQLite writer. Fine for this scope; a production system would use Postgres with row-level locks.
- Text fields are scalars: two edits to the same field conflict, even if they touch different sentences. There is no in-field text merge.
- A device never conflicts with its own earlier edits. So if a device resolves a conflict by choosing "mine" and has more queued edits to that field, those later edits apply over the value the user already chose to overwrite. This follows from treating a device's queue as one ordered stream.
- The record schema is fixed (`title`, `body`, `status`, `tags`, `views`).
- Free hosting tiers reset the SQLite file on restart, so a deployed demo may lose its data when the instance restarts. Attach a persistent disk and set `DB_PATH` for durable data.

## Project layout

```
app/
  main.py        FastAPI routes and status-code mapping
  engine.py      apply_change(), restore, history, pull: all sync logic, no HTTP
  merge.py       field schema, validation, scalar / set / counter strategies
  schemas.py     request models
  docs.py        Swagger examples and descriptions
  static/demo.html   the two-device visual demo served at /demo
  db.py          SQLite schema, connection and transaction helper
tests/
  test_scenarios.py      edge-case scenarios
  test_history_pull.py   history, version view, restore, pull
  test_openapi.py        Swagger examples are executed and verified
  test_extra.py          batch limits, replay header, restore interplay, error handling
  test_chaos.py          randomized stress test with a reference model
demo.py          narrated walkthrough (python demo.py)
design.md        the design spec this was built from
Dockerfile       container image (one worker, SQLite on a mountable /data volume)
pyproject.toml   ruff and mypy configuration
requirements.txt / requirements-dev.txt   runtime / development dependencies
.github/workflows/ci.yml   lint, types and tests on every push
```