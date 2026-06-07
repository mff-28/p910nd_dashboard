-- migrations/002_upgrade_from_v2.sql
-- Dijalankan jika upgrade dari v2 (tidak ada DB sebelumnya)
-- Cukup jalankan 001_init.sql untuk instalasi baru.

-- Tambah kolom jika belum ada (idempotent)
-- SQLite tidak mendukung ADD COLUMN IF NOT EXISTS, jadi pakai try-catch di Python
-- Script ini dokumentasi saja; gunakan manage.py migrate untuk apply

-- Tidak ada data lama yang perlu dimigrasi (v2 tidak punya queue DB)

INSERT OR IGNORE INTO schema_migrations (version) VALUES ('002_upgrade_from_v2');
