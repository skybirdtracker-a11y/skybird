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
from datetime import date, datetime, timedelta
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


CHANNEL_SECRET = "BUFFER_CHANNEL_{city}_{platform}"   # e.g. BUFFER_CHANNEL_KC_TIKTOK
CHANNEL_CACHE_DAYS = 7


def channel_secrets(cfg: Config) -> dict[str, dict[str, str]]:
    """{city: {platform: Buffer channel ID}} for every city: [buffer_channels] in its profile
    (cities/<city>.toml), each replaced by a BUFFER_CHANNEL_<CITY>_<PLATFORM> secret if set."""
    import tomllib
    from .config import cities_dir, city_ids
    out = {}
    for c in city_ids(cfg.root) or [cfg.city_id]:
        try:
            prof = tomllib.loads((cities_dir(cfg.root) / f"{c}.toml").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prof = {}
        for plat in PLATFORMS:
            v = os.environ.get(CHANNEL_SECRET.format(city=c.upper(), platform=plat.upper()), "").strip() \
                or str((prof.get("buffer_channels") or {}).get(plat) or "").strip()
            if v:
                out.setdefault(c, {})[plat] = v
    return out


def channel_list(cfg: Config, want: tuple[str, ...] = (), force: bool = False) -> list[dict]:
    """The Buffer account's channels [{id, name, service}], looked up once and kept in
    cloud/data/buffer_channels.json. Looked up again after a week, on --connect, or when a
    channel ID in the secrets isn't in the saved list (at most once an hour)."""
    import json
    from .publishers import buffer
    path = cfg.shared_dir / "buffer_channels.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        saved = {}
    now = datetime.now().astimezone()
    age = now - datetime.fromisoformat(saved["fetched_at"]) if saved.get("fetched_at") else None
    have = {c["id"] for c in saved.get("channels") or []}
    stale = (age is None or age > timedelta(days=CHANNEL_CACHE_DAYS) or force or
             (any(w not in have for w in want) and age > timedelta(hours=1)))
    if not stale:
        return saved["channels"]
    key = cfg.secrets["buffer_api_key"]
    with buffer.purpose("setup"):
        org = os.environ.get("BUFFER_ORG_ID", "").strip() or saved.get("org_id")
        if not org:
            orgs = buffer.organizations(key)
            if not orgs:
                raise SystemExit("Buffer accepted the key but found no account. Log in at buffer.com once.")
            org = orgs[0]["id"]
        chs = [{"id": c["id"], "name": c.get("name", ""), "service": c.get("service", "")}
               for c in buffer.channels(key, org)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_at": now.isoformat(timespec="seconds"), "org_id": org, "channels": chs},
                               indent=1), encoding="utf-8")
    return chs


def connect_buffer(cfg: Config, force: bool = False) -> tuple[dict, list[str]]:
    """This city's Buffer channels: ({platform: channel ID}, notes). Only this city's own
    IDs ([buffer_channels] in its profile, or BUFFER_CHANNEL_<CITY>_<PLATFORM> secrets) are
    used, never a channel picked by kind; a channel that is missing, isn't on
    the Buffer account, is the wrong kind, or is also given to another city (or another
    platform) is NOT posted to. There is never a fallback to another city's channel."""
    from .publishers import buffer
    for plat in PLATFORMS:
        cfg.secrets.pop(f"buffer_channel_{plat}", None)
    every = channel_secrets(cfg)
    mine = {p: v for p, v in every.get(cfg.city_id, {}).items() if cfg.platform_enabled(p)}
    notes, use = [], {}
    others = {v: f"{c} {p}" for c, chs in every.items() if c != cfg.city_id for p, v in chs.items()}
    for plat, cid in mine.items():
        twin = [p for p, v in mine.items() if v == cid and p != plat]
        if cid in others or twin:
            notes.append(f"{plat}: refused, the same Buffer channel is also set for {others.get(cid) or twin[0]}. "
                         "Each city and platform needs its own channel.")
        else:
            use[plat] = cid
    try:
        listed = channel_list(cfg, tuple(use.values()), force=force)
    except buffer.PublishError as e:
        listed = None
        notes.append(f"couldn't look up the Buffer channels ({str(e)[:200]}); using the IDs as given")
    if listed is not None:
        by_id = {c["id"]: c for c in listed}
        for plat, cid in list(use.items()):
            if cid not in by_id:
                notes.append(f"{plat}: channel {cid} isn't on the Buffer account; not posting there")
                use.pop(plat)
            elif buffer.service_of(by_id[cid]) != plat:
                notes.append(f"{plat}: channel {cid} is a {by_id[cid].get('service')} channel; not posting there")
                use.pop(plat)
    for plat in PLATFORMS:
        if cfg.platform_enabled(plat) and plat not in use and not any(n.startswith(plat) for n in notes):
            notes.append(f"{plat}: no channel ID ([buffer_channels] in cities/{cfg.city_id}.toml or the "
                         f"{CHANNEL_SECRET.format(city=cfg.city_id.upper(), platform=plat.upper())} secret); not posting there")
    for plat, cid in use.items():
        cfg.secrets[f"buffer_channel_{plat}"] = cid
    return use, notes


