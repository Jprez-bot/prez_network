"""SQLite persistence. Every asset is a row with links back to its source,
so the whole library survives restarts and every clip can be traced to the
original recording and timestamp."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable

from . import config

_local = threading.local()
_init_lock = threading.Lock()
_initialized_path: str | None = None

JSON_FIELDS = {
    "meta", "segments", "audio", "scenes", "faces", "scores", "platform_scores",
    "plan", "original_plan", "params", "learned", "content_types", "platforms",
    "extracted", "profile_ids", "delta", "layers", "words", "tags", "chapters",
    "signals",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',
  created_at REAL
);
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY, project_id INTEGER REFERENCES projects(id),
  kind TEXT NOT NULL,              -- recording | twitch_clip | twitch_vod | youtube | gameplay | reaction | clip | other
  category TEXT DEFAULT 'auto',    -- gaming | just_chatting | reaction | irl | auto
  title TEXT, path TEXT NOT NULL, original_name TEXT, stored_copy INTEGER DEFAULT 1,
  duration REAL, width INTEGER, height INTEGER, fps REAL, has_audio INTEGER,
  size INTEGER, status TEXT DEFAULT 'new', meta TEXT DEFAULT '{}', created_at REAL
);
CREATE TABLE IF NOT EXISTS transcripts (
  source_id INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
  engine TEXT, language TEXT, segments TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS analyses (
  source_id INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
  audio TEXT, scenes TEXT, faces TEXT, chapters TEXT, summary TEXT, engine TEXT,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS moments (
  id INTEGER PRIMARY KEY, source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,               -- short | long
  start REAL, end REAL, hook_start REAL, hook_end REAL, payoff_end REAL,
  structure TEXT,                   -- hook_first | context_first
  title TEXT, summary TEXT, reasons TEXT, excerpt TEXT, content_type TEXT,
  scores TEXT, overall_score REAL, platform_scores TEXT, signals TEXT,
  engine TEXT, status TEXT DEFAULT 'candidate', created_at REAL
);
CREATE TABLE IF NOT EXISTS packages (
  id INTEGER PRIMARY KEY, source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
  moment_id INTEGER REFERENCES moments(id) ON DELETE SET NULL,
  name TEXT, long_form_plan_id INTEGER, created_at REAL
);
CREATE TABLE IF NOT EXISTS style_profiles (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, platform TEXT,
  content_types TEXT DEFAULT '[]', description TEXT DEFAULT '',
  params TEXT, learned TEXT DEFAULT '{}', builtin INTEGER DEFAULT 0, created_at REAL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS edit_plans (
  id INTEGER PRIMARY KEY, source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
  moment_id INTEGER REFERENCES moments(id) ON DELETE SET NULL,
  package_id INTEGER REFERENCES packages(id) ON DELETE SET NULL,
  parent_id INTEGER, version INTEGER DEFAULT 1,
  platform TEXT NOT NULL, content_type TEXT, form TEXT,   -- form: long | short
  profile_id INTEGER REFERENCES style_profiles(id),
  status TEXT DEFAULT 'draft',     -- draft | approved | rejected
  title TEXT, description TEXT, tags TEXT DEFAULT '[]',
  plan TEXT, original_plan TEXT, confidence REAL, engine TEXT,
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS renders (
  id INTEGER PRIMARY KEY, plan_id INTEGER REFERENCES edit_plans(id) ON DELETE CASCADE,
  path TEXT, engine TEXT, width INTEGER, height INTEGER, duration REAL,
  status TEXT, log TEXT, plan_version INTEGER, created_at REAL
);
CREATE TABLE IF NOT EXISTS thumbnails (
  id INTEGER PRIMARY KEY, plan_id INTEGER REFERENCES edit_plans(id) ON DELETE CASCADE,
  source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
  kind TEXT,                        -- youtube | cover
  frame_time REAL, text TEXT, path TEXT, layers TEXT DEFAULT '{}', engine TEXT,
  selected INTEGER DEFAULT 0, created_at REAL
);
CREATE TABLE IF NOT EXISTS refs (
  id INTEGER PRIMARY KEY,
  level TEXT NOT NULL,              -- creator | video | section | technique | clip | thumbnail | short_form | caption | premiere | instruction
  name TEXT, creator TEXT, url TEXT, path TEXT,
  section_start REAL, section_end REAL,
  platforms TEXT DEFAULT '[]', content_types TEXT DEFAULT '[]',
  aspects TEXT DEFAULT '',          -- which aspects the reference is for (pacing, captions...)
  notes TEXT DEFAULT '', extracted TEXT DEFAULT '{}', profile_ids TEXT DEFAULT '[]',
  status TEXT DEFAULT 'new', created_at REAL
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY, plan_id INTEGER, profile_id INTEGER, action TEXT,
  delta TEXT DEFAULT '{}', note TEXT DEFAULT '', created_at REAL
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, kind TEXT, target_id INTEGER, status TEXT,
  progress REAL DEFAULT 0, message TEXT DEFAULT '', error TEXT DEFAULT '',
  meta TEXT DEFAULT '{}', created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS operations (
  id INTEGER PRIMARY KEY, source_id INTEGER, plan_id INTEGER, operation TEXT,
  engine TEXT,                      -- adobe-premiere | adobe-photoshop | ffmpeg | opencv | pillow | claude | heuristic | whisper
  detail TEXT, created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_moments_source ON moments(source_id);
CREATE INDEX IF NOT EXISTS idx_plans_source ON edit_plans(source_id);
CREATE INDEX IF NOT EXISTS idx_plans_package ON edit_plans(package_id);
CREATE INDEX IF NOT EXISTS idx_renders_plan ON renders(plan_id);
"""


