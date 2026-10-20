"""What's working: which posts, caption voices, days and posting times do best.

Fair comparisons, not raw totals:
- A post is only counted once it's 2 days old (views keep climbing for a while).
- Each post's views are divided by your typical views on that platform around the
  same time (median of the 4 weeks around it), so a growing account doesn't make
  later posts look better. 1.0 = typical, 1.3 = 30% above typical.
- Each post counts once: its score is the average over the platforms it went to.

Best time to post: SkyBird can only learn this if posts sometimes go out at other
times, so with time testing on (Settings), each post moves to a nearby hour
(1-2 hours earlier or later) about one day in three. Once an hour has enough
posts and beats the usual time clearly, the Engagement tab recommends it; with
"Switch automatically" on, SkyBird changes the post time itself and tells you.
"""
from __future__ import annotations

import json
import statistics
from datetime import date, datetime, time, timedelta

from .config import Config
from .settings import load_settings, save_settings
from .state import load_state, log

MATURE = timedelta(hours=48)
MIN_N = 3            # posts at an hour before it's compared
MIN_LIFT = 0.15      # recommend a new time if it's at least 15% better
AUTO_N, AUTO_LIFT = 4, 0.20   # stricter bar for switching on its own
TEST_EVERY = 3       # each post tests a new time about one day in 3
OFFSETS = (-1, 1, -2, 2)      # hours around the usual time
EARLIEST, GAP = time(7, 0), timedelta(minutes=60)
VOICES = {"friendly": "Friendly Neighbor", "deadpan": "Deadpan Tower", "question": "Question",
          "did_you_know": "Did you know", "guess": "Guess first", "local_tie_in": "Local tie-in"}
CTA_NAMES = {"follow": "\"Follow for Omaha's sky\"", "tag_friend": "\"Tag someone\"", "save": "\"Save this\"",
             "spotlight": "\"Which flight next?\"", "": "No call to action"}
REVIEW_ON = date(2026, 11, 7)   # ~3 weeks after the new caption styles + tagging started
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _num(v):
    return v if isinstance(v, (int, float)) else None


def hour_label(h: int) -> str:
    return f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}"


def time_label(t: time) -> str:
    return datetime.combine(date.min, t).strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")


# ---- data ------------------------------------------------------------------------

def rows(cfg: Config, now: datetime | None = None, days: int = 120) -> list[dict]:
    """One row per post per platform that has a view count, joined with how it was posted."""
    from .metrics import load_metrics
    now = now or datetime.now(cfg.tz)
    start = (now.date() - timedelta(days=days)).isoformat()
    states, out = {}, []
    for key, m in load_metrics(cfg).items():
        d, slot, plat = m.get("post_date"), m.get("slot"), m.get("platform")
        views = _num(m.get("views"))
        if not d or d < start or views is None:
            continue
        if d not in states:
            states[d] = load_state(cfg, date.fromisoformat(d)) or {}
        post = (states[d].get("posts") or {}).get(slot)
        if not post:
            continue
        at = (post.get("results") or {}).get(plat, {}).get("at") or post.get("posted_at")
        try:
            at = datetime.fromisoformat(at)
        except (TypeError, ValueError):
            continue
        eng = sum(_num(m.get(f)) or 0 for f in ("likes", "comments", "shares", "saves"))
        tr = post.get("tracking") or {}
        out.append({"date": d, "slot": slot, "title": post.get("title", slot), "platform": plat,
                    "views": views, "eng": eng / views if views else 0.0, "at": at, "hour": at.hour,
                    "weekday": at.weekday(), "voice": tr.get("caption_style") or post.get("choice", ""),
                    "cta": tr.get("cta", post.get("cta", "")) if tr or "cta" in post else None,
                    "mentions": [h.lower() for h in (tr.get("mentions") or {}).get(plat, [])],
                    "event": tr.get("event", ""),
                    "ss": (_num(m.get("shares")) or 0) + (_num(m.get("saves")) or 0),
                    "comments": _num(m.get("comments")) or 0, "follows": _num(m.get("follows")),
                    "test": bool(post.get("time_test")), "mature": now - at >= MATURE})
    return out


