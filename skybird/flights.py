"""Flight data for the daily videos.

Source: OpenSky Network's flights-by-airport API (free account). OpenSky builds
these lists in a nightly batch, so the 6 AM run always renders *yesterday*.

    python -m skybird opensky-setup      # saves your OpenSky API client id/secret
    python -m skybird render --date 2026-09-29

Classification is by callsign / transponder address (no registry lookups):
  military   US military ICAO block AE0000-AFFFFF, or a military callsign prefix
  commercial airline callsign (3-letter ICAO prefix + number) from AIRLINES
  excluded   air-ambulance callsigns (EXCLUDE_PREFIXES)
  private    everything else (N-numbers, NetJets/Flexjet/etc.)
Edit the lists under [classify] in config.toml to tune it.
"""
from __future__ import annotations

import json
import math
import random
import re
import time
from collections import Counter
from datetime import date, datetime, timedelta

import requests

from .config import Config
from .state import log

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
API = "https://opensky-network.org/api"

METRO = {  # ICAO -> short name
    "KOMA": "Eppley",
    "KMLE": "Millard",
    "KOFF": "Offutt",
    "KCBF": "Council Bluffs",
}
HUB = "KOMA"

AIRLINES = {
    "AAL": "American", "UAL": "United", "DAL": "Delta", "SWA": "Southwest", "SKW": "SkyWest",
    "ENY": "Envoy", "RPA": "Republic", "EDV": "Endeavor", "JIA": "PSA", "ASH": "Mesa",
    "FFT": "Frontier", "AAY": "Allegiant", "SCX": "Sun Country", "JBU": "JetBlue", "ASA": "Alaska",
    "QXE": "Horizon", "GJS": "GoJet", "AWI": "Air Wisconsin", "PDT": "Piedmont", "CPZ": "Compass",
    "NKS": "Spirit", "BRZ": "Breeze", "VXP": "Avelo", "FDX": "FedEx", "UPS": "UPS", "GTI": "Atlas",
    "ABX": "ABX Air", "ATN": "ATI", "CKS": "Kalitta", "MTN": "Mountain Air Cargo", "WSN": "Wiggins",
    "AMF": "Ameriflight", "SLQ": "Sky Lease", "WAL": "Western Global", "NCB": "North Cariboo",
    "CFS": "Empire", "BVN": "Baron", "KFS": "Kalitta Charters",
}
MILITARY_PREFIXES = (
    "RCH", "REACH", "SPAR", "SAM", "EXEC", "PAT", "CNV", "VV", "TOPCT", "COBRA", "SNOOP", "IRON",
    "OFFUT", "RRR", "NAVY", "ARMY", "AF", "KING", "HOBO", "BOLT", "TEAL", "GOLD", "JAKE", "SHARK",
    "ZEUS", "DUKE", "ROCKY", "HAWK", "EVAC", "CASA", "GRIM", "RAID",
)
EXCLUDE_PREFIXES = ("LN", "LIFE", "MEDEVAC", "EVAC", "AIRMED", "AMC", "LG", "AIRLIFE", "CARE", "MERCY", "FLIGHTCARE")
MIL_NAMES = {"RCH": "Reach (C-17/C-5)", "REACH": "Reach (C-17/C-5)", "SNOOP": "RC-135", "COBRA": "RC-135",
             "TOPCT": "E-4B", "SAM": "Special Air Mission", "SPAR": "SPAR (VIP)", "PAT": "Army (PAT)", "CNV": "Navy"}
PRIVATE_OPERATORS = {"EJA": "NetJets", "LXJ": "Flexjet", "GAJ": "Wheels Up", "VJT": "VistaJet",
                     "XOJ": "XOJET", "JTL": "Jet Linx", "TWY": "Solairus", "EJM": "Executive Jet Mgmt"}


class DataError(Exception):
    pass


# ---- OpenSky ----------------------------------------------------------------------

