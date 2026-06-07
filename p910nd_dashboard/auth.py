#!/usr/bin/env python3
"""
auth.py — Authentication helpers
- bcrypt via Werkzeug (pbkdf2:sha256)
- Input validation helpers
"""

import json
import os
import re
import logging
from werkzeug.security import generate_password_hash, check_password_hash

log = logging.getLogger("usb-printer")

AUTH_FILE = os.environ.get("AUTH_FILE", "/etc/usb-printer/auth.json")

# Device paths yang diizinkan: /dev/usb/lp0 sampai /dev/usb/lp9
_DEVICE_RE = re.compile(r"^/dev/usb/lp[0-9]$")

# Port yang diizinkan untuk printer sharing
_ALLOWED_PORTS = set(range(9100, 9111))  # 9100–9110 inclusive


def load_auth() -> dict:
    """Return {username: hashed_password}."""
    try:
        with open(AUTH_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_auth(auth: dict):
    os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
    fd = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(auth, f, indent=2)


def hash_password(password: str) -> str:
    """Gunakan Werkzeug pbkdf2:sha256 (bukan SHA256 bare)."""
    return generate_password_hash(password, method="pbkdf2:sha256:260000")


def verify_password(stored: str, password: str) -> bool:
    return check_password_hash(stored, password)


def validate_device_path(device: str) -> bool:
    """Cegah path traversal dan device yang tidak diizinkan."""
    if not device or not isinstance(device, str):
        return False
    return bool(_DEVICE_RE.match(device))


def validate_port(port) -> bool:
    """Port harus integer dalam range yang diizinkan."""
    try:
        return int(port) in _ALLOWED_PORTS
    except (ValueError, TypeError):
        return False


def validate_username(username: str) -> bool:
    """Username: alphanumeric + underscore + dash, 3-32 char."""
    if not username or not isinstance(username, str):
        return False
    return bool(re.match(r"^[a-zA-Z0-9_\-]{3,32}$", username))