def score(rs: list[dict]) -> list[dict]:
    """Per post (date + slot): views vs the platform's typical views at the time."""
    mature = [r for r in rs if r["mature"]]
    by_plat = {}
    for r in mature:
        by_plat.setdefault(r["platform"], []).append(r)
    posts = {}
    for r in mature:
        near = [x["views"] for x in by_plat[r["platform"]] if abs((x["at"] - r["at"]).days) <= 14]
        base = statistics.median(near) if near else 0
        if base <= 0:
            continue
        p = posts.setdefault((r["date"], r["slot"]),
                             dict(r, scores=[], engs=[], views_sum=0, ss_sum=0, comments_sum=0, follows_sum=None, tags=set()))
        p["scores"].append(r["views"] / base)
        p["engs"].append(r["eng"])
        p["views_sum"] += r["views"]
        p["ss_sum"] += r["ss"]
        p["comments_sum"] += r["comments"]
        if r["follows"] is not None:
            p["follows_sum"] = (p["follows_sum"] or 0) + r["follows"]
        p["tags"].update(r["mentions"])
    out = []
    for p in posts.values():
        p["score"] = sum(p["scores"]) / len(p["scores"])
        p["eng"] = sum(p["engs"]) / len(p["engs"])
        out.append(p)
    return sorted(out, key=lambda p: (p["date"], p["slot"]))


def _group(posts, key):
    g = {}
    for p in posts:
        g.setdefault(key(p), []).append(p)
    out = {}
    for k, v in g.items():
        views = sum(x["views_sum"] for x in v)
        fol = [x["follows_sum"] for x in v if x.get("follows_sum") is not None]
        out[k] = {"n": len(v), "score": sum(x["score"] for x in v) / len(v),
                  "views": views / len(v), "eng": sum(x["eng"] for x in v) / len(v),
                  # the follower-growth signals from the caption handoff (section F)
                  "ss_per_1k": 1000 * sum(x.get("ss_sum", 0) for x in v) / views if views else 0,
                  "comments": sum(x.get("comments_sum", 0) for x in v) / len(v),
                  "follows": sum(fol) / len(fol) if fol else None}
    return out


def _by_mention(posts):
    """Posts with each @handle vs posts with none."""
    out = {}
    handles = sorted({h for p in posts for h in p.get("tags", ())})
    for h in handles:
        out[h] = [p for p in posts if h in p.get("tags", ())]
    out[""] = [p for p in posts if not p.get("tags")]
    flat = {}
    for k, ps in out.items():
        if ps:
            flat.update({k: _group(ps, lambda p: 0)[0]})
    return flat


# ---- best times --------------------------------------------------------------------

def best_times(cfg: Config, posts: list[dict]) -> list[dict]:
    out = []
    for slot in cfg.slots:
        mine = [p for p in posts if p["slot"] == slot.key]
        hours = _group(mine, lambda p: p["hour"])
        cur = hours.get(slot.time.hour)
        rec = {"key": slot.key, "title": slot.title, "current": time_label(slot.time),
               "current_hour": slot.time.hour, "n": len(mine), "best": None, "best_hour": None, "lift": 0,
               "hours": [{"hour": h, "label": hour_label(h), **v} for h, v in sorted(hours.items())]}
        ready = [h for h, v in hours.items() if v["n"] >= MIN_N]
        if not cur or cur["n"] < MIN_N or len(ready) < 2:
            rec["status"] = "learning"
            rec["note"] = (f"{len(mine)} posts measured so far. Needs at least {MIN_N} at the usual time "
                           f"and {MIN_N} at another hour to compare.")
        else:
            h = max(ready, key=lambda h: hours[h]["score"])
            lift = hours[h]["score"] / cur["score"] - 1 if cur["score"] else 0
            if h != slot.time.hour and lift >= MIN_LIFT:
                rec.update(status="switch", best=hour_label(h), best_hour=h, lift=round(lift * 100),
                           note=f"Posts at {hour_label(h)} did {round(lift * 100)}% better than at "
                                f"{rec['current']} ({hours[h]['n']} vs {cur['n']} posts).")
            else:
                rec.update(status="keep", note=f"{rec['current']} is still the best time measured.")
        out.append(rec)
    return out


