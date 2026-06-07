#!/bin/bash
# /opt/usb-printer/udev-add.sh
# Dipanggil udev saat printer USB dicolok ATAU saat ExecStartPost service
#
# FIX:
# - Baca config dari /etc/usb-printer/config.json (bukan /opt/usb-printer/)
# - Argument lp0/lp1 dipetakan ke device yang tepat
# - Port diambil dari config.json (portable, bisa ganti dari dashboard)
# - Fallback default: lp0=9100, lp1=9101, dst (jika belum ada di config)

set -uo pipefail

DEVICE_KEY="${1:-}"
LOCK_DIR="/run/usb-printer"

# Config file: ikuti environment variable jika ada, fallback ke /etc/usb-printer/
CONFIG="${CONFIG_FILE:-/etc/usb-printer/config.json}"

journal_log() {
    local level="$1"; shift
    echo "<${level}>[usb-printer] udev-add: $*" >&2
    logger -t usb-printer "udev-add: $*" 2>/dev/null || true
}

journal_log 6 "Event ADD (${DEVICE_KEY:-boot}) | config=$CONFIG"

mkdir -p "$LOCK_DIR"

# ── Baca port dari config.json ────────────────────────────────────────────────
# Return port yang tersimpan untuk device tertentu, atau "" jika tidak ada
get_port_from_config() {
    local dev="$1"
    if [[ ! -f "$CONFIG" ]]; then
        echo ""
        return
    fi
    python3 - <<EOF 2>/dev/null
import json, sys
try:
    c = json.load(open('$CONFIG'))
    v = c.get('$dev', {}).get('port', '')
    print(str(v) if v else '')
except Exception:
    print('')
EOF
}

# ── Tentukan device target dari argument ──────────────────────────────────────
TARGET_DEVICES=()

if [[ "$DEVICE_KEY" =~ ^lp([0-9]+)$ ]]; then
    # Argument spesifik dari udev: lp0, lp1, lp2, dst
    LP_IDX="${BASH_REMATCH[1]}"
    TARGET_DEVICES=("/dev/usb/lp${LP_IDX}")
    journal_log 6 "Target spesifik: /dev/usb/lp${LP_IDX}"
else
    # boot / service-start / manual / format lama — start semua device yang ada
    for dev in /dev/usb/lp0 /dev/usb/lp1 /dev/usb/lp2 /dev/usb/lp3; do
        [[ -c "$dev" ]] && TARGET_DEVICES+=("$dev")
    done
    journal_log 6 "Mode boot/manual, target: ${TARGET_DEVICES[*]:-none}"
fi

if [[ ${#TARGET_DEVICES[@]} -eq 0 ]]; then
    journal_log 4 "WARN: Tidak ada device LP ditemukan"
    exit 0
fi

# ── Fungsi start satu device ──────────────────────────────────────────────────
start_device() {
    local LP_DEV="$1"

    # Tunggu device benar-benar bisa dibuka (max 15 detik)
    local READY=0
    for i in $(seq 1 15); do
        if [[ -c "$LP_DEV" && -w "$LP_DEV" ]]; then
            READY=1
            break
        fi
        journal_log 6 "Menunggu $LP_DEV siap... ($i/15)"
        sleep 1
    done

    if [[ $READY -eq 0 ]]; then
        journal_log 3 "ERROR: $LP_DEV tidak siap setelah 15 detik"
        return 1
    fi

    # ── Ambil port dari config.json (portable!) ───────────────────────────────
    local PORT
    PORT=$(get_port_from_config "$LP_DEV")

    if [[ -n "$PORT" && "$PORT" =~ ^[0-9]+$ ]]; then
        journal_log 6 "$LP_DEV → port $PORT (dari config.json)"
    else
        # Fallback default jika belum ada di config
        if [[ "$LP_DEV" =~ /dev/usb/lp([0-9])$ ]]; then
            PORT=$((9100 + ${BASH_REMATCH[1]}))
        else
            PORT=9100
        fi
        journal_log 6 "$LP_DEV → port $PORT (default fallback, belum ada di config)"
    fi

    # ── Cek apakah port sudah LISTEN ─────────────────────────────────────────
    if ss -tlnp 2>/dev/null | grep -qE ":${PORT}[[:space:]]"; then
        journal_log 6 "Port $PORT sudah LISTEN, skip $LP_DEV"
        return 0
    fi

    # ── Kill proses stale ─────────────────────────────────────────────────────
    local PID_FILE="$LOCK_DIR/port-${PORT}.pid"
    if [[ -f "$PID_FILE" ]]; then
        local OLD_PID
        OLD_PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
        if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
            journal_log 6 "Kill stale p910nd PID=$OLD_PID port=$PORT"
            kill -TERM "$OLD_PID" 2>/dev/null || true
            sleep 1
            kill -KILL "$OLD_PID" 2>/dev/null || true
        fi
        rm -f "$PID_FILE"
    fi

    # Kill via pgrep juga (untuk proses yang tidak tercatat di PID file)
    local STALE_PIDS
    STALE_PIDS=$(pgrep -f "p910nd.*${LP_DEV}" 2>/dev/null || true)
    if [[ -n "$STALE_PIDS" ]]; then
        journal_log 6 "Kill stale p910nd via pgrep: PIDs=$STALE_PIDS"
        echo "$STALE_PIDS" | xargs kill -KILL 2>/dev/null || true
        sleep 0.5
    fi

    # ── Start p910nd dengan port dari config ──────────────────────────────────
    local LOCK_FILE="$LOCK_DIR/port-${PORT}.lock"

    (
        flock -n 9 || {
            journal_log 6 "SKIP: Port $PORT sudah di-lock oleh proses lain"
            exit 0
        }

        journal_log 6 "Menjalankan: p910nd -f $LP_DEV $(( PORT - 9100 ))"
        p910nd -f "$LP_DEV" $(( PORT - 9100 )) &
        local P910ND_PID=$!

        # Tunggu port benar-benar LISTEN (max 5 detik)
        for i in $(seq 1 10); do
            sleep 0.5
            if ss -tlnp 2>/dev/null | grep -qE ":${PORT}[[:space:]]"; then
                echo "$P910ND_PID" > "$PID_FILE"
                journal_log 6 "OK: p910nd aktif | dev=$LP_DEV port=$PORT PID=$P910ND_PID"
                exit 0
            fi
            if ! kill -0 "$P910ND_PID" 2>/dev/null; then
                journal_log 3 "ERROR: p910nd exit terlalu cepat | dev=$LP_DEV port=$PORT"
                exit 1
            fi
        done

        # Proses masih hidup tapi port belum terdeteksi
        if kill -0 "$P910ND_PID" 2>/dev/null; then
            echo "$P910ND_PID" > "$PID_FILE"
            journal_log 4 "WARN: p910nd jalan PID=$P910ND_PID tapi port $PORT belum LISTEN"
        else
            journal_log 3 "ERROR: p910nd gagal start | dev=$LP_DEV port=$PORT"
            exit 1
        fi

    ) 9>"$LOCK_FILE"
}

# ── Start semua target device ─────────────────────────────────────────────────
for DEV in "${TARGET_DEVICES[@]}"; do
    start_device "$DEV"
done

exit 0
