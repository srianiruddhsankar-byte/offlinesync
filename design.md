# Offline Sync Conflict Backend: Design Spec

Stack: FastAPI, SQLite (WAL), pytest, deployed on Render (or Railway). This file is the source of truth for every decision; the README is built from it.

## 1. Goals and non-goals

**Goals**
- Several devices edit the same records offline and sync later.
- An outdated change never silently overwrites a newer accepted change.
- Changes that don't actually collide are merged, not rejected.
- Retries, duplicates, out-of-order arrival and concurrency never corrupt state.

**Non-goals** (state these in the README as deliberate): auth, CRDTs / operational transforms, in-field text merging, multi-node scaling, a UI.

**Assumptions**
- `device_id` is a self-declared string with no registration or authentication.
- A single SQLite writer is enough. A production version would use Postgres with row locks.
- Client clocks are never trusted. Ordering uses server-assigned versions and IDs only.
- The deployed demo may reset its data on restart (see section 10).

## 2. Data model

### Record schema (fixed; unknown fields are rejected with 422)

| Field | Type | Merge semantics | Op format |
|---|---|---|---|
| `title` | string, max 200 | scalar | `{"set": "x"}` |
| `body` | string, max 10000 | scalar | `{"set": "x"}` |
| `status` | enum `TODO`, `IN_PROGRESS`, `DONE` | scalar | `{"set": "DONE"}` |
| `tags` | set of strings (max 20 items, each max 32) | set | `{"add": [...], "remove": [...]}` |
| `views` | integer counter | counter | `{"increment": 3}` |

- Wrong op for a field type (`increment` on `title`, `set` on `views`) is 422.
- The same element in both `add` and `remove` is 422.
- `deleted` (bool) is server-managed tombstone state, changed only by `delete` and `restore`.
- Counters and sets travel as operations, never as final values. This is what makes concurrent edits commutative.

### Tables

```
records
  id            TEXT PK        -- client-generated UUID
  data          JSON           -- current field values (+ deleted flag)
  version       INTEGER        -- +1 per applied change
  field_meta    JSON           -- {"title": {"version": 7, "device_id": "phone"}, ...}
  created_at, updated_at

change_log                     -- applied changes only; also the pull feed
  change_id     INTEGER PK AUTOINCREMENT   -- the pull cursor
  record_id, version           -- UNIQUE(record_id, version)
  device_id, request_id, base_version
  op            TEXT           -- create | update | delete | restore
  ops           JSON           -- the ops as submitted
  after         JSON           -- resulting values of every touched field
  created_at

requests                       -- idempotency store
  request_id    TEXT PK
  payload_hash  TEXT           -- sha256 of canonical JSON body
  device_id, status_code, response JSON, created_at

devices
  device_id     TEXT PK
  last_sequence INTEGER        -- last sequence that produced an applied change
  first_seen, last_seen
```

- `field_meta` is a denormalized cache for fast conflict checks. The chaos test asserts it always equals what the history implies.
- State at version `v` = fold of `after` over `change_log` entries with `version <= v`. A `create` entry's `after` holds the full record. This powers history, restore, and the `base_value` shown in conflicts.

## 3. Sync request

```json
{
  "request_id": "uuid",
  "device_id": "laptop-01",
  "sequence": 4,
  "record_id": "uuid",
  "op": "update",
  "base_version": 10,
  "partial": false,
  "changes": {
    "title":  {"set": "SwarmSeek v2"},
    "tags":   {"add": ["hackathon"], "remove": []},
    "views":  {"increment": 3}
  }
}
```

| Field | Rule |
|---|---|
| `request_id` | UUID; the idempotency key |
| `device_id` | 1-64 chars, `[A-Za-z0-9_-]` |
| `sequence` | integer >= 1; per device, see section 5 |
| `op` | `create` (base_version must be 0, `changes` holds initial values), `update` (base_version >= 1), `delete` (`changes` empty) |
| `base_version` | the record version the device last saw when it made the edit |
| `partial` | optional, default `false`. `true` applies the non-conflicting fields of an `update` and reports the rest (section 6). Ignored for `create` and `delete`. |

Offline creation works because record IDs are client-generated UUIDs.

## 4. Outcomes and response shapes

