"""Phone push notifications through ntfy (free app for iPhone/Android, no account).

config.toml:
    [notify]
    ntfy_topic = "skybird-omaha-8k2x"   # make it long and hard to guess
    dashboard_url = "http://my-pc:8787"  # what the notification opens when tapped
"""
from __future__ import annotations

import requests

from .config import Config


LAST = {"result": ""}   # what happened to the last notification (for the test button)


def topic_of(cfg: Config) -> str:
    """The topic, without quotes or a pasted https://ntfy.sh/ in front."""
    t = str(cfg.section("notify").get("ntfy_topic") or "").strip().strip('"\'').strip()
    return t.rsplit("/", 1)[-1] if t.startswith("http") else t


def send(cfg: Config, title: str, message: str, *, priority: str = "default", path: str = "/",
         click: str | None = None) -> bool:
    n = cfg.section("notify")
    topic = topic_of(cfg)
    if not topic:
        LAST["result"] = "no ntfy topic set"
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
        r = requests.post(f"{server}/{topic}", data=message.encode("utf-8"), headers=headers, timeout=10)
        LAST["result"] = f"ntfy answered HTTP {r.status_code}" + ("" if r.ok else f": {r.text[:200]}")
        return r.ok
    except requests.RequestException as e:
        LAST["result"] = f"couldn't reach ntfy: {type(e).__name__}"
        return False
