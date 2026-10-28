"""New post formats that take a regular post's slot on chosen days (the 4-week test).

Every day two of the regular posts are swapped for new concepts, rotating so each
concept gets about the same number of tries ([testing] in cloud/config.toml). Military
always stays regular (counts only). Fixed dates win over the rotation:

    [formats]
    "2026-11-01" = { commercial = "lincoln" }     # the Sunday after a Husker home game

The Sunday weekly wrap is its own post (slot "wrap", Sundays only, see [slots.wrap]).

Every format is cut from the same saved flight lists as the regular posts. Only the
Lincoln game-weekend post asks OpenSky for anything extra (about 4 requests).
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from . import flights as fl
from .config import Config
from .state import log

FORMATS = {   # key -> (calendar number, title)
    "guess": ("#01", "Guess the destination"),
    "farthest": ("#02", "Farthest flight"),
    "firstlast": ("#03", "First up, last down"),
    "hours": ("#06", "Busiest hours"),
    "scoreboard": ("#10", "Metro scoreboard"),
    "weekend": ("#14", "Weekend private-jet arrivals"),
    "types": ("#30", "Which jets fly Omaha"),
    "wrap": ("#07", "Sunday weekly wrap"),
    "lincoln": ("", "Game weekend jets · Lincoln"),
}
# The concepts each post can be swapped for (military stays regular: counts only).
POOLS = {
    "commercial": ["guess", "farthest", "firstlast"],
    "private": ["types", "weekend"],
    "all": ["scoreboard", "hours"],
}
ONLY_ON = {"weekend": "Tue"}   # needs Saturday + Sunday complete: posts on Tuesday
LINCOLN = "KLNK"


def title(fmt: str) -> str:
    return FORMATS[fmt][1]


def format_for(cfg: Config, post_date: date, slot_key: str) -> str | None:
    """The format replacing this slot's regular post on this date, if any."""
    if slot_key == "wrap":
        return "wrap"
    return day_plan(cfg, post_date).get(slot_key)


ALL_SLOTS = ["commercial", "private", "military", "all"]


def day_plan(cfg: Config, post_date: date) -> dict[str, str]:
    """{slot: concept} for the day's swapped posts."""
    return day_schedule(cfg, post_date)["formats"]


