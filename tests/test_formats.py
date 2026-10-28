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


def test_volume_test_balanced_levels_and_half_new(cfg):
    from collections import Counter
    from datetime import timedelta
    levels, seen = Counter(), Counter()
    for i in range(28):
        d = date(2026, 10, 28) + timedelta(days=i)
        sc = formats.day_schedule(cfg, d)
        levels[(d.strftime("%a"), sc["level"])] += 1
        assert len(sc["running"]) == sc["level"]
        assert set(sc["formats"]) <= set(sc["running"]) and "military" not in sc["formats"]
        assert len(sc["formats"]) <= (sc["level"] + 1) // 2 or "lincoln" in sc["formats"].values()
        assert all(cfg.slot_active(k, d) == (k in sc["running"]) for k in formats.ALL_SLOTS)
        seen.update(sc["formats"].values())
        if d.strftime("%a") != "Tue":
            assert "weekend" not in sc["formats"].values()
    # every weekday gets 1, 2, 3 and 4 posts once in 4 weeks
    assert len(levels) == 28 and set(levels.values()) == {1}
    assert formats.day_schedule(cfg, date(2026, 11, 1))["formats"]["commercial"] == "lincoln"   # fixed date runs
    assert formats.day_schedule(cfg, date(2026, 10, 30))["running"] == ["private"]            # [posts_on] pick
    assert formats.day_schedule(cfg, date(2026, 10, 27)) == {"level": 4, "running": formats.ALL_SLOTS, "formats": {}}
    assert all(seen[f] >= 2 for f in ("guess", "farthest", "firstlast", "scoreboard", "hours", "types"))


def test_without_volume_test_all_four_post_with_two_concepts(cfg):
    from datetime import timedelta
    cfg.raw["testing"]["volume"] = False
    cfg.raw.pop("posts_on", None)
    for i in range(14):
        d = date(2026, 10, 28) + timedelta(days=i)
        sc = formats.day_schedule(cfg, d)
        assert sc["level"] == 4 and len(sc["formats"]) == 2
    cfg.raw["testing"]["enabled"] = False
    assert formats.day_plan(cfg, date(2026, 10, 29)) == {}
    assert formats.format_for(cfg, date(2026, 11, 1), "wrap") == "wrap"
    assert cfg.slot_active("wrap", date(2026, 11, 1)) and not cfg.slot_active("wrap", date(2026, 11, 2))


def test_new_concepts_data(cfg):
    day = fl.build_day(cfg, fl.demo_raw(date(2026, 10, 26), cfg))
    c = day["posts"]["commercial"]
    far = formats.farthest(c)["list"]
    assert len(far) == 3 and far[0]["miles"] >= far[1]["miles"] >= far[2]["miles"]
    fl_ = formats.first_last(cfg, c)
    assert fl_["first"]["time"] and fl_["last"]["city"] and fl_["between"] == len(c["moves"])
    h = formats.hours(cfg, day["posts"]["all"])
    assert len(h["rows"]) == 24 and h["rows"][0]["hour"] == 3                    # the flight day starts 3:30 AM
    assert h["total"] == sum(1 for m in day["posts"]["all"]["moves"] if m["group"] != "military")
    for fmt, stats in (("farthest", {"farthest_list": {"list": far}}), ("firstlast", {"first_last": fl_}),
                       ("hours", {"hours": h})):
        text = formats.caption_options(fmt, stats, date(2026, 10, 26), date(2026, 10, 28))[0]["text"]
        assert "Monday" in text and "None" not in text and "?" not in text.split("\n")[0][-3:]
    with pytest.raises(ValueError):
        formats.types({"stats": {"top_types": [{"name": "Citation", "count": 3}]}})   # too few: regular post instead


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
    assert engine.tracking(cfg, date(2026, 11, 1), "commercial", c)["day_posts"] == 3
    assert set(st["posts"]) == {"commercial", "private", "military", "wrap"}    # Sunday Nov 1: 3 posts + the wrap
    # Monday Nov 2: a 1-post day (volume test), no wrap
    _stats_file(cfg, date(2026, 11, 2), {k: v["stats"] for k, v in day["posts"].items()})
    st2 = engine.prepare(cfg, date(2026, 11, 2), force=True, render=False, quiet=True)
    assert list(st2["posts"]) == formats.day_schedule(cfg, date(2026, 11, 2))["running"] and len(st2["posts"]) == 1


def test_plan_schedules_the_wrap_only_on_sundays(cfg):
    sun = datetime(2026, 11, 1, 4, 0, tzinfo=cfg.tz)
    times = cloud.plan(cfg, sun.date(), sun)
    assert times["wrap"].strftime("%H:%M") == "10:00" and times["commercial"].strftime("%H:%M") == "09:00"
    assert set(times) == {"commercial", "private", "military", "wrap"}
    mon = datetime(2026, 11, 2, 4, 0, tzinfo=cfg.tz)
    assert set(cloud.plan(cfg, mon.date(), mon)) == set(formats.day_schedule(cfg, mon.date())["running"])


def test_report_compares_posts_per_day(cfg):
    from datetime import timedelta
    from skybird import report
    from skybird.state import save_state
    start = date(2026, 10, 28)
    for i in range(27):
        d = start + timedelta(days=i)
        sc = formats.day_schedule(cfg, d)
        posts = {}
        for slot in sc["running"]:
            views = 1000 + 300 * (4 - sc["level"])          # fewer posts -> more views each, but less in total
            posts[slot] = {"title": slot, "posted_at": f"{d}T09:00:00-05:00",
                           "tracking": {"caption_style": "friendly", "format": sc["formats"].get(slot, ""),
                                        "mentions": {}, "day_posts": sc["level"]},
                           "results": {"instagram": {"ok": True, "id": f"{d}{slot}", "metrics": {"views": views}}}}
        save_state(cfg, d, {"posts": posts})
    md = report.build(cfg, datetime(2026, 11, 24, 8, 20, tzinfo=cfg.tz))["markdown"]
    assert "## Posts per day (last 4 weeks)" in md
    assert "| 2 | 7 |" in md and "| 4 | 7 |" in md and "| 1 | 6 |" in md   # 27 days: one 1-post day still to come
    assert "Most total views so far: **4 posts a day**" in md
    assert "| Wed |" in md
