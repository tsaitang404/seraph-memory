#!/usr/bin/env python3
"""Seraph Memory API — standalone memory ingestion service.

Bridges external editors (e.g. a notes app) into the Hermes memory engine
asynchronously:

    POST /v1/memories   -> enqueue a note for LLM extraction + storage (202)
    GET  /v1/queue/status -> queue depth / worker state
    GET  /health        -> liveness

Design notes
------------
* Runs as its OWN process (not inside the Hermes gateway) so ingestion keeps
  working while Hermes restarts or is down.
* Queue table lives inside the same SQLite file as the facts (WAL mode +
  busy_timeout), so there is no second database to maintain.
* note_id is UNIQUE in the queue: repeated edits of the same note collide and
  UPDATE, so the queue always holds the latest content (natural debounce).
* LLM extraction reuses the exact same code path as the Hermes plugin
  (llm_extract.py), which lazily imports Hermes' provider config, so this
  service must run under the Hermes venv.
* No secrets are hardcoded. Auth token comes from env (SERAPH_API_TOKEN).

Usage
-----
    SERAPH_API_TOKEN=<token> /opt/hermes-agent/venv/bin/python3.12 api_server.py
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("seraph-api")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = Path(os.environ.get("SERAPH_MEMORY_DB", "~/.hermes/memory_store.db")).expanduser()
HOST = os.environ.get("SERAPH_API_HOST", "127.0.0.1")
PORT = int(os.environ.get("SERAPH_API_PORT", "8787"))
TOKEN = os.environ.get("SERAPH_API_TOKEN", "")
POLL_INTERVAL = float(os.environ.get("SERAPH_POLL_INTERVAL", "2"))
MAX_RETRIES = int(os.environ.get("SERAPH_MAX_RETRIES", "3"))
BACKOFF_BASE = float(os.environ.get("SERAPH_BACKOFF_BASE", "5"))

# ---------------------------------------------------------------------------
# .env loading (llm_extract reads DEEPSEEK_API_KEY etc. from env / .env)
# ---------------------------------------------------------------------------

_ENV_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _ENV_RE.match(line)
        if m and m.group(1) not in os.environ:
            os.environ.setdefault(m.group(1), m.group(2).strip("\"'"))


load_env_file(Path("~/.hermes/.env").expanduser())

# ---------------------------------------------------------------------------
# Imports that may depend on the Hermes venv (provider machinery)
# ---------------------------------------------------------------------------

from store import MemoryStore as Store  # noqa: E402
from llm_extract import extract_entities_llm, extract_relations_llm  # noqa: E402

# ---------------------------------------------------------------------------
# Queue table + shared SQLite helpers
# ---------------------------------------------------------------------------

_QUEUE_SQL = """
CREATE TABLE IF NOT EXISTS pending_extractions (
    id INTEGER PRIMARY KEY,
    note_id TEXT UNIQUE,
    title TEXT DEFAULT '',
    content TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    retries INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS note_map (
    note_id TEXT PRIMARY KEY,
    fact_id INTEGER
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Queue:
    """Thin wrapper around the pending_extractions table (own connection)."""

    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(_QUEUE_SQL)
        self.conn.commit()
        self._lock = threading.Lock()
        # Recover tasks that were mid-flight when the process died.
        self.conn.execute(
            "UPDATE pending_extractions SET status='pending' WHERE status='processing'"
        )
        self.conn.commit()

    def enqueue(self, note_id: str, title: str, content: str, tags: str = "") -> bool:
        """Upsert by note_id. Returns True when inserted, False when updated."""
        with self._lock:
            exists = (
                self.conn.execute(
                    "SELECT 1 FROM pending_extractions WHERE note_id=?", (note_id,)
                ).fetchone()
                is not None
            )
            self.conn.execute(
                """
                INSERT INTO pending_extractions (note_id, title, content, tags, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(note_id) DO UPDATE SET
                    title=excluded.title, content=excluded.content, tags=excluded.tags,
                    status='pending', retries=0, updated_at=excluded.updated_at
                """,
                (note_id, title, content, tags, _now(), _now()),
            )
            self.conn.commit()
            return not exists

    def next_task(self) -> sqlite3.Row | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM pending_extractions WHERE status='pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row:
                self.conn.execute(
                    "UPDATE pending_extractions SET status='processing' WHERE id=?",
                    (row["id"],),
                )
                self.conn.commit()
            return row

    def mark_done(self, task_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE pending_extractions SET status='done', updated_at=? WHERE id=?",
                (_now(), task_id),
            )
            self.conn.commit()

    def mark_failed(self, task_id: int, error: str) -> None:
        with self._lock:
            row = self.conn.execute(
                "SELECT retries FROM pending_extractions WHERE id=?", (task_id,)
            ).fetchone()
            retries = (row["retries"] if row else 0) + 1
            if retries >= MAX_RETRIES:
                self.conn.execute(
                    "UPDATE pending_extractions SET status='failed', retries=?, updated_at=? WHERE id=?",
                    (retries, _now(), task_id),
                )
            else:
                self.conn.execute(
                    "UPDATE pending_extractions SET status='pending', retries=?, updated_at=? WHERE id=?",
                    (retries, _now(), task_id),
                )
            self.conn.commit()

    def status(self) -> dict:
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM pending_extractions GROUP BY status"
            ).fetchall()
            return {r["status"]: r["n"] for r in rows} or {"pending": 0}

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------
# Worker: LLM extraction + storage
# ---------------------------------------------------------------------------


def _update_existing_fact(store: Store, fact_id: int, title: str, content: str, tags: str) -> None:
    """Rewrite a fact in place and rebuild its edges + HRR vector."""
    with store._lock:
        store._conn.execute(
            "UPDATE facts SET content=?, title=?, tags=?, updated_at=? WHERE fact_id=?",
            (content, title or store._auto_title(content), tags or "", _now(), fact_id),
        )
        store._conn.execute("DELETE FROM fact_entities WHERE fact_id=?", (fact_id,))
        store._conn.commit()
        store._compute_hrr_vector(fact_id, content)
        store._rebuild_bank("general")


def process_task(queue: Queue, store: Store, task: sqlite3.Row) -> str:
    """Store one note into the memory engine. Returns a status string."""
    note_id, title, content, tags = task["note_id"], task["title"], task["content"], task["tags"]
    content = (content or "").strip()
    if not content:
        return "skipped-empty"

    # 1. LLM extraction (failures never block storage — regex path still applies)
    entities: list = []
    relations: list = []
    try:
        entities = extract_entities_llm(content) or []
        relations = extract_relations_llm(content) or []
    except Exception as e:  # noqa: BLE001
        log.warning("LLM extraction failed for note %s: %s", note_id, e)

    # 2. Insert new fact or update the existing mapping
    row = queue.conn.execute(
        "SELECT fact_id FROM note_map WHERE note_id=?", (note_id,)
    ).fetchone()
    if row is None:
        fact_id = store.add_fact(content, category="general", tags=tags or "", title=title or "")
        queue.conn.execute(
            "INSERT OR REPLACE INTO note_map(note_id, fact_id) VALUES(?, ?)",
            (note_id, fact_id),
        )
        queue.conn.commit()
        log.info("fact %s created from note %s", fact_id, note_id)
    else:
        fact_id = int(row["fact_id"])
        _update_existing_fact(store, fact_id, title, content, tags)
        log.info("fact %s updated from note %s", fact_id, note_id)

    # 3. Link LLM entities + relations
    if entities:
        store._link_entities(fact_id, entities)
    if relations:
        store.add_relations(relations, fact_id=fact_id)

    return "ok"


def worker_loop(queue: Queue, store: Store) -> None:
    log.info("worker started (poll %ss)", POLL_INTERVAL)
    while True:
        task = queue.next_task()
        if task is None:
            time.sleep(POLL_INTERVAL)
            continue
        try:
            process_task(queue, store, task)
            queue.mark_done(task["id"])
        except Exception as e:  # noqa: BLE001
            log.error("task %s failed: %s", task["id"], e)
            queue.mark_failed(task["id"], str(e))
            time.sleep(BACKOFF_BASE)


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

try:
    from fastapi import FastAPI, Header, HTTPException
    from pydantic import BaseModel
    import uvicorn
except ImportError as e:  # pragma: no cover
    log.error("missing dependency: %s (install fastapi+uvicorn in the Hermes venv)", e)
    sys.exit(1)


class MemoryPayload(BaseModel):
    note_id: str
    title: str = ""
    content: str = ""
    tags: str = ""


app = FastAPI(title="Seraph Memory API", version="0.1.0")

_APP_STATE: dict = {}


def _check_auth(authorization: str | None) -> None:
    if not TOKEN:
        raise HTTPException(status_code=500, detail="SERAPH_API_TOKEN not configured")
    expected = f"Bearer {TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid token")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "db": str(DB_PATH)}


@app.post("/v1/memories", status_code=202)
def create_memory(payload: MemoryPayload, authorization: str | None = Header(default=None)) -> dict:
    _check_auth(authorization)
    queue: Queue = _APP_STATE["queue"]
    inserted = queue.enqueue(payload.note_id, payload.title, payload.content, payload.tags)
    return {"accepted": True, "inserted": inserted, "note_id": payload.note_id}


@app.get("/v1/queue/status")
def queue_status(authorization: str | None = Header(default=None)) -> dict:
    _check_auth(authorization)
    queue: Queue = _APP_STATE["queue"]
    return {"queue": queue.status()}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if not TOKEN:
        log.warning("SERAPH_API_TOKEN not set — auth disabled (dev only)")
    store = Store(db_path=DB_PATH)
    queue = Queue(DB_PATH)
    _APP_STATE["store"] = store
    _APP_STATE["queue"] = queue

    threading.Thread(target=worker_loop, args=(queue, store), daemon=True).start()

    log.info("Seraph Memory API listening on %s:%s (db=%s)", HOST, PORT, DB_PATH)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
