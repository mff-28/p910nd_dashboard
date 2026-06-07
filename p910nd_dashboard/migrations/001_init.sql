-- migrations/001_init.sql
-- USB Printer Server v3 — Inisialisasi database antrian cetak
-- Jalankan: sqlite3 /var/lib/usb-printer/queue.db < 001_init.sql

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;

-- ── Tabel utama job cetak ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS print_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Target printer
    device      TEXT    NOT NULL CHECK(device LIKE '/dev/usb/lp_'),

    -- Info pengirim
    username    TEXT    NOT NULL DEFAULT '',
    filename    TEXT    NOT NULL DEFAULT '',
    file_size   INTEGER NOT NULL DEFAULT 0 CHECK(file_size >= 0),

    -- Lokasi data cetak (dihapus setelah selesai/failed)
    spool_path  TEXT    NOT NULL DEFAULT '',

    -- Status: pending | printing | completed | failed | cancelled
    status      TEXT    NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','printing','completed','failed','cancelled')),

    -- Retry tracking
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
    max_retries INTEGER NOT NULL DEFAULT 3 CHECK(max_retries >= 0),

    -- Timestamps (Unix epoch float)
    created_at  REAL    NOT NULL,
    started_at  REAL,
    finished_at REAL,

    -- Pesan error (jika ada)
    error_msg   TEXT    NOT NULL DEFAULT ''
);

-- ── Index ─────────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_jobs_status    ON print_jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_device    ON print_jobs(device);
CREATE INDEX IF NOT EXISTS idx_jobs_username  ON print_jobs(username);
CREATE INDEX IF NOT EXISTS idx_jobs_created   ON print_jobs(created_at DESC);

-- Index gabungan untuk query paling umum: pending jobs per device
CREATE INDEX IF NOT EXISTS idx_jobs_device_status
    ON print_jobs(device, status, created_at ASC);

-- ── Tabel metadata migrasi ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  REAL NOT NULL DEFAULT (unixepoch('now'))
);

INSERT OR IGNORE INTO schema_migrations (version) VALUES ('001_init');
