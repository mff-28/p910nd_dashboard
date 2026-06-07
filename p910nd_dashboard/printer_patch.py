import re

with open('printer.py', 'r') as f:
    content = f.read()

# Ganti get_p910nd_status
old_status = '''def get_p910nd_status(device: str, port: int = 9100) -> str:
    """Return: 'running' | 'stopped' | 'error'\"\"\"
    if not os.path.exists(device):
        return "offline"   # Printer dicabut

    if _is_port_in_use(port):
        return "running"

    # Konfirmasi via pgrep
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"p910nd.*{re.escape(device)}"],
            capture_output=True, text=True, timeout=3
        )
        if result.stdout.strip():
            return "running"
    except Exception:
        pass

    return "stopped"'''

new_status = '''def get_p910nd_status(device: str, port: int = 9100) -> str:
    """Return: \'running\' | \'stopped\' | \'offline\' | \'stale\'
    
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
    return ok, msg'''

content = content.replace(old_status, new_status)

with open('printer.py', 'w') as f:
    f.write(content)

print("printer.py patched")
