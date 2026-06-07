#!/usr/bin/env python3
"""
queue_manager.py — SQLite-based print job queue

Status flow:
  pending → printing → completed
                    ↘ failed → (retry) → pending
  pending → cancelled
"""

import sqlite3
import os
import time
import threading
import logging
from enum import Enum
from contextlib import contextmanager
from datetime import datetime

log = logging.getLogger("usb-printer")

DB_PATH = os.environ.get("QUEUE_DB", "/var/lib/usb-printer/queue.db")
SPOOL_DIR = os.environ.get("SPOOL_DIR", "/var/spool/usb-printer")


class JobStatus(str, Enum):
    PENDING    = "pending"
    PRINTING   = "printing"
    COMPLETED  = "completed"
    FAILED     = "failed"
    CANCELLED  = "cancelled"


SCHEMA = """
CREATE TABLE IF NOT EXISTS print_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device      TEXT    NOT NULL,
    username    TEXT    NOT NULL,
    filename    TEXT    NOT NULL DEFAULT '',
    file_size   INTEGER NOT NULL DEFAULT 0,
    spool_path  TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    created_at  REAL    NOT NULL,
    started_at  REAL,
    finished_at REAL,
    error_msg   TEXT    DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_jobs_status    ON print_jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_device    ON print_jobs(device);
CREATE INDEX IF NOT EXISTS idx_jobs_created   ON print_jobs(created_at DESC);
"""


