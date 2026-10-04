"""The sync engine: apply_change() is pure logic over a DB connection, no HTTP."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from . import merge
from .db import transaction

Result = tuple[int, dict]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(req: dict) -> str:
    canonical = json.dumps(req, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _view(record_id: str, version: int, data: dict) -> dict:
    return {"id": record_id, "version": version, "data": data}


def _row_view(row: sqlite3.Row) -> dict:
    return _view(row["id"], row["version"], json.loads(row["data"]))


# ---------- response helpers ----------

def _reject(status: int, reason: str, req: dict, **extra: Any) -> Result:
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


def _finish(conn: sqlite3.Connection, req: dict, status: int, body: dict, advance: bool) -> Result:
    """Store the final response (idempotency) and optionally advance the device sequence."""
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


def _log(conn: sqlite3.Connection, req: dict, version: int, op: str, after: dict) -> None:
    conn.execute(
        "INSERT INTO change_log (record_id, version, device_id, request_id, base_version, op, ops, after, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            req["record_id"], version, req["device_id"], req["request_id"], req["base_version"],
            op, json.dumps(req["changes"]), json.dumps(after), _now(),
        ),
    )


# ---------- history helpers ----------

def state_at(conn: sqlite3.Connection, record_id: str, version: int) -> dict:
    """Reconstruct record data at a version by folding the 'after' values of the log."""
    state: dict = {}
    rows = conn.execute(
        "SELECT after FROM change_log WHERE record_id = ? AND version <= ? ORDER BY version",
        (record_id, version),
    )
    for r in rows:
        state.update(json.loads(r["after"]))
    return state


def _merged_with(conn: sqlite3.Connection, record_id: str, base: int, device: str) -> list[dict]:
    """Changes by OTHER devices after base_version (what this change was merged on top of)."""
    rows = conn.execute(
        "SELECT version, device_id, after FROM change_log "
        "WHERE record_id = ? AND version > ? AND device_id != ? ORDER BY version",
        (record_id, base, device),
    )
    return [
        {"version": r["version"], "device_id": r["device_id"], "fields": sorted(json.loads(r["after"]))}
        for r in rows
    ]


def _conflict_details(conn, req: dict, data: dict, meta: dict, fields: list[str]) -> list[dict]:
    base_state = state_at(conn, req["record_id"], req["base_version"])
    return [
        {
            "field": f,
            "base_value": base_state.get(f),
            "server_value": data[f],
            "client_value": req["changes"][f]["set"],
            "server_changed_in_version": meta[f]["version"],
            "server_changed_by": meta[f]["device_id"],
        }
        for f in fields
    ]


# ---------- the entry point ----------

def apply_change(conn: sqlite3.Connection, req: dict) -> Result:
    err = merge.validate_changes(req["op"], req["changes"])
    if err:
        return _reject(422, "VALIDATION_ERROR", req, detail=err)

    with transaction(conn):
        # 1. idempotency
        stored = conn.execute(
            "SELECT payload_hash, status_code, response FROM requests WHERE request_id = ?",
            (req["request_id"],),
        ).fetchone()
        if stored:
            if stored["payload_hash"] != _hash(req):
                return _reject(422, "REQUEST_ID_REUSED", req)
            body = json.loads(stored["response"])
            body["replayed"] = True
            return stored["status_code"], body

        # 2. per-device sequence
        dev = conn.execute(
            "SELECT last_sequence FROM devices WHERE device_id = ?", (req["device_id"],)
        ).fetchone()
        expected = (dev["last_sequence"] if dev else 0) + 1
        if req["sequence"] > expected:
            return _reject(409, "OUT_OF_ORDER", req, expected_sequence=expected)
        if req["sequence"] < expected:
            return _reject(409, "STALE_SEQUENCE", req, expected_sequence=expected)

        row = conn.execute("SELECT * FROM records WHERE id = ?", (req["record_id"],)).fetchone()
        handler = {"create": _create, "update": _update, "delete": _delete}[req["op"]]
        return handler(conn, req, row)


# ---------- operations ----------

def _create(conn, req: dict, row: Optional[sqlite3.Row]) -> Result:
    if row is not None:
        body = {
            "outcome": "CONFLICT",
            "reason": "ALREADY_EXISTS",
            "record_id": req["record_id"],
            "current_version": row["version"],
            "record": _row_view(row),
        }
        return _finish(conn, req, 409, body, advance=False)

    data = merge.new_defaults()
    try:
        for f, spec in req["changes"].items():
            data[f] = merge.apply_field(f, data[f], spec)
    except ValueError as e:
        return _reject(422, "VALIDATION_ERROR", req, detail=str(e))

    meta = {f: {"version": 1, "device_id": req["device_id"]} for f in req["changes"]}
    now = _now()
    conn.execute(
        "INSERT INTO records (id, data, version, field_meta, created_at, updated_at) VALUES (?, ?, 1, ?, ?, ?)",
        (req["record_id"], json.dumps(data), json.dumps(meta), now, now),
    )
    _log(conn, req, 1, "create", data)
    body = {
        "outcome": "ACCEPTED",
        "record_id": req["record_id"],
        "version": 1,
        "applied_fields": sorted(req["changes"]),
        "record": _view(req["record_id"], 1, data),
    }
    return _finish(conn, req, 201, body, advance=True)


def _update(conn, req: dict, row: Optional[sqlite3.Row]) -> Result:
    if row is None:
        return _reject(404, "UNKNOWN_RECORD", req)

    rid, device, base = req["record_id"], req["device_id"], req["base_version"]
    data, meta, current = json.loads(row["data"]), json.loads(row["field_meta"]), row["version"]

    if data["deleted"]:
        body = {
            "outcome": "CONFLICT", "reason": "RECORD_DELETED", "record_id": rid,
            "current_version": current, "record": _row_view(row),
        }
        return _finish(conn, req, 409, body, advance=False)
    if base > current:
        return _reject(422, "FUTURE_BASE_VERSION", req, current_version=current)

    changes = req["changes"]

    # Conflict detection: scalar fields only. Sets and counters are commutative.
    conflicts = []
    for f, spec in changes.items():
        if merge.SCHEMA[f] != "scalar":
            continue
        m = meta.get(f)
        if m and m["version"] > base and m["device_id"] != device and data[f] != spec["set"]:
            conflicts.append(f)

    clean = {f: s for f, s in changes.items() if f not in conflicts}
    try:
        new_vals = {f: merge.apply_field(f, data[f], s) for f, s in clean.items()}
    except ValueError as e:
        return _reject(422, "VALIDATION_ERROR", req, detail=str(e))
    effective = {f: v for f, v in new_vals.items() if v != data[f]}

    # All-or-nothing by default; partial mode only proceeds if something clean remains.
    if conflicts and not (req["partial"] and effective):
        body = {
            "outcome": "CONFLICT", "reason": "FIELD_CONFLICT", "record_id": rid,
            "current_version": current,
            "conflicts": _conflict_details(conn, req, data, meta, conflicts),
            "record": _row_view(row),
        }
        return _finish(conn, req, 409, body, advance=False)

    if not effective:  # nothing to change: accepted no-op, no version bump
        body = {
            "outcome": "ACCEPTED", "noop": True, "record_id": rid, "version": current,
            "applied_fields": [], "record": _row_view(row),
        }
        return _finish(conn, req, 200, body, advance=True)

    merged_with = _merged_with(conn, rid, base, device)
    new_version = current + 1
    data.update(effective)
    for f in effective:
        meta[f] = {"version": new_version, "device_id": device}
    conn.execute(
        "UPDATE records SET data = ?, version = ?, field_meta = ?, updated_at = ? WHERE id = ?",
        (json.dumps(data), new_version, json.dumps(meta), _now(), rid),
    )
    _log(conn, req, new_version, "update", effective)

    body = {
        "outcome": "MERGED" if merged_with else "ACCEPTED",
        "record_id": rid,
        "version": new_version,
        "applied_fields": sorted(effective),
        "merged_with": merged_with,
        "record": _view(rid, new_version, data),
    }
    if conflicts:  # partial mode with leftovers
        body["partial"] = True
        body["needs_resolution"] = True
        # conflicting fields were not applied, so data/meta still hold the server's values for them
        body["conflicts"] = _conflict_details(conn, req, data, meta, conflicts)
    return _finish(conn, req, 200, body, advance=True)


def _delete(conn, req: dict, row: Optional[sqlite3.Row]) -> Result:
    if row is None:
        return _reject(404, "UNKNOWN_RECORD", req)

    rid, device, base = req["record_id"], req["device_id"], req["base_version"]
    data, meta, current = json.loads(row["data"]), json.loads(row["field_meta"]), row["version"]

    if base > current:
        return _reject(422, "FUTURE_BASE_VERSION", req, current_version=current)
    if data["deleted"]:
        body = {
            "outcome": "ACCEPTED", "noop": True, "record_id": rid, "version": current,
            "applied_fields": [], "record": _row_view(row),
        }
        return _finish(conn, req, 200, body, advance=True)

    edited = sorted(
        f for f, m in meta.items() if m["version"] > base and m["device_id"] != device
    )
    if edited:
        body = {
            "outcome": "CONFLICT", "reason": "EDITED_SINCE_BASE", "record_id": rid,
            "current_version": current,
            "conflicts": [
                {"field": f, "server_changed_in_version": meta[f]["version"],
                 "server_changed_by": meta[f]["device_id"]}
                for f in edited
            ],
            "record": _row_view(row),
        }
        return _finish(conn, req, 409, body, advance=False)

    merged_with = _merged_with(conn, rid, base, device)
    new_version = current + 1
    data["deleted"] = True
    meta["deleted"] = {"version": new_version, "device_id": device}
    conn.execute(
        "UPDATE records SET data = ?, version = ?, field_meta = ?, updated_at = ? WHERE id = ?",
        (json.dumps(data), new_version, json.dumps(meta), _now(), rid),
    )
    _log(conn, req, new_version, "delete", {"deleted": True})
    body = {
        "outcome": "MERGED" if merged_with else "ACCEPTED",
        "record_id": rid,
        "version": new_version,
        "applied_fields": ["deleted"],
        "merged_with": merged_with,
        "record": _view(rid, new_version, data),
    }
    return _finish(conn, req, 200, body, advance=True)