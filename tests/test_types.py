"""Aircraft types from the FAA registry (OpenSky's type lookup answers 410 Gone since Nov 2026),
and private-plane captions that count what the video counts."""
import gzip
import io
import json
import shutil
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from skybird import aircraft_types, captions, flights as fl  # noqa: E402
from skybird.config import load_config  # noqa: E402

MASTER = ("﻿N-NUMBER,SERIAL NUMBER,MFR MDL CODE,ENG MFR MDL,YEAR MFR,TYPE REGISTRANT,NAME,STREET,CITY,STATE,"
          "TYPE AIRCRAFT,TYPE ENGINE,STATUS CODE,MODE S CODE,MODE S CODE HEX\n"
          "12345 ,560-0001 ,1234567,41514,2001,3,SECRET OWNER LLC ,1 MAIN ST,OMAHA,NE,5,5,V,50012345,A0B1C2    \n"
          "777AB ,17255001 ,2072738,17003,1990,1,JANE PILOT ,2 ELM,LINCOLN,NE,4,1,V,50012346,A0B1C3    \n"
          "88HX  ,1001 ,3000001,00000,2010,1,HELI CO ,3 OAK,OMAHA,NE,6,3,V,50012347,A0B1C4    \n"
          "99Z   ,1 ,9999999,00000,2010,1,NOBODY ,4 PINE,OMAHA,NE,1,0,V,50012348,A0B1C5    \n")
ACFTREF = ("CODE,MFR,MODEL,TYPE-ACFT,TYPE-ENG,AC-CAT,BUILD-CERT-IND,NO-ENG,NO-SEATS\n"
           "1234567,CESSNA ,560XL ,5,5,1,1,02,012\n2072738,CESSNA ,172S ,4,1,1,1,01,004\n"
           "3000001,BELL ,206B ,6,3,1,1,01,005\n")


def faa_zip() -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("MASTER.txt", MASTER)
        z.writestr("ACFTREF.txt", ACFTREF)
    return b.getvalue()


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "cloud").mkdir()
    shutil.copy(ROOT / "cloud" / "config.toml", tmp_path / "cloud" / "config.toml")
    shutil.copytree(ROOT / "cities", tmp_path / "cities")
    shutil.copytree(ROOT / "events", tmp_path / "events")
    aircraft_types._LOADED.clear()
    return tmp_path


def test_faa_registry_gives_jet_prop_heli_and_never_keeps_names(project):
    t = aircraft_types.parse(faa_zip())
    assert t == {"a0b1c2": "j|Citation", "a0b1c3": "p|172S", "a0b1c4": "h|206B"}   # the glider-less row is skipped
    assert "SECRET" not in json.dumps(t) and "PILOT" not in json.dumps(t) and "12345" not in json.dumps(t)


def test_types_come_from_the_faa_and_opensky_410_stops_asking(project, monkeypatch):
    cfg = load_config(project / "cloud" / "config.toml")
    with gzip.open(cfg.shared_dir / aircraft_types.FILE, "wt") as f:
        json.dump(aircraft_types.parse(faa_zip()), f)
    (cfg.shared_dir / "aircraft_cache.json").write_text(json.dumps({"a0b1c3": {"cls": None, "family": None}}))
    cfg.secrets.update(opensky_client_id="id", opensky_client_secret="s")
    asked = []

    class R:
        status_code, headers, text = 410, {}, "Gone"

    class Session:
        def post(self, url, **kw):
            r = R(); r.status_code = 200; r.json = lambda: {"access_token": "t", "expires_in": 1800}
            return r

        def get(self, url, **kw):
            asked.append(url)
            return R()
    monkeypatch.setattr("skybird.notify.send", lambda *a, **k: None)
    info = fl.aircraft_info(cfg, {"a0b1c2", "a0b1c3", "ffff01", "ffff02"}, Session())
    assert info["a0b1c2"]["cls"] == "jet" and info["a0b1c2"]["family"] == "Citation"
    assert info["a0b1c3"]["cls"] == "prop"                                  # saved as unknown before: re-checked
    assert len(asked) == 1                                                  # the first 410 stops the lookups
    fl.aircraft_info(cfg, {"ffff03"}, Session())
    assert len(asked) == 1                                                  # and it never asks again


def test_private_caption_counts_what_the_video_counts(project):
    cfg = load_config(project / "cloud" / "config.toml")
    st = {"flights": 109, "departures": 51, "arrivals": 58, "jets": 16, "small": 80, "unknown": 13}
    text = " ".join(o["text"] for o in captions.build_options(cfg, "private", st, date(2026, 11, 4)))
    assert "109 private-plane takeoffs and landings" in text
    assert "16 by jets, 80 by small planes and 13 we couldn't identify" in text
    assert "16 private jet takeoffs" not in text
    only_jets = {"flights": 16, "departures": 8, "arrivals": 8, "jets": 16, "small": 0, "unknown": 0}
    assert "16 private jet takeoffs and landings" in " ".join(
        o["text"] for o in captions.build_options(cfg, "private", only_jets, date(2026, 11, 4)))


def test_private_stats_card_adds_up(project):
    """Nov 6: the card showed 16 jets, 0 small planes, 51 departures, 58 arrivals (93 planes in no box)."""
    from skybird import render
    cfg = load_config(project / "cloud" / "config.toml")
    raw = [{"callsign": f"N{i}AB", "icao24": f"b{i:05d}", "estDepartureAirport": "KOMA", "estArrivalAirport": "KDEN",
            "firstSeen": 100 + i, "lastSeen": 200 + i} for i in range(6)]
    info = {"b00000": {"cls": "jet", "family": "Citation"}, "b00001": {"cls": "prop", "family": "172S"},
            "b00002": {"cls": "prop", "family": "172S"}}
    post = fl.build_day(cfg, raw, info)["posts"]["private"]
    reel = render.Reel(cfg, "private", date(2026, 11, 4), post, render.Fonts(), True, week=[])
    tiles = {label: n for n, label, _ in reel._stats_content()[0]}
    assert tiles == {"TAKEOFFS + LANDINGS": 6, "PRIVATE JETS": 1, "SMALL PLANES": 2, "TYPE UNKNOWN": 3}
    info.update({f"b{i:05d}": {"cls": "prop", "family": "SR22"} for i in range(3, 6)})
    post = fl.build_day(cfg, raw, info)["posts"]["private"]
    reel = render.Reel(cfg, "private", date(2026, 11, 4), post, render.Fonts(), True, week=[])
    tiles = {label: n for n, label, _ in reel._stats_content()[0]}
    assert tiles["PRIVATE JETS"] + tiles["SMALL PLANES"] == tiles["DEPARTURES"] + tiles["ARRIVALS"] == 6
