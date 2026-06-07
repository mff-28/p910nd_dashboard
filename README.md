# USB Printer Server v3

Share printer USB ke jaringan lokal via TCP/IP (port 9100+). Dibangun dengan Flask + p910nd + SQLite, dengan watchdog otomatis yang mendeteksi printer power on/off tanpa perlu cabut-colok USB.

![Dashboard](docs/screenshot-dashboard.png)

---

## Fitur

- **Multi-printer** — support multi printer USB lebih dari 2
- **Auto-recover** — watchdog restart p910nd otomatis saat printer dinyalakan kembali tanpa cabut USB
- **Port portable** — setiap printer bisa diset ke port berbeda dari dashboard
- **Dashboard web** — monitor status, statistik print, dan log aktivitas
- **Printer Manager** — Start / Stop / Restart / Konfigurasi port per-printer via UI
- **Antrian print** — job di-queue dan diproses satu per satu via worker
- **Autentikasi** — login dengan username + password (bcrypt)
- **Systemd-native** — semua komponen berjalan sebagai systemd service

---

## Arsitektur

```
Windows/Linux Client
        │
        │  TCP port 9100/9101/...
        ▼
┌─────────────────────────────────┐
│        p910nd (per printer)     │  ← Dikelola watchdog & app
│  lp0 → port 9100                │
│  lp1 → port 9101                │
└────────────┬────────────────────┘
             │ /dev/usb/lpX
             ▼
      [USB Printer]

┌─────────────────────────────────┐
│   Flask App (Gunicorn)          │  port 8080
│   + Worker (antrian print)      │
│   + Watchdog (auto-recover)     │
│   + Monitor (activity log)      │
└─────────────────────────────────┘
```

### Komponen

| Service | File | Fungsi |
|---|---|---|
| `usb-printer` | `app.py` | Flask web app + API (Gunicorn) |
| `usb-printer-worker` | `worker.py` | Proses antrian print job |
| `usb-printer-watchdog` | `watchdog.py` | Monitor & auto-recover p910nd |
| `usb-printer-monitor` | `monitor.py` | Catat aktivitas print ke DB |

---

## Persyaratan

- **OS**: Debian 12/13 (atau turunannya)
- **Python**: 3.11+
- **RAM**: minimal 256MB
- **Storage**: minimal 500MB
- **Printer**: terhubung via USB

### Paket sistem yang diinstall otomatis

```
python3 python3-venv python3-dev python3-systemd
p910nd usbutils sqlite3 curl net-tools iproute2
```

---

## Instalasi

### 1. Clone atau download

```bash
git clone https://github.com/mff-28/usb-printer-server.git
cd usb-printer-server
```

### 2. Jalankan installer

```bash
sudo bash install.sh
```

Installer akan otomatis:
1. Install semua dependensi sistem
2. Buat virtual environment Python di `/opt/usb-printer/venv`
3. Buat user `printer` dan group `lp`
4. Copy file aplikasi ke `/opt/usb-printer/`
5. Setup konfigurasi di `/etc/usb-printer/`
6. Inisialisasi database SQLite
7. Install dan enable semua systemd service
8. Install udev rules
9. Prompt buat akun admin pertama
10. Start semua service

### 3. Akses dashboard

Buka browser ke:
```
http://IP-SERVER:8070
```

Login dengan akun yang dibuat saat instalasi.

---

## Struktur File

```
usb-printer-server/
├── app.py                    # Flask application + API routes
├── printer.py                # p910nd management (start/stop/status)
├── worker.py                 # Print job queue processor
├── watchdog.py               # USB printer watchdog (auto-recover)
├── monitor.py                # Print activity logger
├── auth.py                   # Authentication (login/logout/CSRF)
├── queue_manager.py          # SQLite queue manager
├── manage.py                 # CLI management tool
├── gunicorn.conf.py          # Gunicorn configuration
├── install.sh                # Installer script
├── udev-add.sh               # udev handler: printer dicolok/nyala
├── udev-remove.sh            # udev handler: printer dicabut
├── 99-usb-printer.rules      # udev rules
├── requirements.txt          # Python dependencies
├── migrations/               # SQLite migration scripts
│   ├── 001_init.sql
│   ├── 002_upgrade_from_v2.sql
│   └── 003_add_print_activity.sql
├── systemd/
│   ├── usb-printer.service
│   ├── usb-printer-worker.service
│   ├── usb-printer-monitor.service
│   └── usb-printer-watchdog.service
├── templates/
│   ├── index.html            # Dashboard UI
│   └── login.html
└── static/
    └── luci.css              # UI theme (OpenWrt LuCI-inspired)
```