def day_schedule(cfg: Config, post_date: date) -> dict:
    """The day's plan: {"level": posts that day, "running": [slots that post], "formats": {slot: concept}}.

    Volume test ([testing] volume = true): each day posts 1, 2, 3 or 4 of the regular
    slots. The level follows a 4-week rotation, so every weekday gets each level once in
    4 weeks, and which slots run rotates too (so no time of day is favored). About half
    of the posts that run are new concepts (1 → 0 or 1, 2 → 1, 3 → 1 or 2, 4 → 2).
    Otherwise all 4 post and `per_day` of them are concepts.
    Fixed dates under [formats] always run and count as concepts. Each slot takes the
    concept that has run least so far (worked out day by day from the test's start)."""
    t = cfg.section("testing")
    try:
        start = date.fromisoformat(str(t.get("start", "")))
    except ValueError:
        start = None
    pools = {k: [f for f in v if f in FORMATS] for k, v in (t.get("pools") or POOLS).items()}
    on = bool(start) and t.get("enabled", True)
    volume = on and t.get("volume", False)
    used: Counter = Counter()

    def plan_for(d: date) -> dict:
        fixed = {k: v for k, v in (cfg.section("formats").get(d.isoformat()) or {}).items() if v in FORMATS}
        if not on or d < start:
            return {"level": len(ALL_SLOTS), "running": list(ALL_SLOTS), "formats": fixed}
        k = (d - start).days
        if volume:
            level = (d.weekday() + k // 7) % 4 + 1
            new = level // 2 + (level % 2 if k % 2 else 0)
        else:
            level, new = len(ALL_SLOTS), int(t.get("per_day", 2))
        order = [ALL_SLOTS[(k + i) % len(ALL_SLOTS)] for i in range(len(ALL_SLOTS))]
        running = list(fixed) + [x for x in order if x not in fixed]
        running = running[:max(level, len(fixed))]
        plan = dict(fixed)
        for slot in running:
            if len(plan) >= max(new, len(fixed)):
                break
            if slot in plan or not pools.get(slot):
                continue
            ok = [f for f in pools[slot] if ONLY_ON.get(f, d.strftime("%a")) == d.strftime("%a")]
            if ok:
                plan[slot] = min(ok, key=lambda f: (used[f], pools[slot].index(f)))
        running = [x for x in ALL_SLOTS if x in running]   # in the day's order
        return {"level": len(running), "running": running, "formats": plan}

    if on and start <= post_date:
        d = start
        while d < post_date:
            used.update(plan_for(d)["formats"].values())
            d += timedelta(days=1)
    return plan_for(post_date)


# ---- data ---------------------------------------------------------------------------

def guess_choices(post: dict, day: date) -> dict:
    """The top destination by departures (by city, so ORD + MDW count as Chicago) and
    two other cities from the day's top 6, in alphabetical order (so the answer's
    position changes from day to day)."""
    c = Counter(m["other"]["city"] for m in post["moves"] if m["dir"] == "dep" and m["other"])
    ranked = [city for city, _ in c.most_common(6)]
    if len(ranked) < 3:
        return {}
    others = ranked[1:]
    k = day.toordinal()
    pick = [others[k % len(others)], others[(k + 2) % len(others)]]
    if pick[0] == pick[1]:
        pick[1] = others[(k + 1) % len(others)]
    choices = sorted([ranked[0]] + pick)
    return {"answer": ranked[0], "answer_count": c[ranked[0]], "choices": choices,
            "letter": "ABC"[choices.index(ranked[0])]}


def farthest(post: dict) -> dict:
    """The three farthest cities flown to from Eppley (airline departures)."""
    best = {}
    for m in post["moves"]:
        if m["dir"] != "dep" or not m["other"] or not m["metro_place"]:
            continue
        mi = round(fl.miles(m["other"], m["metro_place"]))
        city = m["other"]["city"]
        if city not in best or mi > best[city]["miles"]:
            hrs = ((m["t1"] or 0) - (m["t0"] or 0)) / 3600 if m.get("t0") and m.get("t1") else None
            best[city] = {"city": city, "code": m["other"]["code"], "miles": mi,
                          "hours": round(hrs, 1) if hrs and hrs > 0.3 else None, "airline": m.get("airline")}
    top = sorted(best.values(), key=lambda r: -r["miles"])[:3]
    if not top:
        raise ValueError("no departures with a known destination")
    return {"list": top}


def first_last(cfg: Config, post: dict) -> dict:
    """The day's first airline departure and last airline arrival at Eppley."""
    deps = [m for m in post["moves"] if m["dir"] == "dep" and m.get("t") and m["other"]]
    arrs = [m for m in post["moves"] if m["dir"] == "arr" and m.get("t") and m["other"]]
    if not deps or not arrs:
        raise ValueError("no departures or arrivals")
    f, l = min(deps, key=lambda m: m["t"]), max(arrs, key=lambda m: m["t"])

    def row(m):
        return {"time": fl._clock(cfg, m["t"]), "city": m["other"]["city"], "code": m["other"]["code"],
                "airline": m.get("airline") or "An airline"}
    return {"first": row(f), "last": row(l), "between": len(post["moves"])}


def hours(cfg: Config, post: dict) -> dict:
    """Takeoffs and landings per hour across the metro (military left out: no times)."""
    c = Counter(datetime.fromtimestamp(m["t"], cfg.tz).hour for m in post["moves"]
                if m.get("t") and m["group"] != "military")
    start = cfg.day_start.hour
    order = [(start + i) % 24 for i in range(24)]
    rows = [{"hour": h, "label": f"{(h % 12) or 12}{'a' if h < 12 else 'p'}", "count": c.get(h, 0)} for h in order]
    if not any(r["count"] for r in rows):
        raise ValueError("no flights")
    peak = max(rows, key=lambda r: r["count"])
    day_rows = [r for r in rows if 6 <= r["hour"] <= 21]
    quiet = min(day_rows, key=lambda r: r["count"])

    def name(h):
        return f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}"
    return {"rows": rows, "peak": name(peak["hour"]), "peak_count": peak["count"],
            "quiet": name(quiet["hour"]), "quiet_count": quiet["count"],
            "total": sum(r["count"] for r in rows)}


def types(post: dict) -> dict:
    t = (post["stats"].get("top_types") or [])[:5]
    if len(t) < 2:
        raise ValueError("too few aircraft types known")
    return {"list": t}


