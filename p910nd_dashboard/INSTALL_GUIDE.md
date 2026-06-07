# USB Printer Server v3 — Panduan Instalasi Debian 13

## Langkah Instalasi dari Awal sampai Selesai

---

### Prasyarat
- Debian 13 (Trixie) fresh install
- Akses root atau sudo
- 1+ printer USB terhubung (atau akan dihubungkan nanti)
- 512 MB RAM minimum, 1 GB recommended untuk 100+ user

---

## Langkah 1: Update Sistem

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl wget
```

---

## Langkah 2: Upload/Extract Proyek

```bash
# Jika upload via SCP:
scp usb-printer-server-v3.zip user@server:/tmp/

# Di server:
cd /tmp
unzip usb-printer-server-v3.zip
cd usb-printer-server-v3
```

---

## Langkah 3: Jalankan Installer

```bash
sudo bash install.sh
```

Installer akan:
1. Install semua paket sistem (p910nd, python3, sqlite3, dll)
2. Membuat virtual environment Python dengan Flask, Gunicorn, gevent
3. Membuat user sistem `printer` dengan privilege minimum
4. Menyiapkan direktori:
   - `/opt/usb-printer/` — aplikasi
   - `/etc/usb-printer/` — konfigurasi + auth (mode 750)
   - `/var/lib/usb-printer/` — database SQLite
   - `/var/spool/usb-printer/` — spool file sementara
   - `/run/usb-printer/` — PID + lock files (tmpfs)
5. Menginisialisasi database antrian
6. Install systemd services
7. Install udev rules
8. Meminta Anda membuat akun admin pertama

---

## Langkah 4: Akses Dashboard

Buka browser dan akses:
```
http://IP_SERVER:8080
```

Login dengan akun admin yang dibuat saat instalasi.

---

## Langkah 5: Hubungkan Printer USB

Colokkan printer USB. Sistem akan otomatis:
1. Mendeteksi printer via udev
2. Menjalankan `p910nd` di port 9100 (printer 1) atau 9101 (printer 2)
3. Printer siap menerima job cetak

Verifikasi:
```bash
ls -la /dev/usb/lp*
systemctl status usb-printer
journalctl -u usb-printer -f
```

---

## Langkah 6: Konfigurasi Printer di Dashboard

1. Buka tab **Printer Manager**
2. Klik tombol **Konfigurasi** pada printer yang terdeteksi
3. Set port TCP (9100 untuk printer 1, 9101 untuk printer 2)
4. Klik **Simpan & Mulai**

Dari komputer klien, tambahkan printer dengan protokol **AppSocket/JetDirect**:
- Host: `IP_SERVER`
- Port: `9100` atau `9101`

---

## Manajemen User

```bash
# Tambah user baru
sudo /opt/usb-printer/venv/bin/python3 /opt/usb-printer/manage.py adduser

# Ganti password
sudo /opt/usb-printer/venv/bin/python3 /opt/usb-printer/manage.py passwd

# Lihat daftar user
sudo /opt/usb-printer/venv/bin/python3 /opt/usb-printer/manage.py listusers

# Hapus user
sudo /opt/usb-printer/venv/bin/python3 /opt/usb-printer/manage.py deluser

# Migrasi dari v2 (jika upgrade)
sudo /opt/usb-printer/venv/bin/python3 /opt/usb-printer/manage.py migrate
```

---

## Install Monitor (new in v3)

Monitor (`monitor.py`) records RAW TCP connections to ports 9100/9101/9102
and stores activity into the same SQLite DB. To install:

```bash
# Apply DB migration
sudo sqlite3 /var/lib/usb-printer/queue.db < /opt/usb-printer/migrations/003_add_print_activity.sql

# Enable and start monitor service
sudo cp /opt/usb-printer/systemd/usb-printer-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now usb-printer-monitor.service
```

The monitor runs as the `printer` user and is non-intrusive — it does not
intercept or modify p9100d behavior.

---

## Perintah Sistem

```bash
# Status services
systemctl status usb-printer
systemctl status usb-printer-worker