| Outcome | Meaning | HTTP |
|---|---|---|
| `ACCEPTED` | Applied cleanly. `base_version` equals current, or every intervening change came from the same device. Also used for a no-op (see below). | 200 (201 on create) |
| `MERGED` | Applied on top of newer changes from other devices that touched different fields (or commutative set/counter fields). With `partial: true` it can also carry unresolved `conflicts`. | 200 |
| `CONFLICT` | Nothing written. Needs client resolution. | 409 |
| `REJECTED` | Invalid or unprocessable. Nothing written. | 404 / 409 / 422 |

**Success**
```json
{
  "outcome": "MERGED",
  "request_id": "...",
  "record_id": "...",
  "version": 12,
  "applied_fields": ["title"],
  "merged_with": [{"version": 11, "device_id": "phone", "fields": ["status"]}],
  "record": {"id": "...", "version": 12, "data": {"title": "...", "status": "DONE"}},
  "replayed": false
}
```

**Conflict**
```json
{
  "outcome": "CONFLICT",
  "reason": "FIELD_CONFLICT",
  "request_id": "...",
  "record_id": "...",
  "current_version": 11,
  "conflicts": [{
    "field": "status",
    "base_value": "TODO",
    "server_value": "DONE",
    "client_value": "IN_PROGRESS",
    "server_changed_in_version": 11,
    "server_changed_by": "phone"
  }],
  "record": {"id": "...", "version": 11, "data": {"...": "..."}}
}
```

**Partial success** (`partial: true`, some fields clean, some conflicting)
```json
{
  "outcome": "MERGED",
  "partial": true,
  "needs_resolution": true,
  "version": 12,
  "applied_fields": ["title"],
  "conflicts": [{"field": "status", "base_value": "TODO", "server_value": "DONE", "client_value": "IN_PROGRESS", "server_changed_in_version": 11, "server_changed_by": "phone"}],
  "record": {"...": "..."}
}
```
HTTP is 200 because a version was written; clients must check `needs_resolution`.

**Conflict reasons:** `FIELD_CONFLICT`, `RECORD_DELETED` (update to a tombstone), `EDITED_SINCE_BASE` (delete vs. newer edit by another device), `ALREADY_EXISTS` (create with an existing ID).

**Rejection reasons:** `VALIDATION_ERROR` (422), `UNKNOWN_RECORD` (404), `FUTURE_BASE_VERSION` (422, base_version > current), `OUT_OF_ORDER` (409, includes `expected_sequence`), `STALE_SEQUENCE` (409, sequence already used by a different request), `REQUEST_ID_REUSED` (422, same request_id with a different payload).

**No-op:** if every field in a change already equals the current state, the outcome is `ACCEPTED` with `"noop": true`, no version bump, and no `change_log` row. The request is still stored for idempotency.

## 5. The algorithm (`apply_change`)

Runs inside one `BEGIN IMMEDIATE` transaction, so concurrent syncs serialize.

1. **Validate** structure and types. On failure return 422. Nothing is stored.
2. **Idempotency:** look up `request_id` in `requests`.
   - Same `payload_hash`: return the stored status and body with `"replayed": true` and header `Idempotent-Replay: true`. A replayed `409` stays a `409`.
   - Different hash: `REJECTED / REQUEST_ID_REUSED`.
3. **Sequence check** for the device (create the `devices` row if new). Let `expected = last_sequence + 1`.
   - `sequence > expected`: `REJECTED / OUT_OF_ORDER`. The client resends in order.
   - `sequence < expected`: `REJECTED / STALE_SEQUENCE`.
4. **Load record.** `update` or `delete` on an unknown ID is `REJECTED / UNKNOWN_RECORD` (404). `create` on an existing ID is `CONFLICT / ALREADY_EXISTS`. Update to a tombstone is `CONFLICT / RECORD_DELETED`.
5. **Base version.** `base_version > current` is `REJECTED / FUTURE_BASE_VERSION`.
6. **Conflict detection** (per field, only for scalar fields):
   - Conflict iff `field_meta[f].version > base_version` **and** `field_meta[f].device_id != this device` **and** `current[f] != client's new value`.
   - Same-device exemption: a device never conflicts with its own earlier edits.
   - Same-value exemption: if both sides arrived at the same value, there is nothing to resolve.
   - Set and counter fields never conflict. They are applied to the current value: `(current ∪ add) − remove` and `current + increment`.
   - `delete` conflicts if any field has `version > base_version` by a different device.
