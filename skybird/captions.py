"""Builds 3 caption options per post from the day's stats.json, plus the
hashtag / mention choices shown in the dashboard.

stats.json (written by the render step, every field optional):
{
  "data_date": "2026-09-24",
  "posts": {
    "commercial": {
      "departures": 81, "arrivals": 80,
      "top_destinations": [{"code": "DEN", "city": "Denver", "count": 12}, ...],
      "top_origins":      [{"code": "ORD", "city": "Chicago", "count": 10}, ...],
      "farthest": {"code": "SEA", "city": "Seattle", "miles": 1368},
      "busiest_hour": "7 AM",
      "top_airlines":  [{"name": "Southwest", "count": 31}, ...],
      "top_aircraft":  [{"type": "Boeing 737-800", "count": 22}, ...]
    },
    "private":  { same keys },
    "military": { same keys },
    "all":      { same keys + "airports": [{"code": "OMA", "name": "Eppley", "count": 161}, ...] }
  }
}
"""
from __future__ import annotations

import re
from datetime import date

from .config import Config

MAX_HASHTAGS = {"instagram": 25, "facebook": 10, "tiktok": 5}
MAX_CHARS = {"instagram": 2200, "facebook": 5000, "tiktok": 2200}

SLOT_NOUN = {
    "commercial": "airline flights",
    "private": "private jets",
    "military": "military aircraft",
    "all": "aircraft",
}


