"""Buffer's daily call budget, shared by every run and every city
(cloud/data/buffer_ledger.json). Limits live under [buffer] in cloud/config.toml:

    daily_calls 60        all Buffer calls in a day (UTC), all cities together
    keep_for_posting 20   checks, numbers and setup stop while this many are left: scheduling first
    backoff_pct 20        when Buffer says less than 20% of a rate window is left, only
                          scheduling goes ahead until that window resets

A refused call raises PublishError before anything is sent, so the caller handles it like
any other Buffer problem (a post check waits for the next run, numbers wait for tomorrow).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .config import Config
from .publishers.base import PublishError

DEFAULTS = {"daily_calls": 60, "keep_for_posting": 20, "backoff_pct": 20}
PURPOSES = ("post", "check", "metrics", "setup", "other")


class BufferLedger:
    def __init__(self, cfg: Config, now: datetime | None = None):
        self.cfg = cfg
        self.city = cfg.city_id
        self.path = cfg.shared_dir / "buffer_ledger.json"
        b = cfg.section("buffer") or {}
        self.caps = {k: int(b.get(k, v)) for k, v in DEFAULTS.items()}
        self._now = now
        self.load()

    def now(self) -> datetime:
        return self._now or datetime.now(timezone.utc)

    def load(self) -> None:
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.data = {}

    def save(self) -> None:
        keep = sorted(k for k in self.data if not k.startswith("_"))[-60:]
        self.data = {k: v for k, v in self.data.items() if k.startswith("_") or k in keep}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def day(self) -> dict:
        d = self.data.setdefault(self.now().date().isoformat(), {"total": 0, "cities": {}})
        d["cities"].setdefault(self.city, {})
        return d

    def total(self) -> int:
        return self.day()["total"]

    def backing_off(self) -> str:
        """Why non-posting calls wait right now ('' = they don't)."""
        until = self.data.get("_backoff_until")
        if until and self.now() < datetime.fromisoformat(until):
            return f"Buffer's rate window is nearly used up; checks and numbers wait until {until[11:16]} UTC"
        return ""

    def check(self, what: str) -> None:
        d, c = self.day(), self.caps
        if d["total"] >= c["daily_calls"]:
            raise PublishError(f"SkyBird's daily Buffer limit ({c['daily_calls']} calls) is reached; "
                               "nothing more is sent to Buffer today")
        if what != "post":
            if d["total"] >= c["daily_calls"] - c["keep_for_posting"]:
                raise PublishError(f"the last {c['keep_for_posting']} Buffer calls today are kept for scheduling posts")
            why = self.backing_off()
            if why:
                raise PublishError(why)

    def spend(self, what: str) -> None:
        self.load()
        what = what if what in PURPOSES else "other"
        self.check(what)
        d = self.day()
        d["total"] += 1
        mine = d["cities"][self.city]
        mine[what] = mine.get(what, 0) + 1
        self.save()

    def note(self, headers, body: dict, status: int) -> None:
        """What Buffer said about its rate windows: under backoff_pct left -> back off until it resets.
        Buffer reports each window's `remaining` and `resetSeconds` but not its size, so the size
        is taken as the most ever seen remaining in that window (kept in _window_max)."""
        from .publishers.buffer import rate_windows
        wins = rate_windows(headers, body)
        if status == 429 and not wins:
            wins = [{"limit": 1, "remaining": 0, "reset_seconds": 15 * 60}]
        if not wins:
            return
        self.load()
        seen = self.data.setdefault("_window_max", {})
        for i, w in enumerate(wins):
            seen[str(i)] = max(int(seen.get(str(i), 0)), int(w["remaining"]))
            if not w.get("limit"):
                w["limit"] = seen[str(i)]
        low = [w for w in wins if w.get("limit") and w["remaining"] < self.caps["backoff_pct"] / 100 * w["limit"]]
        self.day()["rate"] = [dict(w, at=self.now().isoformat(timespec="minutes")) for w in wins]
        if low:
            secs = max(int(w.get("reset_seconds") or 15 * 60) for w in low)
            until = (self.now() + timedelta(seconds=secs)).isoformat(timespec="minutes")
            self.data["_backoff_until"] = max(until, self.data.get("_backoff_until") or "")
        self.save()

    def summary(self, days: int = 7) -> list[dict]:
        keys = sorted(k for k in self.data if not k.startswith("_"))[-days:]
        return [dict(self.data[k], day=k) for k in keys]


def use(cfg: Config) -> BufferLedger:
    """Count (and limit) every Buffer call this run makes; remember Buffer's API shape between runs."""
    from .publishers import buffer
    buffer.LEDGER = BufferLedger(cfg)
    buffer.SHAPES_FILE = cfg.shared_dir / "buffer_shape.json"
    buffer.load_shapes()
    return buffer.LEDGER
