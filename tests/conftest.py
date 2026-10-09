import uuid

import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    """Writes are open unless a test sets SYNC_API_KEY itself."""
    monkeypatch.delenv("SYNC_API_KEY", raising=False)


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    init_db()


@pytest.fixture
def client(db_env):
    with TestClient(app) as c:
        yield c


class Device:
    """Tiny helper that tracks a device's sequence number."""

    def __init__(self, client, device_id):
        self.client, self.device_id, self.seq = client, device_id, 0

    def send(self, record_id, op, base_version, changes=None, partial=False, request_id=None, sequence=None):
        body = {
            "request_id": request_id or str(uuid.uuid4()),
            "device_id": self.device_id,
            "sequence": sequence if sequence is not None else self.seq + 1,
            "record_id": record_id,
            "op": op,
            "base_version": base_version,
            "partial": partial,
            "changes": changes or {},
        }
        r = self.client.post(f"/records/{record_id}/sync", json=body)
        if r.status_code in (200, 201) and sequence is None:
            self.seq += 1
        return r, body


@pytest.fixture
def laptop(client):
    return Device(client, "laptop")


@pytest.fixture
def phone(client):
    return Device(client, "phone")


@pytest.fixture
def rid():
    return str(uuid.uuid4())
