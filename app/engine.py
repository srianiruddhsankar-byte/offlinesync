"""The sync engine. apply_change() is pure logic over a DB connection, with no HTTP.

Flow of apply_change (all inside one BEGIN IMMEDIATE transaction):
    validate -> idempotency replay -> sequence check -> load record -> per-operation handler
Every handler either rejects (nothing stored), answers with a final outcome (stored for
replay), or writes exactly one new version of the record plus one change_log row.
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import merge
from .db import transaction

Json = dict[str, Any]
Result = tuple[int, Json]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash(req: Json) -> str:
    canonical = json.dumps(req, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class RecordState:
    """A record as loaded from the database. Mutated in place when a new version is written."""

    id: str
    version: int
    data: Json
    meta: Json  # field -> {"version": int, "device_id": str}: who last wrote each field

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "RecordState":
        return cls(row["id"], row["version"], json.loads(row["data"]), json.loads(row["field_meta"]))

    def view(self) -> Json:
        return {"id": self.id, "version": self.version, "data": self.data}


# ---------------------------------------------------------------- response helpers


def _reject(status: int, reason: str, req: Json, **extra: Any) -> Result:
    """REJECTED outcomes are never stored: they have no effect, so a retry is safe."""
    body = {
        "outcome": "REJECTED",
        "reason": reason,
        "request_id": req["request_id"],
        "record_id": req["record_id"],
        "replayed": False,
        **extra,
    }
    return status, body


def _finish(conn: sqlite3.Connection, req: Json, status: int, body: Json, advance: bool) -> Result:
    """Store the final response (for idempotent replay) and optionally advance the device sequence."""
    body["request_id"] = req["request_id"]
    body["replayed"] = False
    now = _now()
    conn.execute(
        "INSERT INTO requests (request_id, payload_hash, device_id, status_code, response, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (req["request_id"], _hash(req), req["device_id"], status, json.dumps(body), now),
    )
    if advance:
        conn.execute(
            "INSERT INTO devices (device_id, last_sequence, first_seen, last_seen) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET last_sequence = excluded.last_sequence, "
            "last_seen = excluded.last_seen",
            (req["device_id"], req["sequence"], now, now),
        )
    return status, body


def _conflict_body(req: Json, state: RecordState, reason: str, **extra: Any) -> Json:
    return {
        "outcome": "CONFLICT",
        "reason": reason,
        "record_id": req["record_id"],
        "current_version": state.version,
        **extra,
        "record": state.view(),
    }


def _noop_body(state: RecordState, **extra: Any) -> Json:
    """Nothing to change: accepted, but no new version."""
    return {
        "outcome": "ACCEPTED",
        "noop": True,
        "record_id": state.id,
        "version": state.version,
        **extra,
        "applied_fields": [],
        "record": state.view(),
    }


def _applied_body(state: RecordState, applied: list[str], merged_with: list[Json]) -> Json:
    """MERGED if other devices changed the record since the base version, otherwise ACCEPTED."""
    return {
        "outcome": "MERGED" if merged_with else "ACCEPTED",
        "record_id": state.id,
        "version": state.version,
        "applied_fields": applied,
        "merged_with": merged_with,
        "record": state.view(),
    }


# ---------------------------------------------------------------- persistence helpers


def _log(conn: sqlite3.Connection, req: Json, version: int, op: str, after: Json) -> None:
    conn.execute(
        "INSERT INTO change_log "
        "(record_id, version, device_id, request_id, base_version, op, ops, after, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            req["record_id"],
            version,
            req["device_id"],
            req["request_id"],
            req["base_version"],
            op,
            json.dumps(req["changes"]),
            json.dumps(after),
            _now(),
        ),
    )


def _write_version(conn: sqlite3.Connection, req: Json, op: str, state: RecordState, changed: Json) -> None:
    """Persist ONE new version of a record and append it to the change log."""
    state.version += 1
    state.data.update(changed)
    for field in changed:
        state.meta[field] = {"version": state.version, "device_id": req["device_id"]}
    conn.execute(
        "UPDATE records SET data = ?, version = ?, field_meta = ?, updated_at = ? WHERE id = ?",
        (json.dumps(state.data), state.version, json.dumps(state.meta), _now(), state.id),
    )
    _log(conn, req, state.version, op, changed)


def state_at(conn: sqlite3.Connection, record_id: str, version: int) -> Json:
    """Reconstruct record data at a version by folding the 'after' values of the change log."""
    state: Json = {}
    rows = conn.execute(
        "SELECT after FROM change_log WHERE record_id = ? AND version <= ? ORDER BY version",
        (record_id, version),
    )
    for r in rows:
        state.update(json.loads(r["after"]))
    return state


def _merged_with(conn: sqlite3.Connection, record_id: str, base: int, device: str) -> list[Json]:
    """Changes by OTHER devices after base_version: what this change was merged on top of."""
    rows = conn.execute(
        "SELECT version, device_id, after FROM change_log "
        "WHERE record_id = ? AND version > ? AND device_id != ? ORDER BY version",
        (record_id, base, device),
    )
    return [
        {"version": r["version"], "device_id": r["device_id"], "fields": sorted(json.loads(r["after"]))}
        for r in rows
    ]


def _check_replay(conn: sqlite3.Connection, req: Json) -> Result | None:
    """If this request_id was already processed, return the stored result (422 if the payload differs)."""
    stored = conn.execute(
        "SELECT payload_hash, status_code, response FROM requests WHERE request_id = ?",
        (req["request_id"],),
    ).fetchone()
    if not stored:
        return None
    if stored["payload_hash"] != _hash(req):
        return _reject(422, "REQUEST_ID_REUSED", req)
    body = json.loads(stored["response"])
    body["replayed"] = True
    return int(stored["status_code"]), body


# ---------------------------------------------------------------- the entry point


def apply_change(conn: sqlite3.Connection, req: Json) -> Result:
    """Apply one change from one device. Returns (http_status, response_body)."""
    err = merge.validate_changes(req["op"], req["changes"])
    if err:
        return _reject(422, "VALIDATION_ERROR", req, detail=err)

    with transaction(conn):
        replay = _check_replay(conn, req)
        if replay:
            return replay

        # Per-device sequence: a device's operations must arrive in order.
        dev = conn.execute(
            "SELECT last_sequence FROM devices WHERE device_id = ?", (req["device_id"],)
        ).fetchone()
        expected = (dev["last_sequence"] if dev else 0) + 1
        if req["sequence"] > expected:
            return _reject(409, "OUT_OF_ORDER", req, expected_sequence=expected)
        if req["sequence"] < expected:
            return _reject(409, "STALE_SEQUENCE", req, expected_sequence=expected)

        row = conn.execute("SELECT * FROM records WHERE id = ?", (req["record_id"],)).fetchone()
        if req["op"] == "create":
            return _create(conn, req, row)
        if req["op"] == "update":
            return _update(conn, req, row)
        return _delete(conn, req, row)


# ---------------------------------------------------------------- create


def _create(conn: sqlite3.Connection, req: Json, row: sqlite3.Row | None) -> Result:
    if row is not None:
        existing = RecordState.from_row(row)
        return _finish(conn, req, 409, _conflict_body(req, existing, "ALREADY_EXISTS"), advance=False)

    data = merge.new_defaults()
    try:
        for field, spec in req["changes"].items():
            data[field] = merge.apply_field(field, data[field], spec)
    except ValueError as e:
        return _reject(422, "VALIDATION_ERROR", req, detail=str(e))

    now = _now()
    meta = {f: {"version": 1, "device_id": req["device_id"]} for f in req["changes"]}
    conn.execute(
        "INSERT INTO records (id, data, version, field_meta, created_at, updated_at) "
        "VALUES (?, ?, 1, ?, ?, ?)",
        (req["record_id"], json.dumps(data), json.dumps(meta), now, now),
    )
    _log(conn, req, 1, "create", data)
    state = RecordState(req["record_id"], 1, data, meta)
    body = {
        "outcome": "ACCEPTED",
        "record_id": state.id,
        "version": 1,
        "applied_fields": sorted(req["changes"]),
        "record": state.view(),
    }
    return _finish(conn, req, 201, body, advance=True)


# ---------------------------------------------------------------- update


def _scalar_conflicts(req: Json, state: RecordState) -> list[str]:
    """Scalar fields the device wants to change that another device changed after its base version.

    A field conflicts only if ALL hold: it was last written after base_version, by a different
    device, and the current value differs from the one the device wants. Sets and counters are
    commutative and never conflict.
    """
    base, device = req["base_version"], req["device_id"]
    conflicts = []
    for field, spec in req["changes"].items():
        if merge.SCHEMA[field] != "scalar":
            continue
        last = state.meta.get(field)
        if (
            last
            and last["version"] > base
            and last["device_id"] != device
            and state.data[field] != spec["set"]
        ):
            conflicts.append(field)
    return conflicts


def _effective_changes(state: RecordState, changes: Json, skip: list[str]) -> Json:
    """New values for the non-skipped fields that actually differ from the current data."""
    new_values = {
        f: merge.apply_field(f, state.data[f], spec) for f, spec in changes.items() if f not in skip
    }
    return {f: v for f, v in new_values.items() if v != state.data[f]}


def _conflict_details(
    conn: sqlite3.Connection, req: Json, state: RecordState, fields: list[str]
) -> list[Json]:
    """Base, server and client value for each conflicting field."""
    base_state = state_at(conn, req["record_id"], req["base_version"])
    return [
        {
            "field": f,
            "base_value": base_state.get(f),
            "server_value": state.data[f],
            "client_value": req["changes"][f]["set"],
            "server_changed_in_version": state.meta[f]["version"],
            "server_changed_by": state.meta[f]["device_id"],
        }
        for f in fields
    ]


def _update(conn: sqlite3.Connection, req: Json, row: sqlite3.Row | None) -> Result:
    if row is None:
        return _reject(404, "UNKNOWN_RECORD", req)
    state = RecordState.from_row(row)

    if state.data["deleted"]:
        return _finish(conn, req, 409, _conflict_body(req, state, "RECORD_DELETED"), advance=False)
    if req["base_version"] > state.version:
        return _reject(422, "FUTURE_BASE_VERSION", req, current_version=state.version)

    conflicts = _scalar_conflicts(req, state)
    try:
        effective = _effective_changes(state, req["changes"], skip=conflicts)
    except ValueError as e:
        return _reject(422, "VALIDATION_ERROR", req, detail=str(e))

    # All-or-nothing by default; partial mode only proceeds if something clean remains.
    if conflicts and not (req["partial"] and effective):
        details = _conflict_details(conn, req, state, conflicts)
        return _finish(conn, req, 409, _conflict_body(req, state, "FIELD_CONFLICT", conflicts=details), False)

    if not effective:
        return _finish(conn, req, 200, _noop_body(state), advance=True)

    merged_with = _merged_with(conn, state.id, req["base_version"], req["device_id"])
    _write_version(conn, req, "update", state, effective)
    body = _applied_body(state, sorted(effective), merged_with)
    if conflicts:  # partial mode with leftovers; conflicting fields were not applied
        body.update(
            partial=True,
            needs_resolution=True,
            conflicts=_conflict_details(conn, req, state, conflicts),
        )
    return _finish(conn, req, 200, body, advance=True)


# ---------------------------------------------------------------- delete


def _delete(conn: sqlite3.Connection, req: Json, row: sqlite3.Row | None) -> Result:
    if row is None:
        return _reject(404, "UNKNOWN_RECORD", req)
    state = RecordState.from_row(row)
    base, device = req["base_version"], req["device_id"]

    if base > state.version:
        return _reject(422, "FUTURE_BASE_VERSION", req, current_version=state.version)
    if state.data["deleted"]:
        return _finish(conn, req, 200, _noop_body(state), advance=True)

    edited = sorted(f for f, m in state.meta.items() if m["version"] > base and m["device_id"] != device)
    if edited:
        details = [
            {
                "field": f,
                "server_changed_in_version": state.meta[f]["version"],
                "server_changed_by": state.meta[f]["device_id"],
            }
            for f in edited
        ]
        return _finish(
            conn, req, 409, _conflict_body(req, state, "EDITED_SINCE_BASE", conflicts=details), False
        )

    merged_with = _merged_with(conn, state.id, base, device)
    _write_version(conn, req, "delete", state, {"deleted": True})
    return _finish(conn, req, 200, _applied_body(state, ["deleted"], merged_with), advance=True)


# ---------------------------------------------------------------- restore


def restore_version(conn: sqlite3.Connection, req: Json) -> Result:
    """Write a NEW version whose content equals an earlier version. History is never rewritten.

    req: request_id, device_id, record_id, target_version, base_version.
    Restore is a maintenance operation: it does not use the device sequence number.
    """
    internal: Json = {
        "request_id": req["request_id"],
        "device_id": req["device_id"],
        "record_id": req["record_id"],
        "base_version": req["base_version"],
        "sequence": 0,
        "changes": {"restore_to": req["target_version"]},
    }
    target = req["target_version"]
    with transaction(conn):
        replay = _check_replay(conn, internal)
        if replay:
            return replay

        row = conn.execute("SELECT * FROM records WHERE id = ?", (req["record_id"],)).fetchone()
        if row is None:
            return _reject(404, "UNKNOWN_RECORD", internal)
        state = RecordState.from_row(row)

        if target < 1 or target > state.version:
            return _reject(404, "UNKNOWN_VERSION", internal, current_version=state.version)
        if req["base_version"] > state.version:
            return _reject(422, "FUTURE_BASE_VERSION", internal, current_version=state.version)
        if req["base_version"] < state.version:
            return _finish(conn, internal, 409, _conflict_body(internal, state, "STALE_BASE"), advance=False)

        old = state_at(conn, state.id, target)
        diff = {f: old[f] for f in merge.DEFAULTS if f in old and old[f] != state.data.get(f)}
        if not diff:
            return _finish(conn, internal, 200, _noop_body(state, restored_from=target), advance=False)

        _write_version(conn, internal, "restore", state, diff)
        body = {
            "outcome": "ACCEPTED",
            "record_id": state.id,
            "version": state.version,
            "restored_from": target,
            "applied_fields": sorted(diff),
            "record": state.view(),
        }
        return _finish(conn, internal, 200, body, advance=False)


# ---------------------------------------------------------------- read models: history and pull


def _change_row(r: sqlite3.Row) -> Json:
    return {
        "change_id": r["change_id"],
        "record_id": r["record_id"],
        "version": r["version"],
        "device_id": r["device_id"],
        "op": r["op"],
        "base_version": r["base_version"],
        "request_id": r["request_id"],
        "submitted": json.loads(r["ops"]),
        "fields": json.loads(r["after"]),
        "created_at": r["created_at"],
    }


def get_history(conn: sqlite3.Connection, record_id: str) -> list[Json] | None:
    if conn.execute("SELECT 1 FROM records WHERE id = ?", (record_id,)).fetchone() is None:
        return None
    rows = conn.execute("SELECT * FROM change_log WHERE record_id = ? ORDER BY version", (record_id,))
    return [_change_row(r) for r in rows]


def pull_changes(conn: sqlite3.Connection, since: int, limit: int, exclude_device: str | None) -> Json:
    """Changes with change_id > since, in order. The cursor is a server-assigned id, never a timestamp."""
    sql = "SELECT * FROM change_log WHERE change_id > ?"
    params: list[Any] = [since]
    if exclude_device:
        sql += " AND device_id != ?"
        params.append(exclude_device)
    sql += " ORDER BY change_id LIMIT ?"
    params.append(limit + 1)
    rows = conn.execute(sql, params).fetchall()
    has_more = len(rows) > limit
    changes = [_change_row(r) for r in rows[:limit]]
    return {
        "changes": changes,
        "next_cursor": changes[-1]["change_id"] if changes else since,
        "has_more": has_more,
    }
