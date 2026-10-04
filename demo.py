"""Run the sync story end to end:  python demo.py

By default it runs in-process against a temporary database (no server needed).
To hit a running server instead:  BASE_URL=http://localhost:8000 python demo.py
"""

import os
import tempfile
import uuid

BASE_URL = os.environ.get("BASE_URL")
os.environ.setdefault("LOG_LEVEL", "WARNING")  # keep the walkthrough output clean

if BASE_URL:
    import httpx

    client = httpx.Client(base_url=BASE_URL, timeout=10)
else:
    os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "demo.db")
    from fastapi.testclient import TestClient

    from app.db import init_db
    from app.main import app

    init_db()
    client = TestClient(app)

RID = str(uuid.uuid4())
seq = {}


def title(text):
    print(f"\n\033[1m=== {text} ===\033[0m")


def send(device, op, base, changes=None, partial=False, request_id=None, new_sequence=True):
    n = seq.get(device, 0) + 1
    body = {
        "request_id": request_id or str(uuid.uuid4()),
        "device_id": device,
        "sequence": n,
        "record_id": RID,
        "op": op,
        "base_version": base,
        "partial": partial,
        "changes": changes or {},
    }
    r = client.post(f"/records/{RID}/sync", json=body)
    j = r.json()
    if r.status_code in (200, 201):
        seq[device] = n
    line = f"{device:7} -> HTTP {r.status_code}  {j['outcome']}"
    if j.get("reason"):
        line += f" ({j['reason']})"
    if "version" in j:
        line += f"  now at version {j['version']}"
    if j.get("replayed"):
        line += "  [replayed: same answer, nothing applied twice]"
    print(line)
    for c in j.get("conflicts", []):
        if "client_value" in c:
            print(
                f"          field '{c['field']}': base={c['base_value']!r}  "
                f"server={c['server_value']!r} (by {c['server_changed_by']})  "
                f"yours={c['client_value']!r}"
            )
    return j, body


def show():
    d = client.get(f"/records/{RID}").json()
    print(
        f"          record is now: version {d['version']}, "
        f"title={d['data']['title']!r}, status={d['data']['status']!r}, "
        f"tags={d['data']['tags']}, views={d['data']['views']}"
    )


title("1. Laptop creates a record")
send("laptop", "create", 0, {"title": {"set": "Demo"}, "status": {"set": "TODO"}})
show()

title("2. Laptop sets status to DONE (phone has not seen this yet)")
send("laptop", "update", 1, {"status": {"set": "DONE"}})
show()

title("3. Phone, still on version 1, sets status differently -> CONFLICT")
send("phone", "update", 1, {"status": {"set": "IN_PROGRESS"}})
show()

title("4. Phone edits a DIFFERENT field instead -> MERGED")
_, body = send("phone", "update", 1, {"title": {"set": "Phone title"}})
show()

title("5. Phone's request is sent again (lost response / retry) -> replayed")
r = client.post(f"/records/{RID}/sync", json=body)
j = r.json()
print(
    f"phone   -> HTTP {r.status_code}  {j['outcome']}  replayed={j['replayed']}  version still {j['version']}"
)

title("6. Both devices change tags and a counter offline -> everything is kept")
send("laptop", "update", 3, {"tags": {"add": ["work"]}, "views": {"increment": 3}})
send("phone", "update", 3, {"tags": {"add": ["urgent"]}, "views": {"increment": 5}})
show()

title("7. Partial mode: one clean field + one conflicting field")
send("laptop", "update", 5, {"status": {"set": "TODO"}})
send("phone", "update", 5, {"body": {"set": "notes"}, "status": {"set": "DONE"}}, partial=True)
show()

title("8. Phone sends an operation with a gap in its sequence -> OUT_OF_ORDER")
n = seq["phone"] + 2
body = {
    "request_id": str(uuid.uuid4()),
    "device_id": "phone",
    "sequence": n,
    "record_id": RID,
    "op": "update",
    "base_version": 7,
    "changes": {"title": {"set": "x"}},
}
r = client.post(f"/records/{RID}/sync", json=body)
j = r.json()
print(
    f"phone   -> HTTP {r.status_code}  {j['outcome']} ({j['reason']}), "
    f"server expects sequence {j['expected_sequence']}"
)

title("9. Full change history of the record")
for c in client.get(f"/records/{RID}/history").json()["history"]:
    print(f"          v{c['version']:<2} {c['device_id']:7} {c['op']:7} {c['fields']}")

title("10. Restore version 3 (as a NEW version; history is kept)")
current = client.get(f"/records/{RID}").json()["version"]
r = client.post(
    f"/records/{RID}/restore/3",
    json={"request_id": str(uuid.uuid4()), "device_id": "laptop", "base_version": current},
)
j = r.json()
print(
    f"laptop  -> HTTP {r.status_code}  {j['outcome']}  "
    f"restored version {j['restored_from']} as version {j['version']}"
)
show()

title("11. Pull: what changed on the server since a device last synced")
page = client.get("/sync/changes", params={"since": 0, "limit": 3}).json()
print(
    f"first page: {len(page['changes'])} changes, has_more={page['has_more']}, "
    f"next_cursor={page['next_cursor']}"
)
rest = client.get("/sync/changes", params={"since": page["next_cursor"], "exclude_device": "laptop"}).json()
print(f"later changes made by other devices than the laptop: {len(rest['changes'])}")

print("\nDone. Nothing was silently overwritten.")
