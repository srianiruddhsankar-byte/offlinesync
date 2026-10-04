"""Chaos test: simulated devices make random edits while the network duplicates requests,
loses responses and reorders operations. After thousands of operations we check invariants
against an independent reference model.

Run as a test:        pytest tests/test_chaos.py -s
Run the big version:  python -m tests.test_chaos            (prints the headline number)
Scale it up:          CHAOS_STEPS=20000 pytest tests/test_chaos.py -s
"""

import json
import os
import random
import tempfile
import threading
import uuid
from collections import Counter
from copy import deepcopy

import pytest

from app import merge
from app.db import connect, init_db
from app.engine import apply_change, pull_changes, restore_version, state_at

SCALARS = ("title", "body", "status")
TITLES = ["alpha", "beta", "gamma", "delta"]
TAGS = ["a", "b", "c", "d", "e", "f"]


# ---------------------------------------------------------------- reference model


def ref_apply(state: dict, changes: dict) -> dict:
    """Independent re-implementation of the intended merge semantics (not the engine's code)."""
    s = deepcopy(state)
    for f, spec in changes.items():
        if f in SCALARS:
            s[f] = spec["set"]
        elif f == "tags":
            s["tags"] = sorted((set(s["tags"]) | set(spec.get("add", []))) - set(spec.get("remove", [])))
        elif f == "views":
            s["views"] += spec["increment"]
    return s


class Model:
    """Tracks what every record should look like at every version, fed by what the server accepted."""

    def __init__(self):
        self.states: dict[str, list] = {}
        self.applied = 0

    def observe(self, kind: str, req: dict, res: dict) -> None:
        if res.get("replayed") or res.get("noop") or res["outcome"] not in ("ACCEPTED", "MERGED"):
            return
        rid, version = res["record_id"], res["version"]
        if kind == "create":
            new = ref_apply(merge.new_defaults(), req["changes"])
            self.states[rid] = [None, new]
        else:
            history = self.states[rid]
            prev = history[-1]
            if kind == "update":
                new = ref_apply(prev, {f: req["changes"][f] for f in res["applied_fields"]})
            elif kind == "delete":
                new = {**prev, "deleted": True}
            else:  # restore
                new = deepcopy(history[req["target_version"]])
            history.append(new)
        assert version == len(self.states[rid]) - 1, f"version gap for {rid}: {res}"
        assert res["record"]["data"] == self.states[rid][-1], f"server response disagrees with model: {res}"
        self.applied += 1


# ---------------------------------------------------------------- network + devices


class Net:
    def __init__(self, conn, model: Model | None, stats: Counter):
        self.conn, self.model, self.stats = conn, model, stats

    def _count(self, res: dict) -> None:
        self.stats["requests"] += 1
        self.stats[res["outcome"]] += 1
        if res.get("reason"):
            self.stats[res["reason"]] += 1
        if res.get("replayed"):
            self.stats["replayed"] += 1

    def deliver(self, req: dict):
        status, res = apply_change(self.conn, req)
        self._count(res)
        if self.model:
            self.model.observe(req["op"], req, res)
        return status, res

    def restore(self, body: dict):
        status, res = restore_version(self.conn, body)
        self._count(res)
        if self.model:
            self.model.observe("restore", body, res)
        return status, res


