#!/usr/bin/env python3
"""
worker.py — Queue processor daemon
Membaca job pending dari SQLite dan mengirim ke printer via /dev/usb/lpX.

Dijalankan sebagai service terpisah (usb-printer-worker.service).
"""

import os
import sys
import time
import signal
import logging
import socket

# Logging ke systemd journal
try:
    import systemd.journal
    handler = systemd.journal.JournalHandler(SYSLOG_IDENTIFIER="usb-printer-worker")
except ImportError:
    handler = logging.StreamHandler()

log = logging.getLogger("usb-printer-worker")
log.addHandler(handler)
log.setLevel(logging.INFO)

sys.path.insert(0, os.path.dirname(__file__))
from queue_manager import QueueManager
from printer import load_config, get_p910nd_status, auto_recover_p910nd

# ── Konfigurasi ───────────────────────────────────────────────────────────────
POLL_INTERVAL   = 2      # Detik antar polling (saat idle)
ACTIVE_INTERVAL = 0.2    # Detik antar polling (saat ada job)
PRINT_TIMEOUT   = 60     # Detik timeout per chunk penulisan
CHUNK_SIZE      = 65536  # 64 KB per chunk

# Printer yang dikelola (dideteksi otomatis)
MANAGED_DEVICES = [f"/dev/usb/lp{i}" for i in range(4)]

_running = True


def handle_signal(signum, frame):
    global _running
    log.info(f"Signal {signum} diterima, worker shutdown...")
    _running = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


# ── Core print function ───────────────────────────────────────────────────────

def print_job(job: dict, queue: QueueManager) -> bool:
    """
    Kirim spool file ke device printer.
    Return True jika berhasil.
    """
    job_id     = job["id"]
    device     = job["device"]
    spool_path = job["spool_path"]

    if not os.path.exists(device):
        queue.mark_failed(job_id, f"Device {device} tidak ditemukan (printer offline?)")
        return False

    if not spool_path or not os.path.exists(spool_path):
        queue.mark_failed(job_id, "Spool file tidak ditemukan")
        return False

    # Auto-recover p910nd sebelum print
    # Kasus: printer dimatiin lalu dinyalain, p910nd mati tapi device masih ada
    try:
        config = load_config()
        port = config.get(device, {}).get("port", 9100)
        bidi = bool(config.get(device, {}).get("bidirectional", False))
        status = get_p910nd_status(device, int(port))
        if status in ("stopped", "stale"):
            log.warning(f"Job #{job_id}: p910nd {device} status={status}, mencoba auto-recover...")
            ok_r, msg_r = auto_recover_p910nd(device, int(port), bidi)
            if ok_r:
                log.info(f"Job #{job_id}: auto-recover p910nd BERHASIL")
                import time as _t; _t.sleep(1.5)  # Beri waktu p910nd siap
            else:
                log.error(f"Job #{job_id}: auto-recover p910nd GAGAL: {msg_r}")
                # Lanjutkan tetap — worker menulis langsung ke device, bukan via p910nd port
    except Exception as e_rec:
        log.warning(f"Job #{job_id}: auto-recover check error (non-fatal): {e_rec}")

    # Tandai printing sebelum mulai
    if not queue.start_printing(job_id):
        log.warning(f"Job #{job_id} sudah diproses oleh worker lain, skip")
        return False

    log.info(f"PRINT START job #{job_id} device={device} file={spool_path}")

    try:
        # Buka device printer (blocking write)
        with open(spool_path, "rb") as src:
            with open(device, "wb", buffering=0) as dev:
                while True:
                    chunk = src.read(CHUNK_SIZE)
                    if not chunk:
                        break

                    # Tulis dengan timeout check
                    start = time.monotonic()
                    written = 0
                    while written < len(chunk):
                        try:
                            n = dev.write(chunk[written:])
                            written += n
                        except BlockingIOError:
                            if time.monotonic() - start > PRINT_TIMEOUT:
                                raise TimeoutError("Timeout menulis ke printer")
                            time.sleep(0.05)
                        except OSError as e:
                            raise RuntimeError(f"Printer error: {e}")

        queue.mark_completed(job_id)
        log.info(f"PRINT DONE job #{job_id}")
        return True

    except Exception as e:
        err = str(e)
        log.error(f"PRINT FAIL job #{job_id}: {err}")
        queue.mark_failed(job_id, err)

        # Cek apakah printer masih ada — jika tidak, hentikan p910nd
        if not os.path.exists(device):
            log.warning(f"Device {device} hilang saat cetak, printer mungkin dicabut")
            _try_stop_p910nd(device)

        return False


def _try_stop_p910nd(device: str):
    """Hentikan p910nd untuk device yang offline."""
    try:
        from printer import load_config, stop_p910nd
        config = load_config()
        port = config.get(device, {}).get("port", 9100)
        stop_p910nd(device, port)
        log.info(f"p910nd untuk {device} dihentikan setelah printer offline")
    except Exception as e:
        log.warning(f"Gagal stop p910nd untuk {device}: {e}")


# ── Main loop ─────────────────────────────────────────────────────────────────

def run():
    global _running
    queue = QueueManager()
    log.info("Queue worker started")

    # Recovery: tandai job stuck sebagai gagal
    queue.recover_stuck_jobs(timeout_seconds=300)

    # Purge job lama saat startup
    queue.purge_old_jobs(days=30)

    idle_count = 0

    while _running:
        found_job = False

        for device in MANAGED_DEVICES:
            if not _running:
                break

            job = queue.get_next_pending(device)
            if not job:
                continue

            found_job = True
            print_job(job, queue)

        if found_job:
            idle_count = 0
            time.sleep(ACTIVE_INTERVAL)
        else:
            idle_count += 1
            # Recover stuck jobs setiap 5 menit (150 * 2s = 300s)
            if idle_count % 150 == 0:
                queue.recover_stuck_jobs()
            time.sleep(POLL_INTERVAL)

    log.info("Queue worker stopped")


if __name__ == "__main__":
    run()