def print_channels(cfg: Config) -> list[str]:
    """--connect: look the channels up again and show which secret each one belongs in."""
    load_env_secrets(cfg)
    from . import buffer_ledger
    buffer_ledger.use(cfg)
    from .publishers import buffer
    out = []
    for c in channel_list(cfg, force=True):
        out.append(f"{buffer.service_of(c)} '{c.get('name', '')}': {c['id']}")
    use, notes = connect_buffer(cfg)
    out += [f"{cfg.city_name} uses: " + (", ".join(f"{p}={v}" for p, v in use.items()) or "nothing")] + notes
    for ln in out:
        print(ln)
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::notice title=Buffer channels::{ln[:900]}")
    return out


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
        if not cfg.slot_active(slot.key, day):
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
        c = st.get("hub") or st["posts"]["commercial"]   # (the 9 AM post may be a new format)
        out = f"{c.get('departures', '?')} airline departures, {c.get('arrivals', '?')} arrivals ({st.get('data_date', '')})"
        b = st.get("eppley_arrivals") or {}
        if b:
            out += (f". Arrivals: {b.get('from_opensky', 0)} from OpenSky + {b.get('moved_from_council_bluffs', 0)} "
                    f"moved from fields next to {cfg.hub_name} + {b.get('filled_in', 0)} filled in")
        if st.get("opensky_requests"):
            out += f". OpenSky requests this run: {st['opensky_requests']}"
        return out
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def _in_buffer(post: dict) -> bool:
    """This post was really scheduled in Buffer (not a dry run) on at least one platform."""
    return any(r.get("ok") and r.get("id") not in (None, "", "dry-run") for r in (post.get("results") or {}).values())


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
    # The flight day runs to 3:29 AM: a scheduled run before then would miss the end of it.
    if not force and not only and os.environ.get("GITHUB_EVENT_NAME") == "schedule" and now.time() < cfg.day_start:
        log(cfg, f"{now:%I:%M %p}: yesterday's flight day isn't over until {cfg.day_start:%I:%M %p}; the later run does it")
        return {"skipped": "too early"}
    if st and not force and not only and st.get("cloud_done"):
        log(cfg, f"{today}: already scheduled earlier today; nothing to do")
        return {"skipped": "already done"}

    try:   # Eppley's own arrivals board: saved now, compared with SkyBird's count tomorrow
        snapshot_board(cfg, now)
    except Exception as e:
        log(cfg, f"Couldn't read the airport's arrivals board: {e}")
    channel_notes = []
    if not cfg.dry_run:
        from . import buffer_ledger
        buffer_ledger.use(cfg)
        found, channel_notes = connect_buffer(cfg)
        log(cfg, "Buffer channels: " + ", ".join(f"{p}={'yes' if p in found else 'no'}" for p in PLATFORMS
                                                  if cfg.platform_enabled(p)) +
            ("; " + "; ".join(channel_notes) if channel_notes else ""))
    # Posts already sitting in Buffer from an earlier run today (e.g. one that crashed
    # half way) are kept as they are and not scheduled again.
    in_buffer = set() if force or only else {k for k, p in ((before or {}).get("posts") or {}).items()
                                             if _in_buffer(p)}
    keep = {k for k in (before or {}).get("posts", {}) if (only and k not in only) or k in in_buffer}
    st = engine.prepare(cfg, today, force=True, quiet=True)
    if keep:   # put those posts' records back as they were
        with locked(cfg):
            st = load_state(cfg, today)
            for k, p in before.get("posts", {}).items():
                if k in keep:
                    st["posts"][k] = p
            if before.get("cloud_done"):
                st["cloud_done"] = before["cloud_done"]
            save_state(cfg, today, st)
    if st.get("sample") or not any(p["video_exists"] for p in st["posts"].values()):
        why = (engine._render_status(cfg, today).get("error") or "OpenSky didn't answer").strip()
        msg = f"The videos couldn't be made today: {why[:500]} Nothing was scheduled."
        notify.send(cfg, "SkyBird (cloud): no posts today", msg, priority="high", click=BUFFER_APP)
        raise SystemExit(msg)
    if st.get("incomplete") and cfg.section("cloud").get("skip_incomplete", False):
        notify.send(cfg, "SkyBird (cloud): skipped today", st["incomplete"], priority="high", click=BUFFER_APP)
        return {"skipped": "incomplete data"}

    times = plan(cfg, today, now, only, at)
    scheduled, problems = [], []
    from . import report
    report.record_day(cfg, today)   # the day's counts, kept for the reports and the sample page
    if in_buffer:
        log(cfg, f"{today}: already in Buffer from an earlier run, not scheduled again: {', '.join(sorted(in_buffer))}")
    # Serve the videos from GitHub Pages so Buffer's links play and never expire (publishers/pages_host.py)
    links, storage_note = {}, ""
    todo = [Path(st["posts"][sl.key]["video"]) for sl in cfg.slots if sl.key in times and sl.key not in in_buffer
            and (not only or sl.key in only) and st["posts"].get(sl.key, {}).get("video_exists")]
    if todo and not cfg.dry_run:
        from .publishers import github_host, pages_host
        try:
            links = pages_host.publish(todo, today, city=cfg.city_id if cfg.city else None)
            log(cfg, f"Videos live on GitHub Pages ({len(links)}): {next(iter(links.values()))}")
            try:   # the old release copies (Buffer can't play those links) are no longer needed
                github_host.cleanup(now.date() + timedelta(days=github_host.KEEP_DAYS + 1))
            except Exception:
                pass
        except Exception as e:   # Pages not on yet, or a GitHub hiccup: fall back to the free hosts
            storage_note = str(e)
            log(cfg, f"Couldn't store the videos on GitHub: {e}")
            if os.environ.get("GITHUB_ACTIONS"):
                print(f"::warning title=Video storage::{storage_note[:900]}")
    sample_link = ""
    sample = [Path(p["video"]) for k, p in st["posts"].items() if p.get("video_exists")]   # all of the day's videos
    if sample and cfg.dry_run and cfg.city.get("sample_days") and os.environ.get("GITHUB_ACTIONS"):
        # quiet build: the videos and the sample page on GitHub Pages (one push), nothing posts
        from .publishers import pages_host
        try:
            base = pages_host.base_url()
            page = report.sample_page(cfg, now, base)
            pages_host.publish(sample, today, city=cfg.city_id, wait=0, keep_days=int(cfg.city["sample_days"]),
                               pages={f"reports/{cfg.city_id}/sample.html": page})
            sample_link = f"{base}/reports/{cfg.city_id}/sample.html"
        except Exception as e:
            log(cfg, f"Couldn't put the sample on GitHub Pages: {e}")
    jit = just_in_time(cfg) and not cfg.dry_run
    planned = []
    for slot in sorted(cfg.slots, key=lambda s: s.time):
        post = st["posts"].get(slot.key)
        if only and slot.key not in only:
            continue
        if not post or not post["video_exists"] or slot.key not in times or slot.key in in_buffer:
            continue
        due = times[slot.key]
        link = links.get(Path(post["video"]).name)
        if jit and link:   # into Buffer about 2 hours before its time (the hourly check run does it;
            # it has the video's GitHub Pages link but not the file, so no link = schedule now)
            with locked(cfg):
                s2 = load_state(cfg, today)
                p2 = s2["posts"][slot.key]
                p2.update(planned_at=due.isoformat(timespec="seconds"), video_url=link or "", results={})
                p2.pop("jit_skipped", None)
                save_state(cfg, today, s2)
            planned.append(slot.key)
            continue
        oks, bad = _schedule_post(cfg, today, slot, due, link)
        when = due.strftime("%I:%M %p").lstrip("0")
        if oks:
            scheduled.append(f"{when} {slot.title} ({', '.join(oks)})")
        problems += [f"{slot.title} {b}" for b in bad]
    scheduled_later = []
    if planned:   # anything due within the next ~2 hours goes into Buffer right away
        done, _, bad = schedule_due(cfg, now, days=[today], notify_problems=False)
        scheduled += done
        problems += bad
        s2 = load_state(cfg, today)
        scheduled_later = [f"{times[k].strftime('%I:%M %p').lstrip('0')} {s2['posts'][k]['title']}"
                           for k in planned if not s2["posts"][k].get("results") and not s2["posts"][k].get("jit_skipped")]

    if not only:
        with locked(cfg):
            s2 = load_state(cfg, today)
            s2["cloud_done"] = now.isoformat(timespec="seconds")
            save_state(cfg, today, s2)
    body = ("Scheduled in Buffer:\n" + "\n".join(scheduled) if scheduled else
            "" if scheduled_later else "Nothing could be scheduled.")
    if scheduled_later:
        body += ("\n\n" if body else "") + ("Ready; each goes into Buffer about 2 hours before its time:\n" +
                                            "\n".join(scheduled_later))
    if channel_notes:
        body += "\n\nBuffer channels: " + "; ".join(channel_notes)
    eppley = _eppley_counts(cfg, today)
    if eppley:
        body += f"\n\n{cfg.hub_name}: {eppley}"
        if os.environ.get("GITHUB_ACTIONS"):   # shows on the run's page (Annotations)
            print(f"::notice title={cfg.hub_name} flights::{eppley}")
    vs_board = compare_with_board(cfg, cfg.data_date(today))
    if vs_board:
        body += f"\n\n{vs_board}"
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::notice title=Airport board check::{vs_board[:950]}")
    if st.get("incomplete"):
        body += "\n\n⚠️ " + st["incomplete"] + " Check the videos in Buffer; delete them if they look wrong."
    if storage_note:
        body += "\n\n⚠️ The videos couldn't be put on GitHub Pages, so Buffer may not be able to play them: " + storage_note[:400]
    if problems:
        body += "\n\nProblems:\n" + "\n".join(p[:300] for p in problems[:6])
    body += "\n\nReview them in the Buffer app (Queue): edit or delete anything you don't want before it posts."
    if cfg.city.get("comment_policy"):
        body += "\n\nComments: " + cfg.city["comment_policy"]
    if cfg.dry_run and cfg.city.get("sample_days"):
        tc = report.traffic_check(cfg)
        body += (f"\n\nQuiet build sample: {sample_link}" if sample_link else "") + (f"\n\n{tc}" if tc else "")
    n = len(scheduled) + len(scheduled_later)
    title = "SkyBird (cloud): " + (f"{n} post{'s' if n != 1 else ''} " + ("scheduled" if not scheduled_later else "ready")
                                   if n else "already scheduled earlier" if in_buffer else "nothing scheduled")
    notify.send(cfg, title + (" [dry run]" if cfg.dry_run else ""), body,
                priority="high" if problems or not (scheduled or scheduled_later) else "default",
                click=sample_link or BUFFER_APP)
    print(title + "\n" + body)
    if not cfg.dry_run:
        try:   # Buffer's numbers, once a day (a few calls for all recent posts)
            from .report import daily_numbers
            daily_numbers(cfg, now)
        except Exception as e:
            log(cfg, f"Couldn't read Buffer's numbers today: {str(e)[:300]}")
    if not scheduled and not scheduled_later and not in_buffer:
        raise SystemExit(title)
    return {"scheduled": scheduled, "planned": scheduled_later, "problems": problems}