class Device:
    def __init__(self, name: str, rng: random.Random, net: Net, allow_restore: bool = True):
        self.name, self.rng, self.net, self.allow_restore = name, rng, net, allow_restore
        self.replica: dict[str, dict] = {}
        self.queue: list[dict] = []
        self.next_seq = 1
        self.cursor = 0
        self.maybe_applied = False  # a response was lost: the server may already have this op

    # ----- generating work
    def random_changes(self) -> dict:
        changes = {}
        for f in self.rng.sample(["title", "body", "status", "tags", "views"], self.rng.randint(1, 3)):
            if f == "title" or f == "body":
                changes[f] = {"set": self.rng.choice(TITLES)}
            elif f == "status":
                changes[f] = {"set": self.rng.choice(sorted(merge.STATUSES))}
            elif f == "tags":
                add = self.rng.sample(TAGS, self.rng.randint(0, 2))
                remove = self.rng.sample([t for t in TAGS if t not in add], self.rng.randint(0, 2))
                if add or remove:
                    changes[f] = {"add": add, "remove": remove}
            else:
                changes[f] = {"increment": self.rng.randint(-3, 5)}
        return changes or {"views": {"increment": 1}}

    def new_op(self, rid, kind, base, changes):
        return {
            "rid": rid,
            "op": kind,
            "base": base,
            "changes": changes,
            "partial": kind == "update" and self.rng.random() < 0.3,
            "request_id": str(uuid.uuid4()),
        }

    def build(self, op, sequence=None, request_id=None) -> dict:
        return {
            "request_id": request_id or op["request_id"],
            "device_id": self.name,
            "sequence": sequence if sequence is not None else self.next_seq,
            "record_id": op["rid"],
            "op": op["op"],
            "base_version": op["base"],
            "partial": op["partial"],
            "changes": op["changes"],
        }

    def adopt(self, record: dict) -> None:
        self.replica[record["id"]] = {"version": record["version"], "data": deepcopy(record["data"])}

    # ----- actions
    def step(self) -> None:
        r = self.rng.random()
        if r < 0.38:
            self.edit()
        elif r < 0.46:
            self.create()
        elif r < 0.80:
            self.sync_step()
        elif r < 0.90:
            self.pull()
        elif r < 0.93:
            self.inject_out_of_order()
        elif r < 0.96:
            self.delete()
        else:
            self.restore()

    def create(self) -> None:
        rid = str(uuid.uuid4())
        changes = self.random_changes()
        self.replica[rid] = {"version": 1, "data": ref_apply(merge.new_defaults(), changes)}
        self.queue.append(self.new_op(rid, "create", 0, changes))

    def edit(self) -> None:
        if not self.replica:
            return self.create()
        rid = self.rng.choice(list(self.replica))
        self.queue.append(self.new_op(rid, "update", self.replica[rid]["version"], self.random_changes()))

    def delete(self) -> None:
        if self.replica:
            rid = self.rng.choice(list(self.replica))
            self.queue.append(self.new_op(rid, "delete", self.replica[rid]["version"], {}))

    def pull(self) -> bool:
        page = pull_changes(self.net.conn, self.cursor, 50, None)
        for c in page["changes"]:
            rec = self.replica.setdefault(c["record_id"], {"version": 0, "data": {}})
            rec["data"].update(c["fields"])
            rec["version"] = c["version"]
        self.cursor = page["next_cursor"]
        return page["has_more"]

    def inject_out_of_order(self) -> None:
        """Send the SECOND queued op first. The server must refuse it and change nothing."""
        if len(self.queue) < 2 or self.maybe_applied:
            return
        body = self.build(self.queue[1], sequence=self.next_seq + 1, request_id=str(uuid.uuid4()))
        status, res = self.net.deliver(body)
        assert status == 409 and res["reason"] == "OUT_OF_ORDER", res

    def restore(self) -> None:
        if not self.allow_restore or not self.replica:
            return
        rid = self.rng.choice(list(self.replica))
        row = self.net.conn.execute("SELECT version FROM records WHERE id = ?", (rid,)).fetchone()
        if row is None:
            return
        version = row[0]
        stale = version > 1 and self.rng.random() < 0.25
        body = {
            "request_id": str(uuid.uuid4()),
            "device_id": self.name,
            "record_id": rid,
            "target_version": self.rng.randint(1, version),
            "base_version": version - 1 if stale else version,
        }
        status, res = self.net.restore(body)
        if stale:
            assert status == 409 and res["reason"] == "STALE_BASE", res
        else:
            assert status == 200 and res["outcome"] == "ACCEPTED", res
            self.adopt(res["record"])

    def sync_step(self, reliable: bool = False) -> None:
        if not self.queue:
            return
        op = self.queue[0]
        body = self.build(op)
        status, res = self.net.deliver(body)

        if not reliable and res["outcome"] in ("ACCEPTED", "MERGED", "CONFLICT") and self.rng.random() < 0.15:
            s2, r2 = self.net.deliver(body)  # duplicate delivery must replay, never re-apply
            assert r2["replayed"] is True and s2 == status and r2["outcome"] == res["outcome"], (res, r2)
            assert r2.get("version") == res.get("version")
        if not reliable and self.rng.random() < 0.2:  # response lost: client keeps the op and retries later
            self.maybe_applied = True
            return
        self.maybe_applied = False
        self.handle(op, res)

    def handle(self, op: dict, res: dict) -> None:
        outcome = res["outcome"]
        if outcome in ("ACCEPTED", "MERGED"):
            self.queue.pop(0)
            self.next_seq += 1
            self.adopt(res["record"])
            if res.get("needs_resolution"):
                follow = self.resolution(op, res["conflicts"], res["version"])
                if follow:
                    self.queue.insert(0, follow)
        elif outcome == "CONFLICT":
            self.adopt(res["record"])
            if res["reason"] == "FIELD_CONFLICT":
                follow = self.resolution(op, res["conflicts"], res["current_version"])
                if follow:
                    self.queue[0] = follow
                else:
                    self.queue.pop(0)  # nothing left to send; its sequence slot was never used
            else:
                self.queue.pop(0)  # record deleted / delete vs edit: give up on this op
        else:
            raise AssertionError(f"unexpected response: {res}")

    def resolution(self, op: dict, conflicts: list, current_version: int) -> dict | None:
        fields = {c["field"] for c in conflicts}
        changes = {f: s for f, s in op["changes"].items() if f not in fields}
        for c in conflicts:
            if self.rng.random() < 0.5:  # user picks "mine"; otherwise keeps the server's value
                changes[c["field"]] = {"set": c["client_value"]}
        if not changes:
            return None
        return self.new_op(op["rid"], "update", current_version, changes)


