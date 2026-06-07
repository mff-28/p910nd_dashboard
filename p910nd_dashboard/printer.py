#!/usr/bin/env python3
"""
printer.py — USB printer detection dan p910nd management
v3: flock-based locking, device validation, auto-recovery
"""

import subprocess
import os
import glob
import re
import json
import signal
import time
import fcntl
import logging

log = logging.getLogger("usb-printer")

CONFIG_FILE = os.environ.get("CONFIG_FILE", "/etc/usb-printer/config.json")
LOCK_DIR    = os.environ.get("LOCK_DIR", "/run/usb-printer")

VENDOR_NAMES = {
    "04b8": "Epson",
    "03f0": "HP",
    "04a9": "Canon",
    "04f9": "Brother",
    "0924": "Xerox",
    "04e8": "Samsung",
    "0482": "Kyocera",
    "1a86": "Lenovo",
    "06bc": "Oki",
}

# Port yang diperbolehkan — harus sesuai auth.py
ALLOWED_PORTS = set(range(9100, 9111))  # 9100–9110 inclusive


# ── Lock file per-port (flock) ────────────────────────────────────────────────

def _lock_path(port: int) -> str:
    os.makedirs(LOCK_DIR, exist_ok=True)
    return os.path.join(LOCK_DIR, f"port-{port}.lock")


def _pid_path(port: int) -> str:
    os.makedirs(LOCK_DIR, exist_ok=True)
    return os.path.join(LOCK_DIR, f"port-{port}.pid")


def _acquire_lock(port: int):
    """
    Acquire eksklusif flock pada file lock.
    Return (lock_fd, True) jika berhasil, (None, False) jika sudah dipakai.
    Non-blocking — langsung return False jika tidak bisa lock.
    """
    path = _lock_path(port)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd, True
    except BlockingIOError:
        os.close(fd)
        return None, False


def _release_lock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except Exception:
        pass


def _write_pid(port: int, pid: int):
    try:
        path = _pid_path(port)
        with open(path, "w") as f:
            f.write(str(pid))
    except Exception as e:
        log.warning(f"Gagal tulis PID file port {port}: {e}")


def _read_pid(port: int) -> int | None:
    try:
        with open(_pid_path(port)) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _clear_pid(port: int):
    try:
        os.unlink(_pid_path(port))
    except Exception:
        pass

def _set_manual_stop(port: int):
    """Beritahu watchdog: user sengaja stop, jangan auto-restart."""
    try:
        open(os.path.join(LOCK_DIR, f"port-{port}.manual-stop"), "w").close()
    except Exception:
        pass

def _clear_manual_stop(port: int):
    """Hapus flag manual-stop: watchdog boleh auto-restart lagi."""
    try:
        os.remove(os.path.join(LOCK_DIR, f"port-{port}.manual-stop"))
    except FileNotFoundError:
        pass


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Proses ada tapi beda user


def _is_port_in_use(port: int) -> bool:
    """
    Cek apakah port sudah dipakai (oleh p910nd manapun — app atau watchdog).
    Prioritas: cek ss (ground truth) dulu, baru PID file.
    """
    # Cek via ss — ini paling akurat, tidak bergantung siapa yang start p910nd
    try:
        result = subprocess.run(
            ["ss", "-tlnp", f"sport = :{port}"],
            capture_output=True, text=True, timeout=3
        )
        if f":{port}" in result.stdout:
            return True
    except Exception:
        pass

    # Fallback: cek PID file
    pid = _read_pid(port)
    if pid and _is_pid_alive(pid):
        return True

    # PID sudah mati, bersihkan
    _clear_pid(port)
    return False


# ── USB Printer Detection ─────────────────────────────────────────────────────

