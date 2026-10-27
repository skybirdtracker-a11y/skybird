"""The new post formats: schedule, data, captions and how the daily run uses them."""
import json
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from skybird import captions, cloud, engine, flights as fl, formats  # noqa: E402
from skybird.config import load_config  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    shutil.copy(ROOT / "cloud" / "config.toml", tmp_path / "config.toml")
    shutil.copy(ROOT / "events.toml", tmp_path / "events.toml")
    return load_config(tmp_path / "config.toml")


def test_schedule(cfg):
    assert formats.format_for(cfg, date(2026, 10, 28), "commercial") == "guess"
    assert formats.format_for(cfg, date(2026, 10, 28), "private") is None
    assert formats.format_for(cfg, date(2026, 11, 1), "commercial") == "lincoln"
    assert formats.format_for(cfg, date(2026, 11, 1), "wrap") == "wrap"
    assert cfg.slot_active("wrap", date(2026, 11, 1)) and not cfg.slot_active("wrap", date(2026, 11, 2))
    assert cfg.slot_active("commercial", date(2026, 11, 2))


def test_guess_choices_hide_the_answer_in_alphabetical_order(cfg):
    day = fl.build_day(cfg, fl.demo_raw(date(2026, 10, 26), cfg))
    g = formats.guess_choices(day["posts"]["commercial"], date(2026, 10, 26))
    assert len(set(g["choices"])) == 3 and g["answer"] in g["choices"]
    assert g["choices"] == sorted(g["choices"])
    deps = [m["other"]["city"] for m in day["posts"]["commercial"]["moves"] if m["dir"] == "dep" and m["other"]]
    assert g["answer_count"] == max(deps.count(c) for c in set(deps))
    opts = formats.caption_options("guess", {"guess": g}, date(2026, 10, 26), date(2026, 10, 28))
    assert opts[0]["id"] == "guess" and "Monday" in opts[0]["text"]
    assert g["answer"] + "." not in opts[0]["text"]          # the caption never gives it away
    assert all(f"{l}) {c}" in opts[0]["text"] for l, c in zip("ABC", g["choices"]))


def test_scoreboard_week_over_week(cfg):
    formats.record_fields(cfg, date(2026, 10, 21), [{"code": "OMA", "count": 150}, {"code": "MLE", "count": 50}])
    stats = {"airports": [{"code": "OMA", "name": "Eppley", "count": 180}, {"code": "MLE", "name": "Millard", "count": 40}]}
    sb = formats.scoreboard(cfg, date(2026, 10, 28), stats)
    assert sb["total"] == 220 and sb["vs_last_week"] == 10
    opts = formats.caption_options("scoreboard", {"scoreboard": sb}, date(2026, 10, 28), date(2026, 10, 30))
    assert opts[0]["id"] == "deadpan"                       # Fridays are Deadpan Tower
    assert "Eppley 180, Millard 40" in opts[0]["text"] and "Roger, roger." in opts[0]["text"]


def test_week_from_saved_lists(cfg):
    folder = cfg.data_dir / "flights"
    folder.mkdir(parents=True)
    for i in range(1, 7):
        d = date(2026, 10, 30).toordinal() - i
        d = date.fromordinal(d)
        (folder / f"{d}.json").write_text(json.dumps({"flights": fl.demo_raw(d, cfg, seed=i)}))
    today = fl.build_day(cfg, fl.demo_raw(date(2026, 10, 30), cfg))
    w = formats.week(cfg, date(2026, 10, 30), today)
    assert [d["label"] for d in w["days"]] == ["SAT", "SUN", "MON", "TUE", "WED", "THU", "FRI"]
    assert w["days_missing"] == 0 and w["range"] == "Oct 24 – Oct 30"
    assert w["week_total"] == sum(d["total"] for d in w["days"])
    assert w["busiest_day"] and w["top_destinations"]
    text = formats.caption_options("wrap", w, date(2026, 10, 30), date(2026, 11, 1))[0]["text"]
    assert "Oct 24 – Oct 30" in text and "airline flights at Eppley" in text


def test_lincoln_window_and_fair_baseline(cfg):
    thu, sat = formats._game_days(date(2026, 11, 1))
    assert (thu, sat) == (date(2026, 10, 29), date(2026, 10, 31))
    # the weekend before Ohio State (Nov 21) has no home game, so it's the baseline
    assert formats._quiet_saturday(cfg, date(2026, 11, 21)) == date(2026, 11, 14)
    begin = int(cfg.flight_day(thu)[0].timestamp())
    end = formats._utc_midnight(date(2026, 11, 1)) - 1        # Sat 7 PM Omaha = midnight UTC
    pieces = formats._windows(begin, end)
    assert len(pieces) == 2 and pieces[0][0] == begin and pieces[-1][1] == end
    assert all((e - (b - b % 86400)) < 2 * 86400 for b, e in pieces)   # each within 2 UTC days


