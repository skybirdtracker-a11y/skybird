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

from . import engine, notify
from .config import Config
from .state import load_state, locked, save_state

LOOKBACK_DAYS = 14
SETTLE_HOURS = 20          # newer posts don't have meaningful numbers yet
PAUSE = 0.4                # seconds between Buffer requests (keeps the API use gentle)
MIN_POSTS = 2              # a group needs this many posts to be ranked
STYLE_NAMES = {"friendly": "Friendly Neighbor", "deadpan": "Deadpan Tower", "question": "Question",
               "did_you_know": "Did you know", "guess": "Guess first", "local_tie_in": "Local tie-in"}
CTA_NAMES = {"follow": "Follow", "tag_friend": "Tag a friend", "save": "Save this", "spotlight": "Spotlight"}


def collect(cfg: Config, key: str, now: datetime, session=None) -> tuple[int, list[str]]:
    """Fetch Buffer's numbers for recent posts. Returns (posts updated, problems)."""
    from .publishers import buffer
    kw = {"session": session} if session else {}
    done, problems = 0, []
    for back in range(LOOKBACK_DAYS, -1, -1):
        day = now.date() - timedelta(days=back)
        st = load_state(cfg, day)
        if not st:
            continue
        got = {}
        for k, post in st["posts"].items():
            due = engine._ts(post.get("posted_at"))
            if not due or now < due + timedelta(hours=SETTLE_HOURS):
                continue
            for plat, r in (post.get("results") or {}).items():
                if not r.get("ok") or r.get("id") in (None, "", "dry-run"):
                    continue
                try:
                    m = buffer.post_metrics(key, r["id"], **kw)
                except Exception as e:
                    problems.append(f"{day} {post.get('title', k)} {plat}: {str(e)[:300]}")
                    if "rate limit" in str(e) or "rejected the API key" in str(e) or len(problems) >= 6:
                        return done, problems   # stop early instead of hammering Buffer
                    continue
                finally:
                    time.sleep(PAUSE if not session else 0)
                if m:
                    got[(k, plat)] = m
        if got:
            with locked(cfg):
                fresh = load_state(cfg, day)
                for (k, plat), m in got.items():
                    r = fresh["posts"][k]["results"][plat]
                    r["metrics"], r["metrics_at"] = m, now.isoformat(timespec="seconds")
                save_state(cfg, day, fresh)
            done += len(got)
    return done, problems


def rows(cfg: Config, now: datetime) -> list[dict]:
    """One row per post per platform that has numbers."""
    out = []
    for back in range(LOOKBACK_DAYS, -1, -1):
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
              "all": "All airports"}


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
    """Tiny Markdown -> HTML (headings, bullets, tables, bold) in SkyBird colors."""
    import re
    body, table = [], []

    def inline(s):
        return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(s))

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
    n, problems = collect(cfg, key, now, session) if key else (0, ["no Buffer API key"])
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
        link = pages_host.publish_report(rep["html"], now.date())
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
