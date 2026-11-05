"""Weekly performance report (cloud): every Monday morning, after the week's posts
have had time to collect views.

  1. asks Buffer for the numbers of every post from the last 14 days (one request per
     post per platform; nothing is posted and OpenSky isn't used),
  2. saves them with the post (state.json -> results -> metrics),
  3. writes a report comparing post types, caption styles, calls to action and @tags,
     and puts it on GitHub Pages (reports/latest.html) and on the run's page,
  4. sends a short phone notification that opens the report.

"Index" = a post's views compared with the usual (median) views on that platform over
the last 14 days. 100 = typical, 150 = half again as many views.

    python -m skybird --config cloud/config.toml cloud-run --weekly-report
"""
from __future__ import annotations

import html
import os
import statistics
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import engine, notify
from .config import Config
from .state import load_state, locked, save_state

LOOKBACK_DAYS = 28         # the posting-volume test needs 4 weeks to compare
COLLECT_DAYS = 14          # numbers are refreshed for this many days; older ones are kept as saved
SETTLE_HOURS = 20          # newer posts don't have meaningful numbers yet
PAUSE = 0.4                # seconds between Buffer requests (keeps the API use gentle)
MIN_POSTS = 2              # a group needs this many posts to be ranked
STYLE_NAMES = {"friendly": "Friendly Neighbor", "deadpan": "Deadpan Tower", "question": "Question",
               "did_you_know": "Did you know", "guess": "Guess first", "local_tie_in": "Local tie-in"}
CTA_NAMES = {"follow": "Follow", "tag_friend": "Tag a friend", "save": "Save this", "spotlight": "Spotlight"}


def collect(cfg: Config, key: str, now: datetime, session=None) -> tuple[int, list[str]]:
    """Fetch Buffer's numbers for the last 14 days' posts: all of them in one or two
    batched calls (one call per post only if Buffer can't be asked in a batch, and then
    only as far as the day's Buffer budget allows). Returns (posts updated, problems)."""
    from .publishers import buffer
    kw = {"session": session} if session else {}
    wanted = {}   # post id -> [(day, slot, platform)]
    for back in range(COLLECT_DAYS + 1):
        day = now.date() - timedelta(days=back)
        st = load_state(cfg, day)
        if not st:
            continue
        for k, post in st["posts"].items():
            due = engine._ts(post.get("posted_at"))
            if not due or now < due + timedelta(hours=SETTLE_HOURS):
                continue
            for plat, r in (post.get("results") or {}).items():
                if r.get("ok") and r.get("id") not in (None, "", "dry-run") and not r.get("gone"):
                    wanted.setdefault(r["id"], []).append((day, k, plat))
    if not wanted:
        return 0, []
    problems: list[str] = []
    with buffer.purpose("metrics"):
        try:
            got, gone, problems = buffer.many_metrics(key, list(wanted), **kw)
        except Exception as e:
            if "Buffer limit" in str(e) or "kept for scheduling" in str(e) or "rate window" in str(e) \
                    or "rejected the API key" in str(e):
                return 0, [str(e)[:300]]
            got, gone = {}, set()   # no batched reads: one at a time, newest first, within the budget
            for pid in list(wanted)[:20]:
                try:
                    m = buffer.post_metrics(key, pid, **kw)
                except Exception as e2:
                    if "not found" in str(e2).lower():
                        gone.add(pid)
                        continue
                    problems.append(f"{wanted[pid][0][0]} {wanted[pid][0][1]} {wanted[pid][0][2]}: {str(e2)[:300]}")
                    if "rate limit" in str(e2) or "Buffer limit" in str(e2) or "kept for" in str(e2) or len(problems) >= 6:
                        break
                    continue
                finally:
                    time.sleep(PAUSE if not session else 0)
                if m:
                    got[pid] = m
    by_day: dict = {}
    for pid, where in wanted.items():
        for day, k, plat in where:
            if pid in got or pid in gone:
                by_day.setdefault(day, []).append((k, plat, got.get(pid)))
    for day, items in by_day.items():
        with locked(cfg):
            fresh = load_state(cfg, day)
            for k, plat, m in items:
                r = fresh["posts"][k]["results"][plat]
                if m is None:
                    r["gone"] = True   # deleted in Buffer: skip it from now on
                else:
                    r["metrics"], r["metrics_at"] = m, now.isoformat(timespec="seconds")
            save_state(cfg, day, fresh)
    return len(got), problems