def _token(cfg: Config, s) -> str | None:
    sec = cfg.secrets
    cid, csec = sec.get("opensky_client_id"), sec.get("opensky_client_secret")
    if not (cid and csec):
        return None
    if sec.get("_opensky_token") and sec.get("_opensky_token_exp", 0) > time.time() + 60:
        return sec["_opensky_token"]
    r = _call(s.post, TOKEN_URL, "the login server",
              data={"grant_type": "client_credentials", "client_id": cid, "client_secret": csec})
    if r.status_code != 200:
        raise DataError(f"OpenSky rejected the API client (HTTP {r.status_code}). Check the client id/secret "
                        "(opensky-network.org > My OpenSky > API client).")
    body = r.json()
    sec["_opensky_token"], sec["_opensky_token_exp"] = body["access_token"], time.time() + int(body.get("expires_in", 1800))
    return sec["_opensky_token"]


NET_WAITS = (15, 45, 90)  # seconds between retries when OpenSky doesn't answer


def _call(method, url, what: str, **kw):
    """One HTTP call with patient retries for timeouts / dropped connections."""
    last = None
    for attempt in range(len(NET_WAITS) + 1):
        try:
            return method(url, timeout=(20, 120), **kw)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last = e
            if attempt < len(NET_WAITS):
                time.sleep(NET_WAITS[attempt])
    raise DataError(f"Couldn't reach OpenSky ({what} didn't answer after {len(NET_WAITS) + 1} tries). "
                    f"This is usually on OpenSky's side and clears up within the hour. [{type(last).__name__}]")


def _get(cfg: Config, s, path: str, params: dict) -> list:
    tok = _token(cfg, s)
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    for attempt in range(3):
        r = _call(s.get, f"{API}{path}", "the flight-data server", params=params, headers=headers)
        if r.status_code == 404:
            return []  # OpenSky answers 404 when there are no flights
        if r.status_code == 429:
            wait = r.headers.get("X-Rate-Limit-Retry-After-Seconds", "")
            when = f" It resets in about {int(wait) // 3600}h {int(wait) % 3600 // 60}m." if wait.isdigit() else ""
            raise DataError("OpenSky's daily request limit is used up." + when + " Try again then.")
        if r.status_code in (401, 403):
            raise DataError("OpenSky needs a (free) account for flight lists. Run: python -m skybird opensky-setup")
        if r.status_code >= 500 and attempt < 2:
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise DataError(f"OpenSky error HTTP {r.status_code}: {r.text[:200]}")
        return r.json() or []
    return []


def fetch_opensky(cfg: Config, day: date, session=None) -> list[dict]:
    """All departures + arrivals at the metro airports for one flight day
    (3:30 AM to 3:29 AM Central by default; see flight_day_start)."""
    s = session or requests.Session()
    start, stop = cfg.flight_day(day)
    begin, end = int(start.timestamp()), int(stop.timestamp()) - 1
    seen, flights = set(), []
    # One request per airport and direction. (Asking in 6-hour pieces used 4x more of
    # OpenSky's daily allowance and didn't bring back the missing arrivals.)
    for icao in METRO:
        for kind in ("departure", "arrival"):
            got = _get(cfg, s, f"/flights/{kind}", {"airport": icao, "begin": begin, "end": end})
            for f in got:
                key = (f.get("icao24"), f.get("firstSeen"), f.get("lastSeen"))
                if key in seen:
                    continue
                seen.add(key)
                flights.append(f)
    if cfg.secrets.get("opensky_client_id"):
        try:
            cfg.save_secrets()  # keep the cached token
        except OSError:
            pass
    return flights


# ---- classification ---------------------------------------------------------------

def _lists(cfg: Config):
    c = cfg.section("classify")
    airlines = dict(AIRLINES, **{k.upper(): v for k, v in (c.get("airlines") or {}).items()})
    mil = tuple(MILITARY_PREFIXES) + tuple(p.upper() for p in c.get("military_prefixes", []))
    excl = tuple(EXCLUDE_PREFIXES) + tuple(p.upper() for p in c.get("exclude_prefixes", []))
    return airlines, mil, excl