7. **Resolve the conflict set:**
   - No conflicting fields: continue to step 8.
   - Conflicts and `partial: false` (default): return `CONFLICT` listing **every** conflicting field. Nothing is applied, including clean fields.
   - Conflicts and `partial: true`: drop the conflicting fields, keep the clean ones. If no clean field remains, return `CONFLICT` as above. Otherwise continue to step 8 with only the clean fields, and report the dropped ones in `conflicts` with `needs_resolution: true`.
8. **Apply:** compute the new data, `version + 1`, update `field_meta` for touched fields, insert a `change_log` row (`ops` and `after`, containing only fields actually applied), set `devices.last_sequence = sequence`, store the response in `requests`. Commit.
9. **Outcome label:** `MERGED` if any intervening change (version > base_version) came from a different device, otherwise `ACCEPTED`. A partial apply is always `MERGED`, since conflicts can only exist when a different device changed something.

**What gets stored for replay**
- `ACCEPTED`, `MERGED` and `CONFLICT` responses are stored.
- `REJECTED` responses are never stored. They have no effect, so retrying later is safe (for example an `OUT_OF_ORDER` request succeeds once the missing sequence arrives).
- `CONFLICT` and `REJECTED` do **not** advance `last_sequence`. A partial apply **does**, because it wrote a version.

**Backstop:** `requests.request_id` is a primary key. On an `IntegrityError` from a racing duplicate, re-read the stored response and return it.

## 6. Conflict resolution (stateless server)

- The server keeps no pending-conflict state.
- The conflict response carries base, server and client values per field.
- The client chooses per field (keep server, keep client, or a custom value) and submits a **normal update** with `base_version = current_version` from the response and a new `request_id`.

**All-or-nothing mode (default)**
- The conflicted request did not advance the sequence, so the resolution reuses the **same `sequence`**.
- Operations queued behind it are rejected with `OUT_OF_ORDER` until it is resolved. This is intentional: a device's operations apply in order.

**Why all-or-nothing is the default**
- A change is atomic: it either happened or it didn't, so the client never has to reason about a half-applied operation.
- Nothing is lost. The client still holds the full change and resubmits it.
- Outcomes stay predictable and easy to test.

**Partial mode (`partial: true`)**
- Preserves the clean fields immediately, which matches "preserve valid changes wherever possible" at the cost of a two-step outcome.
- The partial apply consumed its sequence, so the follow-up resolution for the dropped fields uses the **next `sequence`** and `base_version = current_version`.

**Client rule:** after any resolution, rebase the remaining queued operations onto the new version so their `base_version` is current.

## 7. Endpoints

| Method and path | Purpose |
|---|---|
| `POST /records/{id}/sync` | Apply one change. HTTP status follows the outcome. |
| `POST /sync` | Batch of changes. Always `200` if the envelope is valid (else 422). Each result carries its own `status`. Items are processed in order and independently. |
| `GET /sync/changes?since=<change_id>&limit=100&exclude_device=<id>` | Pull. Returns changes with `change_id > since`, ordered, plus `next_cursor` and `has_more`. |
| `GET /records/{id}` | Current state and version. |
| `GET /records?include_deleted=false` | List records. |
| `GET /records/{id}/history` | Ordered change history. |
| `GET /records/{id}/versions/{v}` | Reconstructed state at version `v`. |
| `POST /records/{id}/restore/{v}` | Body: `device_id`, `request_id`, `base_version`. Writes a **new** version containing the old content. Stale `base_version` is `409`. Skips the device sequence check. Can undelete. |
| `GET /health` | Liveness (used by the deploy health check). |

**Pull rules**
- `since` is the server-assigned autoincrement `change_id`, never a timestamp. With serialized writes, IDs become visible in commit order, so the cursor is safe.
- Each entry carries `after` values, so a client can apply it without further requests.
- The server doesn't merge on the client's behalf. The demo client simulator reconciles pulled changes with its pending local edits.

**HTTP summary:** 200 OK, 201 create, 404 unknown record, 409 conflict / out-of-order / stale sequence, 422 validation / future base / request-id reuse.