def test_times(cfg: Config, post_date: date) -> dict[str, str]:
    """Which posts try a different time today: {slot_key: "HH:MM"}. Keeps the posts
    in order, an hour apart, between 7 AM and an hour before the last post time."""
    if not load_settings(cfg.data_dir).get("time_tests", True):
        return {}
    try:
        posts = score(rows(cfg))
    except Exception:
        posts = []
    slots = sorted(cfg.slots, key=lambda s: s.time)
    latest = (datetime.combine(post_date, cfg.last_post_time) - GAP).time()
    chosen, prev = {}, None
    for i, slot in enumerate(slots):
        base = datetime.combine(post_date, slot.time)
        t = base
        if (post_date.toordinal() + i) % TEST_EVERY == 0:
            nxt = datetime.combine(post_date, slots[i + 1].time) if i + 1 < len(slots) else None
            seen = {}
            for p in posts:
                if p["slot"] == slot.key:
                    seen[p["hour"]] = seen.get(p["hour"], 0) + 1
            options = []
            for off in OFFSETS:
                c = base + timedelta(hours=off)
                if c.time() < EARLIEST or c.time() > latest or c.date() != post_date:
                    continue
                if (prev and c - prev < GAP) or (nxt and nxt - c < GAP):
                    continue
                options.append((seen.get(c.hour, 0), abs(off), c))
            if options:
                t = min(options, key=lambda o: (o[0], o[1]))[2]  # the least-tested nearby hour
                chosen[slot.key] = t.strftime("%H:%M")
        prev = t
    return chosen


# ---- the page + the weekly note ----------------------------------------------------

def report(cfg: Config, now: datetime | None = None) -> dict:
    now = now or datetime.now(cfg.tz)
    rs = rows(cfg, now)
    posts = score(rs)
    s = load_settings(cfg.data_dir)
    titles = {sl.key: sl.title for sl in cfg.slots}
    by_slot = _group(posts, lambda p: p["slot"])
    by_voice = _group(posts, lambda p: p["voice"])
    by_day = _group(posts, lambda p: p["weekday"])
    tracked = [p for p in posts if p.get("cta") is not None]
    by_cta = _group(tracked, lambda p: p["cta"] or "")
    by_mention = _by_mention([p for p in posts if p["slot"] != "military"])
    by_event = _group(posts, lambda p: "Event days" if p.get("event") else "Normal days")
    times = best_times(cfg, posts)
    heads = []
    if len(posts) >= 4:
        def best(g, name, n=2):
            ok = {k: v for k, v in g.items() if v["n"] >= n}
            if len(ok) < 2:
                return
            k = max(ok, key=lambda k: ok[k]["score"])
            lo = min(ok, key=lambda k: ok[k]["score"])
            gap = round((ok[k]["score"] / ok[lo]["score"] - 1) * 100) if ok[lo]["score"] else 0
            if gap >= 10:
                heads.append(name(k, lo, gap))
        best(by_slot, lambda k, lo, g: f"{titles.get(k, k)} posts do best: {g}% more views than {titles.get(lo, lo)}.")
        best(by_voice, lambda k, lo, g: f"The {VOICES.get(k, k)} caption style beats {VOICES.get(lo, lo)} by {g}%.")
        best(by_day, lambda k, lo, g: f"{DAYS[k]} is your strongest day; {DAYS[lo]} is the weakest ({g}% gap).", n=2)
        ok = {k: v for k, v in by_voice.items() if v["n"] >= 2 and v["ss_per_1k"]}
        if len(ok) >= 2:
            k = max(ok, key=lambda k: ok[k]["ss_per_1k"])
            heads.append(f"Most shares + saves (the best sign of new followers): {VOICES.get(k, k)} captions, "
                         f"{ok[k]['ss_per_1k']:.1f} per 1,000 views.")
        for t in times:
            if t["status"] == "switch":
                heads.append(f"Try {t['title']} at {t['best']} instead of {t['current']}: {t['lift']}% more views so far.")
    return {
        "measured": len(posts), "waiting": len({(r["date"], r["slot"]) for r in rs if not r["mature"]}),
        "headlines": heads,
        "by_slot": [{"key": k, "label": titles.get(k, k), **v} for k, v in by_slot.items()],
        "by_voice": [{"key": k, "label": VOICES.get(k, k or "Custom"), **v} for k, v in by_voice.items()],
        "by_day": [{"key": k, "label": DAYS[k], **v} for k, v in sorted(by_day.items())],
        "tests": [{"group": "Caption style", "rows": [{"label": VOICES.get(k, k or "Custom"), **v} for k, v in by_voice.items()]},
                  {"group": "Call to action", "rows": [{"label": CTA_NAMES.get(k, k), **v} for k, v in by_cta.items()]},
                  {"group": "@mentions", "rows": [{"label": k or "No @mentions", **v} for k, v in by_mention.items()]},
                  {"group": "Event vs normal day", "rows": [{"label": k, **v} for k, v in by_event.items()]}],
        "times": times, "time_tests": s.get("time_tests", True), "auto_times": s.get("auto_best_times", False),
    }