def weekend(cfg: Config, flight_day: date, today: dict) -> dict:
    """Private jets that landed at the Omaha fields on Saturday + Sunday (`flight_day`
    is the Sunday). Saturday comes from the saved flight list."""
    sat = flight_day - timedelta(days=1)
    p = cfg.data_dir / "flights" / f"{sat.isoformat()}.json"
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))["flights"]
    except (OSError, ValueError, KeyError):
        raise ValueError(f"no saved flight list for {sat}")
    info = fl.aircraft_info(cfg, {f.get("icao24") for f in raw if f.get("icao24")}, None)
    sat_moves = fl.build_day(cfg, raw, info)["posts"]["private"]["moves"]
    moves = [m for m in sat_moves + today["posts"]["private"]["moves"] if m["dir"] == "arr"]
    if not moves:
        raise ValueError("no private arrivals")
    origins = Counter((m["other"]["code"], m["other"]["city"]) for m in moves if m["other"])
    n_sat = sum(1 for m in sat_moves if m["dir"] == "arr")
    s = {"arrivals": len(moves), "departures": 0, "flights": len(moves),
         "by_day": [{"label": "SAT", "count": n_sat}, {"label": "SUN", "count": len(moves) - n_sat}],
         "top_origins": [{"code": c, "city": city, "count": n} for (c, city), n in origins.most_common(5)],
         "cities_linked": len(origins),
         "weekend": f"{sat:%b} {sat.day}–" + (f"{flight_day.day}" if sat.month == flight_day.month else f"{flight_day:%b} {flight_day.day}")}
    s["top_destinations"] = s["top_origins"]
    far = None
    for m in moves:
        if m["other"] and m["metro_place"]:
            d = fl.miles(m["other"], m["metro_place"])
            if not far or d > far["miles"]:
                far = {"code": m["other"]["code"], "city": m["other"]["city"], "miles": round(d)}
    if far:
        s["farthest"] = far
    fams = Counter(m["family"] for m in moves if m["family"])
    s["top_types"] = [{"name": n, "count": c} for n, c in fams.most_common(3)]
    return {"moves": moves, "stats": s}


def record_fields(cfg: Config, flight_day: date, airports: list[dict]) -> None:
    """Remember each field's count for the day (for the scoreboard's week-over-week)."""
    path = cfg.data_dir / "daily_counts.json"
    try:
        hist = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        hist = {}
    hist.setdefault(flight_day.isoformat(), {})["fields"] = {a["code"]: a["count"] for a in airports}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(hist, indent=1), encoding="utf-8")


def scoreboard(cfg: Config, flight_day: date, all_stats: dict) -> dict:
    rows = [{"code": a["code"], "name": a["name"], "count": a["count"]} for a in all_stats.get("airports") or []]
    try:
        hist = json.loads((cfg.data_dir / "daily_counts.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        hist = {}
    before = (hist.get((flight_day - timedelta(days=7)).isoformat()) or {}).get("fields") or {}
    total = sum(r["count"] for r in rows)
    out = {"rows": rows, "total": total}
    prev = sum(before.values()) if before else 0
    if prev:
        out["vs_last_week"] = round(100 * (total - prev) / prev)
    return out


def week(cfg: Config, flight_day: date, today: dict) -> dict:
    """The 7 flight days ending `flight_day`, from the saved daily flight lists."""
    days, cities = [], Counter()
    for i in range(6, -1, -1):
        d = flight_day - timedelta(days=i)
        posts = today["posts"] if i == 0 else None
        if posts is None:
            p = cfg.data_dir / "flights" / f"{d.isoformat()}.json"
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))["flights"]
                info = fl.aircraft_info(cfg, {f.get("icao24") for f in raw if f.get("icao24")}, None)
                posts = fl.build_day(cfg, raw, info)["posts"]
            except (OSError, ValueError, KeyError):
                posts = None
        row = {"date": d.isoformat(), "label": d.strftime("%a").upper()}
        if posts:
            row.update(airline=posts["commercial"]["stats"]["flights"], private=posts["private"]["stats"]["flights"],
                       military=posts["military"]["stats"]["flights"], total=posts["all"]["stats"]["flights"])
            for m in posts["commercial"]["moves"]:
                if m["other"]:
                    cities[(m["other"]["code"], m["other"]["city"])] += 1
        days.append(row)
    have = [d for d in days if "total" in d]
    s = {"days": days, "days_missing": 7 - len(have),
         "range": f"{(flight_day - timedelta(days=6)):%b} {(flight_day - timedelta(days=6)).day} – "
                  f"{flight_day:%b} {flight_day.day}"}
    for k in ("airline", "private", "military", "total"):
        s["week_" + k] = sum(d.get(k, 0) for d in have)
    if have:
        b = max(have, key=lambda d: d["total"])
        s["busiest_day"] = datetime.fromisoformat(b["date"]).strftime("%A")
        s["busiest_day_count"] = b["total"]
        s["per_day"] = round(s["week_total"] / len(have))
    s["top_destinations"] = [{"code": c, "city": city, "count": n} for (c, city), n in cities.most_common(5)]
    # the keys the regular captions/hashtags read
    s.update(flights=s["week_total"], departures=s["week_airline"], arrivals=0)
    return s