class QueueManager:
    """
    Thread-safe print queue menggunakan SQLite dengan WAL mode.
    Setiap operasi DB memakai connection baru untuk kompatibilitas
    multi-thread Gunicorn workers.
    """

    def __init__(self, db_path: str = DB_PATH, spool_dir: str = SPOOL_DIR):
        self.db_path  = db_path
        self.spool_dir = spool_dir
        self._lock = threading.Lock()   # untuk operasi start_printing
        self._init_db()
        os.makedirs(spool_dir, exist_ok=True)

    # ── DB helpers ────────────────────────────────────────────────────────────

    @contextmanager
    def _conn(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # ── Submit ────────────────────────────────────────────────────────────────

    def submit_job(
        self,
        device: str,
        data: bytes,
        username: str,
        filename: str = "",
        max_retries: int = 3,
    ) -> int:
        """
        Simpan data ke spool file, lalu insert job ke DB.
        Return job_id.
        """
        # Tulis ke spool dulu (atomic via rename)
        os.makedirs(self.spool_dir, exist_ok=True)
        ts = time.time()
        tmp_path   = os.path.join(self.spool_dir, f"job_{ts:.6f}.tmp")
        spool_path = os.path.join(self.spool_dir, f"job_{ts:.6f}.prn")
        try:
            with open(tmp_path, "wb") as f:
                f.write(data)
            os.rename(tmp_path, spool_path)
        except Exception as e:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise RuntimeError(f"Gagal menulis spool: {e}")

        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO print_jobs
                    (device, username, filename, file_size, spool_path,
                     status, retry_count, max_retries, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (device, username, filename, len(data), spool_path,
                 JobStatus.PENDING, max_retries, ts),
            )
            job_id = cur.lastrowid
        log.info(f"QUEUE SUBMIT job #{job_id} device={device} user={username} size={len(data)}")
        return job_id

    # ── Status transitions ────────────────────────────────────────────────────

    def start_printing(self, job_id: int) -> bool:
        """
        Tandai job sebagai PRINTING. Return False jika job sudah tidak pending.
        Menggunakan lock untuk cegah race condition dua worker.
        """
        with self._lock:
            with self._conn() as conn:
                cur = conn.execute(
                    """
                    UPDATE print_jobs
                    SET status='printing', started_at=?
                    WHERE id=? AND status='pending'
                    """,
                    (time.time(), job_id),
                )
                return cur.rowcount == 1

    def mark_completed(self, job_id: int):
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE print_jobs
                SET status='completed', finished_at=?
                WHERE id=?
                """,
                (time.time(), job_id),
            )
        self._cleanup_spool(job_id)
        log.info(f"QUEUE COMPLETED job #{job_id}")

    def mark_failed(self, job_id: int, error: str = ""):
        """
        Tandai gagal. Jika retry_count < max_retries, reset ke pending.
        Jika sudah habis, tandai failed permanen.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT retry_count, max_retries FROM print_jobs WHERE id=?",
                (job_id,)
            ).fetchone()
            if not row:
                return

            retry_count  = row["retry_count"]
            max_retries  = row["max_retries"]
            new_retry    = retry_count + 1

            if new_retry < max_retries:
                # Bisa retry → kembalikan ke pending
                conn.execute(
                    """
                    UPDATE print_jobs
                    SET status='pending', retry_count=?, error_msg=?, started_at=NULL
                    WHERE id=?
                    """,
                    (new_retry, error, job_id),
                )
                log.warning(f"QUEUE RETRY job #{job_id} attempt={new_retry}/{max_retries} err={error}")
            else:
                # Habis retry → failed permanen
                conn.execute(
                    """
                    UPDATE print_jobs
                    SET status='failed', finished_at=?, error_msg=?, retry_count=?
                    WHERE id=?
                    """,
                    (time.time(), error, new_retry, job_id),
                )
                log.error(f"QUEUE FAILED job #{job_id} final err={error}")
                self._cleanup_spool(job_id)

    def cancel_job(self, job_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE print_jobs
                SET status='cancelled', finished_at=?
                WHERE id=? AND status IN ('pending','failed')
                """,
                (time.time(), job_id),
            )
            ok = cur.rowcount == 1
        if ok:
            self._cleanup_spool(job_id)
            log.info(f"QUEUE CANCELLED job #{job_id}")
        return ok

    def retry_job(self, job_id: int) -> bool:
        """Manual retry: reset failed job ke pending."""
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE print_jobs
                SET status='pending', error_msg='', started_at=NULL, finished_at=NULL
                WHERE id=? AND status IN ('failed', 'cancelled')
                """,
                (job_id,),
            )
            ok = cur.rowcount == 1
        if ok:
            log.info(f"QUEUE MANUAL RETRY job #{job_id}")
        return ok

    # ── Stuck job recovery ────────────────────────────────────────────────────

    def recover_stuck_jobs(self, timeout_seconds: int = 300):
        """
        Job yang status='printing' lebih dari timeout_seconds → mark failed.
        Dipanggil saat startup dan oleh worker periodik.
        """
        cutoff = time.time() - timeout_seconds
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id FROM print_jobs
                WHERE status='printing' AND (started_at IS NULL OR started_at < ?)
                """,
                (cutoff,),
            ).fetchall()
        for row in rows:
            self.mark_failed(row["id"], "Timeout: printer tidak merespons")
        if rows:
            log.warning(f"QUEUE RECOVERY: {len(rows)} stuck jobs direset")

    # ── Query ─────────────────────────────────────────────────────────────────

    def list_jobs(
        self,
        limit: int = 50,
        offset: int = 0,
        status: str = None,
    ) -> list:
        with self._conn() as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT id, device, username, filename, file_size,
                           status, retry_count, max_retries,
                           created_at, started_at, finished_at, error_msg
                    FROM print_jobs
                    WHERE status=?
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (status, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, device, username, filename, file_size,
                           status, retry_count, max_retries,
                           created_at, started_at, finished_at, error_msg
                    FROM print_jobs
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_job(self, job_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM print_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_next_pending(self, device: str) -> dict | None:
        """Ambil job pending tertua untuk device tertentu (FIFO)."""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM print_jobs
                WHERE status='pending' AND device=?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (device,),
            ).fetchone()
        return dict(row) if row else None

    def get_stats(self) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) as cnt
                FROM print_jobs
                GROUP BY status
                """
            ).fetchall()
        counts = {r["status"]: r["cnt"] for r in rows}
        return {
            "pending":   counts.get("pending", 0),
            "printing":  counts.get("printing", 0),
            "completed": counts.get("completed", 0),
            "failed":    counts.get("failed", 0),
            "cancelled": counts.get("cancelled", 0),
            "total":     sum(counts.values()),
        }

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def _cleanup_spool(self, job_id: int):
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT spool_path FROM print_jobs WHERE id=?", (job_id,)
                ).fetchone()
            if row and row["spool_path"] and os.path.exists(row["spool_path"]):
                os.unlink(row["spool_path"])
        except Exception as e:
            log.warning(f"Gagal hapus spool job #{job_id}: {e}")

    def purge_old_jobs(self, days: int = 30):
        """Hapus job lama (completed/cancelled/failed) dari DB."""
        cutoff = time.time() - (days * 86400)
        with self._conn() as conn:
            conn.execute(
                """
                DELETE FROM print_jobs
                WHERE status IN ('completed','cancelled','failed')
                  AND finished_at < ?
                """,
                (cutoff,),
            )
        log.info(f"QUEUE PURGE: hapus job older than {days} days")