def _connect() -> sqlite3.Connection:
    global _initialized_path
    config.ensure_dirs()
    path = str(config.DB_PATH)
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == path:
        return conn
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    with _init_lock:
        if _initialized_path != path:
            conn.executescript(SCHEMA)
            conn.commit()
            _initialized_path = path
    _local.conn = conn
    _local.path = path
    return conn


def reset_connection() -> None:
    """Used by tests when the data dir changes."""
    global _initialized_path
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
    _local.conn = None
    _initialized_path = None


def _decode(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for k, v in out.items():
        if k in JSON_FIELDS and isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except json.JSONDecodeError:
                pass
    return out


def _encode(values: dict) -> dict:
    out = {}
    for k, v in values.items():
        if k in JSON_FIELDS and not isinstance(v, str) and v is not None:
            out[k] = json.dumps(v)
        else:
            out[k] = v
    return out


def now() -> float:
    return time.time()


def insert(table: str, **values: Any) -> int:
    values = _encode(values)
    cols = ", ".join(values)
    qs = ", ".join("?" for _ in values)
    conn = _connect()
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", list(values.values()))
    conn.commit()
    return cur.lastrowid


def update(table: str, row_id: int, key: str = "id", **values: Any) -> None:
    if not values:
        return
    values = _encode(values)
    sets = ", ".join(f"{k}=?" for k in values)
    conn = _connect()
    conn.execute(f"UPDATE {table} SET {sets} WHERE {key}=?", [*values.values(), row_id])
    conn.commit()


def upsert(table: str, key: str, **values: Any) -> None:
    values = _encode(values)
    cols = ", ".join(values)
    qs = ", ".join("?" for _ in values)
    sets = ", ".join(f"{k}=excluded.{k}" for k in values if k != key)
    conn = _connect()
    conn.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({qs}) ON CONFLICT({key}) DO UPDATE SET {sets}",
        list(values.values()),
    )
    conn.commit()


def get(table: str, row_id: Any, key: str = "id") -> dict | None:
    row = _connect().execute(f"SELECT * FROM {table} WHERE {key}=?", (row_id,)).fetchone()
    return _decode(row)


def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    rows = _connect().execute(sql, list(params)).fetchall()
    return [_decode(r) for r in rows]


def one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    row = _connect().execute(sql, list(params)).fetchone()
    return _decode(row)


def execute(sql: str, params: Iterable[Any] = ()) -> None:
    conn = _connect()
    conn.execute(sql, list(params))
    conn.commit()


def delete(table: str, row_id: int) -> None:
    execute(f"DELETE FROM {table} WHERE id=?", (row_id,))


def log_operation(operation: str, engine: str, detail: str = "",
                  source_id: int | None = None, plan_id: int | None = None) -> None:
    """Record which engine handled an operation (Adobe vs FFmpeg vs fallback)."""
    insert("operations", source_id=source_id, plan_id=plan_id, operation=operation,
           engine=engine, detail=detail[:2000], created_at=now())