def classify(f: dict, airlines: dict, mil: tuple, excl: tuple) -> str:
    cs = (f.get("callsign") or "").strip().upper()
    hexid = (f.get("icao24") or "").lower()
    if cs and any(cs.startswith(p) for p in excl) and not cs.startswith("LNK"):
        return "excluded"
    if re.fullmatch(r"a[e-f][0-9a-f]{4}", hexid) or (cs and any(cs.startswith(p) for p in mil) and not re.match(r"N\d", cs)):
        return "military"
    if "KOFF" in (f.get("estDepartureAirport"), f.get("estArrivalAirport")) and not cs[:3] in airlines:
        return "military"
    m = re.match(r"([A-Z]{3})\d", cs)
    if m and m.group(1) in airlines:
        return "commercial"
    return "private"


# ---- airports ---------------------------------------------------------------------

_AIRPORTS = None


def airports() -> dict:
    global _AIRPORTS
    if _AIRPORTS is None:
        import airportsdata
        _AIRPORTS = airportsdata.load("ICAO")
    return _AIRPORTS


def place(icao: str | None) -> dict | None:
    if not icao:
        return None
    a = airports().get(icao)
    if not a:
        return None
    code = a.get("iata") or a.get("lid") or icao
    city = (a.get("city") or a.get("name") or code).split("/")[0].split(",")[0].strip()
    if len(city) > 18:
        city = city[:17].rstrip() + "…"
    return {"icao": icao, "code": code, "city": city, "lat": a["lat"], "lon": a["lon"],
            "name": a.get("name")}


def miles(a: dict, b: dict) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


# ---- aircraft types (OpenSky metadata, cached) ----------------------------------------

FAMILIES = [("CITATION", "Citation"), ("LEARJET", "Learjet"), ("CHALLENGER", "Challenger"), ("GLOBAL", "Global"),
            ("GULFSTREAM", "Gulfstream"), ("G-IV", "Gulfstream"), ("G-V", "Gulfstream"), ("FALCON", "Falcon"),
            ("KING AIR", "King Air"), ("PHENOM", "Phenom"), ("PRAETOR", "Praetor"), ("LEGACY", "Legacy"),
            ("HAWKER", "Hawker"), ("PC-12", "Pilatus PC-12"), ("PC-24", "Pilatus PC-24"), ("HONDAJET", "HondaJet"),
            ("HA-420", "HondaJet"), ("PREMIER", "Premier"), ("BEECHJET", "Beechjet"), ("SOVEREIGN", "Citation"),
            ("LATITUDE", "Citation"), ("LONGITUDE", "Citation"), ("VISION", "Cirrus Vision Jet"), ("SF50", "Cirrus Vision Jet"),
            ("CL-600", "Challenger"), ("CL-30", "Challenger"), ("BD-100", "Challenger"), ("BD-700", "Global"),
            ("560", "Citation"), ("525", "Citation"), ("680", "Citation"), ("750", "Citation"), ("TBM", "TBM")]


def family(model: str | None, manufacturer: str | None) -> str | None:
    txt = f"{manufacturer or ''} {model or ''}".upper()
    for key, name in FAMILIES:
        if key in txt:
            return name
    return (model or "").strip() or None


SMALL_FIELDS = ("KCBF", "KMLE")   # Council Bluffs + Millard: no airline jets land there


def fix_airline_fields(cfg: Config, raw: list[dict]) -> int:
    """Airline jets that OpenSky places at Council Bluffs or Millard really used Eppley.
    (Council Bluffs is 12 km from Eppley, under the approach path: when receivers lose a
    jet low on final, OpenSky picks the nearest airport.) Offutt is left alone: military
    charter airliners really do use it. Returns how many flights were moved."""
    airlines, mil, excl = _lists(cfg)
    n = 0
    for f in raw:
        if classify(f, airlines, mil, excl) != "commercial":
            continue
        for k in ("estArrivalAirport", "estDepartureAirport"):
            if f.get(k) in SMALL_FIELDS:
                f[k] = HUB
                f["moved_to_eppley"] = True
                n += 1
    if n:
        log(cfg, f"Moved {n} airline flight ends from Council Bluffs/Millard to Eppley")
    return n