## 8. Concurrency and consistency

- SQLite in WAL mode, `busy_timeout=5000`, one connection per request, `BEGIN IMMEDIATE` around `apply_change`.
- Duplicates are handled by the idempotency check and the primary-key backstop.
- Out-of-order arrival is handled by per-device sequences. Cross-device order doesn't matter because merge rules are order-independent for sets and counters, and conflict-checked for scalars.
- Invalid or stale requests are validated before any write, and all writes happen in one transaction, so a failure rolls back cleanly.

## 9. API documentation (Swagger / OpenAPI)

The evaluators will open `/docs`, so it has to be usable without reading the README.

- App-level title, description and version; link to the README.
- Route **tags**: `Sync`, `Records`, `History`, `Pull`, `System`. Each route gets a one-line summary and a description of its status codes.
- **Request examples** via `Body(openapi_examples={...})`, named:
  - `create_record`
  - `update_clean` (ACCEPTED)
  - `update_merge` (different field than a newer change, MERGED)
  - `update_conflict` (same field as a newer change, CONFLICT)
  - `resolve_conflict`
  - `tags_add_remove`
  - `counter_increment`
  - `partial_update`
  - `delete_record`
- **Response examples** for every documented status code via `responses={...}` (200 ACCEPTED, 200 MERGED, 200 partial, 409 CONFLICT, 409 OUT_OF_ORDER, 404, 422) using the shapes in section 4, with the response models attached.
- Field descriptions on the Pydantic models (`base_version`, `sequence`, `request_id`, op formats).
- **Smoke test:** `GET /openapi.json` returns 200 and contains the example names above, so the docs can't silently rot.

## 10. Deployment

**Target:** Render web service (Railway works the same way).

- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`. One worker keeps the single-writer story simple.
- Config via env vars: `DB_PATH` (default `./sync.db`).
- Files: `requirements.txt` (pinned), optional `Dockerfile`, `render.yaml` (or the dashboard equivalent).
- Health check path: `/health`.
- `demo.py` reads `BASE_URL` (default `http://localhost:8000`) so the same script runs against the deployed URL.

**Persistence caveat (state it in the README):**
- Free tiers use ephemeral disks, so the SQLite file resets on every restart or redeploy. Free instances also spin down when idle, so the first request after a pause is slow.
- Acceptable for a demo. For durable data, attach a persistent disk and point `DB_PATH` at it (for example `/data/sync.db`), keeping SQLite on a local disk because WAL doesn't work on network filesystems.

**Deploy checklist**
1. App starts locally with the exact production command.
2. `GET /health` is 200 on the deployed URL.
3. `demo.py` runs end to end against the deployed URL.
4. `/docs` renders with the examples.
5. README has the live URL and the persistence note.

## 11. Testing plan

**Scenario tests (one per spec edge case):**
1. Create, then update at the current version: `ACCEPTED`.
2. Different scalar fields from two devices: `MERGED`.
3. Same scalar field, different values: `409`, data unchanged, `base_value`/`server_value`/`client_value` correct.
4. Same field, same value: no conflict.
5. Same-device queued edits to one field: no self-conflict.
6. Tags: concurrent adds merge, a remove is not resurrected by a stale add.
7. Counter: +3 and +5 from the same base give +8; duplicate request does not double-apply.
8. Duplicate `request_id`: identical response, version bumped once; a replayed `409` stays `409`.
9. `request_id` reuse with a different payload: 422.
10. Out-of-order sequence: `OUT_OF_ORDER`, then success once the gap is filled.
11. Stale update never overwrites a newer accepted change.
12. Future `base_version`, unknown record, invalid field or op type: correct codes, no data change.
13. Delete versus edit: both orderings conflict.
14. Concurrent requests (threads, separate connections): exactly one wins for a conflicting pair, none lost for a mergeable pair.
15. History, version reconstruction, restore (including undelete).
16. Pull: cursor monotonic, `exclude_device` works, replaying pulls reproduces server state.
17. Default mode: a change with one clean and one conflicting field writes nothing and lists the conflict.
18. Partial mode: clean field applied, conflicting field untouched, `needs_resolution: true`, sequence advanced, follow-up resolution with the next sequence succeeds.
19. Partial mode with no clean field: plain `CONFLICT`, sequence not advanced.
20. OpenAPI smoke test (section 9) and `/health`.

