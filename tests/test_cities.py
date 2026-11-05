"""Several cities on one engine: Omaha stays as it was, Kansas City is its own city."""
import json
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from skybird import captions, engine, flights as fl, formats  # noqa: E402
from skybird.config import load_config  # noqa: E402


@pytest.fixture
def project(tmp_path):
    (tmp_path / "cloud").mkdir()
    shutil.copy(ROOT / "cloud" / "config.toml", tmp_path / "cloud" / "config.toml")
    shutil.copytree(ROOT / "cities", tmp_path / "cities")
    shutil.copytree(ROOT / "events", tmp_path / "events")
    return tmp_path


def cfg_for(project, city=None):
    return load_config(project / "cloud" / "config.toml", city)


def test_omaha_is_the_default_and_keeps_its_settings(project):
    c = cfg_for(project)
    assert c.city_id == "omaha" and c.hub == "KOMA" and c.hub_name == "Eppley"
    assert list(c.metro) == ["KOMA", "KMLE", "KCBF", "KOFF"]
    assert c.platform_enabled("facebook") and c.platform_enabled("tiktok") and not c.platform_enabled("instagram")
    assert not c.dry_run
    assert c.data_dir == project / "cloud" / "data" / "omaha"
    assert c.local("Morning, Omaha. Eppley moved 12 flights out of OMA.") == "Morning, Omaha. Eppley moved 12 flights out of OMA."


def test_kansas_city_profile(project):
    k = cfg_for(project, "kc")
    assert k.city_id == "kc" and k.hub == "KMCI"
    assert set(k.metro) == {"KMCI", "KMKC", "KOJC", "KIXD", "KLXT", "KGPH", "K3GV", "KK34", "K3EX"}   # KCI, Wheeler Downtown, all public metro fields
    assert k.military_fields == [] and k.excluded == ["KSZL", "KFLV"]
    assert k.dry_run                                            # quiet build: never posts
    assert k.platform_enabled("tiktok") and not k.platform_enabled("facebook") and not k.platform_enabled("instagram")
    assert [s.key for s in k.slots] == ["commercial", "private", "all", "wrap", "rivalry"]
    assert k.data_dir == project / "cloud" / "data" / "kc" and k.shared_dir == project / "cloud" / "data"
    assert k.media_folder(date(2026, 11, 10)) == project / "cloud" / "videos" / "kc"
    assert k.local("Save this for your next trip out of OMA. #SkyBirdOmaha #EppleyAirfield") == \
        "Save this for your next trip out of MCI. #SkyBirdKC #KCI"
    with pytest.raises(SystemExit):
        cfg_for(project, "nowhere")


def test_kc_never_counts_whiteman_or_military_and_rolls_up_small_fields(project):
    k = cfg_for(project, "kc")
    raw = [
        {"callsign": "SWA1", "icao24": "c1", "estDepartureAirport": "KMCI", "estArrivalAirport": "KDEN", "firstSeen": 1, "lastSeen": 2},
        {"callsign": "SWA2", "icao24": "c2", "estDepartureAirport": "KDAL", "estArrivalAirport": "KMCI", "firstSeen": 3, "lastSeen": 4},
        {"callsign": "N1AB", "icao24": "c3", "estDepartureAirport": "KMKC", "estArrivalAirport": "KTEB", "firstSeen": 5, "lastSeen": 6},
        {"callsign": "N2AB", "icao24": "c4", "estDepartureAirport": "KSZL", "estArrivalAirport": "KMKC", "firstSeen": 7, "lastSeen": 8},
        {"callsign": "RCH12", "icao24": "ae0001", "estDepartureAirport": "KMCI", "estArrivalAirport": "KDOV", "firstSeen": 9, "lastSeen": 10},
        {"callsign": "N3AB", "icao24": "c5", "estDepartureAirport": "KOJC", "estArrivalAirport": "KICT", "firstSeen": 11, "lastSeen": 12},
    ] + [{"callsign": f"UAL{i}", "icao24": f"d{i}", "estDepartureAirport": "KMCI", "estArrivalAirport": "KORD",
          "firstSeen": 20 + i, "lastSeen": 30 + i} for i in range(5)]
    day = fl.build_day(k, raw)
    every = day["posts"]["all"]["moves"]
    assert not any(m["metro"] == "KSZL" or (m["other"] or {}).get("code") == "SZL" for m in every)   # Whiteman: never
    assert day["posts"]["military"]["moves"] == [] and not any(m["group"] == "military" for m in every)
    airports = day["posts"]["all"]["stats"]["airports"]
    names = [a["name"] for a in airports]
    assert "Downtown" not in names and "Johnson County Exec" not in names   # fewer than 5 movements each
    assert airports[-1] == {"code": "OTHER", "name": "Other fields", "count": 2}