def test_lincoln_counts_private_jets_only_and_asks_opensky_once(cfg):
    asked = []
    planes = [
        {"callsign": "N123AB", "icao24": "a00001", "estDepartureAirport": "KCMH", "estArrivalAirport": "KLNK"},
        {"callsign": "EJA455", "icao24": "a00002", "estDepartureAirport": "KSEA", "estArrivalAirport": "KLNK"},
        {"callsign": "SWA123", "icao24": "a00003", "estDepartureAirport": "KDEN", "estArrivalAirport": "KLNK"},  # airline
        {"callsign": "RCH12", "icao24": "ae0004", "estDepartureAirport": "KOFF", "estArrivalAirport": "KLNK"},   # military
        {"callsign": "N55X", "icao24": "a00005", "estDepartureAirport": "KLNK", "estArrivalAirport": "KLNK"},    # local
    ]

    def fake_get(cfg_, s, path, params):
        asked.append(params["begin"])
        t = params["begin"] + 3600
        return [dict(p, firstSeen=t - 7200, lastSeen=t) for p in planes] if len(asked) <= 2 else planes[:1] and \
            [dict(planes[0], firstSeen=params["begin"], lastSeen=params["begin"] + 60)]
    orig = fl._get
    fl._get = fake_get
    try:
        out = formats.lincoln(cfg, date(2026, 11, 1), session=object())
        assert len(asked) == 4                                   # 2 for the game weekend + 2 for a normal one
        out2 = formats.lincoln(cfg, date(2026, 11, 1), session=None)   # a remake: from the saved copy
        assert len(asked) == 4
    finally:
        fl._get = orig
    s = out["stats"]
    assert s["arrivals"] == 4 and s["normal"] == 2 and s["change_pct"] == 100
    assert {o["code"] for o in s["top_origins"]} == {"CMH", "SEA"}
    assert out2["stats"]["arrivals"] == 4
    assert all(m["callsign"] == "" for m in out["moves"])        # no tail numbers kept
    text = formats.caption_options("lincoln", s, date(2026, 10, 30), date(2026, 11, 1))[0]["text"]
    assert "4 private jets flew into Lincoln" in text and "vs 2 on a normal weekend" in text


def _stats_file(cfg, post_date, posts):
    f = cfg.media_file(post_date, "stats.json")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"source": "opensky", "complete": True, "posts": posts}))


def test_prepare_uses_the_format_and_the_sunday_wrap(cfg):
    day = fl.build_day(cfg, fl.demo_raw(date(2026, 10, 30), cfg))
    posts = {k: v["stats"] for k, v in day["posts"].items()}
    lin = {"arrivals": 30, "departures": 0, "flights": 30, "normal": 12, "change_pct": 150,
           "top_origins": [{"code": "CMH", "city": "Columbus", "count": 6}], "format": "lincoln", "game_day": "2026-10-31"}
    posts["commercial"] = lin
    posts["wrap"] = dict(formats.week(cfg, date(2026, 10, 30), day), format="wrap")
    _stats_file(cfg, date(2026, 11, 1), posts)
    st = engine.prepare(cfg, date(2026, 11, 1), force=True, render=False, quiet=True)
    c = st["posts"]["commercial"]
    assert c["title"] == "Game weekend jets · Lincoln" and c["format"] == "lincoln"
    assert c["caption"].startswith("Game weekend in Lincoln")
    tags = [h["tag"] for h in c["hashtags"] if h["checked"]]
    assert "#Huskers" in tags and "#EppleyAirfield" not in tags
    assert c["tiktok_priority"][:3] == ["#SkyBirdOmaha", "#Lincoln", "#Huskers"]
    assert not any(m["checked"] and m["handle"].lower() == "@omaairport" for m in c["mentions"]["instagram"])
    assert st["posts"]["wrap"]["title"] == "Sunday weekly wrap"
    assert engine.tracking(cfg, date(2026, 11, 1), "commercial", c)["format"] == "lincoln"
    # Monday: no wrap post, regular captions
    _stats_file(cfg, date(2026, 11, 2), {k: v["stats"] for k, v in day["posts"].items()})
    st2 = engine.prepare(cfg, date(2026, 11, 2), force=True, render=False, quiet=True)
    assert "wrap" not in st2["posts"] and st2["posts"]["commercial"]["format"] == ""


def test_plan_schedules_the_wrap_only_on_sundays(cfg):
    sun = datetime(2026, 11, 1, 4, 0, tzinfo=cfg.tz)
    times = cloud.plan(cfg, sun.date(), sun)
    assert times["wrap"].strftime("%H:%M") == "10:00" and times["commercial"].strftime("%H:%M") == "09:00"
    mon = datetime(2026, 11, 2, 4, 0, tzinfo=cfg.tz)
    assert "wrap" not in cloud.plan(cfg, mon.date(), mon)