def daily_numbers(cfg: Config, now: datetime, session=None) -> tuple[int, list[str]]:
    """Buffer's numbers once a day per city: every recent post (collect) plus each of the
    city's channels' totals for the last 7 days (Buffer's aggregated numbers, one call per
    channel), saved in cloud/data/<city>/channel_numbers.json for the reports."""
    import json
    from .publishers import buffer
    mark = cfg.data_dir / "numbers_day.txt"
    today = now.date().isoformat()
    if mark.exists() and mark.read_text().strip() == today:
        return 0, []
    key = cfg.secrets.get("buffer_api_key")
    if not key:
        return 0, ["no Buffer API key"]
    n, problems = collect(cfg, key, now, session)
    chans = {p: cfg.secrets[f"buffer_channel_{p}"] for p in ("facebook", "tiktok", "instagram")
             if cfg.secrets.get(f"buffer_channel_{p}")}
    org = ""
    try:
        org = json.loads((cfg.shared_dir / "buffer_channels.json").read_text(encoding="utf-8")).get("org_id", "")
    except (OSError, ValueError):
        pass
    path = cfg.data_dir / "channel_numbers.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    kw = {"session": session} if session else {}
    if org and chans:
        start = now - timedelta(days=7)
        with buffer.purpose("metrics"):
            for plat, cid in chans.items():
                try:
                    saved.setdefault(today, {})[plat] = buffer.aggregated(key, org, [cid], start, now, **kw)
                except Exception as e:
                    problems.append(f"{plat} channel totals: {str(e)[:200]}")
                    if "Buffer limit" in str(e) or "kept for" in str(e) or "no aggregated" in str(e):
                        break
    if saved:
        keep = sorted(saved)[-60:]
        path.write_text(json.dumps({k: saved[k] for k in keep}, indent=1), encoding="utf-8")
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text(today)
    return n, problems


