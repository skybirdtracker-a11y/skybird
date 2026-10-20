"""Run SkyBird in the cloud (GitHub Actions) so your PC can be off.

Once a day (see .github/workflows/skybird-daily.yml) this:
  1. fetches yesterday's flights from OpenSky and makes the 4 videos,
  2. builds the captions, hashtags and @tags exactly like the dashboard would
     (the default caption style for the day, the pre-checked tags),
  3. schedules each post in Buffer's queue at its time (9/11/1/6 by default),
  4. sends a phone notification. You review, edit or delete posts in the Buffer app.

Keys come from the repository's Actions secrets (never stored in the code):
  OPENSKY_CLIENT_ID, OPENSKY_CLIENT_SECRET, BUFFER_API_KEY, NTFY_TOPIC (optional)

    python -m skybird --config cloud/config.toml cloud-run [--force] [--dry-run]
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import captions, engine, notify
from .config import PLATFORMS, Config
from .settings import save_settings
from .state import APPROVED, POSTED, load_state, locked, log, save_state

BUFFER_APP = "https://publish.buffer.com"
RETRY_WAIT = 120


def load_env_secrets(cfg: Config) -> list[str]:
    """Copy the GitHub secrets into SkyBird's settings; returns what's missing."""
    env = os.environ
    for name, key in (("OPENSKY_CLIENT_ID", "opensky_client_id"), ("OPENSKY_CLIENT_SECRET", "opensky_client_secret"),
                      ("BUFFER_API_KEY", "buffer_api_key")):
        if env.get(name, "").strip():
            cfg.secrets[key] = env[name].strip()
    if env.get("NTFY_TOPIC", "").strip():
        cfg.raw.setdefault("notify", {})["ntfy_topic"] = env["NTFY_TOPIC"].strip()
    need = {"OPENSKY_CLIENT_ID": "opensky_client_id", "OPENSKY_CLIENT_SECRET": "opensky_client_secret",
            "BUFFER_API_KEY": "buffer_api_key"}
    return [n for n, k in need.items() if not cfg.secrets.get(k)]


def connect_buffer(cfg: Config) -> dict:
    """Find the Instagram / Facebook / TikTok channels on the Buffer account."""
    from .publishers import buffer
    key = cfg.secrets["buffer_api_key"]
    orgs = buffer.organizations(key)
    if not orgs:
        raise SystemExit("Buffer accepted the key but found no account. Log in at buffer.com once.")
    found = buffer.match_channels(buffer.channels(key, orgs[0]["id"]))
    for p in buffer.PLATFORMS:
        if p in found:
            cfg.secrets[f"buffer_channel_{p}"] = found[p]["id"]
    return found


def plan(cfg: Config, day, now: datetime, only: list[str] | None = None, at: str | None = None) -> dict:
    """When each post goes out: its usual time, or (if the job ran late) catch-up
    times an hour apart, the first ~10 minutes from now. Past the last post time = skipped.
    `only` limits it to some posts; `at` ("18:30") sets the time for them (today)."""
    cutoff = datetime.combine(now.date(), cfg.last_post_time, tzinfo=cfg.tz)
    if at:   # a time you picked yourself is allowed after the usual last post time
        cutoff = datetime.combine(now.date(), datetime.max.time(), tzinfo=cfg.tz)
    out, prev = {}, None
    for slot in sorted(cfg.slots, key=lambda s: s.time):
        if only and slot.key not in only:
            continue
        if at:
            hh, mm = at.strip().split(":")
            want = datetime.combine(now.date(), datetime.min.time(), tzinfo=cfg.tz).replace(hour=int(hh), minute=int(mm))
        elif day == now.date():
            want = engine.scheduled_at(cfg, day, slot.time)
        else:   # remaking an earlier day's post: today, at its usual time
            want = engine.scheduled_at(cfg, now.date(), slot.time)
        t = max(want, now + timedelta(minutes=5 if at else 10))
        if prev is not None:
            t = max(t, prev + cfg.catchup_gap)
        if t > cutoff:
            continue
        out[slot.key] = t
        prev = t
    return out


def _eppley_counts(cfg: Config, day) -> str:
    """'118 departures, 104 arrivals' from the day's stats.json (so you can judge the data)."""
    import json
    try:
        st = json.loads(cfg.media_file(day, cfg.section("media").get("stats_file", "stats.json")).read_text(encoding="utf-8"))
        c = st["posts"]["commercial"]
        return f"{c.get('departures', '?')} airline departures, {c.get('arrivals', '?')} arrivals ({st.get('data_date', '')})"
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def run(cfg: Config, *, force: bool = False, now: datetime | None = None,
        only: list[str] | None = None, day=None, at: str | None = None) -> dict:
    """`only` = remake + schedule just these posts (e.g. ["all"]); `day` = the post date
    whose videos to remake (default today: yesterday's flights); `at` = post time "HH:MM"."""
    now = now or engine.now_local(cfg)
    today = day or now.date()
    missing = load_env_secrets(cfg)
    if missing:
        raise SystemExit("Add these in GitHub → Settings → Secrets and variables → Actions: " + ", ".join(missing))
    # The cloud posts the day's default captions: no time tests, nothing waits for approval.
    save_settings(cfg.data_dir, {"time_tests": False, "backend": "buffer"})

    st = load_state(cfg, today)
    before = st
    if st and not force and not only and st.get("cloud_done"):
        log(cfg, f"{today}: already scheduled earlier today; nothing to do")
        return {"skipped": "already done"}

    if not cfg.dry_run:
        found = connect_buffer(cfg)
        log(cfg, "Buffer channels: " + ", ".join(f"{p}={'yes' if p in found else 'missing'}" for p in PLATFORMS))
    st = engine.prepare(cfg, today, force=True, quiet=True)
    if only and before:   # keep the other posts' records as they were (already scheduled)
        with locked(cfg):
            st = load_state(cfg, today)
            for k, p in before.get("posts", {}).items():
                if k not in only:
                    st["posts"][k] = p
            if before.get("cloud_done"):
                st["cloud_done"] = before["cloud_done"]
            save_state(cfg, today, st)
    if st.get("sample") or not any(p["video_exists"] for p in st["posts"].values()):
        msg = "The videos couldn't be made today (OpenSky didn't answer). Nothing was scheduled."
        notify.send(cfg, "SkyBird (cloud): no posts today", msg, priority="high", click=BUFFER_APP)
        raise SystemExit(msg)
    if st.get("incomplete") and cfg.section("cloud").get("skip_incomplete", False):
        notify.send(cfg, "SkyBird (cloud): skipped today", st["incomplete"], priority="high", click=BUFFER_APP)
        return {"skipped": "incomplete data"}

    times = plan(cfg, today, now, only, at)
    scheduled, problems = [], []
    for slot in sorted(cfg.slots, key=lambda s: s.time):
        post = st["posts"].get(slot.key)
        if only and slot.key not in only:
            continue
        if not post or not post["video_exists"] or slot.key not in times:
            continue
        due = times[slot.key]
        caps = {p: engine.caption_for(post, p) for p, on in post["platforms"].items() if on}
        if not caps:
            continue
        with locked(cfg):
            s2 = load_state(cfg, today)
            s2["posts"][slot.key]["status"] = APPROVED
            s2["posts"][slot.key]["tracking"] = engine.tracking(cfg, today, slot.key, s2["posts"][slot.key])
            save_state(cfg, today, s2)
        if cfg.dry_run:
            res = {p: {"ok": True, "id": "dry-run", "detail": f"DRY RUN: would schedule for {due:%I:%M %p}"} for p in caps}
        else:
            from .publishers import buffer
            got = {}
            for attempt in (1, 2):   # the free file hosts have bad minutes; wait and try once more
                try:
                    got = buffer.post(Path(post["video"]), caps, cfg.secrets, due=due)
                except Exception as e:
                    got = {p: e for p in caps}
                hosting = all("temporary host" in str(getattr(r, "detail", "") or r) for r in got.values()
                              if not getattr(r, "ok", False))
                if attempt == 2 or any(getattr(r, "ok", False) for r in got.values()) or not hosting:
                    break
                log(cfg, f"{today} {slot.title}: the video hosts refused; trying again in 2 minutes")
                time.sleep(RETRY_WAIT)
            res = {}
            for p, r in got.items():
                if getattr(r, "ok", False):
                    res[p] = {"ok": True, "id": r.post_id, "detail": r.detail, "at": due.isoformat(timespec="seconds")}
                else:
                    res[p] = {"ok": False, "error": (getattr(r, "detail", "") or str(r))[:600]}
        with locked(cfg):
            s2 = load_state(cfg, today)
            p2 = s2["posts"][slot.key]
            p2["results"] = res
            p2["posted_at"] = due.isoformat(timespec="seconds")
            engine._set_final_status(p2)
            save_state(cfg, today, s2)
        oks = [p for p, r in res.items() if r.get("ok")]
        bad = [f"{p}: {r.get('error', '')}" for p, r in res.items() if not r.get("ok")]
        when = due.strftime("%I:%M %p").lstrip("0")
        if oks:
            scheduled.append(f"{when} {slot.title} ({', '.join(oks)})")
        problems += [f"{slot.title} {b}" for b in bad]
        log(cfg, f"{today} {slot.title}: scheduled for {when} on {', '.join(oks) or 'nothing'}" +
            (f"; failed {'; '.join(bad)}" if bad else ""))

    if not only:
        with locked(cfg):
            s2 = load_state(cfg, today)
            s2["cloud_done"] = now.isoformat(timespec="seconds")
            save_state(cfg, today, s2)
    body = ("Scheduled in Buffer:\n" + "\n".join(scheduled) if scheduled else "Nothing could be scheduled.")
    eppley = _eppley_counts(cfg, today)
    if eppley:
        body += f"\n\nEppley: {eppley}"
        if os.environ.get("GITHUB_ACTIONS"):   # shows on the run's page (Annotations)
            print(f"::notice title=Eppley flights::{eppley}")
    if st.get("incomplete"):
        body += "\n\n⚠️ " + st["incomplete"] + " Check the videos in Buffer; delete them if they look wrong."
    if problems:
        body += "\n\nProblems:\n" + "\n".join(p[:300] for p in problems[:6])
    body += "\n\nReview, edit or delete them in the Buffer app."
    title = "SkyBird (cloud): " + (f"{len(scheduled)} posts scheduled" if scheduled else "nothing scheduled")
    notify.send(cfg, title + (" [dry run]" if cfg.dry_run else ""), body,
                priority="high" if problems or not scheduled else "default", click=BUFFER_APP)
    print(title + "\n" + body)
    if not scheduled:
        raise SystemExit(title)
    return {"scheduled": scheduled, "problems": problems}