def just_in_time(cfg: Config) -> bool:
    return bool(cfg.section("buffer").get("just_in_time", True))


def _queue(cfg: Config) -> tuple[Path, list[dict]]:
    """Posts SkyBird has waiting in Buffer, all cities (cloud/data/buffer_queue.json)."""
    import json
    path = cfg.shared_dir / "buffer_queue.json"
    try:
        return path, json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return path, []


def _save_queue(path: Path, q: list[dict]) -> None:
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(q, indent=1), encoding="utf-8")


def waiting_in_buffer(cfg: Config, now: datetime) -> int:
    """How many SkyBird posts are scheduled in Buffer and not yet out (Buffer's free plan allows 10)."""
    path, q = _queue(cfg)
    live = [e for e in q if datetime.fromisoformat(e["due"]) > now - timedelta(minutes=30) and not e.get("sent")]
    if len(live) != len(q):
        _save_queue(path, live)
    return len(live)


def _schedule_post(cfg: Config, day, slot, due: datetime, link: str | None) -> tuple[list[str], list[str]]:
    """Put one post into Buffer (or the dry-run record) for `due`. Returns (platforms ok, problems)."""
    st = load_state(cfg, day)
    post = st["posts"][slot.key]
    caps = {p: engine.caption_for(post, p) for p, on in post["platforms"].items() if on}
    if not caps:
        return [], []
    with locked(cfg):
        s2 = load_state(cfg, day)
        s2["posts"][slot.key]["status"] = APPROVED
        s2["posts"][slot.key]["tracking"] = engine.tracking(cfg, day, slot.key, s2["posts"][slot.key])
        save_state(cfg, day, s2)
    if cfg.dry_run:
        res = {p: {"ok": True, "id": "dry-run", "detail": f"DRY RUN: would schedule for {due:%I:%M %p}"} for p in caps}
    else:
        from .publishers import buffer
        got = {}
        missing = {p for p in caps if not cfg.secrets.get(f"buffer_channel_{p}")}
        for attempt in (1, 2):   # the free file hosts have bad minutes; wait and try once more
            try:
                got = buffer.post(Path(post["video"]), {p: c for p, c in caps.items() if p not in missing},
                                  cfg.secrets, due=due, video_url=link or None)
            except Exception as e:
                got = {p: e for p in caps if p not in missing}
            hosting = all("temporary host" in str(getattr(r, "detail", "") or r) for r in got.values()
                          if not getattr(r, "ok", False))
            if attempt == 2 or any(getattr(r, "ok", False) for r in got.values()) or not hosting:
                break
            log(cfg, f"{day} {slot.title}: the video hosts refused; trying again in 2 minutes")
            time.sleep(RETRY_WAIT)
        res = {p: {"ok": False, "skipped": True,
                   "error": f"no Buffer channel set for {cfg.city_name} {p}; not posted there"} for p in missing}
        path, q = _queue(cfg)
        for p, r in got.items():
            if getattr(r, "ok", False):
                res[p] = {"ok": True, "id": r.post_id, "detail": r.detail, "at": due.isoformat(timespec="seconds")}
                q.append({"id": r.post_id, "city": cfg.city_id, "platform": p, "due": due.isoformat(timespec="seconds")})
            else:
                res[p] = {"ok": False, "error": (getattr(r, "detail", "") or str(r))[:600]}
        _save_queue(path, q)
    with locked(cfg):
        s2 = load_state(cfg, day)
        p2 = s2["posts"][slot.key]
        p2["results"] = res
        p2["posted_at"] = due.isoformat(timespec="seconds")
        engine._set_final_status(p2)
        save_state(cfg, day, s2)
    oks = [p for p, r in res.items() if r.get("ok")]
    bad = [f"{p}: {r.get('error', '')}" for p, r in res.items() if not r.get("ok") and not r.get("skipped")]
    when = due.strftime("%I:%M %p").lstrip("0")
    if os.environ.get("GITHUB_ACTIONS"):   # readable on the run's page (Annotations)
        for b in bad:
            print(f"::warning title={slot.title}::{b[:900]}")
    log(cfg, f"{day} {slot.title}: scheduled for {when} on {', '.join(oks) or 'nothing'}" +
        (f"; failed {'; '.join(bad)}" if bad else ""))
    return oks, bad