# Restart
systemctl restart usb-printer
systemctl restart usb-printer-worker

# Log realtime
journalctl -u usb-printer -f
journalctl -u usb-printer-worker -f

# Log gabungan
journalctl -u usb-printer -u usb-printer-worker -f

# Statistik antrian
sudo /opt/usb-printer/venv/bin/python3 /opt/usb-printer/manage.py queue-stats

# Purge job lama (>30 hari)
sudo /opt/usb-printer/venv/bin/python3 /opt/usb-printer/manage.py queue-purge
```

---

## Tuning untuk 100+ User

### Gunicorn workers

Edit `/opt/usb-printer/systemd/usb-printer.service`:
```ini
Environment=GUNICORN_WORKERS=4
```

Atau edit `gunicorn.conf.py`:
```python
workers = 4           # 2-4 cukup untuk printer server
worker_connections = 200
```

Setelah edit:
```bash
systemctl daemon-reload
systemctl restart usb-printer
```

### SQLite WAL mode

Sudah aktif secara default. WAL memungkinkan read concurrent dari multiple workers
tanpa blocking write dari queue worker.

### udev untuk 2 printer

Sistem sudah mendukung 2 printer secara native:
- Printer 1: `/dev/usb/lp0` → port 9100
- Printer 2: `/dev/usb/lp1` → port 9101

Konfigurasi setiap printer secara terpisah di dashboard.

---

## Upgrade dari v2

```bash
# Stop service lama
sudo systemctl stop usb-printer

# Backup config
sudo cp -r /etc/usb-printer /etc/usb-printer.bak

# Jalankan installer v3
cd /tmp/usb-printer-server-v3
sudo bash install.sh

# Migrasi password (SHA256 → pbkdf2)
sudo /opt/usb-printer/venv/bin/python3 /opt/usb-printer/manage.py migrate
```

---

## Struktur Direktori Final

```
/opt/usb-printer/              # Aplikasi utama (milik printer:lp)
├── app.py                     # Flask app factory + routes
├── auth.py                    # Password hashing, input validation
├── printer.py                 # USB detection + p910nd management
├── queue_manager.py           # SQLite queue (pending/printing/done/fail)
├── worker.py                  # Queue processor daemon
├── manage.py                  # CLI manajemen user + queue
├── gunicorn.conf.py           # Gunicorn production config
├── requirements.txt
├── templates/
│   ├── login.html             # Login page + CSRF
│   └── index.html             # Dashboard LuCI-style
├── static/
│   └── luci.css               # LuCI theme
├── migrations/
│   ├── 001_init.sql           # Schema DB awal
│   └── 002_upgrade_from_v2.sql
└── venv/                      # Python virtual environment

/etc/usb-printer/              # Konfigurasi (mode 750, milik printer:lp)
├── secret.key                 # Flask session secret (mode 600)
├── auth.json                  # Username → pbkdf2 hash (mode 600)
└── config.json                # Konfigurasi printer (port, bidirectional)

/var/lib/usb-printer/          # Data persisten
└── queue.db                   # SQLite database antrian cetak

/var/spool/usb-printer/        # Spool file sementara (dihapus setelah print)

/run/usb-printer/              # tmpfs — hilang saat reboot
├── gunicorn.pid               # PID Gunicorn master
├── port-9100.pid              # PID p910nd printer 1
├── port-9100.lock             # flock file printer 1
├── port-9101.pid              # PID p910nd printer 2
└── port-9101.lock             # flock file printer 2

/etc/systemd/system/
├── usb-printer.service        # Gunicorn web server
└── usb-printer-worker.service # Queue processor

/etc/udev/rules.d/
└── 99-usb-printer.rules       # Auto start/stop saat printer dicolok/dicabut