MAX_ARRIVAL_LOOKUPS = 120   # OpenSky requests spent filling in missing arrivals each day


def infer_arrivals(cfg: Config, raw: list[dict], day: date, session=None) -> list[dict]:
    """Fill in landings OpenSky missed. Most planes that take off from an Omaha field
    landed there earlier the same day, so each departing plane's previous flight is
    looked up and counted as an arrival at that field (marked "inferred").
    Returns only the added flights. One request per plane, at most MAX_ARRIVAL_LOOKUPS."""
    start, stop = cfg.flight_day(day)
    begin, end = int(start.timestamp()), int(stop.timestamp()) - 1
    known = {(f.get("icao24"), f.get("firstSeen")) for f in raw}   # flights already in the list
    deps = sorted((f for f in raw if f.get("estDepartureAirport") in METRO and f.get("icao24")
                   and f.get("estDepartureAirport") != f.get("estArrivalAirport")), key=lambda f: f.get("firstSeen") or 0)
    airlines, mil, excl = _lists(cfg)
    rank = {"commercial": 0, "private": 1, "military": 2}   # airlines first if the cap is reached
    planes = []
    for f in sorted(deps, key=lambda f: rank.get(classify(f, airlines, mil, excl), 3)):
        if f["icao24"] not in planes:
            planes.append(f["icao24"])
    added, asked = [], 0
    for icao24 in planes[:MAX_ARRIVAL_LOOKUPS]:
        try:
            hist = _get(cfg, session, "/flights/aircraft", {"icao24": icao24, "begin": begin - 12 * 3600, "end": end})
        except DataError as e:
            log(cfg, f"Stopped filling in arrivals after {asked} planes: {e}")
            break
        asked += 1
        hist = sorted(hist, key=lambda h: h.get("lastSeen") or 0)
        for d in (x for x in deps if x["icao24"] == icao24):
            t = d.get("firstSeen") or 0
            prev = [h for h in hist if (h.get("lastSeen") or 0) <= t + 300 and (h.get("lastSeen") or 0) >= t - 18 * 3600
                    and h.get("firstSeen") != d.get("firstSeen")]
            if not prev:
                continue
            p = prev[-1]
            field = d["estDepartureAirport"]
            if (icao24, p.get("firstSeen")) in known or not (begin <= (p.get("lastSeen") or 0) <= end):
                continue   # already counted, or it landed outside this flight day
            if p.get("estArrivalAirport") in METRO or p.get("estDepartureAirport") == field:
                continue   # OpenSky did catch this landing, or it's a local loop
            added.append(dict(p, estArrivalAirport=field, callsign=p.get("callsign") or d.get("callsign"), inferred=True))
            known.add((icao24, p.get("firstSeen")))
    if asked:
        log(cfg, f"Filled in {len(added)} arrivals OpenSky missed (looked up {asked} planes)")
    return added


def aircraft_info(cfg: Config, icao24s: set, session=None) -> dict:
    """{icao24: {"cls": "jet"|"prop"|"heli"|None, "family": str|None}}. Owner/registration are never kept."""
    path = cfg.data_dir / "aircraft_cache.json"
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        cache = {}
    todo = [h for h in icao24s if h and h not in cache]
    if todo and cfg.secrets.get("opensky_client_id") and session is not None:
        headers = {}
        try:
            tok = _token(cfg, session)
            headers = {"Authorization": f"Bearer {tok}"} if tok else {}
        except DataError:
            pass
        for h in todo[:100]:   # at most 100 new aircraft a day (each one is a request)
            try:
                r = session.get(f"{API}/metadata/aircraft/icao/{h}", headers=headers, timeout=20)
                if r.status_code == 429:
                    break        # daily allowance used up: stop, and look the rest up another day
                if r.status_code == 404:
                    cache[h] = {"cls": None, "family": None}   # OpenSky doesn't know this one
                    continue
                if r.status_code != 200:
                    continue     # a hiccup: don't remember it as "unknown"; try again next time
                d = r.json() or {}
                ac = (d.get("icaoAircraftClass") or d.get("icaoaircraftclass") or "").upper()
                cls = "heli" if ac.startswith(("H", "G")) else "jet" if ac.endswith("J") else \
                      "prop" if ac.endswith(("P", "T")) else None
                cache[h] = {"cls": cls, "family": family(d.get("model"), d.get("manufacturerName"))}
            except Exception:
                continue
        try:
            path.write_text(json.dumps(cache), encoding="utf-8")
        except OSError:
            pass
    return {h: cache.get(h, {"cls": None, "family": None}) for h in icao24s}


