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
    if "/" in t:            # "ntfy.sh/my-topic" or "https://ntfy.sh/my-topic" -> "my-topic"
        t = t.rstrip("/").rsplit("/", 1)[-1]
    return t.strip()


def topic_problem(topic: str) -> str:
    """'' if ntfy accepts this topic name, else what's wrong (without showing the topic)."""
    import re
    if re.fullmatch(r"[-_A-Za-z0-9]{1,64}", topic):
        return ""
    odd = sorted({"spaces" if c.isspace() else repr(c) for c in topic if not re.match(r"[-_A-Za-z0-9]", c)})
    return "it contains characters ntfy doesn't allow: " + ", ".join(odd)


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