def _utc_midnight(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _windows(begin: int, end: int) -> list[tuple[int, int]]:
    """Split a time range so each piece spans at most 2 UTC days (OpenSky's limit)."""
    out, b = [], begin
    while b <= end:
        day0 = b - b % 86400
        e = min(end, day0 + 2 * 86400 - 1)
        out.append((b, e))
        b = e + 1
    return out


def _game_days(post_date: date) -> tuple[date, date]:
    """(Thursday, Saturday) of the game weekend before this post (normally the Sunday after)."""
    sat = post_date - timedelta(days=(post_date.weekday() - 5) % 7 or 7)
    return sat - timedelta(days=2), sat


def _quiet_saturday(cfg: Config, sat: date) -> date:
    """The latest Saturday before `sat` with no Husker home game, for the normal weekend."""
    from .events import load_events
    games = [e for e in load_events(cfg) if "husker" in e.get("name", "").lower()
             and "lincoln" in (e.get("name", "") + e.get("note", "")).lower()]
    for weeks in range(1, 5):
        d = sat - timedelta(weeks=weeks)
        if not any(e["start"] <= d <= e["end"] for e in games):
            return d
    return sat - timedelta(weeks=1)


def _lincoln_arrivals(cfg: Config, thu: date, sat: date, session) -> list[dict]:
    begin = int(cfg.flight_day(thu)[0].timestamp())
    end = _utc_midnight(sat + timedelta(days=1)) - 1   # OpenSky has Saturday up to midnight UTC by Sunday morning
    raw, seen = [], set()
    for b, e in _windows(begin, end):
        for f in fl._get(cfg, session, "/flights/arrival", {"airport": LINCOLN, "begin": b, "end": e}):
            key = (f.get("icao24"), f.get("firstSeen"))
            if key not in seen:
                seen.add(key)
                raw.append(f)
    return raw


def _private_jets(cfg: Config, raw: list[dict], info: dict) -> list[dict]:
    airlines, mil, excl = fl._lists(cfg)
    out = []
    for f in raw:
        if fl.classify(f, airlines, mil, excl) != "private":
            continue
        if (info.get(f.get("icao24")) or {}).get("cls") in ("heli", "prop"):
            continue
        dep = f.get("estDepartureAirport")
        if dep == LINCOLN:
            continue   # local flights
        out.append(f)
    return out


def lincoln(cfg: Config, post_date: date, session=None, demo: bool = False) -> dict:
    """{"moves", "stats"} for the game-weekend post: private jets landing in Lincoln from
    Thursday 3:30 AM until OpenSky's Saturday cutoff, compared with a normal weekend.
    Saved, so a remake the same day asks OpenSky nothing."""
    thu, sat = _game_days(post_date)
    cache = cfg.data_dir / "lincoln" / f"{sat.isoformat()}.json"
    if demo:
        game, normal, quiet = _demo_lincoln(cfg, thu, 1), _demo_lincoln(cfg, thu, 0), sat - timedelta(weeks=1)
    elif cache.exists():
        saved = json.loads(cache.read_text(encoding="utf-8"))
        game, normal, quiet = saved["game"], saved["normal"], date.fromisoformat(saved["quiet"])
    else:
        if session is None:
            raise fl.DataError("The Lincoln game-weekend post needs OpenSky (no saved list yet)")
        quiet = _quiet_saturday(cfg, sat)
        game = _lincoln_arrivals(cfg, thu, sat, session)
        normal = _lincoln_arrivals(cfg, quiet - timedelta(days=2), quiet, session)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"game": game, "normal": normal, "quiet": quiet.isoformat()}), encoding="utf-8")
    info = fl.aircraft_info(cfg, {f.get("icao24") for f in game + normal if f.get("icao24")},
                            None if demo else session)
    jets, usual = _private_jets(cfg, game, info), _private_jets(cfg, normal, info)
    hub = fl.place(LINCOLN)
    moves = []
    for f in jets:
        meta = info.get(f.get("icao24")) or {}
        moves.append({"dir": "arr", "metro": LINCOLN, "other": fl.place(f.get("estDepartureAirport")),
                      "metro_place": hub, "t": f.get("lastSeen"), "t0": f.get("firstSeen"), "t1": f.get("lastSeen"),
                      "group": "bizjet", "kind": "private", "airline": None, "family": meta.get("family"), "callsign": ""})
    by_day = Counter(datetime.fromtimestamp(m["t"], cfg.tz).strftime("%a").upper() for m in moves if m.get("t"))
    origins = Counter((m["other"]["code"], m["other"]["city"]) for m in moves if m["other"])
    s = {"arrivals": len(moves), "departures": 0, "flights": len(moves), "normal": len(usual),
         "normal_weekend": f"{quiet - timedelta(days=2):%b} {(quiet - timedelta(days=2)).day}–{quiet.day}",
         "by_day": [{"label": d, "count": by_day.get(d, 0)} for d in ("THU", "FRI", "SAT")],
         "top_origins": [{"code": c, "city": city, "count": n} for (c, city), n in origins.most_common(5)],
         "cities_linked": len(origins), "cutoff": "7 PM" if sat < date(2026, 11, 1) else "6 PM",
         "game_day": sat.isoformat()}
    s["top_destinations"] = s["top_origins"]
    if usual:
        s["change_pct"] = round(100 * (len(moves) - len(usual)) / len(usual))
    far = None
    for m in moves:
        if m["other"]:
            d = fl.miles(m["other"], hub)
            if not far or d > far["miles"]:
                far = {"code": m["other"]["code"], "city": m["other"]["city"], "miles": round(d)}
    if far:
        s["farthest"] = far
    fams = Counter(m["family"] for m in moves if m["family"])
    s["top_types"] = [{"name": n, "count": c} for n, c in fams.most_common(3)]
    log(cfg, f"Lincoln game weekend: {len(moves)} private jets in (normal weekend {len(usual)})")
    return {"moves": moves, "stats": s}