def test_kc_lineup_and_chiefs_game_day(project):
    k = cfg_for(project, "kc")
    sc = formats.day_schedule(k, date(2026, 11, 6))
    assert sc["running"] == ["commercial", "private", "all"]
    assert sc["formats"] == {"commercial": "guess", "all": "scoreboard"}
    # a Chiefs home game on Sun Nov 8 shows on Tuesday's posts (the 2-day lag)
    assert formats.day_schedule(k, date(2026, 11, 10))["formats"]["private"] == "gameday"
    assert "private" not in formats.day_schedule(k, date(2026, 11, 9))["formats"]
    assert not k.slot_active("military", date(2026, 11, 6))
    assert k.slot_active("wrap", date(2026, 11, 8)) and not k.slot_active("rivalry", date(2026, 11, 12))   # off until launch


def test_kc_captions_tags_and_accounts(project):
    k = cfg_for(project, "kc")
    day = fl.build_day(k, fl.demo_raw(date(2026, 11, 4), k))
    st = day["posts"]["commercial"]["stats"]
    text = " ".join(o["text"] for o in captions.build_options(k, "commercial", st, date(2026, 11, 4)))
    assert "Eppley" not in text and "Omaha" not in text and "KCI" in text
    alltext = " ".join(o["text"] for o in captions.build_options(k, "all", day["posts"]["all"]["stats"], date(2026, 11, 4)))
    assert "KCI, Downtown, Johnson County Exec" in alltext and "Offutt" not in alltext
    assert "Gardner" not in alltext.split("Which field")[0] or "other fields" in alltext   # only the fields on the map are named
    assert "nine airfields" in alltext
    tags = [h["tag"] for h in captions.hashtag_choices(k, "commercial", st)]
    assert "#SkyBirdKC" in tags and not any("Omaha" in t or "Eppley" in t for t in tags)
    assert captions.tiktok_priority(k, "commercial")[0] == "#SkyBirdKC"
    handles = [m["handle"] for m in captions.mention_choices(k, "facebook", [], "commercial", st, date(2026, 11, 6))]
    assert "@OMAairport" not in handles                       # never an Omaha account on a KC post
    om = cfg_for(project)
    assert "@OMAairport" in [m["handle"] for m in captions.mention_choices(om, "facebook", [], "commercial", st, date(2026, 11, 6))]
    g = formats.caption_options("guess", {"guess": {"choices": ["Chicago", "Denver", "Dallas"]}}, date(2026, 11, 4),
                                date(2026, 11, 6), k)[0]["text"]
    assert "out of KCI" in g and "Eppley" not in g


