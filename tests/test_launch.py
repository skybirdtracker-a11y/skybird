"""Kansas City's quiet build (sample page, KCI traffic check) and the day-30 launch review."""
import json
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from skybird import cloud, engine, flights as fl, notify, report  # noqa: E402
from skybird.config import load_config  # noqa: E402


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "cloud").mkdir()
    shutil.copy(ROOT / "cloud" / "config.toml", tmp_path / "cloud" / "config.toml")
    shutil.copytree(ROOT / "cities", tmp_path / "cities")
    shutil.copytree(ROOT / "events", tmp_path / "events")
    monkeypatch.setattr(notify, "send", lambda *a, **k: None)
    return tmp_path


def kc(project):
    return load_config(project / "cloud" / "config.toml", "kc")


def test_kc_runs_in_every_workflow_and_stays_quiet(project):
    for f in ("skybird-daily.yml", "skybird-check.yml", "skybird-weekly.yml"):
        assert "city: [omaha, kc]" in (ROOT / ".github" / "workflows" / f).read_text()
    k, om = kc(project), load_config(project / "cloud" / "config.toml")
    assert k.dry_run and not k.city.get("launch_date")
    times = {s.key: s.time for s in k.slots if not s.occasional}
    omaha = {s.key: s.time for s in om.slots}
    daily = [s for s in ("commercial", "private", "all") if k.slot_active(s, date(2026, 11, 10))]
    assert len(daily) == 3
    for key in daily:                                                      # 15 minutes after Omaha's
        a, b = times[key], omaha[key]
        assert (a.hour * 60 + a.minute) - (b.hour * 60 + b.minute) == 15


def day_counts(k, n, moves):
    path = k.data_dir / "post_counts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    start = date(2026, 11, 6)
    path.write_text(json.dumps({(start + timedelta(days=i)).isoformat():
                                {"data_date": (start + timedelta(days=i - 2)).isoformat(), "hub_moves": moves,
                                 "airline_departures": 120, "airline_arrivals": 118, "complete": True}
                                for i in range(n)}))


def test_traffic_check_against_kcis_own_numbers(project):
    k = kc(project)
    assert report.traffic_check(k) == ""                                   # no days yet
    day_counts(k, 5, 290)                                                  # KCI: 9,750 a month = ~321 a day
    line = report.traffic_check(k)
    assert "90%" in line and "in line" in line and "321" in line
    day_counts(k, 5, 150)
    assert "LOW" in report.traffic_check(k)
    om = load_config(project / "cloud" / "config.toml")
    assert report.traffic_check(om) == ""                                  # Omaha has no reference set


def test_record_day_keeps_the_hub_count(project):
    k = kc(project)
    day = fl.build_day(k, fl.demo_raw(date(2026, 11, 4), k))
    f = k.media_file(date(2026, 11, 6), "stats.json"); f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"complete": True, "data_date": "2026-11-04", "hub": {"departures": 9, "arrivals": 8},
                             "posts": {kk: v["stats"] for kk, v in day["posts"].items()}}))
    row = report.record_day(k, date(2026, 11, 6))
    kci = next(a["count"] for a in day["posts"]["all"]["stats"]["airports"] if a["name"] == "KCI")
    assert row["hub_moves"] == kci and row["airline_departures"] == 9
    assert report.daily_counts(k)["2026-11-06"]["data_date"] == "2026-11-04"
    # the per-flight-day history (trend lines, scoreboard, Offutt) lives in another file and is untouched
    hist = k.data_dir / "daily_counts.json"
    hist.write_text(json.dumps({"2026-11-06": {"private": {"jets": 4, "small": 2}, "fields": {"MCI": 9}}}))
    report.record_day(k, date(2026, 11, 6))
    assert json.loads(hist.read_text())["2026-11-06"]["private"] == {"jets": 4, "small": 2}


def test_quiet_build_publishes_videos_and_sample_page(project, monkeypatch):
    k = kc(project)
    day = fl.build_day(k, fl.demo_raw(date(2026, 11, 4), k))

    def fake_render(cfg_, post_date, **kw):
        f = cfg_.media_file(post_date, "stats.json"); f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"source": "opensky", "complete": True, "hub": {"departures": 100, "arrivals": 99},
                                 "posts": {kk: v["stats"] for kk, v in day["posts"].items()}}))
        for s in cfg_.slots:
            cfg_.media_file(post_date, s.file).write_bytes(b"x")
        return True
    monkeypatch.setattr(engine, "run_render", fake_render)
    for key in ("OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET", "BUFFER_API_KEY"):
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "skybirdtracker-a11y/skybird")
    from skybird.publishers import pages_host
    got = {}
    monkeypatch.setattr(pages_host, "publish", lambda files, d, **kw: got.update(kw, files=files) or {})
    monkeypatch.setattr(cloud, "connect_buffer", lambda *a, **k: pytest.fail("Buffer contacted in the quiet build"))
    cloud.run(k, now=datetime(2026, 11, 6, 3, 40, tzinfo=k.tz))
    assert got["city"] == "kc" and got["keep_days"] == 7 and len(got["files"]) == 3
    page = got["pages"]["reports/kc/sample.html"]
    assert "quiet-build sample" in page and "Day 1 of 14" in page and "2026-11-06" in page
    assert "<a href='https://skybirdtracker-a11y.github.io/skybird/videos/kc/2026-11-06/" in page
    assert "9:15 AM" in page and "6:15 PM" in page and "traffic check" in page
    got.clear()                                                            # a late run still shows all 3
    cloud.run(k, now=datetime(2026, 11, 7, 21, 0, tzinfo=k.tz))
    assert len(got["files"]) == 3


