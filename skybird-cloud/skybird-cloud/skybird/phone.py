"""Using the dashboard from your phone.

Turned on in Settings → Use SkyBird on your phone. SkyBird then listens on the
home network (not just this PC) and asks for a PIN on any device other than the
PC itself. The PIN is stored only as a salted hash in secrets.json.

- At home: the phone opens http://<this PC's Wi-Fi address>:8787.
- Anywhere: install Tailscale (free) on the PC and the phone; SkyBird shows the
  Tailscale address too. Nothing is opened to the public internet either way.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import socket
import time

from .config import Config
from .settings import load_settings

FAILS: dict[str, list[float]] = {}
MAX_FAILS, LOCKOUT = 5, 300   # 5 wrong PINs -> wait 5 minutes


def enabled(cfg: Config) -> bool:
    return bool(load_settings(cfg.data_dir).get("phone_access"))


def host(cfg: Config) -> str:
    return "0.0.0.0" if enabled(cfg) else cfg.section("server").get("host", "127.0.0.1")


def _hash(pin: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 200_000).hex()


def set_pin(cfg: Config, pin: str) -> None:
    salt = os.urandom(16)
    cfg.secrets["dashboard_pin_salt"] = salt.hex()
    cfg.secrets["dashboard_pin_hash"] = _hash(pin, salt)
    cfg.save_secrets()


def has_pin(cfg: Config) -> bool:
    return bool(cfg.secrets.get("dashboard_pin_hash") or cfg.section("server").get("pin"))


def check_pin(cfg: Config, pin: str) -> bool:
    plain = str(cfg.section("server").get("pin", "") or "")
    if plain and hmac.compare_digest(pin, plain):
        return True
    h, salt = cfg.secrets.get("dashboard_pin_hash"), cfg.secrets.get("dashboard_pin_salt")
    return bool(h and salt and hmac.compare_digest(_hash(pin, bytes.fromhex(salt)), h))


def locked_out(ip: str) -> int:
    """Seconds this address must wait after too many wrong PINs (0 = may try)."""
    now = time.time()
    recent = [t for t in FAILS.get(ip, []) if now - t < LOCKOUT]
    FAILS[ip] = recent
    return int(LOCKOUT - (now - recent[0])) if len(recent) >= MAX_FAILS else 0


def failed(ip: str) -> None:
    FAILS.setdefault(ip, []).append(time.time())


def is_local(addr: str | None) -> bool:
    """The request came from the PC itself (no PIN needed there)."""
    try:
        return ipaddress.ip_address(addr or "").is_loopback
    except ValueError:
        return False


def addresses() -> list[dict]:
    """This PC's addresses a phone can use: [{"kind": "wifi"|"tailscale", "ip": ...}]."""
    ips = set()
    try:  # the address used to reach the internet = the home network one (nothing is sent)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    out = []
    for ip in sorted(ips):
        a = ipaddress.ip_address(ip)
        if a.is_loopback or a.is_link_local:
            continue
        if a in ipaddress.ip_network("100.64.0.0/10"):
            out.append({"kind": "tailscale", "ip": ip})
        elif a.is_private:
            out.append({"kind": "wifi", "ip": ip})
    return sorted(out, key=lambda x: x["kind"] != "wifi")


def urls(cfg: Config) -> list[dict]:
    port = cfg.section("server").get("port", 8787)
    return [dict(a, url=f"http://{a['ip']}:{port}") for a in addresses()]


def notify_url(cfg: Config) -> str:
    """Where phone notifications should open: Tailscale (works anywhere) first."""
    us = urls(cfg) if enabled(cfg) else []
    ts = [u for u in us if u["kind"] == "tailscale"]
    return (ts or us or [{"url": ""}])[0]["url"]


def qr_svg(url: str) -> str:
    """A QR code (inline SVG) the iPhone camera can scan to open the dashboard."""
    from ._vendor import segno
    return segno.make(url, error="m").svg_inline(scale=4, border=2, dark="#0A2540", light="#ffffff")