def _read_sysfs(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


def _find_usb_device_root(sysfs_path: str) -> str:
    """
    Dari interface path (e.g., /devices/.../1-3.1:1.0), naik parent untuk cari device root.
    PENTING: hanya naik sampai USB device (matches \\d+-\\d+), jangan sampai PCI host.
    Cari file idVendor/product di parent directories.
    """
    try:
        path = os.path.realpath(sysfs_path)
        # Cek langsung path yang diberikan
        if os.path.exists(os.path.join(path, "idVendor")):
            return path
        
        # Naik parent, tapi stop di USB device path
        for _ in range(10):  # Max 10 level
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
            
            # Check jika ada idVendor
            if os.path.exists(os.path.join(path, "idVendor")):
                # Verifikasi ini adalah USB device (path berisi usb\d dan akhir dengan \d-\d)
                if re.search(r"/usb\d+/\d+-[\d.]+$", path):
                    return path
                # Jika tidak match USB pattern, lanjut naik parent
                # tapi jangan sampai PCI host (0000:00:xx)
                if re.search(r"pci0000:|0000:00:", path):
                    # Sudah sampai PCI host, return previous path yang valid
                    return ""
    except Exception:
        pass
    return ""


def _extract_device_from_devpath(devpath: str) -> str:
    """
    Extract USB device path dari udevadm DEVPATH.
    Dari: /devices/pci.../usb1/1-3/1-3.1/1-3.1:1.0/usbmisc/lp0
    Ke:   /devices/pci.../usb1/1-3/1-3.1
    Pattern: remove everything after usb device (interface dan driver).
    """
    try:
        # Interface paths berakhir dengan :X.Y, remove dari sana ke depan
        match = re.search(r"^(.+?)(?:/\d+-[\d.]+:\d+\.\d+/|$)", devpath)
        if match:
            base = match.group(1)
            # Coba cari /usb atau /1-x pattern terakhir untuk device root
            parts = base.split("/")
            for i in range(len(parts) - 1, -1, -1):
                if re.match(r"^\d+-[\d.]*\d+$", parts[i]):
                    # Ini adalah USB device path (e.g., 1-3, 1-3.1)
                    return "/".join(parts[:i+1])
        return base if "usb" in base else ""
    except Exception:
        return ""


def _get_usb_info_from_udev(dev: str) -> dict:
    """Fallback: gunakan udevadm --attribute-walk untuk baca info USB dari parent devices."""
    info = {}
    try:
        result = subprocess.run(
            ["udevadm", "info", "--name", dev, "--attribute-walk"],
            capture_output=True, text=True, timeout=5
        )
        # Parse ATTRS dari output
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("ATTRS{idVendor}=="):
                match = re.search(r'=="([^"]+)"', line)
                if match:
                    info["_vid"] = match.group(1)
            elif line.startswith("ATTRS{idProduct}=="):
                match = re.search(r'=="([^"]+)"', line)
                if match:
                    info["_pid"] = match.group(1)
                    if "_vid" in info:
                        info["usb_id"] = f"{info['_vid']}:{info['_pid']}"
            elif line.startswith("ATTRS{manufacturer}=="):
                match = re.search(r'=="([^"]+)"', line)
                if match:
                    info["vendor"] = match.group(1)
            elif line.startswith("ATTRS{product}=="):
                match = re.search(r'=="([^"]+)"', line)
                if match:
                    info["model"] = match.group(1)
            elif line.startswith("ATTRS{serial}=="):
                match = re.search(r'=="([^"]+)"', line)
                if match:
                    info["serial"] = match.group(1)
    except Exception:
        pass
    return info


def get_usb_printers() -> list:
    printers = []
    devices = sorted(glob.glob("/dev/usb/lp[0-9]"))  # hanya lp0-lp9

    for dev in devices:
        info = {
            "device": dev,
            "name":   "Unknown Printer",
            "vendor": "Unknown",
            "model":  "",
            "serial": "",
            "usb_id": "",
            "online": os.path.exists(dev),
        }

        lp_match = re.search(r"lp(\d+)$", dev)
        if not lp_match:
            continue
        n = lp_match.group(1)

        # Ambil nama printer langsung dari sysfs usbmisc, bila tersedia.
        manufacturer = _read_sysfs(f"/sys/class/usbmisc/lp{n}/device/../manufacturer")
        product = _read_sysfs(f"/sys/class/usbmisc/lp{n}/device/../product")
        serial = _read_sysfs(f"/sys/class/usbmisc/lp{n}/device/../serial")

        if manufacturer or product:
            if manufacturer:
                info["vendor"] = manufacturer
            if product:
                info["model"] = product
            if manufacturer and product:
                info["name"] = f"{manufacturer} {product}"
            elif product:
                info["name"] = product
            elif manufacturer:
                info["name"] = manufacturer
            if serial:
                info["serial"] = serial
            printers.append(info)
            continue

        # Cari sysfs path
        sysfs_candidates = glob.glob(
            f"/sys/class/usb_printer/lp{n}/device"
        ) + glob.glob(
            f"/sys/class/usblp/lp{n}/device"
        )
        usb_base = None
        for candidate in sysfs_candidates:
            try:
                real = os.path.realpath(candidate)
                log.debug(f"[{dev}] sysfs candidate: {real}")
                if os.path.exists(os.path.join(real, "idVendor")):
                    usb_base = real
                else:
                    parent = os.path.dirname(real)
                    if os.path.exists(os.path.join(parent, "idVendor")):
                        usb_base = parent
                        log.debug(f"[{dev}] parent path has idVendor: {parent}")
                    elif re.search(r":\d+\.\d+$", real):
                        usb_base = _find_usb_device_root(real)
                        log.debug(f"[{dev}] deep search path: {usb_base}")
                if usb_base and os.path.exists(os.path.join(usb_base, "idVendor")):
                    log.debug(f"[{dev}] found idVendor at {usb_base}")
                    break
            except Exception as e:
                log.debug(f"[{dev}] sysfs error: {e}")
                continue

        if usb_base:
            log.debug(f"[{dev}] using sysfs path: {usb_base}")
            vendor_id    = _read_sysfs(os.path.join(usb_base, "idVendor"))
            product_id   = _read_sysfs(os.path.join(usb_base, "idProduct"))
            manufacturer = _read_sysfs(os.path.join(usb_base, "manufacturer"))
            product      = _read_sysfs(os.path.join(usb_base, "product"))
            serial       = _read_sysfs(os.path.join(usb_base, "serial"))

            log.debug(f"[{dev}] sysfs read: vendor={vendor_id} product={product_id} mfg={manufacturer} prod={product}")

            info["usb_id"] = f"{vendor_id}:{product_id}" if vendor_id and product_id else ""
            info["serial"] = serial

            if manufacturer:
                info["vendor"] = manufacturer
            elif vendor_id in VENDOR_NAMES:
                info["vendor"] = VENDOR_NAMES[vendor_id]

            if product:
                info["model"] = product
                info["name"]  = f"{info['vendor']} {product}"
            else:
                info["name"] = f"{info['vendor']} ({dev})"
        else:
            # Fallback: gunakan udevadm
            log.debug(f"[{dev}] sysfs failed, trying udevadm fallback")
            udev_info = _get_usb_info_from_udev(dev)
            log.debug(f"[{dev}] udevadm result: {udev_info}")
            if udev_info.get("vendor"):
                info["vendor"] = udev_info["vendor"]
            if udev_info.get("model"):
                info["model"] = udev_info["model"]
            if udev_info.get("serial"):
                info["serial"] = udev_info["serial"]
            if udev_info.get("usb_id"):
                info["usb_id"] = udev_info["usb_id"]
            
            if info["model"]:
                info["name"] = f"{info['vendor']} {info['model']}"
            elif info["vendor"] != "Unknown":
                info["name"] = info["vendor"]

        log.info(f"[{dev}] final: name={info['name']} vendor={info['vendor']} usb_id={info['usb_id']}")
        printers.append(info)

    return printers


# ── p910nd Status ─────────────────────────────────────────────────────────────

def get_p910nd_status(device: str, port: int = 9100) -> str:
    """Return: 'running' | 'stopped' | 'offline' | 'stale'
    
    Catatan:
    - offline  = device /dev/usb/lpX tidak ada (printer dicabut/belum dicolok)
    - stale    = PID masih hidup tapi port tidak LISTEN (printer power off/on tanpa cabut USB)
    - stopped  = tidak ada proses p910nd aktif
    - running  = p910nd aktif dan port sedang LISTEN
    """
    if not os.path.exists(device):
        return "offline"   # Device node tidak ada (printer dicabut secara fisik)

    # Cek PID file
    pid = _read_pid(port)
    pid_alive = pid and _is_pid_alive(pid)

    # Verifikasi port masih LISTEN — ini poin kritis untuk detect printer power off/on
    port_listening = False
    try:
        result = subprocess.run(
            ["ss", "-tlnp", f"sport = :{port}"],
            capture_output=True, text=True, timeout=3
        )
        port_listening = f":{port}" in result.stdout
    except Exception:
        # ss tidak tersedia, fallback ke PID check saja
        port_listening = bool(pid_alive)

    if port_listening:
        return "running"

    if pid_alive:
        # PID hidup tapi port tidak listen = stale state
        # Ini terjadi waktu printer dimatiin (power off) tanpa cabut USB:
        # - /dev/usb/lp0 masih ada (USB device masih terdeteksi)
        # - p910nd PID masih hidup di proses tabel
        # - Tapi p910nd tidak bisa write ke device dan port tidak listen
        log.warning(f"[{device}:{port}] Status STALE: PID {pid} hidup tapi port :{port} tidak listen")
        # Cleanup PID file yang stale
        try:
            os.kill(pid, 0)  # Verifikasi sekali lagi
            # Masih hidup, kill biar bersih
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            if _is_pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _clear_pid(port)
        return "stale"

    # Fallback: cek via pgrep
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"p910nd.*{re.escape(device)}"],
            capture_output=True, text=True, timeout=3
        )
        if result.stdout.strip():
            return "running"
    except Exception:
        pass

    _clear_pid(port)
    return "stopped"


