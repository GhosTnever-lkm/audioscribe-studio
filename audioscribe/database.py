from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS uploads (
  id TEXT PRIMARY KEY, filename TEXT NOT NULL, source_filename TEXT NOT NULL DEFAULT '', path TEXT NOT NULL,
  total_bytes INTEGER NOT NULL, received_bytes INTEGER NOT NULL DEFAULT 0,
  model TEXT NOT NULL DEFAULT 'small', language TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, upload_id TEXT NOT NULL UNIQUE,
  filename TEXT NOT NULL, audio_path TEXT NOT NULL, model TEXT NOT NULL,
  language TEXT, status TEXT NOT NULL DEFAULT 'queued',
  progress REAL NOT NULL DEFAULT 0, duration REAL,
  error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(upload_id) REFERENCES uploads(id)
);
CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS chunks (
  job_id TEXT NOT NULL, chunk_index INTEGER NOT NULL,
  start_seconds REAL NOT NULL, end_seconds REAL NOT NULL,
  completed INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(job_id, chunk_index),
  FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
  start_seconds REAL NOT NULL, end_seconds REAL NOT NULL,
  text TEXT NOT NULL, chunk_index INTEGER NOT NULL,
  FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE,
  UNIQUE(job_id, chunk_index, start_seconds, end_seconds, text)
);
CREATE INDEX IF NOT EXISTS segments_job_start ON segments(job_id, start_seconds);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(uploads)")}
            if "model" not in columns:
                conn.execute("ALTER TABLE uploads ADD COLUMN model TEXT NOT NULL DEFAULT 'small'")
            if "language" not in columns:
                conn.execute("ALTER TABLE uploads ADD COLUMN language TEXT")
            if "source_filename" not in columns:
                conn.execute("ALTER TABLE uploads ADD COLUMN source_filename TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE uploads SET source_filename=filename WHERE source_filename=''")
            # A process interrupted mid-transcription can safely retry; completed chunks are checkpoints.
            conn.execute("UPDATE jobs SET status='queued', updated_at=CURRENT_TIMESTAMP WHERE status='running'")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_upload(self, upload_id: str, filename: str, path: str, size: int, model: str = "small", language: str | None = None, source_filename: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO uploads(id,filename,source_filename,path,total_bytes,model,language) VALUES(?,?,?,?,?,?,?)",
                (upload_id, filename, source_filename if source_filename is not None else filename, path, size, model, language),
            )

    def get_upload(self, upload_id: str):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
            return dict(row) if row else None

    def set_upload_offset(self, upload_id: str, offset: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE uploads SET received_bytes=? WHERE id=?", (offset, upload_id))

    def create_job(self, job_id: str, upload_id: str, filename: str, path: str, model: str, language: str | None) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO jobs(id,upload_id,filename,audio_path,model,language) VALUES(?,?,?,?,?,?)", (job_id, upload_id, filename, path, model, language))

    def get_job(self, job_id: str):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def get_job_by_upload(self, upload_id: str):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE upload_id=?", (upload_id,)).fetchone()
            return dict(row) if row else None

    def list_jobs(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
            return [dict(row) for row in rows]

    def claim_job(self):
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            changed = conn.execute("UPDATE jobs SET status='running', updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='queued'", (row["id"],)).rowcount
            return row["id"] if changed else None

    def set_duration(self, job_id: str, duration: float) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET duration=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (duration, job_id))

    def set_progress(self, job_id: str, progress: float) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET progress=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (max(0, min(1, progress)), job_id))

    def completed_chunks(self, job_id: str) -> set[int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT chunk_index FROM chunks WHERE job_id=? AND completed=1", (job_id,)).fetchall()
            return {int(r[0]) for r in rows}

    def save_chunk(self, job_id: str, index: int, start: float, end: float, segments: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO chunks(job_id,chunk_index,start_seconds,end_seconds,completed) VALUES(?,?,?,?,0)", (job_id, index, start, end))
            conn.execute("DELETE FROM segments WHERE job_id=? AND chunk_index=?", (job_id, index))
            conn.executemany("INSERT INTO segments(job_id,start_seconds,end_seconds,text,chunk_index) VALUES(?,?,?,?,?)", [(job_id, s["start"], s["end"], s["text"], index) for s in segments])
            conn.execute("UPDATE chunks SET completed=1 WHERE job_id=? AND chunk_index=?", (job_id, index))

    def set_job_state(self, job_id: str, status: str, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET status=?, error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, error, job_id))

    def get_segments(self, job_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT start_seconds AS start,end_seconds AS end,text FROM segments WHERE job_id=? ORDER BY start_seconds,id", (job_id,)).fetchall()
            return [dict(row) for row in rows]

    def delete_job(self, job_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            conn.execute("DELETE FROM uploads WHERE id=?", (result["upload_id"],))
            return result