def _mark_sent(cfg: Config, post_id: str) -> None:
    path, q = _queue(cfg)
    if any(e["id"] == post_id for e in q):
        _save_queue(path, [e for e in q if e["id"] != post_id])


def schedule_due(cfg: Config, now: datetime, days: list | None = None,
                 notify_problems: bool = True) -> tuple[list[str], list[str], list[str]]:
    """Put each planned post into Buffer about [buffer] schedule_ahead_hours (2.5) before its
    time, keeping SkyBird's waiting posts at or under [buffer] max_scheduled (10, Buffer's
    free plan). A post whose time passed before a run got to it goes out shortly after,
    an hour after the one before, up to the last post time; after that it's skipped.
    Returns (scheduled lines, still-waiting lines, problems)."""
    if cfg.dry_run:
        return [], [], []
    b = cfg.section("buffer")
    ahead = timedelta(hours=float(b.get("schedule_ahead_hours", 2.5)))
    room = int(b.get("max_scheduled", 10)) - waiting_in_buffer(cfg, now)
    cutoff = datetime.combine(now.date(), cfg.last_post_time, tzinfo=cfg.tz)
    done, waits, problems, connected = [], [], [], False
    prev = None
    for day in days or [now.date()]:
        st = load_state(cfg, day)
        if not st:
            continue
        for slot in sorted(cfg.slots, key=lambda x: x.time):
            post = st["posts"].get(slot.key)
            if not post or not post.get("planned_at") or post.get("jit_skipped") or _in_buffer(post) \
                    or post.get("results"):
                continue
            due = datetime.fromisoformat(post["planned_at"])
            if due - now > ahead:
                continue
            if due < now + timedelta(minutes=10):   # this run is late: shortly after now instead
                due = max(now + timedelta(minutes=10), prev + cfg.catchup_gap if prev else now)
                if due > cutoff:
                    with locked(cfg):
                        s2 = load_state(cfg, day)
                        s2["posts"][slot.key]["jit_skipped"] = now.isoformat(timespec="minutes")
                        save_state(cfg, day, s2)
                    problems.append(f"{slot.title}: no run got to it before the last post time; skipped today")
                    continue
            need = sum(1 for p, on in post["platforms"].items() if on and cfg.platform_enabled(p))
            if need > room:
                waits.append(f"{slot.title}: Buffer already holds {int(b.get('max_scheduled', 10)) - room} "
                             "waiting posts; the next run tries again")
                continue
            if not connected:
                from . import buffer_ledger
                buffer_ledger.use(cfg)
                _, notes = connect_buffer(cfg)
                connected = True
                problems += [f"Buffer channels: {n}" for n in notes if "refused" in n or "isn't on" in n]
            oks, bad = _schedule_post(cfg, day, slot, due, post.get("video_url"))
            room -= len(oks)
            prev = due
            if oks:
                done.append(f"{due.strftime('%I:%M %p').lstrip('0')} {slot.title} ({', '.join(oks)})")
            problems += [f"{slot.title} {x}" for x in bad]
    if notify_problems and (done or problems):
        body = ("Put into Buffer:\n" + "\n".join(done) if done else "") + \
               ("\n\nProblems:\n" + "\n".join(x[:300] for x in problems[:6]) if problems else "")
        if problems:   # a quiet run when all is well; your phone hears about problems
            notify.send(cfg, "SkyBird (cloud): a post couldn't be scheduled", body.strip(), priority="high",
                        click=BUFFER_APP)
    return done, waits, problems