# ---------------------------------------------------------------- invariants


def check_invariants(conn, model: Model | None, devices: list, check_counters: bool = False) -> None:
    ids = {r["id"] for r in conn.execute("SELECT id FROM records")}
    if model:
        assert set(model.states) == ids, "model and server disagree on which records exist"
        total = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
        assert total == model.applied, f"{total} log rows but {model.applied} accepted changes"

    dupes = conn.execute(
        "SELECT request_id FROM change_log GROUP BY request_id HAVING COUNT(*) > 1"
    ).fetchall()
    assert not dupes, "a request was applied more than once"

    for rid in ids:
        row = conn.execute("SELECT * FROM records WHERE id = ?", (rid,)).fetchone()
        data, meta, version = json.loads(row["data"]), json.loads(row["field_meta"]), row["version"]
        log = conn.execute("SELECT * FROM change_log WHERE record_id = ? ORDER BY version", (rid,)).fetchall()

        # 1. versions are contiguous 1..n
        assert [e["version"] for e in log] == list(range(1, version + 1)), f"version gap in {rid}"
        # 2. folding the log reproduces current data
        assert state_at(conn, rid, version) == data, f"log fold != data for {rid}"

        # 3 + 7. field_meta matches history, and no stale scalar write overwrote a newer one
        running: dict = {}
        last: dict = {}
        for e in log:
            fields = json.loads(e["after"])
            if e["op"] == "update":
                for f, val in fields.items():
                    if f in SCALARS:
                        lt = last.get(f)
                        assert not (
                            lt and lt[0] > e["base_version"] and lt[1] != e["device_id"] and running[f] != val
                        ), f"stale overwrite of '{f}' in {rid} at v{e['version']} by {e['device_id']}"
            running.update(fields)
            # a create stores the full record but only the fields it actually set count as "written"
            touched = json.loads(e["ops"]) if e["op"] == "create" else fields
            for f in touched:
                last[f] = (e["version"], e["device_id"])
        assert meta == {f: {"version": v, "device_id": d} for f, (v, d) in last.items()}, (
            f"field_meta drift in {rid}"
        )

        # 6. independent reference model agrees at every version
        if model:
            states = model.states[rid]
            assert len(states) - 1 == version
            for v in range(1, version + 1):
                assert states[v] == state_at(conn, rid, v), f"{rid} differs from model at v{v}"

        # counter conservation (only valid when there are no restores)
        if check_counters:
            total = sum(
                json.loads(e["ops"])["views"]["increment"]
                for e in log
                if e["op"] in ("create", "update") and "views" in json.loads(e["ops"])
            )
            assert data["views"] == total, f"counter drift in {rid}: {data['views']} vs {total}"

    # 8. after a full sync every device replica equals the server
    for d in devices:
        assert set(d.replica) == ids, f"{d.name} replica is missing records"
        for rid, rec in d.replica.items():
            row = conn.execute("SELECT data, version FROM records WHERE id = ?", (rid,)).fetchone()
            assert rec["version"] == row["version"] and rec["data"] == json.loads(row["data"]), (
                f"{d.name} replica of {rid} diverged"
            )


