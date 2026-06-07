#!/usr/bin/env python3
"""
manage.py — CLI manajemen user USB Printer Server
v3: menggunakan Werkzeug pbkdf2:sha256 (bukan SHA256 bare)

Jalankan sebagai root: sudo python3 /opt/usb-printer/manage.py <perintah>
"""

import json
import os
import sys
import getpass
import re

from auth import (
    AUTH_FILE, load_auth, save_auth,
    hash_password, validate_username,
)


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_adduser(_args):
    username = input("Username: ").strip()
    if not validate_username(username):
        print("ERROR: Username harus 3-32 karakter (huruf, angka, _, -)")
        sys.exit(1)

    auth = load_auth()
    if username in auth:
        print(f"ERROR: User '{username}' sudah ada. Gunakan 'passwd' untuk ganti password.")
        sys.exit(1)

    password = getpass.getpass("Password: ")
    confirm  = getpass.getpass("Konfirmasi password: ")
    if password != confirm:
        print("ERROR: Password tidak cocok.")
        sys.exit(1)
    if len(password) < 8:
        print("ERROR: Password minimal 8 karakter.")
        sys.exit(1)

    auth[username] = hash_password(password)
    save_auth(auth)
    print(f"✓ User '{username}' berhasil dibuat.")


def cmd_passwd(_args):
    auth = load_auth()
    username = input("Username: ").strip()
    if username not in auth:
        print(f"ERROR: User '{username}' tidak ditemukan.")
        sys.exit(1)

    password = getpass.getpass("Password baru: ")
    confirm  = getpass.getpass("Konfirmasi: ")
    if password != confirm:
        print("ERROR: Password tidak cocok.")
        sys.exit(1)
    if len(password) < 8:
        print("ERROR: Password minimal 8 karakter.")
        sys.exit(1)

    auth[username] = hash_password(password)
    save_auth(auth)
    print(f"✓ Password '{username}' diperbarui.")


def cmd_deluser(_args):
    auth = load_auth()
    username = input("Username yang akan dihapus: ").strip()
    if username not in auth:
        print(f"ERROR: User '{username}' tidak ditemukan.")
        sys.exit(1)
    confirm = input(f"Yakin hapus user '{username}'? [y/N]: ").strip().lower()
    if confirm != "y":
        print("Dibatalkan.")
        sys.exit(0)
    del auth[username]
    save_auth(auth)
    print(f"✓ User '{username}' dihapus.")


def cmd_listusers(_args):
    auth = load_auth()
    if not auth:
        print("Tidak ada user.")
        return
    print(f"{'Username':<24}  Hash (10 char)")
    print("-" * 50)
    for u, h in auth.items():
        print(f"{u:<24}  {h[:10]}…")


def cmd_migrate_sha256(_args):
    """
    Migrasi password lama (SHA256 bare) ke Werkzeug pbkdf2.
    Password lama tidak bisa dimigrasi otomatis — reset semua user.
    """
    auth = load_auth()
    old_style = [u for u, h in auth.items() if not h.startswith("pbkdf2:")]
    if not old_style:
        print("Semua user sudah menggunakan pbkdf2. Tidak ada yang perlu dimigrasi.")
        return
    print(f"User dengan hash lama (SHA256): {', '.join(old_style)}")
    print("Untuk keamanan, password harus direset manual.")
    for u in old_style:
        print(f"\nReset password untuk '{u}':")
        pw = getpass.getpass("Password baru: ")
        cf = getpass.getpass("Konfirmasi: ")
        if pw != cf:
            print("Tidak cocok, skip.")
            continue
        auth[u] = hash_password(pw)
        print(f"✓ Password '{u}' diperbarui ke pbkdf2.")
    save_auth(auth)
    print("\n✓ Migrasi selesai.")


def cmd_queue_stats(_args):
    """Tampilkan statistik antrian cetak."""
    sys.path.insert(0, os.path.dirname(__file__))
    from queue_manager import QueueManager
    q = QueueManager()
    stats = q.get_stats()
    print("\nStatistik Antrian Cetak:")
    print("-" * 30)
    for k, v in stats.items():
        print(f"  {k:<12}: {v}")


def cmd_queue_purge(_args):
    """Hapus job lama (>30 hari) dari database."""
    from queue_manager import QueueManager
    q = QueueManager()
    days = int(input("Hapus job lebih dari berapa hari? [30]: ").strip() or "30")
    q.purge_old_jobs(days)
    print(f"✓ Job lebih dari {days} hari dihapus.")


# ── Dispatch ──────────────────────────────────────────────────────────────────

COMMANDS = {
    "adduser":        (cmd_adduser,        "Tambah user baru"),
    "passwd":         (cmd_passwd,         "Ganti password user"),
    "deluser":        (cmd_deluser,        "Hapus user"),
    "listusers":      (cmd_listusers,      "Lihat daftar user"),
    "migrate":        (cmd_migrate_sha256, "Migrasi hash SHA256 lama ke pbkdf2"),
    "queue-stats":    (cmd_queue_stats,    "Statistik antrian cetak"),
    "queue-purge":    (cmd_queue_purge,    "Hapus job antrian lama"),
}


if __name__ == "__main__":
    if os.geteuid() != 0:
        print("Jalankan sebagai root: sudo python3 manage.py <perintah>")
        sys.exit(1)

    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in COMMANDS:
        print("USB Printer Server v3 — User Manager")
        print()
        print("Penggunaan: sudo python3 manage.py <perintah>")
        print()
        print("Perintah:")
        for c, (_, desc) in COMMANDS.items():
            print(f"  {c:<16} {desc}")
        sys.exit(0 if cmd == "" else 1)

    COMMANDS[cmd][0](sys.argv[2:])