def auto_recover_p910nd(device: str, port: int = 9100, bidirectional: bool = False) -> tuple:
    """
    Auto-recover p910nd jika printer tersedia tapi service tidak running.
    Dipanggil dari app.py (api_printers) dan worker.py sebelum print.
    
    Return: (recovered: bool, message: str)
    """
    if not os.path.exists(device):
        return False, f"Device {device} tidak ada"
    
    status = get_p910nd_status(device, port)
    
    if status == "running":
        return True, "Sudah running"
    
    # Status: stopped, stale — coba restart
    log.info(f"[{device}:{port}] Auto-recover: status={status}, mencoba restart p910nd...")
    ok, msg = start_p910nd(device, port, bidirectional)
    if ok:
        log.info(f"[{device}:{port}] Auto-recover BERHASIL: {msg}")
    else:
        log.error(f"[{device}:{port}] Auto-recover GAGAL: {msg}")
    return ok, msg


# ── Start / Stop / Restart ────────────────────────────────────────────────────

def start_p910nd(device: str, port: int = 9100, bidirectional: bool = False):
    """
    Mulai p910nd untuk device dan port yang diberikan.
    Menggunakan flock untuk mencegah race condition.
    """

    if not re.match(r"^/dev/usb/lp[0-9]$", device):
        return False, f"Device path tidak valid: {device}"

    if not os.path.exists(device):
        return False, f"Device {device} tidak ditemukan (printer dicabut?)"

    if port not in ALLOWED_PORTS:
        return False, f"Port {port} tidak diizinkan"

    # Hapus manual-stop flag — user minta start lagi
    _clear_manual_stop(port)

    lock_fd, acquired = _acquire_lock(port)
    if not acquired:
        return False, f"Port {port} sedang digunakan (lock aktif)"

    try:
        if _is_port_in_use(port):
            # Port sudah LISTEN = p910nd sudah berjalan
            # (bisa distart watchdog atau app — tidak perlu pgrep)
            # Catat PID ke file jika belum ada (sinkronisasi dengan watchdog)
            if _read_pid(port) is None:
                try:
                    r = subprocess.run(
                        ["pgrep", "-f", f"p910nd.*{device}"],
                        capture_output=True, text=True
                    )
                    if r.stdout.strip():
                        _write_pid(port, int(r.stdout.strip().split()[0]))
                except Exception:
                    pass
            return True, f"Printer sudah berjalan di port {port}"

        cmd = ["p910nd"]

        if bidirectional:
            cmd.append("-b")

        # p910nd menerima port index 0–10 (mapped ke 9100–9110)
        if 9100 <= port <= 9110:
            p910_port = str(port - 9100)
        else:
            return False, f"Port {port} tidak didukung (gunakan 9100–9110)"

        cmd += ["-f", device, p910_port]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            close_fds=True,
        )

        time.sleep(1.2)

        if proc.poll() is None:
            _write_pid(port, proc.pid)

            log.info(
                f"p910nd started: {device} -> :{port} "
                f"(PID {proc.pid})"
            )

            return True, f"p910nd started: {device} → port {port}"

        err = proc.stderr.read().decode(
            "utf-8",
            errors="replace"
        ).strip()

        log.error(
            f"p910nd gagal start {device}:{port}: {err}"
        )

        return False, f"p910nd gagal: {err or 'exit non-zero'}"

    except FileNotFoundError:
        return _start_socat(device, port)

    except Exception as e:
        log.error(f"Exception start_p910nd: {e}")
        return False, str(e)

    finally:
        _release_lock(lock_fd)

