"""Background jobs (analysis, rendering, reference analysis) persisted in SQLite.

Jobs run on a small thread pool. If the app restarts mid-job, the job is
marked ``interrupted`` and can be retried from the dashboard; all completed
work is already in the database.
"""
from __future__ import annotations

import json
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

from . import db

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jimbo-job")
_lock = threading.Lock()
HANDLERS: dict = {}


def register(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


def recover_interrupted() -> int:
    rows = db.query("SELECT id FROM jobs WHERE status IN ('queued','running')")
    for r in rows:
        db.update("jobs", r["id"], status="interrupted", message="App restarted before this finished",
                  updated_at=db.now())
    db.execute("UPDATE sources SET status='new' WHERE status='analyzing'")
    db.execute("UPDATE renders SET status='failed', log='interrupted by restart' WHERE status='rendering'")
    return len(rows)


def submit(kind: str, target_id: int, meta: dict | None = None, sync: bool = False) -> int:
    if kind not in HANDLERS:
        raise ValueError(f"unknown job kind {kind}")
    job_id = db.insert("jobs", kind=kind, target_id=target_id, status="queued", progress=0,
                       message="Queued", meta=meta or {}, created_at=db.now(), updated_at=db.now())
    if sync:
        _run(job_id)
    else:
        _pool.submit(_run, job_id)
    return job_id


def _run(job_id: int) -> None:
    job = db.get("jobs", job_id)
    if not job:
        return
    db.update("jobs", job_id, status="running", message="Starting", updated_at=db.now())

    def progress(p: float, msg: str = "") -> None:
        db.update("jobs", job_id, progress=round(max(0.0, min(1.0, p)), 3), message=msg[:300], updated_at=db.now())

    try:
        meta = _meta(job)
        result = HANDLERS[job["kind"]](job["target_id"], progress=progress, **meta)
        msg = result.get("message") if isinstance(result, dict) else result
        db.update("jobs", job_id, status="done", progress=1.0,
                  message=str(msg or (db.get("jobs", job_id) or {}).get("message") or "Done")[:300], updated_at=db.now())
    except Exception as e:  # noqa: BLE001
        db.update("jobs", job_id, status="failed", error=traceback.format_exc()[-4000:],
                  message=str(e)[:300], updated_at=db.now())


def retry(job_id: int) -> int:
    job = db.get("jobs", job_id)
    if not job:
        raise ValueError("job not found")
    return submit(job["kind"], job["target_id"], _meta(job))


def _meta(job: dict) -> dict:
    m = job.get("meta") or {}
    return json.loads(m) if isinstance(m, str) else dict(m)