def check_hosts(session=None) -> list[str]:
    """Try each free video host with a small test file; one line per host."""
    import tempfile
    import requests
    from .publishers import buffer
    s = session or requests.Session()
    out = []
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "skybird-host-test.mp4"
        f.write_bytes(os.urandom(512 * 1024))
        for name, *_ in buffer.HOSTS:
            others = tuple(n for n, *_ in buffer.HOSTS if n != name)
            try:
                url, _ = buffer.host_video(f, s, skip=others)
                line = f"{name}: OK {url}"
            except Exception as e:
                line = f"{name}: {str(e)[:300]}"
            out.append(line)
            print(line)
            if os.environ.get("GITHUB_ACTIONS"):
                print(f"::notice title=Video host::{line}")
    return out


def test_notify(cfg: Config) -> bool:
    """Send one test notification; report whether ntfy accepted it (the topic itself stays secret)."""
    load_env_secrets(cfg)
    topic = notify.topic_of(cfg)
    ok = notify.send(cfg, "SkyBird test notification",
                     "If you see this, SkyBird's phone notifications work. Tap to open Buffer.", click=BUFFER_APP)
    raw = str(cfg.section("notify").get("ntfy_topic") or "")
    tidy = " (quotes or a link were removed from it)" if raw.strip() != topic else ""
    bad = notify.topic_problem(topic) if topic else ""
    line = (f"Topic set: {'yes, ' + str(len(topic)) + ' characters' + tidy if topic else 'NO - add the NTFY_TOPIC secret'}. "
            + (f"Problem with it: {bad}. " if bad else "") +
            f"Sent: {'yes' if ok else 'no'} - {notify.LAST['result']}")
    print(line)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::notice title=Notification test::{line}")
    return ok


