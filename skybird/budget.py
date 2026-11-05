"""The OpenSky ledger: every request SkyBird makes, counted per day, city and category,
shared by every run and every city (cloud/data/opensky_ledger.json).

Limits live in cloud/config.toml under [opensky_budget] (defaults below). They are
ceilings, not targets:

    total 3,999 a day for all of SkyBird (the account allows 4,000)
    per city 1,600: flight lists 40, arrival fill-in 600, aircraft types 400, backfill 60
    the rest (799) is a shared reserve for manual runs and retries

Order of importance: a city's core flight lists first. Fill-in, type lookups and backfill
stop before they could eat into the flight lists another city still needs today.

OpenSky's own count wins when it sends one: the X-Rate-Limit-Remaining header on each
answer. Under 400 left, fill-in and type lookups stop; under 100, only a city that doesn't
have its day's lists yet may ask (lists only). A 429 stops all OpenSky work until it
resets (saved data is used instead), and its retry-after time lines the ledger's day up
with OpenSky's reset.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from .config import Config, city_ids

CATEGORIES = ("lists", "fill_in", "types", "backfill")
NAMES = {"lists": "flight lists", "fill_in": "arrival fill-in", "types": "aircraft types", "backfill": "backfill"}
DEFAULTS = {"total": 3999, "per_city": 1600, "lists": 40, "fill_in": 600, "types": 400, "backfill": 60,
            "low_remaining": 400, "critical_remaining": 100, "alert_pct": 80, "keep_for_lists": 40}
ACCOUNT_LIMIT = 4000   # the owner's OpenSky allowance per day: the total cap may never go above it


class BudgetError(Exception):
    """A request the ledger won't allow (a cap reached, low credit, or OpenSky said 429)."""