def buffer_table(cfg: Config) -> list[str]:
    """Buffer calls per day (all cities; this city's by purpose)."""
    from .buffer_ledger import BufferLedger
    lg = BufferLedger(cfg)
    days = lg.summary(7)
    if not days:
        return []
    lines = ["## Buffer use (last 7 days)", "",
             f"| Day | {cfg.city_name} | Scheduling | Checks | Numbers | Setup | All cities |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for d in days:
        mine = d.get("cities", {}).get(cfg.city_id, {})
        lines.append(f"| {d['day']} | {sum(mine.values())} | {mine.get('post', 0)} | {mine.get('check', 0)} | "
                     f"{mine.get('metrics', 0)} | {mine.get('setup', 0) + mine.get('other', 0)} | {d.get('total', 0)} |")
    lines += ["", f"Limit: {lg.caps['daily_calls']} Buffer calls a day for all cities; the last "
              f"{lg.caps['keep_for_posting']} are kept for scheduling posts.", ""]
    return lines


def rows(cfg: Config, now: datetime) -> list[dict]:
    """One row per post per platform that has numbers."""
    out = []
    launch = cfg.city.get("launch_date") if cfg.city else None
    lookback = max(LOOKBACK_DAYS, (now.date() - date.fromisoformat(str(launch))).days if launch else 0)
    for back in range(lookback, -1, -1):
        day = now.date() - timedelta(days=back)
        st = load_state(cfg, day)
        if not st:
            continue
        for k, post in st["posts"].items():
            tr = post.get("tracking") or {}
            for plat, r in (post.get("results") or {}).items():
                m = r.get("metrics") or {}
                if not r.get("ok"):
                    continue
                out.append({
                    "day": day, "slot": k, "title": post.get("title", k), "platform": plat,
                    "format": tr.get("format") or k, "style": tr.get("caption_style") or "",
                    "cta": tr.get("cta") or "", "tagged": bool((tr.get("mentions") or {}).get(plat)),
                    "event": tr.get("event") or "", "weekday": day.strftime("%a"),
                    "views": m.get("views"), "likes": m.get("likes", 0), "comments": m.get("comments", 0),
                    "shares": m.get("shares", 0), "saves": m.get("saves", 0), "follows": m.get("follows"),
                    "checked": r.get("checked", ""), "has_numbers": bool(m),
                    "day_posts": tr.get("day_posts") or 4,
                })
    medians = {}
    for plat in {r["platform"] for r in out}:
        vals = [r["views"] for r in out if r["platform"] == plat and r["views"]]
        medians[plat] = statistics.median(vals) if vals else None
    for r in out:
        med = medians.get(r["platform"])
        r["index"] = round(100 * r["views"] / med) if r["views"] is not None and med else None
        r["interactions"] = sum(r[x] or 0 for x in ("likes", "comments", "shares", "saves"))
    return out


def _group(rs: list[dict], key: str, names: dict | None = None) -> list[tuple[str, int, int, float]]:
    """[(label, posts, median index, interactions per 1k views)] best first."""
    groups: dict[str, list[dict]] = {}
    for r in rs:
        if r["index"] is None:
            continue
        label = r[key]
        if isinstance(label, bool):
            label = "With @tags" if label else "No @tags"
        groups.setdefault((names or {}).get(label, label) or "(none)", []).append(r)
    out = []
    for label, g in groups.items():
        views = sum(r["views"] or 0 for r in g)
        out.append((label, len(g), round(statistics.median(r["index"] for r in g)),
                    round(1000 * sum(r["interactions"] for r in g) / views, 1) if views else 0.0))
    return sorted(out, key=lambda t: (t[1] < MIN_POSTS, -t[2]))


SLOT_NAMES = {"commercial": "Eppley commercial", "private": "Private jets", "military": "Offutt military",
              "all": "All airports", "guess": "#01 Guess the destination", "scoreboard": "#10 Metro scoreboard",
              "wrap": "#07 Sunday weekly wrap", "lincoln": "Lincoln game-weekend jets",
              "farthest": "#02 Farthest flight", "firstlast": "#03 First up, last down", "hours": "#06 Busiest hours",
              "weekend": "#14 Weekend arrivals", "types": "#30 Which jets fly Omaha"}
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def volume_tables(rs: list[dict]) -> list[str]:
    """Posting-volume test: per-post index and total views per day, by posts that day,
    and total views by weekday x posts that day (days where every post has numbers)."""
    days: dict = {}
    for r in rs:
        days.setdefault(r["day"], []).append(r)
    full = {d: g for d, g in days.items() if all(r["has_numbers"] for r in g)}
    if not full:
        return []
    lines = ["## Posts per day (last 4 weeks)", "",
             "| Posts that day | Days | Index per post | Total views per day |", "|---|---:|---:|---:|"]
    best = None
    for n in (1, 2, 3, 4):
        ds = [g for g in full.values() if g[0]["day_posts"] == n]
        if not ds:
            continue
        idx = [r["index"] for g in ds for r in g if r["index"] is not None]
        tot = round(statistics.mean(sum(r["views"] or 0 for r in g) for g in ds))
        lines.append(f"| {n} | {len(ds)} | {round(statistics.median(idx)) if idx else '–'} | {tot:,} |")
        if len(ds) >= 2 and (best is None or tot > best[1]):
            best = (n, tot)
    lines += ["", "| Day | " + " | ".join(f"{n} post{'s' if n > 1 else ''}" for n in (1, 2, 3, 4)) + " |",
              "|---|" + "---:|" * 4]
    for name in DAY_NAMES:
        cells = []
        for n in (1, 2, 3, 4):
            ds = [g for d, g in full.items() if d.strftime("%a") == name and g[0]["day_posts"] == n]
            cells.append(f"{round(statistics.mean(sum(r['views'] or 0 for r in g) for g in ds)):,}" if ds else "–")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += ["", "Total views per day across Instagram, Facebook and TikTok. Each weekday gets each level once "
              "every 4 weeks, so this table fills in over the month. The Sunday wrap is extra and not counted as a level."]
    if best:
        lines.append(f"\nMost total views so far: **{best[0]} post{'s' if best[0] > 1 else ''} a day** "
                     f"({best[1]:,} views a day).")
    lines.append("")
    return lines


def opensky_table(cfg: Config) -> list[str]:
    """OpenSky requests per day for the last 7 ledger days: this city by category, all cities."""
    from .budget import CATEGORIES, Ledger
    lg = Ledger(cfg)
    days = lg.summary(7)
    if not days:
        return []
    lines = ["## OpenSky use (last 7 days)", "",
             f"| Day | {cfg.city_name} requests | Lists | Fill-in | Types | Backfill | {cfg.city_name} credits | All cities | OpenSky credits left |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for d in days:
        mine = d.get("cities", {}).get(cfg.city_id, {})
        cr = (d.get("credits") or {}).get(cfg.city_id)
        lines.append(f"| {d['day']} | {sum(mine.values()):,} | " + " | ".join(str(mine.get(c, 0)) for c in CATEGORIES)
                     + f" | {f'{cr:,}' if cr else '–'} | {d.get('total', 0):,} | {d['remaining'] if d.get('remaining') is not None else '–'}"
                     + (" (429)" if d.get("blocked") else "") + " |")
    caps = lg.caps
    lines += ["", f"Limits per day: {int(caps['per_city']):,} per city, {int(caps['total']):,} for all cities "
              f"(the account allows 4,000). Alerts at {caps['alert_pct']}%. Credits: OpenSky's own count, measured "
              "from its remaining-credits header; one request can cost several credits.", ""]
    return lines


def build(cfg: Config, now: datetime) -> dict:
    """The report as {"title", "summary" (phone text), "markdown", "html"}."""
    rs = rows(cfg, now)
    week_end = now.date() - timedelta(days=1)
    week_start = week_end - timedelta(days=6)
    week = [r for r in rs if week_start <= r["day"] <= week_end]
    prev = [r for r in rs if r["day"] < week_start]
    title = f"SkyBird week of {week_start:%b %-d} – {week_end:%b %-d}"
    posts = {(r["day"], r["slot"]) for r in week}
    failed = sum(1 for r in week if r["checked"] == "failed")
    numbered = [r for r in week if r["has_numbers"]]

    def tot(rr, f):
        return sum(r[f] or 0 for r in rr)

    lines = [f"# {title}", ""]
    if not numbered:
        lines += ["Buffer didn't return numbers for this week's posts yet, so there's nothing to compare.",
                  "The report fills in once Buffer reports views (posts need about a day)."]
    v, pv = tot(week, "views"), tot(prev, "views")
    change = f" ({'+' if v >= pv else ''}{round(100 * (v - pv) / pv)}% vs the week before)" if pv else ""
    lines += ["## The week", "",
              f"- **{len(posts)}** posts went out" + (f", **{failed}** failed on a platform" if failed else ""),
              f"- **{v:,}** views{change}",
              f"- **{tot(week, 'likes'):,}** likes · **{tot(week, 'comments'):,}** comments · "
              f"**{tot(week, 'shares'):,}** shares · **{tot(week, 'saves'):,}** saves", ""]
    plats = sorted({r["platform"] for r in week})
    if plats:
        lines += ["| Platform | Posts | Views | Interactions |", "|---|---:|---:|---:|"]
        for p in plats:
            pr = [r for r in week if r["platform"] == p]
            lines.append(f"| {p.title()} | {len(pr)} | {tot(pr, 'views'):,} | {sum(r['interactions'] for r in pr):,} |")
        lines.append("")
    from .config import PLATFORMS
    paused = [p for p in PLATFORMS if not cfg.platform_enabled(p)]
    if paused:
        lines += [", ".join(f"{'TikTok' if p == 'tiktok' else p.title()}: paused" for p in paused), ""]
    tables = [("Post type", "format", SLOT_NAMES), ("Caption style", "style", STYLE_NAMES),
              ("Call to action", "cta", CTA_NAMES), ("@tags", "tagged", None), ("Day of week", "weekday", None)]
    winners = []
    for name, key, names in tables:
        g = _group(rs, key, names)
        if not g:
            continue
        lines += [f"## {name} (last 14 days)", "", f"| {name} | Posts | Index | Interactions per 1k views |",
                  "|---|---:|---:|---:|"]
        for label, n, idx, per in g:
            lines.append(f"| {label}{' (too few to call)' if n < MIN_POSTS else ''} | {n} | {idx} | {per} |")
        lines.append("")
        top = [t for t in g if t[1] >= MIN_POSTS]
        if len(top) >= 2:
            winners.append(f"{name}: {top[0][0]} leads ({top[0][2]} vs {top[-1][2]} for {top[-1][0]})")
    lines += volume_tables(rs)
    tc = traffic_check(cfg)
    if tc:
        lines += ["## Traffic check", "", tc, ""]
    lines += launch_review(cfg, rs, now)
    lines += opensky_table(cfg)
    lines += buffer_table(cfg)
    ranked = sorted((r for r in week if r["index"] is not None), key=lambda r: -r["index"])
    if ranked:
        lines += ["## Best and worst this week", "", "| Post | Platform | Views | Index |", "|---|---|---:|---:|"]
        for r in ranked[:3] + ([None] if len(ranked) > 6 else []) + (ranked[-3:] if len(ranked) > 6 else []):
            if r is None:
                lines.append("| … | | | |")
                continue
            lines.append(f"| {r['day']:%a %b %-d} {SLOT_NAMES.get(r['format'], r['format'])} | {r['platform'].title()} "
                         f"| {r['views']:,} | {r['index']} |")
        lines.append("")
    if winners:
        lines += ["## What's working", ""] + [f"- {w}" for w in winners] + [""]
    lines += ["Index: views compared with the platform's usual post over the last 14 days (100 = typical).",
              "Groups with fewer than 2 posts are shown but not ranked."]
    md = "\n".join(lines)
    summary = (f"{len(posts)} posts, {v:,} views{change}." +
               (f" {winners[0]}." if winners else "") + (f" {failed} failed." if failed else ""))
    return {"title": title, "summary": summary, "markdown": md, "html": to_html(title, md)}


def to_html(title: str, md: str) -> str:
    """Tiny Markdown -> HTML (headings, bullets, tables, bold, links) in SkyBird colors."""
    import re
    body, table = [], []

    def inline(s):
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(s, quote=False))
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s\"']+)\)", r"<a href='\2'>\1</a>", s)   # [text](link)
        return s.replace("&lt;br&gt;", "<br>")

    def flush():
        if table:
            head, *rest = [[c.strip() for c in row.strip("|").split("|")] for row in table if not set(row) <= set("|-: ")]
            body.append("<div class=t><table><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr>" +
                        "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rest) +
                        "</table></div>")
            table.clear()
    for ln in md.splitlines():
        if ln.startswith("|"):
            table.append(ln)
            continue
        flush()
        if ln.startswith("# "):
            body.append(f"<h1>{inline(ln[2:])}</h1>")
        elif ln.startswith("## "):
            body.append(f"<h2>{inline(ln[3:])}</h2>")
        elif ln.startswith("- "):
            body.append(f"<li>{inline(ln[2:])}</li>")
        elif ln.strip():
            body.append(f"<p>{inline(ln)}</p>")
    flush()
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>"
            ":root{--bg:#F6F2E7;--ink:#0A2540;--navy:#014291;--blue:#0999EA;--line:#d9d3c3}"
            "@media(prefers-color-scheme:dark){:root{--bg:#0A2540;--ink:#F6F2E7;--navy:#AEE1D8;--blue:#5cc0f5;--line:#24405f}}"
            "body{background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif;max-width:760px;"
            "margin:0 auto;padding:24px 16px}h1{color:var(--navy);font-style:italic;font-size:26px}"
            "h2{color:var(--navy);font-size:18px;margin-top:28px}li{margin:4px 0}.t{overflow-x:auto}"
            "table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}"
            "th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}"
            "th{font-size:12px;text-transform:uppercase;letter-spacing:.04em}td:not(:first-child){text-align:right}"
            "th:not(:first-child){text-align:right}p{color:var(--ink);opacity:.85}</style>" + "\n".join(body))


