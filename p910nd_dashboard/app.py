#!/usr/bin/env python3
"""
USB Printer Server v3 — Production-grade
Flask + Gunicorn + CSRF + bcrypt
"""


import os
import re
import time
import json
import logging
import secrets
from functools import wraps
from datetime import datetime, timedelta

from flask import (
    Flask, render_template, jsonify, request,
    session, redirect, url_for, abort, g
)
from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.security import check_password_hash
import systemd.journal

from printer import (
    get_usb_printers,
    get_p910nd_status,
    start_p910nd,
    stop_p910nd,
    restart_p910nd,
    auto_recover_p910nd,
)
from auth import load_auth, validate_device_path, validate_port
import sqlite3

# ── Logging ke systemd journal ────────────────────────────────────────────────
log = logging.getLogger("usb-printer")
log.addHandler(systemd.journal.JournalHandler(SYSLOG_IDENTIFIER="usb-printer"))
log.setLevel(logging.INFO)

# ── App factory ───────────────────────────────────────────────────────────────

def create_app():
    app = Flask(__name__)

    # Secret key dari file (dibuat saat install)
    SECRET_KEY_FILE = os.environ.get("SECRET_KEY_FILE", "/etc/usb-printer/secret.key")
    app.secret_key = _load_secret(SECRET_KEY_FILE)

    # Security settings
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=False,     # Set True jika pakai HTTPS
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=50 * 1024 * 1024,  # 50 MB max upload
        WTF_CSRF_TIME_LIMIT=3600,
        WTF_CSRF_SSL_STRICT=False,
    )

    csrf = CSRFProtect(app)

    # ── Error handlers ────────────────────────────────────────────────────────
    @app.errorhandler(CSRFError)
    def handle_csrf(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "CSRF token invalid"}), 400
        return redirect(url_for("login_page"))

    @app.errorhandler(403)
    def forbidden(e):
        return jsonify({"error": "Forbidden"}), 403

    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Not found"}), 404
        return redirect(url_for("index"))

    # ── Auth helpers ──────────────────────────────────────────────────────────
    def login_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("logged_in"):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Unauthorized"}), 401
                return redirect(url_for("login_page"))
            return f(*args, **kwargs)
        return decorated

    def _audit(msg: str):
        username = session.get("username", "system")
        ip = request.remote_addr
        log.info(f"AUDIT [{ip}] [{username}] {msg}")

    def _default_port_for_device(device: str) -> int:
        m = re.match(r"^/dev/usb/lp([0-9])$", device)
        if m:
            return 9100 + int(m.group(1))
        return 9100

    def _open_activity_db():
        db_path = os.environ.get("QUEUE_DB", "/var/lib/usb-printer/queue.db")
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS print_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME NOT NULL,
            client_ip TEXT NOT NULL,
            printer_port INTEGER NOT NULL,
            printer_device TEXT NOT NULL
        );
        """)
        return conn

    # ── Auth routes ───────────────────────────────────────────────────────────
    @app.route("/login", methods=["GET"])
    def login_page():
        if session.get("logged_in"):
            return redirect(url_for("index"))
        error = request.args.get("error", "")
        return render_template("login.html", error=error)

    @app.route("/login", methods=["POST"])
    def login_post():
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        # Validasi input dasar
        if not username or not password:
            return redirect(url_for("login_page", error="invalid"))
        if len(username) > 64 or len(password) > 256:
            return redirect(url_for("login_page", error="invalid"))

        auth = load_auth()
        if not auth:
            return redirect(url_for("login_page", error="setup"))

        stored = auth.get(username)
        # Selalu lakukan check_password_hash untuk menghindari timing attack
        if stored and check_password_hash(stored, password):
            session.clear()
            session.permanent = True
            session["logged_in"] = True
            session["username"] = username
            session["login_time"] = time.time()
            log.info(f"LOGIN OK [{request.remote_addr}] {username}")
            return redirect(url_for("index"))
        else:
            log.warning(f"LOGIN FAIL [{request.remote_addr}] {username}")
            time.sleep(1)  # rate-limit sederhana
            return redirect(url_for("login_page", error="invalid"))

    @app.route("/logout")
    def logout():
        user = session.get("username", "?")
        session.clear()
        log.info(f"LOGOUT {user}")
        return redirect(url_for("login_page"))

    # ── Main routes ───────────────────────────────────────────────────────────
    @app.route("/")
    @login_required
    def index():
        return render_template("index.html", username=session.get("username", ""))

    # ── API: Printers ─────────────────────────────────────────────────────────
    @app.route("/api/printers")
    @login_required
    def api_printers():
        from printer import load_config
        config = load_config()
        printers = get_usb_printers()
        result = []
        for p in printers:
            dev = p["device"]
            cfg = config.get(dev, {})
            port = cfg.get("port", _default_port_for_device(dev))
            bidi = bool(cfg.get("bidirectional", False))
            status = get_p910nd_status(dev, int(port))

            # Auto-recover: jika printer ada tapi p910nd mati/stale, restart otomatis
            # Ini menangani kasus printer dimatiin lalu dinyalain tanpa cabut USB
            if status in ("stopped", "stale") and p.get("online", False):
                log.info(f"Auto-recover triggered for {dev} (status={status})")
                ok_recover, msg_recover = auto_recover_p910nd(dev, int(port), bidi)
                if ok_recover:
                    status = "running"
                    log.info(f"Auto-recover OK: {dev} -> :{port}")
                else:
                    log.warning(f"Auto-recover FAIL: {dev}: {msg_recover}")

            result.append({**p, "port": port, "status": status})
        return jsonify({"printers": result, "timestamp": time.time()})

    @app.route("/api/start", methods=["POST"])
    @login_required
    def api_start():
        data = request.get_json(silent=True) or {}
        device = data.get("device", "")
        port   = data.get("port", 9100)

        # Validasi input
        if not validate_device_path(device):
            return jsonify({"success": False, "message": "Device path tidak valid"}), 400
        if not validate_port(port):
            return jsonify({"success": False, "message": "Port tidak valid (9100-9110)"}), 400

        port = int(port)
        bidirectional = bool(data.get("bidirectional", False))

        from printer import save_config, load_config
        config = load_config()
        config[device] = {"port": port, "bidirectional": bidirectional}
        save_config(config)

        ok, msg = start_p910nd(device, port, bidirectional)
        _audit(f"START {device}:{port} -> {'OK' if ok else 'FAIL'} {msg}")
        return jsonify({"success": ok, "message": msg})

    @app.route("/api/stop", methods=["POST"])
    @login_required
    def api_stop():
        data = request.get_json(silent=True) or {}
        device = data.get("device", "")
        if not validate_device_path(device):
            return jsonify({"success": False, "message": "Device path tidak valid"}), 400

        from printer import load_config
        config = load_config()
        port = config.get(device, {}).get("port", _default_port_for_device(device))
        ok, msg = stop_p910nd(device, port)
        _audit(f"STOP {device}:{port} -> {'OK' if ok else 'FAIL'}")
        return jsonify({"success": ok, "message": msg})

    @app.route("/api/restart", methods=["POST"])
    @login_required
    def api_restart():
        data = request.get_json(silent=True) or {}
        device = data.get("device", "")
        if not validate_device_path(device):
            return jsonify({"success": False, "message": "Device path tidak valid"}), 400

        from printer import load_config
        config = load_config()
        cfg  = config.get(device, {})
        port = int(cfg.get("port", _default_port_for_device(device)))
        bidi = bool(cfg.get("bidirectional", False))
        ok, msg = restart_p910nd(device, port, bidi)
        _audit(f"RESTART {device}:{port} -> {'OK' if ok else 'FAIL'}")
        return jsonify({"success": ok, "message": msg})

    # ── API: Dashboard summary ────────────────────────────────────────────────
    @app.route("/api/dashboard")
    @login_required
    def api_dashboard():
        from printer import load_config
        config   = load_config()
        printers = get_usb_printers()
        p_status = []
        for p in printers:
            dev  = p["device"]
            port = config.get(dev, {}).get("port", _default_port_for_device(dev))
            p_status.append({
                "device": dev,
                "name":   p.get("name", dev),
                "status": get_p910nd_status(dev, port),
                "port":   port,
            })
        return jsonify({
            "printers": p_status,
            "server_time": datetime.now().isoformat(),
        })

    # ── API: Print activity (monitor) ─────────────────────────────────────
    @app.route("/api/print-activity/summary")
    @login_required
    def api_print_activity_summary():
        try:
            conn = _open_activity_db()
            cur = conn.cursor()
            total_all = cur.execute("SELECT COUNT(*) as cnt FROM print_activity").fetchone()[0]
            total_today = cur.execute("SELECT COUNT(*) as cnt FROM print_activity WHERE strftime('%Y-%m-%d', timestamp)=strftime('%Y-%m-%d', 'now')").fetchone()[0]
            printers_active = cur.execute("SELECT COUNT(DISTINCT printer_device) as cnt FROM print_activity WHERE timestamp >= datetime('now', '-1 day')").fetchone()[0]
            ports_active = cur.execute("SELECT COUNT(DISTINCT printer_port) as cnt FROM print_activity WHERE timestamp >= datetime('now', '-1 day')").fetchone()[0]
            last = cur.execute("SELECT timestamp, client_ip, printer_port, printer_device FROM print_activity ORDER BY timestamp DESC LIMIT 10").fetchall()
            last_acts = [dict(r) for r in last]
            top_clients = [dict(r) for r in cur.execute("SELECT client_ip, COUNT(*) as cnt FROM print_activity GROUP BY client_ip ORDER BY cnt DESC LIMIT 10").fetchall()]
        except Exception:
            return jsonify({"error": "DB unavailable"}), 500
        finally:
            try:
                conn.close()
            except Exception:
                pass

        return jsonify({
            "total_all": int(total_all),
            "total_today": int(total_today),
            "printers_active": int(printers_active),
            "ports_active": int(ports_active),
            "last_activity": last_acts,
            "top_clients": top_clients,
        })

    @app.route("/api/print-activity/recent")
    @login_required
    def api_print_activity_recent():
        limit = min(int(request.args.get("limit", 50)), 500)
        try:
            conn = _open_activity_db()
            rows = conn.execute("SELECT id, timestamp, client_ip, printer_port, printer_device FROM print_activity ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
            data = [dict(r) for r in rows]
        except Exception:
            return jsonify({"error": "DB unavailable"}), 500
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return jsonify({"activity": data})

    # ── API: Logs ─────────────────────────────────────────────────────────────
    @app.route("/api/logs")
    @login_required
    def api_logs():
        """Ambil log dari systemd journal."""
        try:
            import subprocess
            result = subprocess.run(
                ["journalctl", "-u", "usb-printer", "-n", "200",
                 "--no-pager", "--output=short-iso"],
                capture_output=True, text=True, timeout=5
            )
            lines = result.stdout.strip().splitlines()
        except Exception:
            lines = ["(log tidak tersedia)"]
        return jsonify({"logs": lines})

    # ── Health check (no auth needed) ─────────────────────────────────────────
    @app.route("/health")
    def health():
        return jsonify({"status": "ok", "time": time.time()})

    return app


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_secret(path: str) -> str:
    try:
        with open(path) as f:
            key = f.read().strip()
        if len(key) >= 32:
            return key
    except FileNotFoundError:
        pass
    key = secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key)
    except Exception as e:
        log.warning(f"Tidak bisa simpan secret key: {e}")
    return key


# ── Entrypoint (dipakai Gunicorn) ─────────────────────────────────────────────
app = create_app()

if __name__ == "__main__":
    # Dev only — production pakai Gunicorn
    app.run(host="0.0.0.0", port=8070, debug=False)
