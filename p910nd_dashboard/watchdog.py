#!/usr/bin/env python3
"""
watchdog.py — USB Printer Watchdog v2
- Auto start p910nd saat printer power on (tanpa cabut USB)
- Auto stop saat printer power off
- Tidak restart kalau user sengaja stop dari dashboard (maintenance mode)
- Port portable: baca dari config.json
"""

import os
import sys
import time
import signal
import logging
import json
import subprocess

try:
    import systemd.journal
    handler = systemd.journal.JournalHandler(SYSLOG_IDENTIFIER="usb-printer-watchdog")
except ImportError:
    handler = logging.StreamHandler()

log = logging.getLogger("usb-printer-watchdog")
log.addHandler(handler)
log.setLevel(logging.INFO)

# ── Konfigurasi ───────────────────────────────────────────────────────────────
CONFIG_FILE   = os.environ.get("CONFIG_FILE",  "/etc/usb-printer/config.json")
LOCK_DIR      = os.environ.get("LOCK_DIR",     "/run/usb-printer")
SCAN_INTERVAL = float(os.environ.get("WATCHDOG_INTERVAL", "3"))
MANAGED_DEVICES = [f"/dev/usb/lp{i}" for i in range(4)]
DEFAULT_PORTS = {f"/dev/usb/lp{i}": 9100 + i for i in range(4)}

_running = True

def handle_signal(signum, frame):
    global _running
    log.info(f"Signal {signum}, watchdog shutdown...")
    _running = False

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT,  handle_signal)

# ── Maintenance flag ──────────────────────────────────────────────────────────
# File /run/usb-printer/port-XXXX.manual-stop dibuat app.py saat user stop manual
# Watchdog tidak akan restart selama file ini ada
# File dihapus saat user Start lagi dari dashboard

def is_manual_stop(port: int) -> bool:
    return os.path.exists(os.path.join(LOCK_DIR, f"port-{port}.manual-stop"))

def set_manual_stop(port: int):
    open(os.path.join(LOCK_DIR, f"port-{port}.manual-stop"), "w").close()

def clear_manual_stop(port: int):
    try:
        os.remove(os.path.join(LOCK_DIR, f"port-{port}.manual-stop"))
    except FileNotFoundError:
        pass

# ── Config helpers ────────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def get_port(device: str, config: dict) -> int:
    return int(config.get(device, {}).get("port", DEFAULT_PORTS.get(device, 9100)))

def get_bidi(device: str, config: dict) -> bool:
    return bool(config.get(device, {}).get("bidirectional", False))

# ── Port & process helpers ────────────────────────────────────────────────────

def is_port_listening(port: int) -> bool:
    try:
        with open("/proc/net/tcp") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) > 3 and parts[3] == "0A":
                    if int(parts[1].split(":")[1], 16) == port:
                        return True
    except Exception:
        pass
    try:
        r = subprocess.run(["ss", "-tlnp", f"sport = :{port}"],
                           capture_output=True, text=True, timeout=2)
        return f":{port}" in r.stdout
    except Exception:
        return False

def read_pid(port: int):
    try:
        with open(os.path.join(LOCK_DIR, f"port-{port}.pid")) as f:
            return int(f.read().strip())
    except Exception:
        return None

def write_pid(port: int, pid: int):
    try:
        with open(os.path.join(LOCK_DIR, f"port-{port}.pid"), "w") as f:
            f.write(str(pid))
    except Exception as e:
        log.warning(f"Tidak bisa tulis PID file port {port}: {e}")

def clear_pid(port: int):
    try:
        os.remove(os.path.join(LOCK_DIR, f"port-{port}.pid"))
    except FileNotFoundError:
        pass

def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False

def kill_stale(device: str, port: int):
    pid = read_pid(port)
    if pid and is_pid_alive(pid):
        log.info(f"Kill stale PID={pid} port={port}")
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.8)
            if is_pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    clear_pid(port)
    try:
        r = subprocess.run(["pgrep", "-f", f"p910nd.*{device}"],
                           capture_output=True, text=True, timeout=3)
        for p in r.stdout.strip().splitlines():
            try:
                os.kill(int(p.strip()), signal.SIGKILL)
            except Exception:
                pass
    except Exception:
        pass