/etc/sudoers.d/
└── usb-printer                # user printer boleh jalankan p910nd saja
```

---

## Troubleshooting

### Service tidak mau start
```bash
journalctl -u usb-printer -n 50 --no-pager
```

### Printer tidak terdeteksi
```bash
lsusb                          # Lihat perangkat USB
ls -la /dev/usb/               # Cek device node
udevadm monitor                # Monitor udev events realtime

# Debug printer name detection
bash /opt/usb-printer/diagnostic-usb.sh
```

Jika output menunjukkan sysfs paths tidak ada, `printer.py` akan fallback ke `udevadm` untuk membaca info USB.

### Job cetak gagal terus
```bash
# Lihat error message di dashboard tab Antrian
# Atau via CLI:
sqlite3 /var/lib/usb-printer/queue.db \
  "SELECT id,device,status,error_msg FROM print_jobs ORDER BY id DESC LIMIT 10;"
```

### Port sudah dipakai
```bash
ss -tlnp | grep 9100           # Cek proses di port 9100
ls -la /run/usb-printer/       # Lihat PID files
```

### Reset database antrian
```bash
sudo systemctl stop usb-printer-worker
sudo sqlite3 /var/lib/usb-printer/queue.db "DELETE FROM print_jobs;"
sudo systemctl start usb-printer-worker
```

---

## Fix v3-fixed: Printer Tidak Bisa Print Setelah Dimatikan

### Kenapa ini terjadi?

Ketika printer **dimatikan (power off) tanpa mencabut kabel USB**:
- `/dev/usb/lp0` masih ada (kernel tetap melihat USB device)
- p910nd PID masih tercatat di `/run/usb-printer/port-9100.pid`
- Tapi p910nd tidak bisa berkomunikasi ke printer yang mati
- Saat printer dinyalakan lagi, udev event `add` **tidak fired** karena USB tidak pernah disconnect
- Akibatnya: p910nd dalam kondisi "stale" — port 9100 tidak LISTEN

### Fix yang diterapkan di v3-fixed

1. **`printer.py`** — `get_p910nd_status()` kini mendeteksi status `stale` (PID hidup tapi port tidak LISTEN) dan otomatis kill proses stale
2. **`printer.py`** — Fungsi baru `auto_recover_p910nd()` untuk restart otomatis
3. **`app.py`** — `api_printers()` auto-recover saat dashboard dibuka dan status `stale`/`stopped`
4. **`worker.py`** — Auto-recover sebelum setiap job print
5. **`udev-add.sh`** — Lebih andal: deteksi stale, tunggu port LISTEN (bukan hanya tunggu PID)
6. **`usb-printer.service`** — `ExecStartPost` trigger `udev-add.sh` saat service/reboot start

### Cara upgrade dari v3 ke v3-fixed

```bash
# Stop service
sudo systemctl stop usb-printer usb-printer-worker usb-printer-monitor

# Copy file baru
sudo cp printer.py worker.py app.py monitor.py /opt/usb-printer/
sudo cp udev-add.sh /opt/usb-printer/
sudo chmod +x /opt/usb-printer/udev-add.sh

# Update systemd service
sudo cp systemd/usb-printer.service /etc/systemd/system/
sudo cp systemd/usb-printer-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload

# Restart
sudo systemctl start usb-printer usb-printer-worker usb-printer-monitor
```

### Verifikasi fix berjalan

```bash
# Matikan printer, tunggu 5 detik, nyalakan lagi
# Lalu cek log:
journalctl -t usb-printer -f

# Harusnya muncul:
# [usb-printer] Auto-recover triggered for /dev/usb/lp0 (status=stale)
# [usb-printer] Auto-recover OK: /dev/usb/lp0 -> :9100
# ATAU:
# [usb-printer] p910nd started: /dev/usb/lp0 → port 9100

# Cek port listening:
ss -tlnp | grep 9100
```

### Quick fix manual (tanpa upgrade)

```bash
# Jalankan ini setiap kali printer nyala lagi setelah dimatiin:
sudo /opt/usb-printer/udev-add.sh manual

# Atau via dashboard: Printer Manager → Restart
```
