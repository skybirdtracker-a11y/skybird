"""Event calendar: holidays and Omaha events that change the daily posts.

events.toml holds [[event]] entries. On any day inside an event (or inside its
lead-up window) the event's caption hook and hashtags are added to that day's
posts, and its bonus-post ideas show on the dashboard.
"""
from __future__ import annotations

import tomllib
from datetime import date, timedelta
from pathlib import Path

from .config import Config


def load_events(cfg: Config) -> list[dict]:
    p = cfg.root / "events.toml"
    if not p.exists():
        p = cfg.root.parent / "events.toml"   # cloud/config.toml uses the project's events.toml
    if not p.exists():
        return []
    raw = tomllib.loads(p.read_text(encoding="utf-8"))
    out = []
    for ev in raw.get("event", []):
        ev = dict(ev)
        ev["start"] = _d(ev["start"])
        ev["end"] = _d(ev.get("end", ev["start"]))
        out.append(ev)
    return sorted(out, key=lambda e: (e["start"], -int(e.get("priority", 1))))


def _d(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def events_for(cfg: Config, day: date, slot_key: str | None = None) -> list[dict]:
    """Events active on `day` (including lead-up days), highest priority first.
    Each returned dict gets `phase` = "lead_up" or "during" and the matching
    `caption_hook`."""
    active = []
    for ev in load_events(cfg):
        slots = ev.get("slots", ["all_posts"])
        if slot_key and "all_posts" not in slots and slot_key not in slots:
            continue
        lead = int(ev.get("lead_days", 0))
        if ev["start"] <= day <= ev["end"]:
            phase, hook = "during", ev.get("hook", "")
        elif ev["start"] - timedelta(days=lead) <= day < ev["start"]:
            phase, hook = "lead_up", ev.get("lead_hook", ev.get("hook", ""))
        else:
            continue
        e = dict(ev, phase=phase, caption_hook=hook)
        active.append(e)
    return sorted(active, key=lambda e: -int(e.get("priority", 1)))


def upcoming(cfg: Config, today: date, days: int = 45) -> list[dict]:
    end = today + timedelta(days=days)
    return [e for e in load_events(cfg) if e["end"] >= today and e["start"] - timedelta(days=int(e.get("lead_days", 0))) <= end]