# ---- build the day ------------------------------------------------------------------

def build_day(cfg: Config, raw: list[dict], info: dict | None = None) -> dict:
    """-> {"posts": {slot: {"moves": [...], "stats": {...}}}, "complete": bool, "why": str}

    Each move: dir dep|arr, metro (ICAO), other (place or None), t (time at the metro field),
    t0/t1 (flight start/end), group (airline|bizjet|prop|military), airline, family."""
    airlines, mil, excl = _lists(cfg)
    info = info or {}
    out = []
    for f in raw:
        dep, arr = f.get("estDepartureAirport"), f.get("estArrivalAirport")
        kind = classify(f, airlines, mil, excl)
        if kind == "excluded":
            continue
        cs = (f.get("callsign") or "").strip().upper()
        meta = info.get(f.get("icao24"), {})
        if meta.get("cls") == "heli":
            continue  # helicopters: mostly medical/news, never shown
        if kind == "commercial":
            group = "airline"
        elif kind == "military":
            group = "military"
        elif meta.get("cls") == "prop":
            group = "prop"
        else:
            group = "bizjet"  # jets, plus unknown types from named jet operators / N-numbers
        base = {"callsign": cs, "kind": kind, "group": group,
                "airline": airlines.get(cs[:3]) if kind == "commercial" else PRIVATE_OPERATORS.get(cs[:3]),
                "family": meta.get("family"), "t0": f.get("firstSeen"), "t1": f.get("lastSeen")}
        if dep in METRO and dep != arr:
            out.append(dict(base, dir="dep", metro=dep, other=place(arr), metro_place=place(dep), t=f.get("firstSeen")))
        if arr in METRO and dep != arr:
            out.append(dict(base, dir="arr", metro=arr, other=place(dep), metro_place=place(arr), t=f.get("lastSeen")))
        if dep in METRO and dep == arr:
            out.append(dict(base, dir="local", metro=dep, other=None, metro_place=place(dep), t=f.get("firstSeen")))
    posts = {
        "commercial": [m for m in out if m["group"] == "airline" and m["metro"] == HUB],
        "private": [m for m in out if m["group"] == "bizjet" and m["metro"] != "KOFF"],
        "military": [m for m in out if m["group"] == "military"],
        "all": out,
    }
    day = {"posts": {k: {"moves": v, "stats": stats(cfg, k, v)} for k, v in posts.items()}}
    c = day["posts"]["commercial"]["stats"]
    last = max([m["t"] for m in posts["commercial"] if m["dir"] == "dep" and m.get("t")] or [0])
    # The last departure should be 9 PM or later (counted within the flight day,
    # so one after midnight counts as late, not early).
    shift = timedelta(hours=cfg.day_start.hour, minutes=cfg.day_start.minute)
    late_enough = bool(last) and (datetime.fromtimestamp(last, cfg.tz) - shift).time() >= \
        (datetime(2000, 1, 1, 21) - shift).time()
    # Arrivals should roughly match departures; OpenSky sometimes hasn't finished the
    # arrivals (its overnight batch, or planes not tracked down to the runway) yet.
    arrivals_ok = c["arrivals"] >= max(40, 0.6 * c["departures"])
    day["complete"] = c["departures"] >= 60 and late_enough and arrivals_ok
    day["why"] = "" if day["complete"] else (
        f"Eppley shows {c['departures']} airline departures and {c['arrivals']} arrivals, the last departure at "
        f"{datetime.fromtimestamp(last, cfg.tz).strftime('%I:%M %p').lstrip('0') if last else 'n/a'}. "
        "Eppley has about 70-75 airline departures a day and about as many arrivals (Omaha Airport "
        "Authority), running past 9 PM, so OpenSky's data looks unfinished.")
    return day


