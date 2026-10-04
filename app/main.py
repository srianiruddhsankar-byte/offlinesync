"""FastAPI routes. All sync logic lives in engine.py; this file maps it to HTTP."""

import json
import logging
import os
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, Depends, FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .db import connect, init_db
from .docs import (
    APP_DESCRIPTION,
    BATCH_EXAMPLES,
    RESTORE_EXAMPLES,
    RID,
    SYNC_EXAMPLES,
    SYNC_RESPONSES,
    TAGS_METADATA,
)
from .engine import apply_change, get_history, pull_changes, restore_version, state_at
from .schemas import BatchRequest, RestoreRequest, SyncRequest

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("sync")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(
    title="Offline Sync Conflict API",
    description=APP_DESCRIPTION,
    version="1.0.0",
    openapi_tags=TAGS_METADATA,
    lifespan=lifespan,
)


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def _respond(status: int, body: dict[str, Any]) -> JSONResponse:
    """JSON response; replayed answers carry an Idempotent-Replay header."""
    headers = {"Idempotent-Replay": "true"} if body.get("replayed") else None
    return JSONResponse(status_code=status, content=body, headers=headers)


def _log_result(device: str, status: int, body: dict[str, Any]) -> None:
    logger.info(
        "device=%s record=%s outcome=%s%s status=%s version=%s replayed=%s",
        device,
        body.get("record_id"),
        body.get("outcome"),
        f"/{body['reason']}" if body.get("reason") else "",
        status,
        body.get("version", body.get("current_version")),
        body.get("replayed"),
    )


# ---------------------------------------------------------------- error handling


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [{"loc": [str(p) for p in e["loc"]], "msg": e["msg"]} for e in exc.errors()]
    return JSONResponse(
        status_code=422,
        content={"outcome": "REJECTED", "reason": "VALIDATION_ERROR", "errors": errors},
    )


@app.exception_handler(sqlite3.OperationalError)
async def database_busy_handler(request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
    """A locked or unavailable database is temporary: tell the client to retry."""
    logger.warning("database error on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={
            "outcome": "REJECTED",
            "reason": "DATABASE_BUSY",
            "detail": "temporarily unavailable, retry",
        },
        headers={"Retry-After": "1"},
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"outcome": "REJECTED", "reason": "INTERNAL_ERROR", "detail": "unexpected server error"},
    )


# ---------------------------------------------------------------- routes


@app.get("/health", tags=["System"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/records/{record_id}/sync", tags=["Sync"], summary="Apply one change", responses=SYNC_RESPONSES)
def sync_one(
    record_id: str = Path(
        description="UUID of the record. Must match `record_id` in the body.", examples=[RID]
    ),
    req: SyncRequest = Body(openapi_examples=SYNC_EXAMPLES),
    conn: sqlite3.Connection = Depends(get_conn),
) -> JSONResponse:
    """Apply one change from one device.

    - `base_version` is the record version the device last saw.
    - `sequence` numbers a device's operations (1, 2, 3, ...). A conflict does not consume a sequence number.
    - Retrying with the same `request_id` is safe and returns the original result.
    - `partial: true` applies the non-conflicting fields of an update and reports the rest.
    """
    if req.record_id != record_id:
        return JSONResponse(
            status_code=422,
            content={
                "outcome": "REJECTED",
                "reason": "VALIDATION_ERROR",
                "detail": "record_id in body does not match the URL",
            },
        )
    status, body = apply_change(conn, req.model_dump())
    _log_result(req.device_id, status, body)
    return _respond(status, body)


@app.post("/sync", tags=["Sync"], summary="Apply a batch of changes, in order")
def sync_batch(
    batch: BatchRequest = Body(openapi_examples=BATCH_EXAMPLES),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Apply up to 100 changes in order. Always returns 200 if the envelope is valid;
    each result carries its own `status` and outcome. Items succeed or fail independently."""
    results = []
    for item in batch.changes:
        status, body = apply_change(conn, item.model_dump())
        _log_result(item.device_id, status, body)
        results.append({"status": status, **body})
    return {"results": results}


@app.get("/sync/changes", tags=["Pull"], summary="Pull changes after a cursor")
def pull(
    since: int = Query(0, ge=0, description="Return changes with change_id greater than this."),
    limit: int = Query(100, ge=1, le=500),
    exclude_device: str | None = Query(None, description="Skip changes made by this device."),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    return pull_changes(conn, since, limit, exclude_device)


@app.get("/records/{record_id}", tags=["Records"], summary="Current state of a record")
def get_record(record_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
    row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        return JSONResponse(status_code=404, content={"reason": "UNKNOWN_RECORD", "record_id": record_id})
    return {"id": row["id"], "version": row["version"], "data": json.loads(row["data"])}


@app.get("/records", tags=["Records"], summary="List records")
def list_records(
    include_deleted: bool = False, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    rows = conn.execute("SELECT * FROM records ORDER BY created_at").fetchall()
    out = []
    for r in rows:
        data = json.loads(r["data"])
        if data["deleted"] and not include_deleted:
            continue
        out.append({"id": r["id"], "version": r["version"], "data": data})
    return {"records": out}


@app.get("/records/{record_id}/history", tags=["History"], summary="Change history of a record")
def history(record_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
    changes = get_history(conn, record_id)
    if changes is None:
        return JSONResponse(status_code=404, content={"reason": "UNKNOWN_RECORD", "record_id": record_id})
    return {"record_id": record_id, "history": changes}


@app.get(
    "/records/{record_id}/versions/{version}", tags=["History"], summary="Record state at a past version"
)
def version_view(record_id: str, version: int, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
    row = conn.execute("SELECT version FROM records WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        return JSONResponse(status_code=404, content={"reason": "UNKNOWN_RECORD", "record_id": record_id})
    if version < 1 or version > row["version"]:
        return JSONResponse(
            status_code=404, content={"reason": "UNKNOWN_VERSION", "current_version": row["version"]}
        )
    return {"id": record_id, "version": version, "data": state_at(conn, record_id, version)}


@app.post(
    "/records/{record_id}/restore/{version}",
    tags=["History"],
    summary="Restore an earlier version as a new version",
)
def restore(
    record_id: str = Path(examples=[RID]),
    version: int = Path(description="The earlier version whose content to restore.", examples=[6]),
    req: RestoreRequest = Body(openapi_examples=RESTORE_EXAMPLES),
    conn: sqlite3.Connection = Depends(get_conn),
) -> JSONResponse:
    """Writes the content of an earlier version as a NEW version; history is never rewritten.
    `base_version` must equal the current version (otherwise 409). Can un-delete a record."""
    status, body = restore_version(
        conn,
        {
            "request_id": req.request_id,
            "device_id": req.device_id,
            "record_id": record_id,
            "target_version": version,
            "base_version": req.base_version,
        },
    )
    _log_result(req.device_id, status, body)
    return _respond(status, body)