def test_omaha_data_moves_into_its_own_folder_once(project):
    data = project / "cloud" / "data"
    (data / "2026-11-04").mkdir(parents=True)
    (data / "2026-11-04" / "state.json").write_text("{}")
    (data / "flights").mkdir()
    (data / "settings.json").write_text("{}")
    (data / "aircraft_cache.json").write_text('{"a1": {"cls": "jet"}}')
    c = cfg_for(project)
    assert (data / "omaha" / "2026-11-04" / "state.json").exists() and (data / "omaha" / "settings.json").exists()
    assert (data / "aircraft_cache.json").exists() and not (data / "omaha" / "aircraft_cache.json").exists()   # shared
    assert not (data / "2026-11-04").exists()
    assert cfg_for(project, "kc").data_dir == data / "kc"    # KC's folder is separate; Omaha's stays put
    cfg_for(project)
    assert (data / "omaha" / "2026-11-04" / "state.json").exists() and (data / "kc").is_dir()
    assert c.shared_dir == data


def test_pages_videos_per_city_and_old_layout_kept(tmp_path, monkeypatch):
    import subprocess
    from skybird.publishers import pages_host as ph

    class S:
        def get(self, url, **kw):
            r = type("R", (), {})(); r.status_code = 206; r.headers = {"Content-Type": "video/mp4"}; r.close = lambda: None
            return r
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    monkeypatch.setenv("SKYBIRD_PAGES_REMOTE", str(bare))
    monkeypatch.setenv("GITHUB_REPOSITORY", "SkybirdTracker-a11y/skybird"); monkeypatch.setenv("GITHUB_TOKEN", "T")
    v = tmp_path / "a.mp4"; v.write_bytes(b"x")
    ph.publish([v], date(2026, 11, 4), S(), wait=5)                          # the old layout (before cities)
    links = ph.publish([v], date(2026, 11, 5), S(), wait=5, city="omaha")
    assert links["a.mp4"].endswith("/videos/omaha/2026-11-05/a.mp4")
    ph.publish([v], date(2026, 11, 5), S(), wait=5, city="kc")
    ph.publish_report("<p>r</p>", date(2026, 11, 5), city="omaha")
    files = subprocess.run(["git", "--git-dir", str(bare), "ls-tree", "-r", "--name-only", "gh-pages"],
                           capture_output=True, text=True).stdout.split()
    assert {"videos/2026-11-04/a.mp4", "videos/omaha/2026-11-05/a.mp4", "videos/kc/2026-11-05/a.mp4",
            "reports/omaha/latest.html", "reports/latest.html"} <= set(files)
    ph.publish([v], date(2026, 11, 8), S(), wait=5, city="kc")               # 3 days later: old days drop off
    files = subprocess.run(["git", "--git-dir", str(bare), "ls-tree", "-r", "--name-only", "gh-pages"],
                           capture_output=True, text=True).stdout.split()
    assert "videos/2026-11-04/a.mp4" not in files and "videos/kc/2026-11-08/a.mp4" in files


def test_kc_day_makes_posts_only_for_kc_platforms(project, monkeypatch):
    """A dry-run KC day: only KC's platforms (TikTok now), no military, no Instagram."""
    from skybird import cloud, notify
    k = cfg_for(project, "kc")
    day = fl.build_day(k, fl.demo_raw(date(2026, 11, 4), k))

    def fake_render(cfg_, post_date, **kw):
        posts = {kk: v["stats"] for kk, v in day["posts"].items()}
        f = cfg_.media_file(post_date, "stats.json"); f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"source": "opensky", "complete": True, "posts": posts}))
        for s in cfg_.slots:
            cfg_.media_file(post_date, s.file).write_bytes(b"x")
        return True
    monkeypatch.setattr(engine, "run_render", fake_render)
    monkeypatch.setattr(notify, "send", lambda *a, **kw: None)
    for key in ("OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET", "BUFFER_API_KEY"):
        monkeypatch.setenv(key, "x")
    called = []
    monkeypatch.setattr(cloud, "connect_buffer", lambda *a: called.append(1))
    out = cloud.run(k, now=datetime(2026, 11, 6, 3, 40, tzinfo=k.tz))
    from skybird.state import load_state
    st = load_state(k, date(2026, 11, 6))
    assert set(st["posts"]) == {"commercial", "private", "all"}
    for post in st["posts"].values():
        assert set(post["results"]) == {"tiktok"} and post["results"]["tiktok"]["id"] == "dry-run"
    assert called == []                                       # dry run: Buffer never contacted
    assert "Guess the destination" in st["posts"]["commercial"]["title"]
    assert len(out["scheduled"]) == 3


