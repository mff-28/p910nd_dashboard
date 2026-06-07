#!/bin/bash
# /opt/usb-printer/udev-remove.sh
# Dipanggil udev saat printer USB dicabut
# v3: tandai job printing sebagai failed, hentikan p910nd

set -uo pipefail

DEVICE_KEY="${1:-}"
LOCK_DIR="/run/usb-printer"
QUEUE_DB="/var/lib/usb-printer/queue.db"

journal_log() {
    local level="$1"; shift
    logger -t usb-printer "udev-remove: $*" 2>/dev/null || true
}

journal_log 6 "Event REMOVE (${DEVICE_KEY})"

KILLED=0

# ── Hentikan semua p910nd via PID file ───────────────────────────────────────
for PID_FILE in "$LOCK_DIR"/port-*.pid; do
    [[ -f "$PID_FILE" ]] || continue

    PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
    PORT=$(basename "$PID_FILE" | sed 's/port-\(.*\)\.pid/\1/')

    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
        journal_log 6 "Stop p910nd PID=$PID port=$PORT"
        kill -TERM "$PID" 2>/dev/null || true
        sleep 0.8
        kill -KILL "$PID" 2>/dev/null || true
        KILLED=$((KILLED + 1))
    fi
    rm -f "$PID_FILE"
done

# ── Tandai job 'printing' sebagai failed di SQLite ────────────────────────────
# Ini memastikan job tidak hilang — bisa di-retry dari UI
if [[ -f "$QUEUE_DB" ]] && command -v sqlite3 &>/dev/null; then
    TS=$(date +%s.%N)
    UPDATED=$(sqlite3 "$QUEUE_DB" \
        "UPDATE print_jobs
         SET status='failed', finished_at=$TS,
             error_msg='Printer dicabut saat mencetak'
         WHERE status='printing';
         SELECT changes();" 2>/dev/null || echo "0")
    if [[ "$UPDATED" -gt 0 ]]; then
        journal_log 4 "$UPDATED job ditandai failed (printer dicabut)"
    fi
else
    journal_log 4 "WARN: Tidak bisa update queue DB (sqlite3 tidak ada atau DB tidak ada)"
fi

journal_log 6 "$KILLED proses p910nd dihentikan"
exit 0