def run(cfg: Config, now: datetime | None = None, session=None) -> dict:
    """Collect the numbers, write the report, publish it and notify."""
    now = now or engine.now_local(cfg)
    if os.environ.get("BUFFER_API_KEY", "").strip():
        cfg.secrets["buffer_api_key"] = os.environ["BUFFER_API_KEY"].strip()
    if os.environ.get("NTFY_TOPIC", "").strip():
        cfg.raw.setdefault("notify", {})["ntfy_topic"] = os.environ["NTFY_TOPIC"].strip()
    key = cfg.secrets.get("buffer_api_key")
    if key:
        from . import buffer_ledger
        from .cloud import connect_buffer
        buffer_ledger.use(cfg)
        try:
            connect_buffer(cfg)   # this city's own channels, for the channel totals
        except Exception as e:
            print(f"Couldn't check the Buffer channels: {e}")
        n, problems = daily_numbers(cfg, now, session)   # already read today: uses what was saved
    else:
        n, problems = 0, ["no Buffer API key"]
    rep = build(cfg, now)
    md = rep["markdown"]
    if problems:
        md += "\n\n## Couldn't read\n\n" + "\n".join(f"- {p}" for p in problems)
        rep["html"] = to_html(rep["title"], md)
    folder = cfg.data_dir / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{now.date().isoformat()}.md").write_text(md, encoding="utf-8")
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md + "\n")
    for p in problems:
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning title=Buffer numbers::{p[:900]}")
    link = ""
    try:
        from .publishers import pages_host
        link = pages_host.publish_report(rep["html"], now.date(), city=cfg.city_id if cfg.city else None)
    except Exception as e:
        print(f"Couldn't put the report on GitHub Pages: {e}")
        if os.environ.get("GITHUB_RUN_ID"):
            link = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                    f"{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    notify.send(cfg, "SkyBird (cloud): weekly report", rep["summary"] +
                (f"\n\nCouldn't read {len(problems)} post(s) from Buffer; see the report." if problems else "") +
                "\n\nTap to open the full report.", click=link or None)
    rep.update(collected=n, problems=problems, link=link)
    return rep