def _demo_lincoln(cfg: Config, thu: date, game: int) -> list[dict]:
    import random
    rnd = random.Random(11 + game)
    origins = ["KCMH", "KSEA", "KDAL", "KAPA", "KTEB", "KMDW", "KSDL", "KHOU", "KMSP", "KMKC", "KOMA", "KDSM", "KVNY"]
    out = []
    for i in range(46 if game else 21):
        dep = rnd.choice(origins)
        t1 = cfg.flight_day(thu)[0].timestamp() + rnd.random() * (2 * 86400 + 12 * 3600)
        d = (fl.miles(fl.place(dep), fl.place(LINCOLN)) / 430 + 0.4) * 3600
        out.append({"callsign": f"N{rnd.randint(100, 999)}{rnd.choice('ABCDEFGH')}", "icao24": "a%05x" % rnd.randint(0, 0xFFFFF),
                    "estDepartureAirport": dep, "estArrivalAirport": LINCOLN, "firstSeen": int(t1 - d), "lastSeen": int(t1)})
    return out


# ---- captions, hashtags, @tags -------------------------------------------------------

FORMAT_TAGS = {
    "guess": ["#EppleyAirfield", "#OMA", "#travel", "#quiz"],
    "farthest": ["#EppleyAirfield", "#OMA", "#travel"],
    "firstlast": ["#EppleyAirfield", "#OMA", "#earlybird", "#redeye"],
    "hours": ["#EppleyAirfield", "#OmahaMetro", "#traveltips"],
    "types": ["#privatejet", "#bizjet", "#businessaviation"],
    "weekend": ["#privatejet", "#bizjet", "#businessaviation", "#weekend"],
    "scoreboard": ["#EppleyAirfield", "#Millard", "#CouncilBluffs", "#OffuttAFB", "#OmahaMetro"],
    "wrap": ["#EppleyAirfield", "#OmahaMetro", "#weekinreview"],
    "lincoln": ["#Huskers", "#GBR", "#Lincoln", "#LincolnNE", "#MemorialStadium", "#privatejet", "#bizjet"],
}
TIKTOK = {
    "guess": ["#SkyBirdOmaha", "#Omaha", "#EppleyAirfield", "#avgeek", "#quiz"],
    "farthest": ["#SkyBirdOmaha", "#Omaha", "#EppleyAirfield", "#avgeek", "#travel"],
    "firstlast": ["#SkyBirdOmaha", "#Omaha", "#EppleyAirfield", "#avgeek", "#planespotting"],
    "hours": ["#SkyBirdOmaha", "#Omaha", "#avgeek", "#aviation", "#traveltips"],
    "types": ["#SkyBirdOmaha", "#Omaha", "#privatejet", "#avgeek", "#aviation"],
    "weekend": ["#SkyBirdOmaha", "#Omaha", "#privatejet", "#avgeek", "#aviation"],
    "scoreboard": ["#SkyBirdOmaha", "#Omaha", "#avgeek", "#aviation", "#planespotting"],
    "wrap": ["#SkyBirdOmaha", "#Omaha", "#avgeek", "#aviation", "#planespotting"],
    "lincoln": ["#SkyBirdOmaha", "#Lincoln", "#Huskers", "#privatejet", "#avgeek"],
}
CTA = {   # (id, text)
    "guess": ("tag_friend", "Tag someone who flies out of Eppley way too much."),
    "farthest": ("save", "Save this for your next trip out of OMA."),
    "firstlast": ("tag_friend", "Tag someone who flies out of Eppley way too much."),
    "hours": ("save", "Save this for your next trip out of OMA."),
    "types": ("follow", "Follow for Omaha's sky, every morning. ✈️"),
    "weekend": ("spotlight", "Which flight should we spotlight tomorrow? Tell us below."),
    "scoreboard": ("follow", "Follow for Omaha's sky, every morning. ✈️"),
    "wrap": ("follow", "Follow for Omaha's sky, every morning. ✈️"),
    "lincoln": ("follow", "Follow for Omaha's sky, every morning. ✈️"),
}