### Path di server setelah install

| Path | Isi |
|---|---|
| `/opt/usb-printer/` | File aplikasi |
| `/opt/usb-printer/venv/` | Python virtual environment |
| `/etc/usb-printer/config.json` | Konfigurasi port per-printer |
| `/etc/usb-printer/auth.json` | Data user login |
| `/var/lib/usb-printer/queue.db` | Database antrian & aktivitas |
| `/var/spool/usb-printer/` | Spool file print job |
| `/run/usb-printer/` | PID files & lock files (runtime) |

---

## Konfigurasi

### Port per printer

Edit via dashboard → **Printer Manager** → **Konfigurasi**, atau edit langsung:

```json
// /etc/usb-printer/config.json
{
  "/dev/usb/lp0": {
    "port": 9100,
    "bidirectional": false
  },
  "/dev/usb/lp1": {
    "port": 9101,
    "bidirectional": false
  }
}
```

Port yang didukung: **9100–9109** (p910nd v0.97 limitation).

### Watchdog interval

Default: cek setiap 3 detik. Ubah di `/etc/systemd/system/usb-printer-watchdog.service`:

```ini
Environment=WATCHDOG_INTERVAL=3
```

---

## API Endpoints

Semua endpoint memerlukan autentikasi (session cookie).

| Method | Endpoint | Fungsi |
|---|---|---|
| `GET` | `/api/printers` | List semua printer + status |
| `POST` | `/api/start` | Start p910nd untuk printer |
| `POST` | `/api/stop` | Stop p910nd untuk printer |
| `POST` | `/api/restart` | Restart p910nd untuk printer |
| `GET` | `/api/dashboard` | Statistik ringkasan |
| `GET` | `/api/print-activity/summary` | Ringkasan aktivitas print |
| `GET` | `/api/print-activity/recent` | Log aktivitas terbaru |
| `GET` | `/api/logs` | System log |
| `GET` | `/health` | Health check (tidak perlu auth) |

### Contoh request

```bash
# Login dulu
curl -c cookies.txt -X POST http://SERVER:8080/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"password"}'

# Lihat status printer
curl -b cookies.txt http://SERVER:8080/api/printers

# Start printer lp1
curl -b cookies.txt -X POST http://SERVER:8080/api/start \
  -H "Content-Type: application/json" \
  -d '{"device":"/dev/usb/lp1"}'
```

---

## Setup Windows (Print Client)

### Menggunakan Standard TCP/IP Port

1. **Control Panel** → **Devices and Printers** → **Add a printer**
2. Pilih **"Add a local printer or network printer with manual settings"**
3. **"Create a new port"** → pilih **"Standard TCP/IP Port"**
4. Masukkan:
   - **Hostname**: IP server (contoh: `192.168.1.10`)
   - **Port name**: `USB-Printer-1` (bebas)
5. **Device Type**: Generic Network Card
6. **Protocol**: Raw, **Port Number**: `9100` (atau sesuai config)
7. Install driver printer sesuai model

### Menggunakan LPR

1. Langkah 1–4 sama seperti di atas
5. **Protocol**: LPR, **Queue Name**: `lp0`

---

## Cara Kerja Watchdog

Masalah klasik: printer dimatikan (power off) **tanpa** mencabut kabel USB — udev event `remove` tidak fired, p910nd tidak tahu printer mati.

Solusi watchdog:

```
Loop setiap 3 detik:
  ├── Device ada + p910nd mati/stale → AUTO START p910nd
  ├── Device hilang + p910nd jalan  → AUTO STOP p910nd bersih
  ├── User Stop dari dashboard       → Set manual-stop flag, SKIP restart
  ├── User Start dari dashboard      → Clear flag, watchdog jaga lagi
  └── Port berubah di config         → Restart p910nd ke port baru otomatis
```

### File flag di `/run/usb-printer/`