class Ledger:
    def __init__(self, cfg: Config, now: datetime | None = None):
        self.cfg = cfg
        self.city = cfg.city_id
        self.path = cfg.shared_dir / "opensky_ledger.json"
        b = dict(DEFAULTS, **(cfg.section("opensky_budget") or {}))
        b["total"] = min(int(b["total"]), ACCOUNT_LIMIT - 1)
        self.caps = b
        self._now = now
        self.load()

    def load(self) -> None:
        """Re-read the shared file: another run or city may have spent since."""
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.data = {}

    # ---- the day ----------------------------------------------------------------
    def now(self) -> datetime:
        return self._now or datetime.now(timezone.utc)

    def day_key(self) -> str:
        """The ledger's day, lined up with OpenSky's reset (learned from a 429; UTC midnight until then)."""
        reset = int(self.data.get("_reset_minute_utc", 0))
        return (self.now() - timedelta(minutes=reset)).date().isoformat()

    def day(self) -> dict:
        d = self.data.setdefault(self.day_key(), {"total": 0, "cities": {}})
        d["cities"].setdefault(self.city, {c: 0 for c in CATEGORIES})
        return d

    def used(self, city: str | None = None) -> int:
        """Requests a city used today (all cities: total())."""
        return sum(self.day()["cities"].get(city or self.city, {}).values())

    def total(self) -> int:
        return self.day()["total"]

    def save(self) -> None:
        keep = sorted(k for k in self.data if not k.startswith("_"))[-60:]   # 60 days of history
        self.data = {k: v for k, v in self.data.items() if k.startswith("_") or k in keep}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # ---- asking ------------------------------------------------------------------
    def check(self, cat: str) -> None:
        """Raise BudgetError if this request isn't allowed now."""
        d, c = self.day(), self.caps
        mine = d["cities"][self.city]
        if d.get("blocked"):
            raise BudgetError("OpenSky's daily limit was reached (HTTP 429); no more requests until it resets. "
                              "Saved data is used instead.")
        rem = d.get("remaining")
        if rem is not None:
            if rem < c["critical_remaining"] and (cat != "lists" or self.city in d.get("lists_done", [])):
                raise BudgetError(f"OpenSky says only {rem} requests are left today: only flight lists for cities "
                                  "that don't have their day yet")
            if rem < c["low_remaining"] and cat in ("fill_in", "types"):
                raise BudgetError(f"OpenSky says only {rem} requests are left today: {NAMES[cat]} stop")
        if mine[cat] >= int(c[cat]):
            raise BudgetError(f"{self.cfg.city_name}'s daily {NAMES[cat]} limit ({c[cat]}) is reached")
        reserve = os.environ.get("SKYBIRD_USE_RESERVE") in ("1", "true", "yes")   # manual runs, retries
        if sum(mine.values()) >= int(c["per_city"]) and not reserve:
            raise BudgetError(f"{self.cfg.city_name}'s daily OpenSky limit ({c['per_city']}) is reached")
        if d["total"] >= c["total"]:
            raise BudgetError(f"SkyBird's daily OpenSky limit ({c['total']}, all cities) is reached")
        if cat != "lists":   # never eat into the flight lists another city still needs today
            waiting = [x for x in city_ids(self.cfg.root) if x != self.city and x not in d.get("lists_done", [])]
            if d["total"] + 1 > c["total"] - int(c["keep_for_lists"]) * len(waiting):
                raise BudgetError(f"kept back for {', '.join(waiting)}'s flight lists")

    def spend(self, cat: str) -> None:
        self.load()
        self.check(cat)
        d = self.day()
        d["cities"][self.city][cat] += 1
        d["total"] += 1
        self._alerts(d)
        self.save()

    def lists_done(self) -> None:
        """This city has its day's core flight lists (lower-priority work may use what's left)."""
        self.load()
        d = self.day()
        if self.city not in d.setdefault("lists_done", []):
            d["lists_done"].append(self.city)
            self.save()

    # ---- what OpenSky says ---------------------------------------------------------
    def note_headers(self, headers) -> None:
        rem = (headers or {}).get("X-Rate-Limit-Remaining")
        if rem is not None and str(rem).strip().lstrip("-").isdigit():
            self.load()
            self.day()["remaining"] = int(rem)
            self.save()

    def note_429(self, retry_after: str | None) -> None:
        self.load()
        d = self.day()
        d["blocked"] = True
        if retry_after and str(retry_after).isdigit():
            reset = self.now() + timedelta(seconds=int(retry_after))
            d["resets_at"] = reset.isoformat(timespec="minutes")
            # the ledger's day now starts when OpenSky resets: today's count (and the block)
            # belongs to the day that ends at that reset
            old = self.day_key()
            self.data["_reset_minute_utc"] = reset.hour * 60 + reset.minute
            new = self.day_key()
            if new != old:
                self.data[new] = self.data.pop(old)
        self.save()

    # ---- alerts ----------------------------------------------------------------------
    def _alerts(self, d: dict) -> None:
        pct = int(self.caps["alert_pct"]) / 100
        sent = d.setdefault("alerts", [])
        checks = [("total", d["total"], self.caps["total"], "all cities"),
                  (self.city, sum(d["cities"][self.city].values()), self.caps["per_city"], self.cfg.city_name)]
        for key, n, cap, who in checks:
            if n >= pct * int(cap) and key not in sent:
                sent.append(key)
                from . import notify
                notify.send(self.cfg, "SkyBird: OpenSky use is high today",
                            f"{who}: {n:,} of {int(cap):,} requests used today ({round(100 * n / int(cap))}%). "
                            "SkyBird stops before the limit; check what else is using the account.",
                            priority="high")

    # ---- summary -----------------------------------------------------------------------
    def summary(self, days: int = 7) -> list[dict]:
        """[{day, total, cities: {city: {category: n}}, remaining, blocked}] newest last."""
        keys = sorted(k for k in self.data if not k.startswith("_"))[-days:]
        return [dict(self.data[k], day=k) for k in keys]


def ledger(cfg: Config) -> Ledger:
    return Ledger(cfg)