def _clock(cfg: Config, ts) -> str:
    return datetime.fromtimestamp(ts, cfg.tz).strftime("%I:%M %p").lstrip("0") if ts else ""


def stats(cfg: Config, slot: str, moves: list[dict]) -> dict:
    deps = [m for m in moves if m["dir"] == "dep"]
    arrs = [m for m in moves if m["dir"] == "arr"]

    def top(ms, k=5):
        c = Counter((m["other"]["code"], m["other"]["city"]) for m in ms if m["other"])
        return [{"code": code, "city": city, "count": n} for (code, city), n in c.most_common(k)]

    s = {"departures": len(deps), "arrivals": len(arrs), "flights": len(moves)}
    if slot == "military":  # counts only: no cities, no times, no callsigns
        return s
    cities = {m["other"]["code"] for m in moves if m["other"]}
    s["cities_linked"] = len(cities)
    both = Counter((m["other"]["code"], m["other"]["city"]) for m in moves if m["other"])
    s["top_destinations"] = [{"code": a, "city": b, "count": n} for (a, b), n in both.most_common(5)]
    s["top_origins"] = top(arrs)
    far = None
    for m in moves:
        if m["other"] and m["metro_place"] and m["group"] != "military":
            d = miles(m["other"], m["metro_place"])
            if not far or d > far["miles"]:
                far = {"code": m["other"]["code"], "city": m["other"]["city"], "miles": round(d)}
    if far:
        s["farthest"] = far
    civil = [m for m in moves if m["group"] != "military"]
    hours = Counter(datetime.fromtimestamp(m["t"], cfg.tz).hour for m in civil if m.get("t"))
    if hours:
        h, n = hours.most_common(1)[0]
        s["busiest_hour"] = f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}"
        s["busiest_hour_count"] = n
    first = min([m["t"] for m in deps if m.get("t") and m["group"] != "military"] or [0])
    if first:
        s["first_wheels_up"] = _clock(cfg, first)
    if slot == "commercial":
        s["top_airlines"] = [{"name": n, "count": c} for n, c in Counter(m["airline"] for m in moves if m["airline"]).most_common(5)]
    if slot == "private":
        s["top_types"] = [{"name": n, "count": c} for n, c in Counter(m["family"] for m in moves if m["family"]).most_common(5)]
        s["top_operators"] = [{"name": n, "count": c} for n, c in Counter(m["airline"] for m in moves if m["airline"]).most_common(3)]
    if slot == "all":
        c = Counter(m["metro"] for m in moves)
        s["airports"] = [{"code": place(i)["code"] if place(i) else i, "name": METRO[i], "count": c.get(i, 0)} for i in METRO]
        s["by_group"] = dict(Counter(m["group"] for m in moves))
    return s


# ---- Offutt weekly counts (for the military stats bars) -----------------------------------

def military_week(cfg: Config, day: date, today_count: int, session=None, backfill: bool = True) -> list[dict]:
    path = cfg.data_dir / "daily_counts.json"
    try:
        hist = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        hist = {}
    hist.setdefault(day.isoformat(), {})["military"] = today_count
    airlines, mil, excl = _lists(cfg)
    for i in range(1, 7):
        d = day - timedelta(days=i)
        if "military" in hist.get(d.isoformat(), {}) or not backfill or session is None:
            continue
        try:
            start, stop = cfg.flight_day(d)
            b, e = int(start.timestamp()), int(stop.timestamp()) - 1
            raw = _get(cfg, session, "/flights/departure", {"airport": "KOFF", "begin": b, "end": e}) + \
                _get(cfg, session, "/flights/arrival", {"airport": "KOFF", "begin": b, "end": e})
            n = sum(1 for f in raw if classify(f, airlines, mil, excl) == "military")
            hist.setdefault(d.isoformat(), {})["military"] = n
        except Exception:
            break
    try:
        path.write_text(json.dumps(hist, indent=1), encoding="utf-8")
    except OSError:
        pass
    week = []
    for i in range(6, -1, -1):
        d = day - timedelta(days=i)
        week.append({"date": d.isoformat(), "label": d.strftime("%a").upper(), "count": hist.get(d.isoformat(), {}).get("military")})
    return week