def test_kc_events_land_on_the_posts_that_show_those_flights(project):
    """match = "flight_day": a Sunday Chiefs game is on Tuesday's posts (2-day lag), with the
    teaser on the posts before; the game-day private post the same Tuesday."""
    from skybird import events
    k = cfg_for(project, "kc")

    def hooks(d, slot="all"):
        return [(e["name"], e["phase"]) for e in events.events_for(k, d, slot)]
    jets = "Chiefs vs Jets (Arrowhead)"
    assert (jets, "lead_up") in hooks(date(2026, 11, 6)) and (jets, "lead_up") in hooks(date(2026, 11, 8))
    assert not any(n == jets for n, _ in hooks(date(2026, 11, 9)))       # Monday's posts show Saturday
    assert (jets, "during") in hooks(date(2026, 11, 10))                  # Tuesday's posts show Sunday
    assert not any(n == jets for n, _ in hooks(date(2026, 11, 11)))
    assert formats.day_schedule(k, date(2026, 11, 10))["formats"]["private"] == "gameday"
    # a multi-day event covers the posts showing each of its days
    big12 = "Big 12 men's basketball tournament (T-Mobile Center)"
    on = [d for d in (date(2027, 3, 10) + timedelta(days=i) for i in range(9)) if (big12, "during") in hooks(d)]
    assert on[0] == date(2027, 3, 11) and on[-1] == date(2027, 3, 16)
    # Omaha's events keep matching the post date
    om = cfg_for(project)
    ev = events.load_events(om)[0]
    assert ev.get("match") != "flight_day" and any(e["name"] == ev["name"] for e in events.events_for(om, ev["start"]))


def test_kc_event_file_follows_the_brand_rules(project):
    import tomllib
    raw = tomllib.loads((project / "events" / "kc.toml").read_text())["event"]
    assert all(e.get("match") == "flight_day" for e in raw)
    games = [e for e in raw if e.get("gameday")]
    assert all("Chiefs" in e["name"] for e in games) and len(games) == 5
    assert all(e["start"].weekday() in (0, 5, 6) for e in games)          # Sun/Mon (Week 18 may be Sat)
    concerts = [e for e in raw if "concert" in e["name"].lower()]
    assert concerts and all("(T-Mobile Center)" in e["name"] for e in concerts)   # by venue, never the artist


def test_scoreboard_fits_many_fields(project):
    """Kansas City has nine fields: the board shows the 4 busiest and the rest as Other fields."""
    k = cfg_for(project, "kc")
    stats = {"airports": [{"code": c, "name": c, "count": n} for c, n in
                          [("MCI", 300), ("MKC", 40), ("OJC", 30), ("JCI", 25), ("LXT", 12), ("GPH", 9), ("OTHER", 6)]]}
    sb = formats.scoreboard(k, date(2026, 11, 4), stats)
    assert [r["code"] for r in sb["rows"]] == ["MCI", "MKC", "OJC", "JCI", "OTHER"]
    assert sb["rows"][-1]["count"] == 12 + 9 + 6 and sb["total"] == 422
    om = cfg_for(project)                                                  # Omaha's 4 fields: unchanged
    four = {"airports": [{"code": c, "name": c, "count": n} for c, n in [("OMA", 180), ("MLE", 20), ("CBF", 9), ("OFF", 6)]]}
    assert [r["code"] for r in formats.scoreboard(om, date(2026, 11, 4), four)["rows"]] == ["OMA", "MLE", "CBF", "OFF"]