**Chaos test** (`tests/test_chaos.py`, seeded RNG so failures reproduce)
- 3-5 simulated devices, each with a local replica and a pending queue.
- Random actions: offline edits, sync (with `partial` randomly on or off), duplicated sends, delayed/reordered sends, concurrent sends via threads, pulls, auto-resolving conflicts (random server/client choice).
- Check these invariants after 10,000 operations:
  1. Versions per record are contiguous `1..n`.
  2. Folding `change_log` reproduces `records.data` exactly.
  3. `field_meta` matches the history.
  4. Each `ACCEPTED`/`MERGED` response maps to exactly one `change_log` row.
  5. `views` equals the sum of accepted increments (independent oracle).
  6. A simple reference model replaying accepted changes one at a time ends in the same state.
  7. No scalar field was overwritten by a stale change from a different device with a different value (holds for partial applies too).
  8. After a full sync of every device, all replicas equal the server.
- README line: "N randomized operations, 0 invariant violations (seed X)".

## 12. Repo layout

```
app/
  main.py        # FastAPI routes, status-code mapping, OpenAPI metadata
  engine.py      # apply_change(): pure logic, no HTTP
  merge.py       # scalar / set / counter strategies
  schemas.py     # Pydantic models, field schema, OpenAPI examples
  db.py          # connection, migrations, transaction helper
tests/
  test_scenarios.py
  test_chaos.py
client_sim.py    # tiny device simulator (replica + queue + rebase)
demo.py          # scripted story, BASE_URL configurable
requirements.txt
render.yaml      # or Dockerfile
DESIGN.md -> README.md
```

`engine.py` takes plain data and returns an outcome object. Both `/sync` and `/records/{id}/sync` are thin wrappers, so batch is just a loop.

## 13. Build order and cuts

| Step | Work | Time |
|---|---|---|
| 1 | Schema, db helper, create/get | 1 h |
| 2 | `apply_change` core: versions, sequence, idempotency, scalar conflicts, transaction | 2 h |
| 3 | Set and counter strategies | 1 h |
| 4 | Scenario tests 1-16 | 1.5 h |
| 5 | Pull endpoint, history, versions, restore | 1.5 h |
| 6 | Batch endpoint, chaos test | 1.5 h |
| 7 | Partial mode plus tests 17-19, chaos update | 1 h |
| 8 | Swagger metadata, examples, smoke test | 0.5 h |
| 9 | `client_sim.py`, `demo.py`, README | 1.5 h |
| 10 | Deployment and checklist | 0.75 h |

Total: about 12.25 hours. Do the steps in order; each leaves the project in a working state.

**If time runs short, cut in this order:** demo polish, partial mode, deployment, restore, counters. **Never cut:** scenario tests, the README, idempotency, the same-device rule, Swagger examples for the core outcomes.

## 14. README outline

1. What it is (3 lines), live URL, and how to run it locally
2. How sync works (diagram: device to engine to current state + change log, pull back to devices)
3. How changes are tracked (versions, `base_version`, `field_meta`, `change_log`)
4. Conflict detection rules (section 5, step 6) with a conflicting and a non-conflicting scenario side by side
5. Conflict resolution strategy (section 6), including why all-or-nothing is the default and what partial mode adds
6. Outcome table and example requests/responses (section 4)
7. Edge cases handled (duplicates, out-of-order, stale, concurrent, same-device)
8. Testing, including the chaos test result
9. Deployment notes and the persistence caveat
10. Assumptions and known limits (self-declared device IDs, no auth, SQLite single writer, no in-field text merge, client must rebase its queue)

## 15. Demo script (`demo.py`)

1. Both devices start at v10 and go offline.
2. Laptop edits `title`, phone edits `status`; both sync: `MERGED`, v12.
3. Both edit `status` differently: the second gets `409` with all three values; resolve; accepted.
4. A change touching a clean and a conflicting field: show default mode (nothing applied), then partial mode (clean field applied, `needs_resolution`).
5. Concurrent tag adds merge; concurrent `views` increments sum.
6. Send the same request twice: first applied, second replayed, version unchanged.
7. Show history, then restore an earlier version.
8. Run the chaos test: N operations, 0 invariant violations.