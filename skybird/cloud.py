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
        out = f"{c.get('departures', '?')} airline departures, {c.get('arrivals', '?')} arrivals ({st.get('data_date', '')})"
        b = st.get("eppley_arrivals") or {}
        if b:
            out += (f". Arrivals: {b.get('from_opensky', 0)} from OpenSky + {b.get('moved_from_council_bluffs', 0)} "
                    f"moved from Council Bluffs/Millard + {b.get('filled_in', 0)} filled in")
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
    if not cfg.dry_run:
        found = connect_buffer(cfg)
        log(cfg, "Buffer channels: " + ", ".join(f"{p}={'yes' if p in found else 'missing'}" for p in PLATFORMS))
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
    if in_buffer:
        log(cfg, f"{today}: already in Buffer from an earlier run, not scheduled again: {', '.join(sorted(in_buffer))}")
    # Store the videos on GitHub so Buffer's links never expire (see publishers/github_host.py)
    links, storage_note = {}, ""
    todo = [Path(st["posts"][sl.key]["video"]) for sl in cfg.slots if sl.key in times and sl.key not in in_buffer
            and (not only or sl.key in only) and st["posts"].get(sl.key, {}).get("video_exists")]
    if todo and not cfg.dry_run:
        from .publishers import github_host
        try:
            links = github_host.upload(todo, today)
            removed = github_host.cleanup(now.date())
            log(cfg, f"Videos stored on GitHub ({len(links)})" + (f"; removed {removed} old day(s)" if removed else ""))
        except Exception as e:   # NotAvailable, or GitHub hiccup: fall back to the free hosts
            storage_note = str(e)
            log(cfg, f"Couldn't store the videos on GitHub: {e}")
            if os.environ.get("GITHUB_ACTIONS"):
                print(f"::warning title=Video storage::{storage_note[:900]}")
    for slot in sorted(cfg.slots, key=lambda s: s.time):
        post = st["posts"].get(slot.key)
        if only and slot.key not in only:
            continue
        if not post or not post["video_exists"] or slot.key not in times or slot.key in in_buffer:
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
                    got = buffer.post(Path(post["video"]), caps, cfg.secrets, due=due,
                                      video_url=links.get(Path(post["video"]).name))
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
        if os.environ.get("GITHUB_ACTIONS"):   # readable on the run's page (Annotations)
            for b in bad:
                print(f"::warning title={slot.title}::{b[:900]}")
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
    vs_board = compare_with_board(cfg, cfg.data_date(today))
    if vs_board:
        body += f"\n\n{vs_board}"
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::notice title=Airport board check::{vs_board[:950]}")
    if st.get("incomplete"):
        body += "\n\n⚠️ " + st["incomplete"] + " Check the videos in Buffer; delete them if they look wrong."
    if storage_note:
        body += "\n\n⚠️ The videos couldn't be stored on GitHub, so Buffer may lose them before posting: " + storage_note[:300]
    if problems:
        body += "\n\nProblems:\n" + "\n".join(p[:300] for p in problems[:6])
    body += "\n\nReview them in the Buffer app (Queue): edit or delete anything you don't want before it posts."
    n = len(scheduled)
    title = "SkyBird (cloud): " + (f"{n} post{'s' if n != 1 else ''} scheduled" if scheduled else
                                   "already scheduled earlier" if in_buffer else "nothing scheduled")
    notify.send(cfg, title + (" [dry run]" if cfg.dry_run else ""), body,
                priority="high" if problems or not scheduled else "default", click=BUFFER_APP)
    print(title + "\n" + body)
    if not scheduled and not in_buffer:
        raise SystemExit(title)
    return {"scheduled": scheduled, "problems": problems}


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
    """~30 minutes after each post time: ask Buffer whether the posts really went out,
    and alert your phone if one failed or still hasn't gone out an hour after its time."""
    from datetime import timedelta as _td
    from .publishers import buffer
    now = now or engine.now_local(cfg)
    if os.environ.get("BUFFER_API_KEY", "").strip():
        cfg.secrets["buffer_api_key"] = os.environ["BUFFER_API_KEY"].strip()
    if os.environ.get("NTFY_TOPIC", "").strip():
        cfg.raw.setdefault("notify", {})["ntfy_topic"] = os.environ["NTFY_TOPIC"].strip()
    key = cfg.secrets.get("buffer_api_key")
    lines, alerts = [], []
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
                    status = buffer.post_status(key, r["id"])
                except Exception as e:
                    status = f"unknown ({str(e)[:80]})"
                late = now > due + _td(hours=1)
                when = due.strftime("%I:%M %p").lstrip("0")
                if status == "sent":
                    r["checked"] = "sent"
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


def snapshot_board(cfg: Config, now: datetime | None = None, session=None) -> int:
    """Read Eppley's arrivals board and keep each flight (latest status) under its flight
    day (3:30 AM to 3:29 AM). Several readings a day build the whole day's list."""
    import json
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
    bpath = cfg.data_dir / "board" / f"{flight_day.isoformat()}.json"
    fpath = cfg.data_dir / "flights" / f"{flight_day.isoformat()}.json"
    if not bpath.exists() or not fpath.exists():
        return ""
    board = list(json.loads(bpath.read_text(encoding="utf-8")).values())
    flights = json.loads(fpath.read_text(encoding="utf-8"))["flights"]
    ours = {(f.get("callsign") or "").strip().upper() for f in flights if f.get("estArrivalAirport") == "KOMA"}
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