| File | Fungsi |
|---|---|
| `port-9100.pid` | PID proses p910nd untuk port 9100 |
| `port-9100.lock` | flock untuk prevent race condition |
| `port-9100.manual-stop` | Flag: user sengaja stop, watchdog jangan restart |

---

## Manajemen Service

```bash
# Status semua service
systemctl status usb-printer usb-printer-worker usb-printer-watchdog usb-printer-monitor

# Restart semua
systemctl restart usb-printer usb-printer-worker usb-printer-watchdog

# Lihat log real-time
journalctl -t usb-printer -f
journalctl -t usb-printer-watchdog -f

# Manual start/stop p910nd (tanpa watchdog)
/opt/usb-printer/udev-add.sh lp0    # Start printer lp0
/opt/usb-printer/udev-add.sh lp1    # Start printer lp1
/opt/usb-printer/udev-remove.sh lp0 # Stop printer lp0
```

---

## Troubleshooting

### Printer offline di Windows padahal sudah nyala

```bash
# Cek status p910nd
ss -tlnp | grep -E '9100|9101'

# Cek log watchdog
journalctl -t usb-printer-watchdog -n 20

# Manual recover
/opt/usb-printer/udev-add.sh lp0
/opt/usb-printer/udev-add.sh lp1
```

Di Windows: klik kanan printer → **See what's printing** → **Printer** → **Use Printer Online**

### p910nd error "Operation not permitted"

```bash
# Cek permission device
ls -la /dev/usb/lp*
# Harus: crw-rw---- root lp

# Fix permission
usermod -aG lp printer
udevadm trigger
```

### Dashboard error 500

```bash
journalctl -u usb-printer.service -n 50 --no-pager | grep -i error
```

### Cek semua proses berjalan

```bash
# p910nd
ps aux | grep p910nd | grep -v grep

# Port listening
ss -tlnp | grep -E '8080|9100|9101'

# Services
systemctl is-active usb-printer usb-printer-worker usb-printer-watchdog
```

### Reset database

```bash
systemctl stop usb-printer-worker usb-printer-monitor
rm /var/lib/usb-printer/queue.db
systemctl start usb-printer  # auto-recreate saat start
```

---

## Upgrade

### Dari v3 ke v3-fixed (patch watchdog)

```bash
cd usb-printer-server

systemctl stop usb-printer usb-printer-worker usb-printer-watchdog

# Copy file yang diupdate
cp app.py printer.py worker.py monitor.py watchdog.py /opt/usb-printer/
cp udev-add.sh /opt/usb-printer/ && chmod +x /opt/usb-printer/udev-add.sh

# Install service watchdog (baru di v3-fixed)
cp systemd/usb-printer-watchdog.service /etc/systemd/system/
cp systemd/usb-printer.service /etc/systemd/system/
systemctl daemon-reload

systemctl enable usb-printer-watchdog
systemctl start usb-printer usb-printer-worker usb-printer-watchdog
```

---

## Changelog

### v3-fixed (2026-06-07)
- **Fix**: Printer dimatikan (power off) tanpa cabut USB kini auto-recover dalam ≤3 detik
- **New**: `watchdog.py` — monitor aktif setiap 3 detik, auto start/stop p910nd
- **Fix**: `udev-add.sh` — argumen `lp0`/`lp1` kini dipetakan ke device yang tepat (bukan selalu lp0)
- **Fix**: Port portable — baca dari `config.json` bukan hardcode
- **Fix**: p910nd v0.97 tidak support flag `-p`, ganti ke positional argument offset
- **Fix**: Counter "Total Hari Ini" di dashboard selalu 0 (timezone UTC/localtime mismatch)
- **Fix**: "Port sudah digunakan oleh proses lain" saat Start printer yang sudah berjalan
- **Fix**: `Group=printer` → `Group=lp` di monitor service
- **Fix**: Line endings CRLF di monitor.py

### v3 (2026-06-02)
- Rilis awal dengan Flask + Gunicorn + SQLite queue
- Multi-printer support
- Dashboard web dengan statistik
- Autentikasi session

---

## Lisensi

MIT License — bebas digunakan dan dimodifikasi.

---

## Kontribusi

Pull request dan issue sangat disambut. Pastikan:
1. Test di Debian 12/13
2. Ikuti code style yang ada
3. Update CHANGELOG di README