def _n(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _places(items, k=3) -> list[str]:
    out = []
    for it in (items or [])[:k]:
        city, code = it.get("city"), it.get("code")
        out.append(f"{city} ({code})" if city and code else (city or code or "?"))
    return out


def _join(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " & " + words[-1]


def city_hashtags(stats: dict, k: int = 3) -> list[str]:
    tags = []
    for it in (stats.get("top_destinations") or [])[:k]:
        city = it.get("city")
        if city:
            tags.append("#" + re.sub(r"[^A-Za-z0-9]", "", city.title()))
    return tags


# TikTok allows only 5 hashtags, so each post gets its best 5: the brand tag,
# the city, the subject of the video, then broad aviation tags people browse.
# On event days the event tag (e.g. #CWS) takes the place of a broad one.
# Override per post in config.toml under [hashtags.tiktok].
TIKTOK_TOP = {
    "commercial": ["#SkyBirdOmaha", "#Omaha", "#EppleyAirfield", "#avgeek", "#planespotting"],
    "private": ["#SkyBirdOmaha", "#Omaha", "#privatejet", "#avgeek", "#aviation"],
    "military": ["#SkyBirdOmaha", "#Omaha", "#OffuttAFB", "#USAF", "#militaryaviation"],
    "all": ["#SkyBirdOmaha", "#Omaha", "#avgeek", "#aviation", "#planespotting"],
}


def tiktok_priority(cfg: Config, slot_key: str) -> list[str]:
    return list(cfg.section("hashtags").get("tiktok", {}).get(slot_key) or TIKTOK_TOP.get(slot_key, TIKTOK_TOP["all"]))


def pick_tiktok(hashtags: list[dict], priority: list[str], k: int = 5) -> list[str]:
    """The best k of the checked hashtags for TikTok."""
    checked = [h for h in hashtags if h.get("checked")]
    lower = {h["tag"].lower(): h["tag"] for h in checked}
    # Tags you typed in yourself and the day's event tag go in right after the
    # brand + city tags, ahead of the broad ones.
    special = [h["tag"] for h in checked if h.get("source") == "custom"] + \
        [h["tag"] for h in checked if h.get("source") == "event"][:1]
    wanted = [lower[t.lower()] for t in priority if t.lower() in lower]
    wanted = wanted[:2] + special + wanted[2:]
    out, seen = [], set()
    for t in wanted + [h["tag"] for h in checked]:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:k]
AIRLINE_TAGS = {"Southwest": "#SouthwestAirlines", "United": "#UnitedAirlines", "American": "#AmericanAirlines",
                "Delta": "#DeltaAirLines", "Allegiant": "#Allegiant", "Frontier": "#FrontierAirlines",
                "SkyWest": "#SkyWest", "Sun Country": "#SunCountry", "Alaska": "#AlaskaAirlines",
                "FedEx": "#FedEx", "UPS": "#UPSAirlines", "Breeze": "#BreezeAirways", "Spirit": "#SpiritAirlines"}
FAMILY_TAGS = {"Citation": "#CessnaCitation", "Learjet": "#Learjet", "Challenger": "#Challenger",
               "Global": "#BombardierGlobal", "Gulfstream": "#Gulfstream", "Falcon": "#DassaultFalcon",
               "King Air": "#KingAir", "Phenom": "#EmbraerPhenom", "Praetor": "#EmbraerPraetor",
               "Hawker": "#Hawker", "Pilatus PC-12": "#PilatusPC12", "Pilatus PC-24": "#PilatusPC24",
               "HondaJet": "#HondaJet"}


def _first(items):
    return items[0] if items else {}


def build_options(cfg: Config, slot_key: str, stats: dict, data_date: date,
                  events: list[dict] | None = None) -> list[dict]:
    """Caption options: the brand voices (docs/SKYBIRD-BRAND-SPEC.md, section 9:
    Friendly Neighbor, Deadpan Tower, Question) plus the styles being tested for
    follower growth (Did you know, Guess first, Local tie-in on event days)."""
    from datetime import timedelta
    day = data_date.strftime("%A")
    post_day = data_date + timedelta(days=1)
    dep, arr = _n(stats.get("departures")), _n(stats.get("arrivals"))
    total = _n(stats.get("flights")) or dep + arr
    top = _first(stats.get("top_destinations"))
    top_city = top.get("city") or top.get("code")
    far = stats.get("farthest") or {}
    airline = _first(stats.get("top_airlines"))
    ftype = _first(stats.get("top_types"))
    field = max(stats.get("airports") or [{}], key=lambda a: _n(a.get("count")))
    ev = events[0] if events else None
    ev_hook = (ev.get("caption_hook") or "").strip() if ev else ""
    n = f"{total:,}" if total else "every"

    if slot_key == "military":  # counts only: no routes, callsigns, tails or times
        friendly = (f"Morning, Omaha. Offutt logged at least {n} military movements on {day}. "
                    "That's a floor: plenty of military flights don't broadcast their position.")
        deadpan = f"Offutt: {n}. The rest were apparently invisible."
        question = f"Offutt logged at least {n} military movements on {day}. Can you hear them from your house?"
    elif slot_key == "commercial":
        friendly = f"Morning, Omaha. Eppley moved {n} airline flights on {day}: {dep} out, {arr} in."
        if top_city:
            friendly += f" {top_city} was the favorite again with {top.get('count')} trips."
        if far.get("code"):
            friendly += f" The longest ran to {far['code']}, {_n(far.get('miles')):,} miles."
        deadpan = f"{n} airline flights at Eppley on {day}."
        deadpan += f" {top_city} got {top.get('count')} of them. We did not get a window seat." if top_city else " We did not get a window seat."
        question = "Which route do you fly most out of Eppley?"
        if top_city:
            question += f" {top_city} led {day} with {top.get('count')} flights."
    elif slot_key == "private":
        friendly = f"Morning, Omaha. {n} private jet takeoffs and landings across the metro fields on {day}."
        if top_city:
            friendly += f" Most popular city: {top_city}."
        if ftype.get("name"):
            friendly += f" Most common type: {ftype['name']}."
        deadpan = f"{n} private jet takeoffs and landings on {day}. None of them offered us a ride."
        question = "If you had a private jet for a day, where would you take it from Omaha?"
        if top_city:
            question += f" {day}'s favorite: {top_city}."
    else:
        friendly = f"Morning, Omaha. {n} flights across Eppley, Millard, Council Bluffs and Offutt on {day}."
        if field.get("code"):
            friendly += f" Busiest field: {field.get('name') or field['code']} with {_n(field.get('count'))}."
        deadpan = f"{n} takeoffs and landings across the metro on {day}. We counted every one. Twice."
        question = f"Which field is closest to you: Eppley, Millard, Council Bluffs or Offutt? {n} flights across the metro on {day}."
    if ev_hook:
        friendly += f" {ev_hook}"
    friendly += "\n\nSee you up there."
    deadpan += "\n\nRoger, roger."

    # Styles being tested for follower growth (caption handoff, section D)
    busiest_hour = stats.get("busiest_hour")
    if slot_key == "commercial":
        dyk = (f"Did you know? Eppley's busiest hour yesterday was {busiest_hour}. If you've ever wondered why "
               "security was packed, now you know. ✈️") if busiest_hour else \
              f"Did you know? Eppley handled {n} airline flights yesterday, about one every few minutes. ✈️"
        guess = "Guess before you watch: what was Eppley's top destination yesterday? 👇 Answer at the end of the video."
    elif slot_key == "private":
        dyk = f"Did you know? Private jets made {n} takeoffs and landings around the Omaha metro yesterday."
        if ftype.get("name"):
            dyk += f" The most common type was the {ftype['name']}."
        guess = "Guess: how many private jets flew into Omaha yesterday? Closest guess in the comments gets bragging rights."
    elif slot_key == "military":
        dyk = (f"Did you know? Offutt logged at least {n} military movements yesterday, and that's only "
               "the ones broadcasting their position.")
        guess = ""  # military stays counts-only and low-key: no guessing games
    else:
        dyk = f"Did you know? There were {n} takeoffs and landings across the Omaha metro's four airfields yesterday."
        if field.get("code"):
            dyk += f" Busiest field: {field.get('name') or field['code']}."
        guess = "Which Omaha-area airport do you think was busiest yesterday? Drop your guess 👇"
    local = ""
    if ev and slot_key != "military":
        noun = {"commercial": "airline flights at Eppley", "private": "private jet takeoffs and landings"}.get(slot_key, "takeoffs and landings across the metro")
        if "thanksgiving" in ev.get("name", "").lower():
            local = f"Thanksgiving travel check: Eppley moved {n} flights yesterday. Where are you headed? 🦃"
        elif ev_hook:
            local = f"{ev_hook} {n} {noun} yesterday."

    opts = [
        {"id": "friendly", "label": "Friendly", "text": friendly.strip()},
        {"id": "deadpan", "label": "Deadpan", "text": deadpan.strip()},
        {"id": "question", "label": "Question", "text": question.strip()},
        {"id": "did_you_know", "label": "Did you know", "text": dyk.strip()},
    ]
    if guess:
        opts.append({"id": "guess", "label": "Guess first", "text": guess})
    if local:
        opts.append({"id": "local_tie_in", "label": "Local tie-in", "text": local})

    # A call to action at the end, rotating day to day (section D4)
    cta = pick_cta(slot_key, post_day)
    if cta:
        for o in opts:
            o["text"] += "\n\n" + cta[1]

    # Which style is picked by default rotates, so every style gets tested.
    # Fridays keep the Deadpan Tower voice (brand rule); event days lead with the local tie-in.
    ids = [o["id"] for o in opts]
    if post_day.weekday() == 4:
        first = "deadpan"
    elif local:
        first = "local_tie_in"
    else:
        cycle = [i for i in ROTATION if i in ids]
        # offset by post, so the four posts on one day don't all use the same style
        from .config import SLOT_ORDER
        shift = SLOT_ORDER.index(slot_key) if slot_key in SLOT_ORDER else 0
        first = cycle[(post_day.toordinal() + shift) % len(cycle)]
    opts.sort(key=lambda o: o["id"] != first)
    for o in opts:
        o["cta"] = cta[0] if cta else ""
    return opts


ROTATION = ["friendly", "did_you_know", "guess", "question"]
CTAS = [  # (id, text, posts it fits)
    ("follow", "Follow for Omaha's sky, every morning. ✈️", {"commercial", "private", "military", "all"}),
    ("tag_friend", "Tag someone who flies out of Eppley way too much.", {"commercial", "all"}),
    ("save", "Save this for your next trip out of OMA.", {"commercial"}),
    ("spotlight", "Which flight should we spotlight tomorrow? Tell us below.", {"commercial", "private", "all"}),
]


def pick_cta(slot_key: str, post_day: date):
    fits = [c for c in CTAS if slot_key in c[2]]
    return fits[post_day.toordinal() % len(fits)] if fits else None


def brand_tags(slot_key: str, stats: dict) -> list[str]:
    """Data-driven tags from the spec: top airlines + cities (commercial), aircraft families (private)."""
    tags = []
    if slot_key == "commercial":
        tags += [AIRLINE_TAGS[a["name"]] for a in (stats.get("top_airlines") or [])[:3] if a.get("name") in AIRLINE_TAGS]
        tags += city_hashtags(stats)
    if slot_key == "private":
        tags += [FAMILY_TAGS[a["name"]] for a in (stats.get("top_types") or [])[:3] if a.get("name") in FAMILY_TAGS]
    return tags


def hashtag_choices(cfg: Config, slot_key: str, stats: dict,
                    events: list[dict] | None = None) -> list[dict]:
    """[{"tag","checked","source"}] de-duplicated, in display order."""
    h = cfg.section("hashtags")
    seen, out = set(), []

    def add(tag, checked, source):
        tag = tag.strip()
        if not tag:
            return
        if not tag.startswith("#"):
            tag = "#" + tag
        tag = re.sub(r"\s+", "", tag)
        if tag.lower() in seen:
            return
        seen.add(tag.lower())
        out.append({"tag": tag, "checked": checked, "source": source})

    for ev in events or []:
        for t in ev.get("hashtags", []):
            add(t, True, "event")
    for t in brand_tags(slot_key, stats):
        add(t, True, "post")
    for t in h.get(slot_key, {}).get("default", []):
        add(t, True, "post")
    for t in h.get("common", {}).get("default", []):
        add(t, True, "common")
    for t in city_hashtags(stats):
        add(t, False, "cities")
    for t in h.get(slot_key, {}).get("optional", []):
        add(t, False, "post")
    for t in h.get("common", {}).get("optional", []):
        add(t, False, "common")
    return out


# Accounts to @mention (caption handoff, sections A + B).
# confirmed = handle taken from the organization's own website. The others are
# only offered once you tick "I checked this handle" in Settings → Tag accounts.
ACCOUNTS = {
    "eppley": {"name": "Eppley Airfield", "instagram": "@OMAairport", "facebook": "@OMAairport", "confirmed": True},
    "wowt": {"name": "WOWT 6 News", "instagram": "@wowt6news", "facebook": "@wowt6news", "confirmed": True},
    "kmtv": {"name": "KMTV 3 News Now", "instagram": "@3newsnow", "facebook": "@3NewsNow", "confirmed": True},
    "visitomaha": {"name": "Visit Omaha", "instagram": "@visitomaha"},
    "ketv": {"name": "KETV NewsWatch 7", "instagram": "@ketv"},
    "owh": {"name": "Omaha World-Herald", "instagram": "@omahaworldherald"},
    "southwest": {"name": "Southwest", "airline": "Southwest", "instagram": "@southwestair"},
    "united": {"name": "United", "airline": "United", "instagram": "@united"},
    "delta": {"name": "Delta", "airline": "Delta", "instagram": "@delta"},
    "american": {"name": "American", "airline": "American", "instagram": "@americanair"},
    "allegiant": {"name": "Allegiant", "airline": "Allegiant", "instagram": "@allegiant"},
    "frontier": {"name": "Frontier", "airline": "Frontier", "instagram": "@flyfrontier"},
}
# Don't wear out our welcome: how often each account may be pre-checked, counted
# over the last 7 days of posts that actually went out (any post, any time you
# ticked it yourself too). Past the limit it's still offered, just unchecked.
NEWS = ("wowt", "kmtv", "ketv", "owh")   # only on big "who's in town" events (news_tag in events.toml)
WEEKLY_CAP = {"eppley": 7, "visitomaha": 3, "news": 2}   # airlines: no limit, tagged whenever they're relevant
VISIT_DAYS = (1, 3, 5)                   # Visit Omaha on the 6 PM post Tue / Thu / Sat
NEVER_TAG = ("offutt", "55thwing", "55wg", "55th_wing")   # location tag only, never an @
MAX_CHECKED = 4


def recent_tags(cfg: Config, day: date, days: int = 7) -> dict[str, int]:
    """How many posts tagged each handle (lowercase) in the `days` days before `day`."""
    from datetime import timedelta
    from .state import load_state
    counts: dict[str, int] = {}
    for i in range(1, days + 1):
        st = load_state(cfg, day - timedelta(days=i)) or {}
        for post in (st.get("posts") or {}).values():
            tags = {h.lower() for hs in ((post.get("tracking") or {}).get("mentions") or {}).values() for h in hs}
            for h in tags:
                counts[h] = counts.get(h, 0) + 1
    return counts


def account_handle(cfg: Config, key: str, platform: str) -> str:
    """The account's handle on this platform if it's confirmed (by us or by you), else ''."""
    from .settings import load_settings
    a = ACCOUNTS[key]
    yours = load_settings(cfg.data_dir).get("account_handles", {}).get(key, {})
    if yours.get(platform):
        return yours[platform]   # a handle you entered or confirmed in Settings
    return a.get(platform, "") if a.get("confirmed") else ""


def _blocked(handle: str) -> bool:
    h = handle.lower().lstrip("@")
    return any(n in h for n in NEVER_TAG)


def mention_choices(cfg: Config, platform: str, events: list[dict] | None = None, slot_key: str = "",
                    stats: dict | None = None, post_day: date | None = None) -> list[dict]:
    """@mention checkboxes for one post, pre-checked sparingly:
    - commercial: @OMAairport; the day's top 1-2 airlines (always, when they're in the data)
    - 6 PM all-airports: @OMAairport only on event days; Visit Omaha Tue/Thu/Sat
      (at most 3x a week); ONE local news outlet only on big "who's in town"
      events (news_tag in events.toml: Berkshire, CWS, a home opener...)
    - private: only an event's own account; military: never any @
    Never more than 4 checked; never Offutt or the 55th Wing."""
    if slot_key == "military":
        return []
    stats = stats or {}
    used = recent_tags(cfg, post_day) if post_day else {}
    during = [e for e in events or [] if e.get("phase", "during") == "during"]
    out, seen = [], set()

    def add(handle, checked, why="", cap=None):
        if not handle or handle.lower() in seen or _blocked(handle):
            return
        if checked and cap is not None and used.get(handle.lower(), 0) >= cap:
            checked, why = False, why + " (tagged enough this week)"
        seen.add(handle.lower())
        out.append({"handle": handle, "checked": bool(checked), "why": why})

    for ev in events or []:
        for m in (ev.get("mentions") or {}).get(platform, []):
            add(m, True, "event")
    if slot_key == "commercial":
        add(account_handle(cfg, "eppley", platform), True, "airport", WEEKLY_CAP["eppley"])
        for a in (stats.get("top_airlines") or [])[:2]:
            key = next((k for k, v in ACCOUNTS.items() if v.get("airline") == a.get("name")), None)
            if key:
                add(account_handle(cfg, key, platform), True, "airline")
    if slot_key == "all":
        add(account_handle(cfg, "eppley", platform), bool(events), "airport", WEEKLY_CAP["eppley"])
        if post_day:
            add(account_handle(cfg, "visitomaha", platform), post_day.weekday() in VISIT_DAYS, "community",
                WEEKLY_CAP["visitomaha"])
            if any(e.get("news_tag") for e in during):
                outlets = [k for k in NEWS if account_handle(cfg, k, platform)]
                if outlets:   # one outlet, taking turns, so no station gets every big event
                    pick = outlets[post_day.toordinal() % len(outlets)]
                    add(account_handle(cfg, pick, platform), True, "news", WEEKLY_CAP["news"])
    from .settings import load_settings
    extra = load_settings(cfg.data_dir).get("mentions", {}).get(platform, [])
    for m in list(cfg.section("mentions").get(platform, [])) + list(extra):
        add(m, False)
    n = 0
    for m in out:   # 2-4 @mentions per post at most
        if m["checked"]:
            n += 1
            m["checked"] = n <= MAX_CHECKED
    return out

def final_caption(text: str, hashtags: list[str], mentions: list[str], platform: str) -> str:
    """Assemble caption + mentions + hashtags within the platform's limits."""
    # Never send the old OpenSky credit line, even if it was saved into a caption earlier
    text = "\n".join(l for l in text.splitlines() if not re.match(r"\s*Data:\s*OpenSky", l, re.I)).strip()
    tags = hashtags[: MAX_HASHTAGS.get(platform, 25)]

    def build(tags):
        # caption -> @tags -> hashtags
        parts = [text.strip(), " ".join(mentions), " ".join(tags)]
        return "\n\n".join(p for p in parts if p)
    caption = build(tags)
    limit = MAX_CHARS.get(platform, 2200)
    while len(caption) > limit and tags:
        tags = tags[:-1]
        caption = build(tags)
    return caption[:limit]
