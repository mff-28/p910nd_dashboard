#!/usr/bin/env python3
"""
monitor.py — Lightweight monitor for p9100d TCP connections

Monitors /proc/net/tcp(6) for new ESTABLISHED connections to ports
9100/9101/9102 and logs an activity row into the SQLite database at
`/var/lib/usb-printer/queue.db` by default.

Design notes:
- Non-intrusive: does not bind/listen on ports or change p9100d.
- Uses inode observed in /proc/net/tcp lines to avoid double-counting
  the same TCP connection.
- Minimal dependencies (only standard library).
"""
import os
import time
import sqlite3
import logging
from datetime import datetime

from typing import Set, Dict

log = logging.getLogger("usb-printer")

# Config
TARGET_PORTS = {9100, 9101, 9102}
DB_PATH = os.environ.get("QUEUE_DB", "/var/lib/usb-printer/queue.db")
SCAN_INTERVAL = float(os.environ.get("MONITOR_SCAN_INTERVAL", "0.6"))


def _hex_to_ipv4(h: str) -> str:
    # h is 8 hex chars little-endian, e.g. '0100007F' -> 127.0.0.1
    try:
        h = h.zfill(8)
        bytes_le = [h[i:i+2] for i in range(0, 8, 2)]
        bytes_be = bytes_le[::-1]
        return '.'.join(str(int(b, 16)) for b in bytes_be)
    except Exception:
        return "0.0.0.0"


def _parse_proc_tcp(path: str = "/proc/net/tcp"):
    """Yield dicts: {'local_ip','local_port','rem_ip','rem_port','st','inode'}"""
    try:
        with open(path, "r") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 10:
            continue
        local, rem, st, _, _, _, _, _, _, inode = parts[1:11]
        lip, lport = local.split(":")
        rip, rport = rem.split(":")
        try:
            lport = int(lport, 16)
            rport = int(rport, 16)
        except Exception:
            continue
        yield {
            "local_ip": _hex_to_ipv4(lip),
            "local_port": lport,
            "rem_ip": _hex_to_ipv4(rip),
            "rem_port": rport,
            "st": st,
            "inode": inode,
        }


def _ensure_table(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS print_activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME NOT NULL,
        client_ip TEXT NOT NULL,
        printer_port INTEGER NOT NULL,
        printer_device TEXT NOT NULL
    );
    """)


def load_port_mapping() -> Dict[int, str]:
    """Try to map port -> device by reading printer config file used by app.
    Fallback: assume /dev/usb/lp0 for 9100, lp1 for 9101, etc.
    """
    try:
        from printer import load_config
        cfg = load_config()
        inv = {}
        for dev, v in cfg.items():
            try:
                port = int(v.get("port", 9100))
            except Exception:
                port = 9100
            inv[port] = dev
        # Fill defaults
        for idx, p in enumerate(sorted(TARGET_PORTS)):
            if p not in inv:
                inv[p] = f"/dev/usb/lp{idx}"
        return inv
    except Exception:
        # simple fallback
        return {p: f"/dev/usb/lp{n}" for n, p in enumerate(sorted(TARGET_PORTS))}


def monitor_loop():
    seen_inodes: Set[str] = set()
    port_map = load_port_mapping()

    # Ensure DB directory exists
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    while True:
        try:
            entries = list(_parse_proc_tcp("/proc/net/tcp"))
            entries += list(_parse_proc_tcp("/proc/net/tcp6"))

            current_inodes = set()
            to_record = []

            for e in entries:
                if e["st"] != "01":
                    continue
                if e["local_port"] not in TARGET_PORTS:
                    continue
                inode = e["inode"]
                current_inodes.add(inode)
                if inode in seen_inodes:
                    continue
                # New connection detected
                client_ip = e["rem_ip"]
                port = e["local_port"]
                device = port_map.get(port, f"/dev/usb/lp0")
                ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')  # UTC, konsisten dengan SQLite datetime('now')
                to_record.append((ts, client_ip, port, device))

            if to_record:
                try:
                    conn = sqlite3.connect(DB_PATH, timeout=5)
                    _ensure_table(conn)
                    cur = conn.cursor()
                    cur.executemany(
                        "INSERT INTO print_activity (timestamp, client_ip, printer_port, printer_device) VALUES (?,?,?,?)",
                        to_record,
                    )
                    conn.commit()
                    conn.close()
                    for r in to_record:
                        log.info(f"MONITOR: record activity {r[1]} -> :{r[2]} device={r[3]}")
                except Exception as exc:
                    log.warning(f"MONITOR DB insert failed: {exc}")

            # Update seen set and prune old/closed connections
            seen_inodes |= current_inodes
            # prune seen_inodes to only those currently present to avoid unbounded growth
            seen_inodes &= current_inodes

        except Exception as e:
            log.warning(f"monitor loop error: {e}")

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    log.info("Starting usb-printer monitor")
    monitor_loop()
