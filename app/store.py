"""SQLite-backed job store.

SQLite keeps jobs durable across restarts without extra infrastructure. Every
state transition is a conditional UPDATE (``WHERE status = ...``) so the API and
worker threads can't overwrite each other's changes.
"""

import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.models import Crop, JobStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    status           TEXT NOT NULL,
    source_video     TEXT NOT NULL,
    crop_top         INTEGER NOT NULL,
    crop_bottom      INTEGER NOT NULL,
    crop_left        INTEGER NOT NULL,
    crop_right       INTEGER NOT NULL,
    source_width     INTEGER NOT NULL,
    source_height    INTEGER NOT NULL,
    source_duration  REAL,
    output_width     INTEGER NOT NULL,
    output_height    INTEGER NOT NULL,
    output_path      TEXT,
    output_size      INTEGER,
    output_expired   INTEGER NOT NULL DEFAULT 0,
    progress         REAL NOT NULL DEFAULT 0,
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  REAL NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    error_code       TEXT,
    error_message    TEXT,
    idempotency_key  TEXT UNIQUE,
    request_hash     TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    started_at       REAL,
    completed_at     REAL
);
CREATE INDEX IF NOT EXISTS jobs_status_next_attempt ON jobs (status, next_attempt_at);
"""


@dataclass
class Job:
    id: str
    status: JobStatus
    source_video: str
    crop: Crop
    source_width: int
    source_height: int
    source_duration: float | None
    output_width: int
    output_height: int
    output_path: str | None = None
    output_size: int | None = None
    output_expired: bool = False
    progress: float = 0.0
    attempts: int = 0
    next_attempt_at: float = 0.0
    cancel_requested: bool = False
    error_code: str | None = None
    error_message: str | None = None
    idempotency_key: str | None = None
    request_hash: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float | None = None
    completed_at: float | None = None

    @classmethod
    def new(cls, **fields) -> "Job":
        now = time.time()
        return cls(id=uuid.uuid4().hex, status=JobStatus.PENDING, created_at=now, updated_at=now, next_attempt_at=now, **fields)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        data = dict(row)
        crop = Crop(top=data.pop("crop_top"), bottom=data.pop("crop_bottom"), left=data.pop("crop_left"), right=data.pop("crop_right"))
        data["status"] = JobStatus(data["status"])
        data["output_expired"] = bool(data["output_expired"])
        data["cancel_requested"] = bool(data["cancel_requested"])
        return cls(crop=crop, **data)


class DuplicateIdempotencyKey(Exception):
    pass


class JobStore:
    def __init__(self, db_path: Path):
        self._db_path = db_path
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # A short-lived connection per operation keeps this safe to call from any thread.
        conn = sqlite3.connect(self._db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    # --- reads ---------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def find_by_idempotency_key(self, key: str) -> Job | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE idempotency_key = ?", (key,)).fetchone()
        return Job.from_row(row) if row else None

    def list_jobs(self, status: JobStatus | None = None, limit: int = 50) -> list[Job]:
        query, params = "SELECT * FROM jobs", []
        if status:
            query, params = query + " WHERE status = ?", [status.value]
        with self._connect() as conn:
            rows = conn.execute(query + " ORDER BY created_at DESC LIMIT ?", (*params, limit)).fetchall()
        return [Job.from_row(r) for r in rows]

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    # --- writes --------------------------------------------------------------

    def insert(self, job: Job) -> None:
        columns = {
            "id": job.id, "status": job.status.value, "source_video": job.source_video,
            "crop_top": job.crop.top, "crop_bottom": job.crop.bottom, "crop_left": job.crop.left, "crop_right": job.crop.right,
            "source_width": job.source_width, "source_height": job.source_height, "source_duration": job.source_duration,
            "output_width": job.output_width, "output_height": job.output_height,
            "next_attempt_at": job.next_attempt_at, "idempotency_key": job.idempotency_key, "request_hash": job.request_hash,
            "created_at": job.created_at, "updated_at": job.updated_at,
        }  # fmt: skip
        sql = f"INSERT INTO jobs ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"
        try:
            with self._connect() as conn:
                conn.execute(sql, tuple(columns.values()))
        except sqlite3.IntegrityError as exc:
            if job.idempotency_key and "idempotency_key" in str(exc):
                raise DuplicateIdempotencyKey(job.idempotency_key) from exc
            raise

    def claim_next(self) -> Job | None:
        """Atomically move the oldest due pending job to processing and return it."""
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT id FROM jobs WHERE status = 'pending' AND next_attempt_at <= ? ORDER BY next_attempt_at, created_at LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE jobs SET status = 'processing', attempts = attempts + 1, progress = 0, started_at = ?, updated_at = ? WHERE id = ?",
                (now, now, row["id"]),
            )
            return Job.from_row(conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone())

    def update_progress(self, job_id: str, progress: float) -> None:
        self._update(job_id, JobStatus.PROCESSING, progress=progress)

    def mark_completed(self, job_id: str, output_path: str, output_size: int) -> bool:
        now = time.time()
        return self._update(
            job_id, JobStatus.PROCESSING,
            status=JobStatus.COMPLETED.value, progress=100.0, output_path=output_path, output_size=output_size,
            error_code=None, error_message=None, completed_at=now,
        )  # fmt: skip

    def mark_failed(self, job_id: str, code: str, message: str) -> bool:
        return self._update(
            job_id, JobStatus.PROCESSING,
            status=JobStatus.FAILED.value, error_code=code, error_message=message, completed_at=time.time(),
        )  # fmt: skip

    def mark_cancelled(self, job_id: str) -> bool:
        return self._update(job_id, JobStatus.PROCESSING, status=JobStatus.CANCELLED.value, completed_at=time.time())

    def schedule_retry(self, job_id: str, delay_s: float, code: str, message: str) -> bool:
        return self._update(
            job_id, JobStatus.PROCESSING,
            status=JobStatus.PENDING.value, next_attempt_at=time.time() + delay_s, progress=0.0,
            error_code=code, error_message=message,
        )  # fmt: skip

    def requeue(self, job_id: str) -> bool:
        """Return an interrupted job to the queue without charging it an attempt."""
        with self._connect() as conn:
            now = time.time()
            cur = conn.execute(
                "UPDATE jobs SET status = 'pending', attempts = MAX(attempts - 1, 0), progress = 0, next_attempt_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'processing'",
                (now, now, job_id),
            )
            return cur.rowcount == 1

    def request_cancel(self, job_id: str) -> Job | None:
        """Cancel a pending job outright; flag a processing job for the worker to stop."""
        now = time.time()
        with self._transaction() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'cancelled', cancel_requested = 1, completed_at = ?, updated_at = ? WHERE id = ? AND status = 'pending'",
                (now, now, job_id),
            )
            conn.execute(
                "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ? AND status = 'processing'",
                (now, job_id),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def recover_interrupted(self, max_attempts: int) -> int:
        """On startup, resolve jobs left in processing by a previous crash."""
        now = time.time()
        with self._transaction() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'cancelled', completed_at = ?, updated_at = ? WHERE status = 'processing' AND cancel_requested = 1",
                (now, now),
            )
            conn.execute(
                "UPDATE jobs SET status = 'failed', error_code = 'WORKER_INTERRUPTED', "
                "error_message = 'Processing was interrupted and no attempts remain', completed_at = ?, updated_at = ? "
                "WHERE status = 'processing' AND attempts >= ?",
                (now, now, max_attempts),
            )
            cur = conn.execute(
                "UPDATE jobs SET status = 'pending', progress = 0, next_attempt_at = ?, updated_at = ? WHERE status = 'processing'",
                (now, now),
            )
            return cur.rowcount

    def expire_outputs(self, completed_before: float) -> list[tuple[str, str]]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT id, output_path FROM jobs WHERE status = 'completed' AND output_expired = 0 AND completed_at < ?",
                (completed_before,),
            ).fetchall()
            conn.executemany("UPDATE jobs SET output_expired = 1, updated_at = ? WHERE id = ?", [(time.time(), r["id"]) for r in rows])
        return [(r["id"], r["output_path"]) for r in rows]

    def _update(self, job_id: str, expected: JobStatus, **fields) -> bool:
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ? AND status = ?",
                (*fields.values(), job_id, expected.value),
            )
            return cur.rowcount == 1
