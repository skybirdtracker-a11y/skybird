"""Settings you change from the dashboard (data/settings.json) and
pre-decisions for future days (data/plan.json)."""
from __future__ import annotations

import json
from pathlib import Path

DEFAULTS = {
    "autopost": True,        # master switch: False = nothing posts (packages still build)
    "dry_run": None,         # None = use config.toml; True/False = dashboard override
    "auto_approve": {},      # {"military": True} = approve that post automatically every day
    "times": {},             # {"commercial": "09:30"} overrides the post time in config.toml
    "mentions": {},          # {"instagram": ["@handle"]} extra accounts to offer as tags
    "media_folder": None,    # None = config.toml [media] folder; else e.g. C:/Users/joshu/OneDrive/Desktop/Sky Bird/videos/{data_date}
    "backend": None,         # None = config.toml [publisher] backend; else upload_post / manual / direct
    "time_tests": True,      # some days, try each post 1-2 hours earlier/later to learn the best time
    "auto_best_times": False,  # switch a post's time on its own once another hour clearly wins
    "phone_access": False,   # listen on the home network so a phone can open the dashboard (PIN required)
    "account_handles": {},   # {"visitomaha": {"instagram": "@visitomaha"}}: handles you looked up and confirmed
}


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def _write(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_settings(data_dir: Path) -> dict:
    s = dict(DEFAULTS)
    s.update(_read(data_dir / "settings.json", {}))
    return s


def save_settings(data_dir: Path, updates: dict) -> dict:
    s = load_settings(data_dir)
    for k, v in updates.items():
        if k == "auto_approve" and isinstance(v, dict):
            s["auto_approve"] = {**s.get("auto_approve", {}), **{a: bool(b) for a, b in v.items()}}
        elif k == "account_handles" and isinstance(v, dict):
            cur = dict(s.get("account_handles", {}))
            for key, per in v.items():
                cur[key] = {p: h for p, h in {**cur.get(key, {}), **per}.items() if h}
            s["account_handles"] = cur
        elif k == "times" and isinstance(v, dict):
            s["times"] = {**s.get("times", {}), **v}
        elif k in DEFAULTS:
            s[k] = v
    _write(data_dir / "settings.json", s)
    return s


# ---- plan: decisions made ahead of time for future days ------------------------

def load_plan(data_dir: Path) -> dict:
    return _read(data_dir / "plan.json", {})


def set_plan(data_dir: Path, day: str, slot: str, decision: str | None) -> dict:
    """decision: "approve" | "skip" | None (clear)."""
    plan = load_plan(data_dir)
    day_plan = plan.setdefault(day, {})
    if decision:
        day_plan[slot] = decision
    else:
        day_plan.pop(slot, None)
    if not day_plan:
        plan.pop(day, None)
    _write(data_dir / "plan.json", plan)
    return plan