def check_posts(cfg: Config, now: datetime | None = None) -> list[str]:
    """The hourly check run: puts the posts due in the next ~2 hours into Buffer
    (schedule_due), then, from ~15 minutes after each post time, asks Buffer whether the
    posts really went out, and alerts your phone if one failed or still hasn't gone out
    an hour after its time."""
    from datetime import timedelta as _td
    from . import buffer_ledger
    from .publishers import buffer
    now = now or engine.now_local(cfg)
    if os.environ.get("BUFFER_API_KEY", "").strip():
        cfg.secrets["buffer_api_key"] = os.environ["BUFFER_API_KEY"].strip()
    if os.environ.get("NTFY_TOPIC", "").strip():
        cfg.raw.setdefault("notify", {})["ntfy_topic"] = os.environ["NTFY_TOPIC"].strip()
    key = cfg.secrets.get("buffer_api_key")
    lines, alerts = [], []
    if key and not cfg.dry_run:
        buffer_ledger.use(cfg)
        done, waits, probs = schedule_due(cfg, now, days=[now.date() - _td(days=1), now.date()])
        lines += [f"put into Buffer: {d}" for d in done] + waits + probs
    try:
        n = snapshot_board(cfg, now)
        lines.append(f"airport arrivals board read: {n} flights")
    except Exception as e:
        lines.append(f"couldn't read the airport's arrivals board: {str(e)[:120]}")
    for day in (now.date() - _td(days=1), now.date()):
        st = load_state(cfg, day)
        if not st or not key:
            continue
        changed = False
        for k, post in st["posts"].items():
            due = engine._ts(post.get("posted_at"))
            if not due or now < due + _td(minutes=15) or post.get("checked"):
                continue
            for plat, r in (post.get("results") or {}).items():
                if not r.get("ok") or r.get("id") in (None, "", "dry-run") or r.get("checked"):
                    continue
                try:
                    with buffer.purpose("check"):
                        status = buffer.post_status(key, r["id"])
                except Exception as e:
                    status = f"unknown ({str(e)[:400]})"
                    if "Buffer limit" in str(e) or "kept for scheduling" in str(e) or "rate window" in str(e):
                        lines.append(f"{day} {post['title']} {plat}: not checked ({str(e)[:160]}); the next run tries")
                        continue
                late = now > due + _td(hours=1)
                when = due.strftime("%I:%M %p").lstrip("0")
                if status == "sent":
                    r["checked"] = "sent"
                    _mark_sent(cfg, r["id"])
                elif status == "failed" or (late and status == "waiting"):
                    r["checked"] = status
                    alerts.append(f"{post['title']} ({when}) on {plat}: " +
                                  ("Buffer says it FAILED" if status == "failed" else "still not posted an hour later"))
                elif late:
                    r["checked"] = "unknown"   # couldn't tell; don't keep asking
                lines.append(f"{day} {post['title']} {when} {plat}: {status}")
                changed = True
            if all(r.get("checked") or not r.get("ok") for r in (post.get("results") or {}).values()):
                post["checked"] = True
        if changed:
            with locked(cfg):
                fresh = load_state(cfg, day)
                for k in fresh["posts"]:
                    fresh["posts"][k]["results"] = st["posts"][k].get("results", {})
                    if st["posts"][k].get("checked"):
                        fresh["posts"][k]["checked"] = True
                save_state(cfg, day, fresh)
    for ln in lines:
        print(ln)
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::notice title=Post check::{ln}")
    if alerts:
        notify.send(cfg, "SkyBird (cloud): a post didn't go out", "\n".join(alerts) +
                    "\n\nOpen Buffer to see why (often a missing video); you can re-post it from there.",
                    priority="high", click=BUFFER_APP)
    return lines


