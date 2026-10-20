"""Loads config.toml (settings) and secrets.json (tokens)."""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SLOT_ORDER = ["commercial", "private", "military", "all"]
PLATFORMS = ["instagram", "facebook", "tiktok"]


def _parse_hhmm(value: str) -> time:
    hh, mm = value.strip().split(":")
    return time(int(hh), int(mm))


@dataclass
class Slot:
    key: str
    title: str
    time: time
    file: str
    platforms: list[str]


@dataclass
class Config:
    root: Path
    raw: dict
    secrets_path: Path
    secrets: dict = field(default_factory=dict)

    # ---- general ---------------------------------------------------------
    @property
    def general(self) -> dict:
        return self.raw.get("general", {})

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.general.get("timezone", "America/Chicago"))

    @property
    def dry_run(self) -> bool:
        if os.environ.get("SKYBIRD_DRY_RUN") in ("1", "true", "yes"):
            return True
        from .settings import load_settings
        override = load_settings(self.data_dir).get("dry_run")
        if override is not None:
            return bool(override)
        return bool(self.general.get("dry_run", True))

    @property
    def autopost(self) -> bool:
        from .settings import load_settings
        return bool(load_settings(self.data_dir).get("autopost", True))

    @property
    def data_dir(self) -> Path:
        p = self.root / self.general.get("data_dir", "data")
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def prepare_time(self) -> time:
        return _parse_hhmm(self.general.get("prepare_time", "06:00"))

    @property
    def late_approval(self) -> str:
        """"catch_up" (default; the older "post" means the same) or "skip"."""
        v = self.general.get("late_approval", "catch_up")
        return "skip" if v == "skip" else "catch_up"

    @property
    def catchup_gap(self) -> timedelta:
        return timedelta(minutes=int(self.general.get("catchup_gap_minutes", 60)))

    @property
    def day_start(self) -> time:
        """A flight day runs from this time to one minute before it the next morning
        (3:30 AM to 3:29 AM), so late-night flights count toward the day they belong to."""
        return _parse_hhmm(self.general.get("flight_day_start", "03:30"))

    def flight_day(self, day):
        """(start, end) of the tracked window for one flight day, as aware datetimes."""
        start = datetime.combine(day, self.day_start, tzinfo=self.tz)
        return start, start + timedelta(days=1)

    @property
    def last_post_time(self) -> time:
        return _parse_hhmm(self.general.get("last_post_time", "22:00"))

    @property
    def late_window(self) -> timedelta:
        return timedelta(minutes=int(self.general.get("late_window_minutes", 240)))

    # ---- sections --------------------------------------------------------
    def section(self, name: str) -> dict:
        return self.raw.get(name, {})

    @property
    def slots(self) -> list[Slot]:
        out = []
        raw_slots = self.raw.get("slots", {})
        from .settings import load_settings
        times = load_settings(self.data_dir).get("times", {})  # changed from the dashboard
        for key in SLOT_ORDER + [k for k in raw_slots if k not in SLOT_ORDER]:
            s = raw_slots.get(key)
            if not s:
                continue
            out.append(Slot(
                key=key,
                title=s.get("title", key.title()),
                time=_parse_hhmm(times.get(key) or s.get("time", "12:00")),
                file=s.get("file", f"{key}.mp4"),
                platforms=[p for p in s.get("platforms", PLATFORMS) if p in PLATFORMS],
            ))
        return out

    def slot(self, key: str) -> Slot:
        for s in self.slots:
            if s.key == key:
                return s
        raise KeyError(f"Unknown slot '{key}'. Known: {[s.key for s in self.slots]}")

    def platform_enabled(self, platform: str) -> bool:
        return bool(self.section(platform).get("enabled", True))

    def data_date(self, post_date: date) -> date:
        return post_date + timedelta(days=int(self.section("media").get("data_day_offset", -1)))

    def _media_template(self) -> str:
        from .settings import load_settings
        return (load_settings(self.data_dir).get("media_folder")
                or self.section("media").get("folder", "media/{data_date}"))

    def media_file(self, post_date: date, name: str) -> Path:
        """Path of one day's file. A folder with {data_date}/{post_date} in it gets a
        subfolder per day; a plain folder gets date-prefixed names in one place."""
        folder = self.media_folder(post_date)
        if "{data_date}" in self._media_template() or "{post_date}" in self._media_template():
            return folder / name
        return folder / f"{self.data_date(post_date).isoformat()}_{name}"

    def media_folder(self, post_date: date) -> Path:
        template = self._media_template()
        folder = template.format(post_date=post_date.isoformat(),
                                 data_date=self.data_date(post_date).isoformat())
        p = Path(folder)
        return p if p.is_absolute() else self.root / p

    def upload_post_opts(self) -> dict:
        """[upload_post] settings; values chosen in `uploadpost-setup` win."""
        up = dict(self.section("upload_post"))
        for k in ("profile", "facebook_page_id"):
            if self.secrets.get(f"upload_post_{k}"):
                up[k] = self.secrets[f"upload_post_{k}"]
        return up

    # ---- secrets ---------------------------------------------------------
    def save_secrets(self) -> None:
        tmp = self.secrets_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.secrets, indent=2))
        tmp.replace(self.secrets_path)


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("SKYBIRD_CONFIG", "config.toml")).resolve()
    if not path.exists():
        raise SystemExit(f"Config not found: {path}\nCopy config.example.toml to config.toml first.")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    secrets_path = path.parent / "secrets.json"
    secrets = json.loads(secrets_path.read_text()) if secrets_path.exists() else {}
    return Config(root=path.parent, raw=raw, secrets_path=secrets_path, secrets=secrets)