def caption_options(fmt: str, stats: dict, data_date: date, post_date: date) -> list[dict]:
    """Caption options for a format (first = the default)."""
    day = data_date.strftime("%A")
    s = stats
    opts = []
    if fmt == "guess":
        g = s.get("guess") or {}
        ch = g.get("choices") or ["?", "?", "?"]
        opts.append(("guess", "Guess first",
                     f"Guess before you watch 👇 Where did the most flights out of Eppley go on {day}?\n"
                     f"A) {ch[0]}  B) {ch[1]}  C) {ch[2]}\nAnswer at the end. No peeking at the comments."))
    elif fmt == "farthest":
        top = (s.get("farthest_list") or {}).get("list") or [{}]
        f = top[0]
        hrs = f" (about {f['hours']:g} hours in the air)" if f.get("hours") else ""
        opts.append(("friendly", "Friendly",
                     f"The farthest anyone flew from Eppley on {day}: {f.get('city', '?')}, {f.get('miles', 0):,} miles away{hrs}. See you up there."))
        opts.append(("question", "Question",
                     f"Farthest trip out of Eppley on {day}: {f.get('city', '?')}. What's the farthest you've flown from OMA?"))
    elif fmt == "firstlast":
        fl_ = s.get("first_last") or {}
        a, b = fl_.get("first") or {}, fl_.get("last") or {}
        opts.append(("friendly", "Friendly",
                     f"Eppley's day on {day} started at {a.get('time')} with {a.get('airline')} to {a.get('city')} and ended "
                     f"at {b.get('time')} when {b.get('airline')} landed from {b.get('city')}. "
                     f"{fl_.get('between', 0)} airline flights in between. See you up there."))
        opts.append(("question", "Question",
                     f"Ever been on Eppley's first flight out? On {day} it left at {a.get('time')} for {a.get('city')}."))
    elif fmt == "hours":
        h = s.get("hours") or {}
        opts.append(("did_you_know", "Did you know",
                     f"When is Omaha's sky busiest? On {day} the peak was {h.get('peak')}, with {h.get('peak_count')} "
                     f"takeoffs and landings across the metro. Quietest daytime hour: {h.get('quiet')}."))
        opts.append(("question", "Question", f"Busiest hour over Omaha on {day}: {h.get('peak')}. Were you up there?"))
    elif fmt == "types":
        t = (s.get("types") or {}).get("list") or [{}, {}]
        opts.append(("did_you_know", "Did you know",
                     f"Did you know? The private jet you were most likely to see over Omaha on {day} was a "
                     f"{t[0].get('name')}: {t[0].get('count')} takeoffs and landings. Runner-up: {t[1].get('name')}."))
        opts.append(("guess", "Guess first",
                     f"Guess before you watch: which private jet flew Omaha the most on {day}? 👇 Answer at the end."))
    elif fmt == "weekend":
        top = (s.get("top_origins") or [{}])[0]
        far = (s.get("farthest") or {}).get("city")
        opts.append(("friendly", "Friendly",
                     f"{s.get('arrivals', 0)} private jets flew into Omaha over the weekend. Most came from {top.get('city')} "
                     f"({top.get('count')})." + (f" Farthest trip in: {far}." if far else "") + " See you up there."))
        opts.append(("deadpan", "Deadpan", f"{s.get('arrivals', 0)} private jets came to Omaha this weekend. Still no invitation. Roger, roger."))
    elif fmt == "scoreboard":
        sb = {r["name"]: r["count"] for r in (s.get("scoreboard") or {}).get("rows", [])}
        line = (f"Eppley {sb.get('Eppley', 0)}, Millard {sb.get('Millard', 0)}, "
                f"Council Bluffs {sb.get('Council Bluffs', 0)}, Offutt at least {sb.get('Offutt', 0)}")
        dead = ("deadpan", "Deadpan", f"{day}'s scoreboard: {line}. Roger, roger.")
        ques = ("question", "Question", f"Which field is closest to you? {day}'s scoreboard: {line}.")
        opts += [dead, ques] if post_date.weekday() == 4 else [ques, dead]
    elif fmt == "wrap":
        top = (s.get("top_destinations") or [{}])[0].get("city")
        opts.append(("friendly", "Friendly",
                     f"Omaha's week in the sky ({s.get('range', '')}): {s.get('week_airline', 0):,} airline flights at "
                     f"Eppley, {s.get('week_private', 0):,} private jet takeoffs and landings and at least "
                     f"{s.get('week_military', 0):,} at Offutt. Busiest day: {s.get('busiest_day', '?')}."
                     + (f" Top city: {top}." if top else "") + " See you up there."))
        opts.append(("question", "Question",
                     f"{s.get('busiest_day', 'One day')} was Omaha's busiest day in the sky this week, with "
                     f"{s.get('busiest_day_count', 0):,} takeoffs and landings. Did you fly?"))
    elif fmt == "lincoln":
        n, normal = s.get("arrivals", 0), s.get("normal", 0)
        top = (s.get("top_origins") or [{}])[0].get("city")
        far = (s.get("farthest") or {}).get("city")
        vs = f", vs {normal} on a normal weekend" if normal else ""
        opts.append(("local_tie_in", "Local tie-in",
                     f"Game weekend in Lincoln 🏈 {n} private jets flew into Lincoln from Thursday through game day{vs}."
                     + (f" Most came from {top}." if top else "") + (f" Farthest trip in: {far}." if far else "") + " GBR."))
        opts.append(("deadpan", "Deadpan", f"{n} private jets landed in Lincoln for the game. Ours is still in the shop. Roger, roger."))
        opts.append(("guess", "Guess first", "Guess before you watch: how many private jets flew into Lincoln for the game? 👇 Answer at the end."))
    cta = CTA[fmt]
    return [{"id": i, "label": label, "text": text + "\n\n" + cta[1], "cta": cta[0]} for i, label, text in opts]


def hashtag_choices(cfg: Config, fmt: str, base_slot: str, stats: dict, events: list[dict]) -> list[dict]:
    from . import captions
    out = captions.hashtag_choices(cfg, base_slot, stats, events)
    if fmt == "lincoln":   # not an Omaha-field post: drop the Omaha airfield tags
        out = [h for h in out if h["tag"].lower() not in ("#eppleyairfield", "#councilbluffs", "#oma", "#omahaairport",
                                                          "#millard", "#offuttafb", "#travel")]
    have = {h["tag"].lower() for h in out}
    extra = [{"tag": t, "checked": True, "source": "post"} for t in FORMAT_TAGS[fmt] if t.lower() not in have]
    return extra + out


def base_slot(fmt: str, slot_key: str) -> str:
    """Which regular post's tag rules a format follows."""
    return {"lincoln": "private", "wrap": "all", "weekend": "private", "types": "private"}.get(fmt, slot_key)