# ---- a city's quiet build and launch -----------------------------------------------------

def _hub_moves(cfg: Config, posts: dict) -> int | None:
    """All takeoffs and landings at the hub that day (airlines, cargo, private), from the 'all' post."""
    short = cfg.hub[1:] if len(cfg.hub) == 4 else cfg.hub
    for a in ((posts.get("all") or {}).get("airports") or []):
        if a.get("code") in (cfg.hub, short) or a.get("name") == cfg.hub_name:
            return a.get("count")
    return None


def record_day(cfg: Config, post_date: date) -> dict:
    """Keep the day's counts (cloud/data/<city>/daily_counts.json, 60 days): the videos and
    their stats.json aren't kept between runs, the sample page and reports need the numbers."""
    import json
    try:
        st = json.loads(cfg.media_file(post_date, "stats.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    posts = st.get("posts") or {}
    hub = st.get("hub") or {}
    row = {"data_date": st.get("data_date") or cfg.data_date(post_date).isoformat(),
           "hub_moves": _hub_moves(cfg, posts), "airline_departures": hub.get("departures"),
           "airline_arrivals": hub.get("arrivals"), "complete": bool(st.get("complete", True)),
           "opensky": st.get("opensky_requests", "")}
    path = cfg.data_dir / "daily_counts.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    saved[post_date.isoformat()] = row
    keep = sorted(saved)[-60:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: saved[k] for k in keep}, indent=1), encoding="utf-8")
    return row


