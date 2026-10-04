import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .db import connect, init_db
from .engine import apply_change
from .schemas import BatchRequest, SyncRequest


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Offline Sync Conflict API",
    description="Synchronizes changes from multiple devices, detecting conflicts instead of overwriting data.",
    version="0.1.0",
    lifespan=lifespan,
)


def get_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    errors = [{"loc": [str(p) for p in e["loc"]], "msg": e["msg"]} for e in exc.errors()]
    return JSONResponse(
        status_code=422,
        content={"outcome": "REJECTED", "reason": "VALIDATION_ERROR", "errors": errors},
    )


@app.get("/health", tags=["System"])
def health():
    return {"status": "ok"}


@app.post("/records/{record_id}/sync", tags=["Sync"], summary="Apply one change")
def sync_one(record_id: str, req: SyncRequest, conn=Depends(get_conn)):
    if req.record_id != record_id:
        return JSONResponse(
            status_code=422,
            content={"outcome": "REJECTED", "reason": "VALIDATION_ERROR",
                     "detail": "record_id in body does not match the URL"},
        )
    status, body = apply_change(conn, req.model_dump())
    return JSONResponse(status_code=status, content=body)


@app.post("/sync", tags=["Sync"], summary="Apply a batch of changes, in order")
def sync_batch(batch: BatchRequest, conn=Depends(get_conn)):
    results = []
    for item in batch.changes:
        status, body = apply_change(conn, item.model_dump())
        results.append({"status": status, **body})
    return {"results": results}


@app.get("/records/{record_id}", tags=["Records"], summary="Current state of a record")
def get_record(record_id: str, conn=Depends(get_conn)):
    row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        return JSONResponse(status_code=404, content={"reason": "UNKNOWN_RECORD", "record_id": record_id})
    return {"id": row["id"], "version": row["version"], "data": json.loads(row["data"])}


@app.get("/records", tags=["Records"], summary="List records")
def list_records(include_deleted: bool = False, conn=Depends(get_conn)):
    rows = conn.execute("SELECT * FROM records ORDER BY created_at").fetchall()
    out = []
    for r in rows:
        data = json.loads(r["data"])
        if data["deleted"] and not include_deleted:
            continue
        out.append({"id": r["id"], "version": r["version"], "data": data})
    return {"records": out}