def get_status(device: str, port: int) -> str:
    if not os.path.exists(device):
        return "offline"
    if is_port_listening(port):
        return "running"
    pid = read_pid(port)
    if pid and is_pid_alive(pid):
        return "stale"
    return "stopped"

# ── Start p910nd ──────────────────────────────────────────────────────────────

def start_p910nd(device: str, port: int, bidi: bool) -> bool:
    kill_stale(device, port)

    offset = port - 9100
    if not (0 <= offset <= 9):
        log.error(f"Port {port} tidak didukung p910nd v0.97 (hanya 9100-9109)")
        return False

    cmd = ["p910nd", "-f", device]
    if bidi:
        cmd.append("-b")
    cmd.append(str(offset))

    log.info(f"START: {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        log.error("p910nd tidak ditemukan! apt install p910nd")
        return False

    for _ in range(16):
        time.sleep(0.5)
        if is_port_listening(port):
            write_pid(port, proc.pid)
            log.info(f"p910nd OK: dev={device} port={port} PID={proc.pid}")
            return True
        if proc.poll() is not None:
            log.error(f"p910nd exit cepat rc={proc.returncode} dev={device} port={port}")
            return False

    if proc.poll() is None:
        write_pid(port, proc.pid)
        log.warning(f"p910nd jalan PID={proc.pid} tapi port {port} belum LISTEN")
        return True
    return False

def stop_p910nd(device: str, port: int):
    kill_stale(device, port)
    log.info(f"STOP p910nd: dev={device} port={port}")

# ── State tracking ────────────────────────────────────────────────────────────
_prev_state: dict  = {}   # device → 'online'|'offline'
_prev_port: dict   = {}   # device → port (deteksi perubahan port dari dashboard)

def watchdog_loop():
    log.info(f"Watchdog started | interval={SCAN_INTERVAL}s | config={CONFIG_FILE}")

    while _running:
        config = load_config()

        for device in MANAGED_DEVICES:
            try:
                port   = get_port(device, config)
                bidi   = get_bidi(device, config)
                status = get_status(device, port)
                exists = os.path.exists(device)
                prev   = _prev_state.get(device, "unknown")
                curr   = "online" if exists else "offline"
                prev_port = _prev_port.get(device)

                # ── Deteksi perubahan port dari dashboard ─────────────────────
                if prev_port is not None and prev_port != port and curr == "online":
                    log.info(f"[{device}] Port berubah {prev_port}→{port}, restart p910nd")
                    stop_p910nd(device, prev_port)
                    clear_manual_stop(port)  # port baru = fresh start
                    ok = start_p910nd(device, port, bidi)
                    log.info(f"[{device}] Restart port baru {'OK' if ok else 'GAGAL'} port={port}")

                # ── Printer baru terdeteksi (power on / dicolok) ──────────────
                elif curr == "online" and prev != "online":
                    log.info(f"[{device}] Printer ON → port={port}")
                    # Hapus manual-stop flag karena printer baru nyala
                    clear_manual_stop(port)

                # ── Printer hilang (power off / dicabut) ──────────────────────
                elif curr == "offline" and prev == "online":
                    log.info(f"[{device}] Printer OFF → stop port={port}")
                    if status in ("running", "stale"):
                        stop_p910nd(device, port)
                    # Hapus manual-stop juga biar kalau nyala lagi auto-start
                    clear_manual_stop(port)

                # ── Printer ada tapi p910nd tidak running → auto start ─────────
                elif curr == "online" and status in ("stopped", "stale"):
                    if is_manual_stop(port):
                        # User sengaja stop dari dashboard — jangan restart
                        pass
                    else:
                        log.info(f"[{device}] status={status} → auto-start port={port}")
                        ok = start_p910nd(device, port, bidi)
                        if ok:
                            log.info(f"[{device}] Auto-recover OK port={port}")
                        else:
                            log.error(f"[{device}] Auto-recover GAGAL port={port}")

                _prev_state[device] = curr
                _prev_port[device]  = port

            except Exception as e:
                log.warning(f"[{device}] error non-fatal: {e}")

        time.sleep(SCAN_INTERVAL)

    log.info("Watchdog stopped")


if __name__ == "__main__":
    os.makedirs(LOCK_DIR, exist_ok=True)
    watchdog_loop()
