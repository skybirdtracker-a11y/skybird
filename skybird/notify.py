"""Phone push notifications through ntfy (free app for iPhone/Android, no account).

config.toml:
    [notify]
    ntfy_topic = "skybird-omaha-8k2x"   # make it long and hard to guess
    dashboard_url = "http://my-pc:8787"  # what the notification opens when tapped
"""
from __future__ import annotations

import requests

from .config import Config


def send(cfg: Config, title: str, message: str, *, priority: str = "default", path: str = "/",
         click: str | None = None) -> bool:
    n = cfg.section("notify")
    topic = (n.get("ntfy_topic") or "").strip()
    if not topic:
        return False
    server = n.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": "airplane"}
    from .phone import notify_url
    url = n.get("dashboard_url") or notify_url(cfg)   # opens the dashboard on your phone when tapped
    if click:
        headers["Click"] = click
    elif url:
        headers["Click"] = url.rstrip("/") + path
    try:
        requests.post(f"{server}/{topic}", data=message.encode("utf-8"), headers=headers, timeout=10)
        return True
    except requests.RequestException:
        return False