BOARD = "https://www.flyoma.com/flight-information/arrivals-departures/"


def read_board(session=None) -> dict:
    """Eppley's own arrivals/departures board (the tables on flyoma.com):
    {"arrivals": [...], "departures": [...]}, each row {airline, flight, city, status, time, gate}."""
    import html as _html
    import re
    import requests
    from .publishers.buffer import UA
    s = session or requests.Session()
    page = s.get(BOARD, headers={"User-Agent": UA}, timeout=30).text
    out = {"arrivals": [], "departures": []}
    for t in re.findall(r"(?is)<table.*?</table>", page):
        head = " ".join(re.findall(r"(?is)<th[^>]*>(.*?)</th>", t)).lower()
        kind = "arrivals" if "origin" in head else "departures" if "destination" in head else None
        if not kind:
            continue
        for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", t):
            cells = [_html.unescape(re.sub(r"<[^>]+>", " ", c)).strip() for c in re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)]
            cells = [re.sub(r"\s+", " ", c) for c in cells]
            if len(cells) >= 5 and cells[1]:
                out[kind].append({"airline": cells[0], "flight": cells[1], "city": cells[2], "status": cells[3],
                                  "time": cells[4], "gate": cells[5] if len(cells) > 5 else ""})
    return out


# Airline names on the board -> callsign prefixes that may fly them (mainline + regionals)
OPERATORS = {"southwest": ["SWA"], "american": ["AAL", "ENY", "SKW", "RPA", "JIA", "PDT", "ASH"],
             "delta": ["DAL", "EDV", "SKW", "RPA", "ASH"], "united": ["UAL", "SKW", "RPA", "ASH", "GJS", "UCA"],
             "alaska": ["ASA", "QXE", "SKW"], "allegiant": ["AAY"], "frontier": ["FFT"], "sun country": ["SCX"],
             "breeze": ["MXY"], "spirit": ["NKS"], "jetblue": ["JBU"]}


IATA = {"southwest": "WN", "american": "AA", "delta": "DL", "united": "UA", "alaska": "AS", "allegiant": "G4",
        "frontier": "F9", "sun country": "SY", "breeze": "MX", "spirit": "NK", "jetblue": "B6"}


def _board_time(txt: str, now: datetime):
    """'3:10 PM' (today) or '10/22 3:25 PM' -> aware datetime."""
    import re
    m = re.search(r"(?:(\d{1,2})/(\d{1,2})\s+)?(\d{1,2}):(\d{2})\s*([AP]M)", txt or "", re.I)
    if not m:
        return None
    mo, dd, hh, mm, ap = m.groups()
    h = int(hh) % 12 + (12 if ap.upper() == "PM" else 0)
    d = now.date()
    if mo:
        y = now.year + (1 if int(mo) < now.month - 6 else -1 if int(mo) > now.month + 6 else 0)
        d = date(y, int(mo), int(dd))
    return datetime(d.year, d.month, d.day, h, int(mm), tzinfo=now.tzinfo)


def board_on(cfg: Config) -> bool:
    """Only Omaha has an airport arrivals board SkyBird reads (flyoma.com)."""
    return bool(cfg.city.get("board_check")) if cfg.city else True


def snapshot_board(cfg: Config, now: datetime | None = None, session=None) -> int:
    """Read Eppley's arrivals board and keep each flight (latest status) under its flight
    day (3:30 AM to 3:29 AM). Several readings a day build the whole day's list."""
    import json
    if not board_on(cfg):
        return 0
    now = now or engine.now_local(cfg)
    rows = read_board(session)["arrivals"]
    folder = cfg.data_dir / "board"
    folder.mkdir(parents=True, exist_ok=True)
    days: dict = {}
    shift = timedelta(hours=cfg.day_start.hour, minutes=cfg.day_start.minute)
    for r in rows:
        t = _board_time(r["time"], now)
        if not t:
            continue
        fday = (t - shift).date().isoformat()
        days.setdefault(fday, {})[f"{r['airline']} {r['flight']} {t:%H:%M}"] = dict(r, at=t.isoformat(), seen=now.isoformat(timespec="minutes"))
    for fday, flights in days.items():
        path = folder / f"{fday}.json"
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        old.update(flights)
        path.write_text(json.dumps(old, indent=1), encoding="utf-8")
    for p in sorted(folder.glob("*.json"))[:-10]:
        p.unlink(missing_ok=True)
    return len(rows)