# ---- sample data (clearly labeled; never posted by accident) -----------------------

def demo_raw(day: date, cfg: Config, seed: int = 7) -> list[dict]:
    """Made-up but realistic day (about 90 airline departures, 5 AM to 10:30 PM)."""
    rnd = random.Random(seed)
    start = datetime.combine(day, datetime.min.time(), tzinfo=cfg.tz).timestamp()
    hub = place("KOMA")
    common = ["KORD", "KDEN", "KPHX", "KMSP", "KDFW", "KATL", "KLAS", "KMDW", "KDTW", "KCLT", "KIAH", "KSLC",
              "KLGA", "KDCA", "KSTL", "KMEM", "KDAL", "KHOU", "KLAX", "KSEA", "CYYZ", "KPHL", "KBNA", "KMCO"]
    weights = [21, 16, 10, 9, 6, 6, 5, 5, 4, 4, 3, 3, 4, 3, 3, 2, 2, 2, 2, 1, 1, 1, 2, 2]
    biz = ["KAPA", "KSDL", "KTEB", "KVNY", "KPWK", "KHOU", "KFXE", "KDAL", "KMKC", "KLNK", "KDSM", "KICT", "KASE", "KBCT"]
    mil = ["KTIK", "KWRI", "KDOV", "KBAD", "KFFO", "KCHS", "KSKA", "KBIF"]
    raw = []

    def dur(a, b):
        pa, pb = place(a) or hub, place(b) or hub
        return (miles(pa, pb) / 470 + 0.45) * 3600

    def add(cs, hexid, dep, arr, t_at_metro, departing):
        d = dur(dep, arr)
        t0, t1 = (t_at_metro, t_at_metro + d) if departing else (t_at_metro - d, t_at_metro)
        raw.append({"callsign": cs, "icao24": hexid, "estDepartureAirport": dep, "estArrivalAirport": arr,
                    "firstSeen": int(t0), "lastSeen": int(t1)})
    als = ["SWA", "UAL", "AAL", "DAL", "SKW", "ENY", "RPA", "AAY", "FFT", "EDV", "JIA", "FDX", "UPS"]
    for i in range(170):
        dest = rnd.choices(common, weights)[0]
        al = rnd.choice(als)
        departing = i % 2 == 0 and i < 180
        t = start + (5.3 + rnd.random() * 17.2) * 3600
        if departing:
            add(f"{al}{rnd.randint(100, 4999)}", "a%05x" % rnd.randint(0, 0xDFFFF), "KOMA", dest, t, True)
        elif i < 150:
            add(f"{al}{rnd.randint(100, 4999)}", "a%05x" % rnd.randint(0, 0xDFFFF), dest, "KOMA", t, False)
    for i in range(38):
        home = rnd.choice(["KOMA", "KOMA", "KMLE", "KCBF"])
        d = rnd.choice(biz + common[:6])
        cs = rnd.choice([f"EJA{rnd.randint(100, 999)}", f"LXJ{rnd.randint(300, 599)}",
                         f"N{rnd.randint(10, 999)}{rnd.choice('ABCDEFGHJKLMNPRSTUVWXYZ')}"])
        t = start + (6 + rnd.random() * 15) * 3600
        if i % 2:
            add(cs, "a%05x" % rnd.randint(0, 0xDFFFF), home, d, t, True)
        else:
            add(cs, "a%05x" % rnd.randint(0, 0xDFFFF), d, home, t, False)
    for i in range(9):
        d = rnd.choice(mil)
        t = start + (7 + rnd.random() * 12) * 3600
        add(rnd.choice(["RCH", "SNOOP", "COBRA", "TOPCT"]) + str(rnd.randint(10, 99)), "ae%04x" % rnd.randint(0, 0xFFFF),
            *(("KOFF", d, t, True) if i % 2 else (d, "KOFF", t, False)))
    return raw
