"""Aircraft types from the FAA's public aircraft registry (OpenSky's aircraft-type lookup was
retired: since early Nov 2026 it answers "410 Gone").

The FAA publishes every US-registered aircraft with its engine type as ReleasableAircraft.zip
(MASTER.txt + ACFTREF.txt). SkyBird downloads it about once a month (GitHub Actions only) and
keeps ONLY each aircraft's Mode S hex code, its class (jet / prop / heli) and its model family:
    cloud/data/faa_types.json.gz   {icao24: "j|Citation"}
Names, addresses, registrations and every other column are never read into the saved file,
and nothing from it is ever shown except "jet", "small plane" and a model family.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import os
import time
import zipfile
from pathlib import Path

URL = "https://registry.faa.gov/database/ReleasableAircraft.zip"
FILE = "faa_types.json.gz"
MAX_AGE_DAYS = 30
CLS = {"j": "jet", "p": "prop", "h": "heli"}
JET_ENGINES = {"4", "5"}                         # turbo-jet, turbo-fan
PROP_ENGINES = {"1", "2", "7", "8", "10", "11"}  # piston, turboprop, 2/4-cycle, electric, rotary
ROTOR = {"6", "9"}                               # rotorcraft, gyroplane

_LOADED: dict[str, dict] = {}


def _rows(data: bytes):
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    head = [h.strip().upper() for h in next(reader)]
    for row in reader:
        yield {h: (row[i].strip() if i < len(row) else "") for i, h in enumerate(head)}


def parse(zip_bytes: bytes) -> dict[str, str]:
    """{icao24 hex (lowercase): "j|Family"} from the FAA zip. Reads only the type columns."""
    from .flights import family
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = {n.upper(): n for n in z.namelist()}
    models = {}
    for r in _rows(z.read(names["ACFTREF.TXT"])):
        models[r.get("CODE", "")] = (r.get("MFR", ""), r.get("MODEL", ""), r.get("TYPE-ACFT", ""), r.get("TYPE-ENG", ""))
    out = {}
    for r in _rows(z.read(names["MASTER.TXT"])):
        hexid = r.get("MODE S CODE HEX", "").lower()
        if not hexid:
            continue
        mfr, model, ref_acft, ref_eng = models.get(r.get("MFR MDL CODE", ""), ("", "", "", ""))
        acft = r.get("TYPE AIRCRAFT", "") or ref_acft
        eng = r.get("TYPE ENGINE", "") or ref_eng
        cls = "h" if acft in ROTOR else "j" if eng in JET_ENGINES else "p" if eng in PROP_ENGINES else ""
        if cls:
            out[hexid] = f"{cls}|{family(model, mfr) or ''}"
    return out


def _download(session=None) -> bytes:
    import requests
    from .publishers.buffer import UA
    s = session or requests
    r = s.get(URL, headers={"User-Agent": UA}, timeout=300)
    r.raise_for_status()
    return r.content


def table(cfg, session=None) -> dict[str, str]:
    """The saved hex -> type table, refreshed from the FAA when it's over a month old (only on
    GitHub, where downloads are allowed). {} when there is none yet."""
    path: Path = cfg.shared_dir / FILE
    key = str(path)
    if key in _LOADED:
        return _LOADED[key]
    fresh = path.exists() and time.time() - path.stat().st_mtime < MAX_AGE_DAYS * 86400
    if not fresh and (os.environ.get("GITHUB_ACTIONS") or session is not None):
        try:
            data = parse(_download(session))
            if len(data) > 1000 or session is not None:   # a real registry has ~300,000 entries
                path.parent.mkdir(parents=True, exist_ok=True)
                with gzip.open(path, "wt", encoding="utf-8") as f:
                    json.dump(data, f, separators=(",", ":"))
        except Exception as e:   # the FAA site is down or changed: use what's saved, try again next run
            from .state import log
            log(cfg, f"Couldn't refresh the FAA aircraft registry ({str(e)[:200]}); using the saved copy")
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            _LOADED[key] = json.load(f)
    except (OSError, ValueError):
        _LOADED[key] = {}
    return _LOADED[key]


def lookup(cfg, icao24: str, session=None) -> dict | None:
    """{"cls": "jet"|"prop"|"heli", "family": str|None}, or None if the FAA doesn't list it."""
    v = table(cfg, session).get((icao24 or "").lower())
    if not v:
        return None
    c, _, fam = v.partition("|")
    return {"cls": CLS.get(c), "family": fam or None}