def daily_counts(cfg: Config) -> dict:
    import json
    try:
        return json.loads((cfg.data_dir / "daily_counts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def traffic_check(cfg: Config, days: int = 14) -> str:
    """SkyBird's average day at the hub against the airport's own published traffic
    ([traffic] in the city profile: monthly_ops and its source). '' if there's no reference."""
    ref = cfg.city.get("traffic") or {}
    monthly = ref.get("monthly_ops")
    counts = [r["hub_moves"] for _, r in sorted(daily_counts(cfg).items())[-days:] if r.get("hub_moves")]
    if not monthly or not counts:
        return ""
    ours, theirs = statistics.mean(counts), float(monthly) * 12 / 365
    pct = round(100 * ours / theirs)
    lo, hi = ref.get("low_pct", 70), ref.get("high_pct", 115)
    verdict = ("in line" if lo <= pct <= hi else
               "LOW: OpenSky may be missing flights; check before launch" if pct < lo else
               "HIGH: something may be counted twice; check before launch")
    return (f"{cfg.hub_name} traffic check: SkyBird averages {ours:,.0f} takeoffs and landings a day over "
            f"{len(counts)} days; {cfg.hub_name} reports about {theirs:,.0f} a day "
            f"({int(monthly):,} a month, {ref.get('source', 'airport statistics')}). {pct}%: {verdict} "
            f"(expected {lo}–{hi}%: OpenSky misses some small planes).")


def sample_page(cfg: Config, now: datetime, base: str = "") -> str:
    """The quiet build's sample: every dry-run day so far, what would have posted, the video
    (last week), the counts, the traffic check and OpenSky use."""
    counts = daily_counts(cfg)
    start = cfg.city.get("quiet_build_start") or ""
    keep = int(cfg.city.get("sample_days", 7))
    days = [d for d in sorted(counts) if d >= start][-int(cfg.city.get("quiet_build_days", 14)):]
    lines = [f"# {cfg.city.get('brand', cfg.city_name)}: quiet-build sample", "",
             f"Quiet build: dry run, nothing posts. Day {len(days)} of {cfg.city.get('quiet_build_days', 14)}. "
             f"Videos are kept for the last {keep} days.", ""]
    tc = traffic_check(cfg)
    if tc:
        lines += [tc, ""]
    lines += ["| Post date | Flights from | " + f"{cfg.hub_name} moves | Airline dep / arr | Would post |", "|---|---|---:|---:|---|"]
    for d in reversed(days):
        r = counts[d]
        st = load_state(cfg, date.fromisoformat(d)) or {}
        recent = date.fromisoformat(d) >= now.date() - timedelta(days=keep - 1)
        posts = []
        slot_time = {sl.key: sl.time for sl in cfg.slots}
        for k, p in sorted((st.get("posts") or {}).items(), key=lambda kv: str(slot_time.get(kv[0], ""))):
            if not (p.get("results") or p.get("video_exists")):
                continue
            t = engine._ts(p.get("posted_at"))
            when = (t.strftime("%I:%M %p") if t else slot_time[k].strftime("%I:%M %p") if k in slot_time else "").lstrip("0")
            name = Path(p.get("video") or "").name
            link = f"[{p.get('title', k)}]({base}/videos/{cfg.city_id}/{d}/{name})" if recent and name and base \
                else p.get("title", k)
            posts.append(f"{when} {link}")
        flag = "" if r.get("complete", True) else " ⚠️ incomplete"
        lines.append(f"| {d} | {r.get('data_date', '')}{flag} | {r.get('hub_moves') or '–'} | "
                     f"{r.get('airline_departures', '–')} / {r.get('airline_arrivals', '–')} | {'<br>'.join(posts) or '–'} |")
    lines.append("")
    lines += opensky_table(cfg)
    return to_html(f"{cfg.city_name} sample", "\n".join(lines))


REVIEW = {"review_day": 30, "keep_pct": 25, "keep_views": 150, "viral_views": 3000, "facebook_daily": 1400}


def launch_review(cfg: Config, rs: list[dict], now: datetime) -> list[str]:
    """The day-30 review of a city's launch (launch_date in its profile):
    - keep a channel if its average views per post are at least keep_pct (25%) of the city's
      best channel, or at least keep_views (150);
    - success = TikTok views grew week over week (the last full week beat the one before)
      AND one post passed viral_views (3,000);
    - Facebook, if on, must average facebook_daily (1,400) views a day."""
    launch = cfg.city.get("launch_date")
    if not launch:
        return []
    launch = date.fromisoformat(str(launch))
    c = dict(REVIEW, **(cfg.city.get("review") or {}))
    day_n = (now.date() - launch).days + 1
    if day_n < 1:
        return []
    mine = [r for r in rs if r["day"] >= launch and r["views"] is not None]
    lines = [f"## {cfg.city_name} launch review", ""]
    final = day_n >= c["review_day"]
    lines.append(f"Day {day_n} of {c['review_day']}" + (": the review." if final else ": so far (decided on day "
                 f"{c['review_day']}, {(launch + timedelta(days=c['review_day'] - 1)):%b %-d})."))
    lines.append("")
    if not mine:
        return lines + ["No numbers from Buffer yet.", ""]
    avg = {p: statistics.mean(r["views"] for r in mine if r["platform"] == p) for p in {r["platform"] for r in mine}}
    best = max(avg.values())
    lines += ["| Channel | Posts | Avg views | vs best | Keep? |", "|---|---:|---:|---:|---|"]
    for p, a in sorted(avg.items(), key=lambda x: -x[1]):
        keep = a >= c["keep_pct"] / 100 * best or a >= c["keep_views"]
        n = sum(1 for r in mine if r["platform"] == p)
        lines.append(f"| {p.title()} | {n} | {a:,.0f} | {round(100 * a / best) if best else 0}% | "
                     f"{'keep' if keep else 'drop' if final else 'below the bar'} |")
    # TikTok week over week (weeks counted from launch)
    tt = [r for r in mine if r["platform"] == "tiktok"]
    weeks: dict[int, int] = {}
    for r in tt:
        weeks[(r["day"] - launch).days // 7] = weeks.get((r["day"] - launch).days // 7, 0) + r["views"]
    full = [weeks.get(w, 0) for w in range(max(0, (now.date() - launch).days // 7))]
    grew = len(full) >= 2 and full[-1] > full[-2]   # the last full week beat the week before
    top = max((r["views"] for r in mine), default=0)
    lines += ["", f"TikTok views by week: {', '.join(f'{v:,}' for v in full) or 'no full week yet'} "
              f"({'grew week over week' if grew else 'did not grow last week' if len(full) >= 2 else 'too early'}).",
              f"Best post: {top:,} views (the bar is {c['viral_views']:,})."]
    ok = grew and top >= c["viral_views"]
    fb = [r for r in mine if r["platform"] == "facebook"]
    if fb:
        per_day = sum(r["views"] for r in fb) / max(1, min(day_n, (now.date() - launch).days + 1))
        fb_ok = per_day >= c["facebook_daily"]
        ok = ok and fb_ok
        lines.append(f"Facebook: {per_day:,.0f} views a day (needs {c['facebook_daily']:,}): {'met' if fb_ok else 'not met'}.")
    lines += ["", f"**{'Success' if ok else 'Not there yet' if not final else 'Did not meet the bar'}**"
              + (" (final)." if final else " (so far)."), ""]
    return lines