def flush(devices: list, rng: random.Random) -> None:
    """Deliver everything still queued (reliable network), then let every device pull to the end."""
    for _ in range(50_000):
        active = [d for d in devices if d.queue]
        if not active:
            break
        rng.choice(active).sync_step(reliable=True)
    else:
        raise AssertionError("queues never drained")
    for d in devices:
        while d.pull():
            pass


# ---------------------------------------------------------------- runners


def run_single(seed: int, steps: int, n_devices: int = 4) -> Counter:
    rng, stats, model = random.Random(seed), Counter(), Model()
    conn = connect()
    conn.execute("PRAGMA synchronous=OFF")
    net = Net(conn, model, stats)
    devices = [Device(f"dev{i}", random.Random(rng.random()), net) for i in range(n_devices)]
    for _ in range(steps):
        rng.choice(devices).step()
    flush(devices, rng)
    check_invariants(conn, model, devices)
    conn.close()
    return stats


def run_threaded(seed: int, steps: int, n_threads: int = 4) -> Counter:
    stats = Counter()
    devices = [
        Device(f"dev{i}", random.Random(seed * 100 + i), Net(None, None, stats), allow_restore=False)
        for i in range(n_threads)
    ]
    errors: list = []

    def worker(d: Device) -> None:
        try:
            conn = connect()
            conn.execute("PRAGMA synchronous=OFF")
            d.net.conn = conn
            for _ in range(steps):
                d.step()
            conn.close()
        except BaseException as e:  # noqa: BLE001 - surface any failure from a thread
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(d,)) for d in devices]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors[0]

    conn = connect()
    for d in devices:
        d.net.conn = conn
    flush(devices, random.Random(seed))
    check_invariants(conn, None, devices, check_counters=True)
    conn.close()
    return stats


# ---------------------------------------------------------------- tests


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_chaos_with_reference_model(db_env, seed):
    stats = run_single(seed, int(os.environ.get("CHAOS_STEPS", "2500")))
    print(
        f"\nseed {seed}: {stats['requests']} requests, 0 invariant violations "
        f"(merged={stats['MERGED']}, conflicts={stats['CONFLICT']}, replays={stats['replayed']}, "
        f"out_of_order={stats['OUT_OF_ORDER']})"
    )
    # make sure the chaos actually exercised the hard paths
    assert stats["MERGED"] and stats["CONFLICT"] and stats["replayed"] and stats["OUT_OF_ORDER"]


def test_chaos_concurrent_devices(db_env):
    stats = run_threaded(seed=7, steps=400)
    print(f"\nconcurrent: {stats['requests']} requests from 4 threads, 0 invariant violations")
    assert stats["MERGED"] and stats["CONFLICT"]


if __name__ == "__main__":
    total, seeds = 0, range(1, 6)
    steps = int(os.environ.get("CHAOS_STEPS", "4000"))
    for s in seeds:
        os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), f"chaos{s}.db")
        init_db()
        stats = run_single(s, steps)
        total += stats["requests"]
        print(f"seed {s}: {stats['requests']} requests ok")
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "chaos_threads.db")
    init_db()
    t = run_threaded(seed=7, steps=800)
    total += t["requests"]
    print(f"concurrent run: {t['requests']} requests ok")
    print(f"\n{total} randomized operations, 0 invariant violations (seeds {list(seeds)} + concurrent run)")