def test_pages_keep_a_week_of_kc_but_three_days_of_omaha(tmp_path, monkeypatch):
    from skybird.publishers import pages_host as ph
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    monkeypatch.setenv("SKYBIRD_PAGES_REMOTE", str(bare))
    monkeypatch.setenv("GITHUB_REPOSITORY", "SkybirdTracker-a11y/skybird"); monkeypatch.setenv("GITHUB_TOKEN", "T")
    v = tmp_path / "a.mp4"; v.write_bytes(b"x")
    for i in range(8):
        d = date(2026, 11, 6) + timedelta(days=i)
        ph.publish([v], d, wait=0, city="omaha")
        ph.publish([v], d, wait=0, city="kc", keep_days=7, pages={"reports/kc/sample.html": "<p>s</p>"})
    files = subprocess.run(["git", "--git-dir", str(bare), "ls-tree", "-r", "--name-only", "gh-pages"],
                           capture_output=True, text=True).stdout.split()
    kc_days = {f.split("/")[2] for f in files if f.startswith("videos/kc/") and f.endswith(".mp4")}
    om_days = {f.split("/")[2] for f in files if f.startswith("videos/omaha/") and f.endswith(".mp4")}
    assert len(kc_days) == 7 and len(om_days) == 3 and "reports/kc/sample.html" in files


def rows(launch, spec):
    """spec: [(days after launch, platform, views)]"""
    return [{"day": launch + timedelta(days=d), "platform": p, "views": v} for d, p, v in spec]


def test_day_30_review(project):
    k = kc(project)
    launch = date(2026, 12, 1)
    k.city["launch_date"] = launch.isoformat()
    now = datetime(2026, 12, 31, 8, 0, tzinfo=k.tz)                       # day 31
    good = [(d, "tiktok", 200 + 40 * d) for d in range(28)] + [(27, "tiktok", 3500)] + \
           [(d, "facebook", 120) for d in range(28)]
    text = "\n".join(report.launch_review(k, rows(launch, good), now))
    assert "Day 31 of 30: the review." in text and "(grew week over week)" in text
    assert "| Tiktok |" in text and "keep" in text
    assert "Facebook: " in text and "not met" in text                     # 120 a post is far below 1,400 a day
    assert "Did not meet the bar" in text and "(final)" in text
    fb_line = next(ln for ln in text.splitlines() if ln.startswith("| Facebook"))
    assert fb_line.endswith("drop |")                                      # under 25% of TikTok and under 150
    tiktok_only = [x for x in good if x[1] == "tiktok"]
    assert "**Success** (final)." in "\n".join(report.launch_review(k, rows(launch, tiktok_only), now))
    flat = [(d, "tiktok", 500) for d in range(28)] + [(27, "tiktok", 4000)] + [(14, "tiktok", 9000)]
    assert "did not grow last week" in "\n".join(report.launch_review(k, rows(launch, flat), now))
    early = "\n".join(report.launch_review(k, rows(launch, tiktok_only[:10]), datetime(2026, 12, 11, tzinfo=k.tz)))
    assert "Day 11 of 30: so far" in early and "(so far)" in early


def test_keep_rule_150_views_or_a_quarter_of_the_best(project):
    k = kc(project)
    launch = date(2026, 12, 1)
    k.city["launch_date"] = launch.isoformat()
    now = datetime(2026, 12, 31, tzinfo=k.tz)
    spec = [(d, "tiktok", 1000) for d in range(14)] + [(d, "facebook", 160) for d in range(14)] + \
           [(d, "instagram", 100) for d in range(14)]
    lines = report.launch_review(k, rows(launch, spec), now)
    verdict = {ln.split("|")[1].strip(): ln.split("|")[5].strip() for ln in lines if ln.startswith("| ") and "Channel" not in ln}
    assert verdict == {"Tiktok": "keep", "Facebook": "keep", "Instagram": "drop"}   # 160 >= 150; 100 < 150 and < 25%
