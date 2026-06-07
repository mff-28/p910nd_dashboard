#!/bin/bash
# install.sh — USB Printer Server v3 untuk Debian 13
# Jalankan sebagai root: sudo bash install.sh
#
# Apa yang dilakukan script ini:
#   1. Install semua dependensi sistem
#   2. Buat virtual environment Python dengan semua package
#   3. Buat user/group 'printer'
#   4. Copy file aplikasi ke /opt/usb-printer
#   5. Setup direktori, permission, sudoers
#   6. Inisialisasi database SQLite
#   7. Install systemd services (utama + worker)
#   8. Install udev rules
#   9. Buat akun admin pertama
#  10. Start semua service

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'

log()  { echo -e "${CYAN}[*]${NC} $1"; }
ok()   { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
hdr()  { echo -e "\n${BOLD}── $1 ──${NC}"; }

[[ "$EUID" -ne 0 ]] && err "Jalankan sebagai root: sudo bash install.sh"

# ── Deteksi Debian 13 ─────────────────────────────────────────────────────────
if [[ -f /etc/os-release ]]; then
    source /etc/os-release
    if [[ "${ID}" != "debian" ]]; then
        warn "Script ini dirancang untuk Debian. OS terdeteksi: ${ID}"
        read -rp "Lanjutkan? [y/N]: " yn
        [[ "${yn,,}" != "y" ]] && exit 1
    fi
fi

INSTALL_DIR="/opt/usb-printer"
VENV_DIR="$INSTALL_DIR/venv"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="/etc/usb-printer"
DATA_DIR="/var/lib/usb-printer"
SPOOL_DIR="/var/spool/usb-printer"
RUN_DIR="/run/usb-printer"
LOG_TAG="usb-printer"

echo -e "\n${BOLD}╔══════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║   USB Printer Server v3 — Installer       ║${NC}"
echo -e "${BOLD}║   Debian 13 + Gunicorn + SQLite Queue      ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════╝${NC}\n"

# ── 1. Dependensi sistem ──────────────────────────────────────────────────────
hdr "1. Dependensi Sistem"
log "Update apt dan install paket..."

apt-get update -qq

PACKAGES=(
    python3
    python3-venv
    python3-dev
    python3-systemd
    p910nd
    usbutils
    sqlite3
    curl
    net-tools
    iproute2
)

apt-get install -y -qq "${PACKAGES[@]}"
ok "Dependensi sistem terinstall"

# ── 2. Virtual Environment Python ────────────────────────────────────────────
hdr "2. Python Virtual Environment"
log "Membuat venv di $VENV_DIR..."

mkdir -p "$INSTALL_DIR"
python3 -m venv "$VENV_DIR" --system-site-packages

log "Menginstall Python packages..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip

"$VENV_DIR/bin/pip" install --quiet \
    "flask>=3.0,<4" \
    "flask-wtf>=1.2" \
    "werkzeug>=3.0" \
    "gunicorn>=22.0" \
    "gevent>=24.0" \
    "greenlet>=3.0"

ok "Python packages terinstall:"
"$VENV_DIR/bin/pip" show flask gunicorn | grep -E "^(Name|Version):" | paste - -

# ── 3. User dan Group ────────────────────────────────────────────────────────
hdr "3. User System"
log "Membuat user 'printer'..."

if getent group lp >/dev/null 2>&1; then
    ok "Group 'lp' sudah ada"
else
    groupadd --system lp
    ok "Group 'lp' dibuat"
fi

if id printer &>/dev/null; then
    ok "User 'printer' sudah ada"
else
    useradd \
        --system \
        --no-create-home \
        --shell /usr/sbin/nologin \
        --gid lp \
        --groups lp \
        --comment "USB Printer Server" \
        printer
    ok "User 'printer' dibuat"
fi

usermod -aG lp printer 2>/dev/null || true

# ── 4. Sudoers ───────────────────────────────────────────────────────────────
hdr "4. Sudoers"
SUDOERS_FILE="/etc/sudoers.d/usb-printer"
cat > "$SUDOERS_FILE" << 'SUDOERS'
# USB Printer Server v3
# User 'printer' boleh menjalankan p910nd dan socat saja
# Tidak ada wildcard — hanya binary yang explicit
Defaults!/usr/sbin/p910nd env_reset
printer ALL=(root) NOPASSWD: /usr/sbin/p910nd
printer ALL=(root) NOPASSWD: /usr/bin/p910nd
printer ALL=(root) NOPASSWD: /usr/bin/socat
SUDOERS
chmod 440 "$SUDOERS_FILE"
visudo -c -f "$SUDOERS_FILE" || { err "Sudoers file tidak valid!"; }
ok "Sudoers dikonfigurasi"

# ── 5. Copy file aplikasi ────────────────────────────────────────────────────
hdr "5. File Aplikasi"
log "Copy ke $INSTALL_DIR..."

mkdir -p "$INSTALL_DIR/templates" "$INSTALL_DIR/static" "$INSTALL_DIR/systemd" "$INSTALL_DIR/migrations"

FILES_TO_COPY=(
    app.py printer.py worker.py manage.py auth.py queue_manager.py
    monitor.py
    gunicorn.conf.py
    udev-add.sh udev-remove.sh
)

for f in "${FILES_TO_COPY[@]}"; do
    if [[ -f "$SCRIPT_DIR/$f" ]]; then
        cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f"
    else
        warn "File tidak ditemukan: $f (skip)"
    fi
done

[[ -f "$SCRIPT_DIR/templates/index.html" ]] && cp "$SCRIPT_DIR/templates/index.html" "$INSTALL_DIR/templates/"
[[ -f "$SCRIPT_DIR/templates/login.html" ]] && cp "$SCRIPT_DIR/templates/login.html" "$INSTALL_DIR/templates/"
[[ -f "$SCRIPT_DIR/static/luci.css" ]]      && cp "$SCRIPT_DIR/static/luci.css"      "$INSTALL_DIR/static/"

for f in "$SCRIPT_DIR/migrations"/*.sql; do
    [[ -f "$f" ]] && cp "$f" "$INSTALL_DIR/migrations/"
done

chmod +x "$INSTALL_DIR/udev-add.sh" "$INSTALL_DIR/udev-remove.sh" "$INSTALL_DIR/manage.py"
chown -R printer:lp "$INSTALL_DIR"
# Venv boleh dimiliki root (lebih aman)
chown -R root:lp "$VENV_DIR"
chmod -R g+rX "$VENV_DIR"

ok "File aplikasi terinstall"

# ── 6. Direktori data, config, spool ────────────────────────────────────────
hdr "6. Direktori"

mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$SPOOL_DIR" "$RUN_DIR"

# Config default
[[ -f "$CONFIG_DIR/config.json" ]] || echo '{}' > "$CONFIG_DIR/config.json"

# Auth file (kosong — diisi saat adduser)
[[ -f "$CONFIG_DIR/auth.json" ]] || echo '{}' > "$CONFIG_DIR/auth.json"

# Secret key
if [[ ! -f "$CONFIG_DIR/secret.key" ]]; then
    python3 -c "import secrets; print(secrets.token_hex(32))" > "$CONFIG_DIR/secret.key"
    chmod 600 "$CONFIG_DIR/secret.key"
fi

chown -R printer:lp "$CONFIG_DIR" "$DATA_DIR" "$SPOOL_DIR" "$RUN_DIR"
chmod 750 "$CONFIG_DIR" "$DATA_DIR" "$SPOOL_DIR"
chmod 700 "$CONFIG_DIR/secret.key" "$CONFIG_DIR/auth.json" 2>/dev/null || true
ok "Direktori disiapkan"

# ── 7. Database SQLite ───────────────────────────────────────────────────────
hdr "7. Database"
log "Inisialisasi SQLite queue database..."

DB_PATH="$DATA_DIR/queue.db"
if [[ ! -f "$DB_PATH" ]]; then
    sqlite3 "$DB_PATH" < "$INSTALL_DIR/migrations/001_init.sql"
    chown printer:lp "$DB_PATH"
    chmod 660 "$DB_PATH"
    ok "Database dibuat: $DB_PATH"
else
    ok "Database sudah ada: $DB_PATH"
fi

# ── 8. tmpfiles.d ────────────────────────────────────────────────────────────
hdr "8. tmpfiles.d"
cat > /etc/tmpfiles.d/usb-printer.conf << 'TF'
# USB Printer Server — runtime directory
d /run/usb-printer 0755 printer lp -
TF
systemd-tmpfiles --create /etc/tmpfiles.d/usb-printer.conf 2>/dev/null || true
ok "tmpfiles.d dikonfigurasi"

# ── 9. systemd services ──────────────────────────────────────────────────────
hdr "9. systemd Services"

cp "$SCRIPT_DIR/systemd/usb-printer.service"         /etc/systemd/system/
cp "$SCRIPT_DIR/systemd/usb-printer-worker.service"  /etc/systemd/system/
cp "$SCRIPT_DIR/systemd/usb-printer-monitor.service" /etc/systemd/system/
cp "$SCRIPT_DIR/watchdog.py"                        "$INSTALL_DIR/"
cp "$SCRIPT_DIR/systemd/usb-printer-watchdog.service" /etc/systemd/system/
systemctl enable usb-printer-watchdog.service
systemctl daemon-reload
systemctl enable usb-printer.service
systemctl enable usb-printer-worker.service
systemctl enable usb-printer-monitor.service
ok "systemd services terinstall dan enabled (termasuk monitor)"

# ── 10. udev rules ───────────────────────────────────────────────────────────
hdr "10. udev Rules"
cp "$SCRIPT_DIR/99-usb-printer.rules" /etc/udev/rules.d/
udevadm control --reload-rules
udevadm trigger
ok "udev rules terinstall"

# ── 12. Start services ───────────────────────────────────────────────────────
hdr "12. Start Services"
systemctl start usb-printer.service
sleep 3
systemctl start usb-printer-worker.service
systemctl start usb-printer-monitor.service

# Verifikasi
if systemctl is-active --quiet usb-printer; then
    ok "usb-printer.service berjalan"
else
    warn "usb-printer.service gagal start"
    echo "    Cek: journalctl -u usb-printer -n 30"
fi

if systemctl is-active --quiet usb-printer-worker; then
    ok "usb-printer-worker.service berjalan"
else
    warn "usb-printer-worker.service gagal start"
fi

if systemctl is-active --quiet usb-printer-monitor; then
    ok "usb-printer-monitor.service berjalan"
else
    warn "usb-printer-monitor.service gagal start (opsional)"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║   ${GREEN}✓ USB Printer Server v3 berhasil diinstall!${NC}${BOLD}        ║${NC}"
echo -e "${BOLD}╠══════════════════════════════════════════════════════╣${NC}"
echo -e "${BOLD}║${NC}                                                      ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}  Buka browser: ${CYAN}http://${IP}:8080${NC}             ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}                                                      ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}  Manajemen user:                                     ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}    sudo $VENV_DIR/bin/python3 $INSTALL_DIR/manage.py adduser    ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}    sudo $VENV_DIR/bin/python3 $INSTALL_DIR/manage.py listusers  ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}                                                      ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}  Perintah sistem:                                    ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}    systemctl status usb-printer                      ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}    systemctl status usb-printer-worker               ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}    journalctl -u usb-printer -f                      ${BOLD}║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${NC}\n"