def _mark(cfg: Config) -> dict:
    p = cfg.data_dir / "insights.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _save_mark(cfg: Config, m: dict) -> None:
    (cfg.data_dir / "insights.json").write_text(json.dumps(m, indent=1), encoding="utf-8")


def daily(cfg: Config, now: datetime | None = None) -> list[str]:
    """Once a day: switch post times if that's turned on, and send the Monday summary."""
    from . import notify
    now = now or datetime.now(cfg.tz)
    mark = _mark(cfg)
    if mark.get("day") == now.date().isoformat() or now.time() < time(8, 0):
        return []
    mark["day"] = now.date().isoformat()
    done = []
    rep = report(cfg, now)
    if load_settings(cfg.data_dir).get("auto_best_times"):
        for t in rep["times"]:
            if t["status"] != "switch":
                continue
            hrs = {h["hour"]: h for h in t["hours"]}
            if hrs[t["best_hour"]]["n"] < AUTO_N or hrs[t["current_hour"]]["n"] < AUTO_N or t["lift"] < AUTO_LIFT * 100:
                continue
            slot = next(s for s in cfg.slots if s.key == t["key"])
            new_t = time(t["best_hour"], slot.time.minute)
            others = [datetime.combine(now.date(), s.time) for s in cfg.slots if s.key != slot.key]
            mine = datetime.combine(now.date(), new_t)
            order = sorted(cfg.slots, key=lambda s: s.time).index(slot)
            new_order = sum(1 for o in others if o < mine)
            if new_order != order or any(abs(o - mine) < GAP for o in others):
                continue  # would cross or crowd another post; leave it as a recommendation
            new = new_t.strftime("%H:%M")
            save_settings(cfg.data_dir, {"times": {t["key"]: new}})
            msg = f"{t['title']} now posts at {t['best']} (was {t['current']}): {t['lift']}% more views there."
            log(cfg, "Best time: " + msg)
            notify.send(cfg, "SkyBird changed a post time", msg, path="/engagement")
            done.append(f"time:{t['key']}")
    if now.date() >= REVIEW_ON and not mark.get("review_sent"):
        mark["review_sent"] = now.date().isoformat()
        notify.send(cfg, "SkyBird: 3-week caption test check-in",
                    "Time to compare caption styles, calls to action and @tags. Open Engagement → "
                    "Caption & tagging tests: keep what wins, drop what doesn't.", path="/engagement")
        done.append("review")
    if now.weekday() == 0 and rep["measured"]:
        lines = rep["headlines"][:4] or ["Not enough posts measured yet to call a winner. Keep posting!"]
        notify.send(cfg, f"SkyBird weekly: {rep['measured']} posts measured", "\n".join(lines), path="/engagement")
        done.append("weekly")
    _save_mark(cfg, mark)
    return done
