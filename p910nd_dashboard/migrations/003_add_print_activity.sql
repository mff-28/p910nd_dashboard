-- migrations/003_add_print_activity.sql
-- Tambah tabel print_activity untuk merekam koneksi TCP ke p9100d

PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS print_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL,
    client_ip TEXT NOT NULL,
    printer_port INTEGER NOT NULL,
    printer_device TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_print_activity_time ON print_activity(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_print_activity_client ON print_activity(client_ip);
CREATE INDEX IF NOT EXISTS idx_print_activity_port ON print_activity(printer_port);

INSERT OR IGNORE INTO schema_migrations (version) VALUES ('003_add_print_activity');