def compare_with_board(cfg: Config, flight_day: date) -> str:
    """'Airport board: 74 arrivals (2 cancelled), SkyBird found 70; missing AA 4042, ...'
    using the saved board readings and the day's master flight list."""
    import json
    import re
    if not board_on(cfg):
        return ""
    bpath = cfg.data_dir / "board" / f"{flight_day.isoformat()}.json"
    fpath = cfg.data_dir / "flights" / f"{flight_day.isoformat()}.json"
    if not bpath.exists() or not fpath.exists():
        return ""
    board = list(json.loads(bpath.read_text(encoding="utf-8")).values())
    flights = json.loads(fpath.read_text(encoding="utf-8"))["flights"]
    ours = {(f.get("callsign") or "").strip().upper() for f in flights if f.get("estArrivalAirport") == cfg.hub}
    ours_nums = {}
    for cs in ours:
        m = re.match(r"([A-Z]{3})(\d+)", cs)
        if m:
            ours_nums.setdefault(m.group(2).lstrip("0"), set()).add(m.group(1))
    live = [b for b in board if "cancel" not in b.get("status", "").lower()]
    cancelled = len(board) - len(live)
    found, missing = 0, []
    for b in live:
        num = re.sub(r"\D", "", b["flight"]).lstrip("0")
        ops = OPERATORS.get(b["airline"].lower().strip(), [])
        if num and ours_nums.get(num, set()) & set(ops or ours_nums.get(num, set())):
            found += 1
        else:
            missing.append(f"{IATA.get(b['airline'].lower().strip(), b['airline'])} {b['flight']}")
    times = sorted(b["at"] for b in board)
    since = datetime.fromisoformat(times[0]).strftime("%I:%M %p").lstrip("0") if times else ""
    out = (f"Airport board: {len(live)} arrivals" + (f" ({cancelled} cancelled)" if cancelled else "") +
           (f" from {since}" if since else "") + f"; SkyBird found {found} of them")
    if missing:
        out += f"; missing {len(missing)}: " + ", ".join(missing[:12]) + ("…" if len(missing) > 12 else "")
    return out


def probe_board(session=None) -> list[str]:
    """Report what Eppley's board lists right now (how many rows, which times)."""
    out = []
    try:
        b = read_board(session)
        rows = b["arrivals"]
        times = [r["time"] for r in rows]
        out.append(f"arrivals board: {len(rows)} rows, first {times[0] if times else '-'}, last {times[-1] if times else '-'}")
    except Exception as e:
        out.append(f"couldn't read the board: {type(e).__name__}: {str(e)[:200]}")
    for ln in out:
        print(ln)
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::notice title=Airport board::{ln[:950]}")
    return out


def check_opensky(cfg: Config) -> list[str]:
    """Send OpenSky ONE arrival fill-in request (plus a second shape if the first is
    refused) for a plane from the latest saved flight list, and report its exact answer."""
    import json
    import requests
    from . import flights as fl
    load_env_secrets(cfg)
    out = []
    files = sorted((cfg.data_dir / "flights").glob("*.json"))
    if not files:
        out.append("no saved flight list yet")
    else:
        day = date.fromisoformat(files[-1].stem)
        raw = json.loads(files[-1].read_text(encoding="utf-8"))["flights"]
        dep = next((f for f in raw if f.get("estDepartureAirport") == cfg.hub and f.get("icao24")
                    and (f.get("callsign") or "").startswith(("SWA", "UAL", "AAL", "DAL"))), None)
        if not dep:
            out.append(f"no airline departure in the {day} list")
        else:
            s = requests.Session()
            tok = fl._token(cfg, s)
            h = {"Authorization": f"Bearer {tok}"} if tok else {}
            start, stop = cfg.flight_day(day)
            tries = [("as SkyBird asks (36 h window)", int(start.timestamp()) - 12 * 3600, int(stop.timestamp()) - 1),
                     ("24 h before its departure", dep["firstSeen"] - 24 * 3600, dep["firstSeen"])]
            for label, b, e in tries:
                r = s.get(f"{fl.API}/flights/aircraft", params={"icao24": dep["icao24"], "begin": b, "end": e},
                          headers=h, timeout=60)
                body = r.text[:300].replace("\n", " ")
                n = len(r.json()) if r.status_code == 200 else 0
                out.append(f"{label}: HTTP {r.status_code}, {n} flights; limit left "
                           f"{r.headers.get('X-Rate-Limit-Remaining', '?')}; answer: {body}")
                if r.status_code == 200:
                    break
    for ln in out:
        print(ln)
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::notice title=OpenSky check::{ln[:950]}")
    return out