def _start_socat(device: str, port: int):
    """Fallback ke socat jika p910nd tidak ada."""
    try:
        proc = subprocess.Popen(
            ["socat",
             f"TCP-LISTEN:{port},reuseaddr,fork",
             f"OPEN:{device},wronly"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        time.sleep(0.8)
        if proc.poll() is None:
            _write_pid(port, proc.pid)
            log.info(f"socat fallback started: {device} -> :{port}")
            return True, f"socat started: port {port} (mode fallback)"
        else:
            err = proc.stderr.read().decode("utf-8", errors="replace").strip()
            return False, f"socat gagal: {err}"
    except FileNotFoundError:
        return False, "p910nd dan socat tidak ditemukan. Install: apt install p910nd"
    except Exception as e:
        return False, str(e)


def stop_p910nd(device: str, port: int = 9100):
    """Hentikan p910nd. Tandai job aktif sebagai gagal.
    Set manual-stop flag supaya watchdog tidak auto-restart.
    """
    _set_manual_stop(port)  # Beritahu watchdog: ini stop manual
    killed = False

    pid = _read_pid(port)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            if _is_pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
            killed = True
            log.info(f"p910nd stopped: PID {pid} port {port}")
        except ProcessLookupError:
            pass
        except Exception as e:
            log.warning(f"stop_p910nd error: {e}")
        finally:
            _clear_pid(port)

    # Fallback: pgrep
    for prog in ("p910nd", "socat"):
        try:
            result = subprocess.run(
                ["pgrep", "-f", f"{prog}.*{port}"],
                capture_output=True, text=True, timeout=3
            )
            for pid_str in result.stdout.strip().splitlines():
                try:
                    os.kill(int(pid_str), signal.SIGTERM)
                    killed = True
                except Exception:
                    pass
        except Exception:
            pass

    status = "Printer server stopped" if killed else "Tidak ada proses aktif"
    return True, status


def restart_p910nd(device: str, port: int = 9100, bidirectional: bool = False):
    stop_p910nd(device, port)
    time.sleep(1)
    return start_p910nd(device, port, bidirectional)


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(config: dict):
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(config, f, indent=2)
        os.replace(tmp, CONFIG_FILE)  # atomic
    except Exception as e:
        log.error(f"save_config gagal: {e}")
