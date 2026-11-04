"""Score SkyBird's posts from Buffer's numbers (used by the twice-weekly check-in).

Input: one or more JSON files saved from Buffer's list_posts (with metrics), either the
raw API answer ({"edges": [...]}) or the tool-result wrapper ([{"type": "text", "text": ...}]).

    python tools/score_posts.py page1.json page2.json [--now 2026-11-04T12:00Z]

Prints, per concept: days run, posts, median score (100 = a typical post on that
platform, last 3 weeks), views, likes, comments, shares; then platform totals, the top
posts and hour of day. Posts under 36 hours old are left out (numbers still coming in).
Facebook reports impressions instead of views.
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")
CONCEPTS = [  # caption text -> concept
    ("Where did the most flights out of Eppley", "#01 Guess the destination"),
    ("scoreboard", "#10 Metro scoreboard"),
    ("When is Omaha's sky busiest", "#06 Busiest hours"),
    ("farthest anyone flew", "#02 Farthest flight"),
    (r"day on .* started at", "#03 First up, last down"),
    ("Game weekend in Lincoln", "Lincoln game weekend"),
    ("week in the sky", "#07 Sunday wrap"),
    ("private jet you were most likely", "#30 Which jets fly Omaha"),
    ("over the weekend", "#14 Weekend arrivals"),
]
REGULAR = {9: "Regular 9 AM Eppley", 11: "Regular 11 AM private", 13: "Regular 1 PM Offutt", 18: "Regular 6 PM all airports"}


def load(paths):
    for p in paths:
        data = json.load(open(p, encoding="utf-8"))
        if isinstance(data, list):
            data = json.loads(data[0]["text"])
        for e in data.get("edges", []):
            yield e["node"]


def rows(nodes, now):
    out = []
    for n in nodes:
        m = {x["type"]: x["value"] for x in (n.get("metrics") or [])}
        due = datetime.fromisoformat(n["dueAt"].replace("Z", "+00:00"))
        t = due.astimezone(TZ)
        concept = next((name for pat, name in CONCEPTS if re.search(pat, n.get("text") or "")), None) \
            or REGULAR.get(t.hour, "Other")
        views = m.get("impressions") if n["channelService"] == "facebook" else m.get("views")
        out.append({"t": t, "plat": n["channelService"], "concept": concept, "views": views,
                    "likes": m.get("reactions", 0) or 0, "comments": m.get("comments", 0) or 0,
                    "shares": m.get("shares", 0) or 0, "watch": m.get("averageTimeWatched"),
                    "fresh": (now - due).total_seconds() < 36 * 3600})
    return out


def main(argv):
    now = datetime.now(timezone.utc)
    if "--now" in argv:
        i = argv.index("--now")
        now = datetime.fromisoformat(argv[i + 1].replace("Z", "+00:00"))
        argv = argv[:i] + argv[i + 2:]
    rs = [r for r in rows(load(argv), now) if not r["fresh"] and r["views"] is not None]
    med = {p: statistics.median([r["views"] for r in rs if r["plat"] == p] or [0]) for p in {r["plat"] for r in rs}}
    for r in rs:
        r["score"] = 100 * r["views"] / med[r["plat"]] if med[r["plat"]] else None
    print(f"{len(rs)} posts with numbers. Typical views per post: " + ", ".join(f"{p} {v:g}" for p, v in med.items()))
    groups = {}
    for r in rs:
        groups.setdefault(r["concept"], []).append(r)
    print(f"\n{'concept':30} days posts score  views likes comments shares")
    for c, g in sorted(groups.items(), key=lambda kv: -statistics.median([x["score"] for x in kv[1] if x["score"]] or [0])):
        sc = [x["score"] for x in g if x["score"]]
        print(f"{c:30} {len({x['t'].date() for x in g}):4} {len(g):5} {round(statistics.median(sc)) if sc else '-':>5} "
              f"{sum(x['views'] for x in g):6} {sum(x['likes'] for x in g):5} {sum(x['comments'] for x in g):8} "
              f"{sum(x['shares'] for x in g):6}")
    print("\nplatform    posts  views likes comments")
    for p in sorted(med):
        g = [r for r in rs if r["plat"] == p]
        print(f"{p:10} {len(g):6} {sum(r['views'] for r in g):6} {sum(r['likes'] for r in g):5} {sum(r['comments'] for r in g):8}")
    print("\ntop posts:")
    for r in sorted([r for r in rs if r["score"]], key=lambda r: -r["score"])[:8]:
        print(f"  {r['t']:%a %b %d %I %p} {r['plat']:9} {r['concept']:28} views={r['views']} likes={r['likes']} "
              f"comments={r['comments']} score={round(r['score'])}")
    print("\nhour of day: posts, median score")
    hours = {}
    for r in rs:
        if r["score"]:
            hours.setdefault(r["t"].hour, []).append(r["score"])
    for h in sorted(hours):
        print(f"  {h:2}:00  {len(hours[h]):3}  {round(statistics.median(hours[h]))}")


if __name__ == "__main__":
    main(sys.argv[1:])
