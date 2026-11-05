"""Buffer: each city's own channels only, a daily call budget, posts put into Buffer just in time."""
import json
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from skybird import buffer_ledger, cloud, engine, flights as fl, notify  # noqa: E402
from skybird.config import load_config  # noqa: E402
from skybird.publishers import buffer  # noqa: E402
from skybird.publishers.base import PublishError, PublishResult  # noqa: E402

CHANNELS = [{"id": "a" * 24, "name": "SkyBird - Omaha", "service": "facebook"},
            {"id": "b" * 24, "name": "skybirdomaha", "service": "tiktok"},
            {"id": "c" * 24, "name": "skybirdkc", "service": "tiktok"}]


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "cloud").mkdir()
    shutil.copy(ROOT / "cloud" / "config.toml", tmp_path / "cloud" / "config.toml")
    shutil.copytree(ROOT / "cities", tmp_path / "cities")
    shutil.copytree(ROOT / "events", tmp_path / "events")
    for k in list(__import__("os").environ):
        if k.startswith("BUFFER_CHANNEL_"):
            monkeypatch.delenv(k)
    monkeypatch.setattr(notify, "send", lambda *a, **k: None)
    monkeypatch.setattr(buffer, "LEDGER", None)
    buffer._SHAPE.clear()
    data = tmp_path / "cloud" / "data"
    data.mkdir()
    (data / "buffer_channels.json").write_text(json.dumps(
        {"fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"), "org_id": "o" * 24,
         "channels": CHANNELS}))
    return tmp_path


def cfg_for(project, city="omaha"):
    c = load_config(project / "cloud" / "config.toml", city)
    c.secrets["buffer_api_key"] = "key"
    return c


# ---- channels ------------------------------------------------------------------------

def test_kc_never_uses_an_omaha_channel(project, monkeypatch):
    monkeypatch.setenv("BUFFER_CHANNEL_OMAHA_FACEBOOK", "a" * 24)
    monkeypatch.setenv("BUFFER_CHANNEL_OMAHA_TIKTOK", "b" * 24)
    kc = cfg_for(project, "kc")
    use, notes = cloud.connect_buffer(kc)
    assert use == {} and not any(k.startswith("buffer_channel_") for k in kc.secrets)
    assert any("BUFFER_CHANNEL_KC_TIKTOK" in n for n in notes)            # missing = don't post there
    monkeypatch.setenv("BUFFER_CHANNEL_KC_TIKTOK", "c" * 24)
    use, _ = cloud.connect_buffer(kc)
    assert use == {"tiktok": "c" * 24}                                    # TikTok only, its own channel
    om = cfg_for(project)
    assert cloud.connect_buffer(om)[0] == {"facebook": "a" * 24, "tiktok": "b" * 24}


def test_a_shared_channel_id_is_refused(project, monkeypatch):
    monkeypatch.setenv("BUFFER_CHANNEL_OMAHA_TIKTOK", "b" * 24)
    monkeypatch.setenv("BUFFER_CHANNEL_KC_TIKTOK", "b" * 24)              # the same channel for both cities
    use, notes = cloud.connect_buffer(cfg_for(project, "kc"))
    assert use == {} and any("refused" in n and "omaha tiktok" in n for n in notes)
    use, notes = cloud.connect_buffer(cfg_for(project))
    assert "tiktok" not in use and any("refused" in n for n in notes)
    monkeypatch.setenv("BUFFER_CHANNEL_OMAHA_FACEBOOK", "b" * 24)        # one channel for two platforms
    monkeypatch.setenv("BUFFER_CHANNEL_KC_TIKTOK", "c" * 24)
    use, notes = cloud.connect_buffer(cfg_for(project))
    assert use == {}


def test_wrong_kind_or_unknown_channel_is_not_used(project, monkeypatch):
    monkeypatch.setenv("BUFFER_CHANNEL_OMAHA_FACEBOOK", "c" * 24)        # a TikTok channel
    monkeypatch.setenv("BUFFER_CHANNEL_OMAHA_TIKTOK", "d" * 24)          # not on the account
    asked = []
    monkeypatch.setattr(buffer, "organizations", lambda *a, **k: asked.append(1) or [{"id": "o" * 24}])
    monkeypatch.setattr(buffer, "channels", lambda *a, **k: CHANNELS)
    use, notes = cloud.connect_buffer(cfg_for(project))
    assert use == {} and any("tiktok channel" in n for n in notes) and any("isn't on" in n for n in notes)


def test_omaha_lookup_by_kind_until_its_secrets_exist_never_takes_kcs(project, monkeypatch):
    monkeypatch.setenv("BUFFER_CHANNEL_KC_TIKTOK", "c" * 24)
    chans = [CHANNELS[2], CHANNELS[0], CHANNELS[1]]                       # KC's TikTok listed first
    (project / "cloud" / "data" / "buffer_channels.json").write_text(json.dumps(
        {"fetched_at": datetime.now().astimezone().isoformat(), "org_id": "o" * 24, "channels": chans}))
    use, notes = cloud.connect_buffer(cfg_for(project))
    assert use == {"facebook": "a" * 24, "tiktok": "b" * 24}
    assert any("BUFFER_CHANNEL_OMAHA" in n for n in notes)
    kc_profile = project / "cities" / "kc.toml"                            # KC has no such fallback
    assert "buffer_lookup_fallback" not in kc_profile.read_text()


def test_channels_are_looked_up_once_and_kept(project, monkeypatch):
    (project / "cloud" / "data" / "buffer_channels.json").unlink()
    calls = []
    monkeypatch.setattr(buffer, "organizations", lambda *a, **k: calls.append("orgs") or [{"id": "o" * 24}])
    monkeypatch.setattr(buffer, "channels", lambda *a, **k: calls.append("channels") or CHANNELS)
    monkeypatch.setenv("BUFFER_CHANNEL_OMAHA_TIKTOK", "b" * 24)
    for _ in range(3):
        cloud.connect_buffer(cfg_for(project))
    assert calls == ["orgs", "channels"]


# ---- the call budget ---------------------------------------------------------------------

class Resp:
    def __init__(self, data, headers=None, status=200, errors=None):
        self.status_code, self.headers = status, headers or {}
        self._b = {"data": data, **({"errors": errors} if errors else {})}
        self.text = json.dumps(self._b)

    def json(self):
        return self._b


class Sess:
    def __init__(self, headers=None):
        self.calls, self.headers = [], headers or {}

    def post(self, url, json=None, **kw):
        self.calls.append(json["query"])
        return Resp({"ok": 1}, self.headers)


def test_sixty_calls_a_day_with_the_last_twenty_kept_for_scheduling(project):
    c = cfg_for(project)
    lg = buffer_ledger.use(c)
    s = Sess()
    with buffer.purpose("check"):
        for _ in range(40):
            buffer.gql("key", "query { a }", session=s)
        with pytest.raises(PublishError, match="kept for scheduling"):
            buffer.gql("key", "query { a }", session=s)
    with buffer.purpose("post"):
        for _ in range(20):
            buffer.gql("key", "mutation { a }", session=s)
        with pytest.raises(PublishError, match="60 calls"):
            buffer.gql("key", "mutation { a }", session=s)
    assert len(s.calls) == 60 and lg.total() == 60
    k = buffer_ledger.use(cfg_for(project, "kc"))                          # shared by every city
    with pytest.raises(PublishError):
        with buffer.purpose("post"):
            buffer.gql("key", "mutation { a }", session=s)
    assert k.day()["cities"]["omaha"] == {"check": 40, "post": 20}


def test_backs_off_under_20_percent_of_a_rate_window(project):
    lg = buffer_ledger.use(cfg_for(project))
    s = Sess({"X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "15", "X-RateLimit-Reset": "600"})
    buffer.gql("key", "query { a }", session=s)
    assert lg.backing_off()
    with pytest.raises(PublishError, match="rate window"):
        with buffer.purpose("metrics"):
            buffer.gql("key", "query { a }", session=s)
    with buffer.purpose("post"):                                           # scheduling still goes ahead
        buffer.gql("key", "mutation { a }", session=s)
    later = buffer_ledger.BufferLedger(lg.cfg, now=datetime.now(timezone.utc) + timedelta(minutes=11))
    assert not later.backing_off()
    ext = buffer.rate_windows({}, {"extensions": {"rateLimit": [{"limit": 50, "remaining": 40, "resetSeconds": 30}]}})
    assert ext == [{"limit": 50, "remaining": 40, "reset_seconds": 30}]


def test_backs_off_when_buffer_gives_no_window_size(project):
    """Buffer's rateLimit entries carry remaining + resetSeconds only: the size is the most seen."""
    lg = buffer_ledger.use(cfg_for(project))

    def answer(rem):
        class S(Sess):
            def post(self, url, json=None, **kw):
                r = Resp({"ok": 1})
                r._b["extensions"] = {"rateLimit": [{"remaining": rem, "resetSeconds": 900},
                                                    {"remaining": 2000, "resetSeconds": 1000000}]}
                return r
        return S()
    buffer.gql("key", "query { a }", session=answer(149))
    assert not lg.backing_off()
    buffer.gql("key", "query { a }", session=answer(25))                  # 25 of ~149 left: under 20%
    assert lg.backing_off()


def test_numbers_for_many_posts_in_one_call(project):
    buffer_ledger.use(cfg_for(project))
    buffer._SHAPE["post"] = {"arg": "input: {id: %s}", "fields": "id status", "metrics": "metrics { type value }"}

    class S(Sess):
        def post(self, url, json=None, **kw):
            self.calls.append(json["query"])
            return Resp({"p0": {"id": "x", "metrics": [{"type": "views", "value": 900}]}, "p1": None,
                         "p2": {"id": "z", "metrics": [{"type": "views", "value": 50}, {"type": "likes", "value": 3}]}},
                        errors=[{"message": "Post not found", "path": ["p1"]}])
    s = S()
    got, gone, problems = buffer.many_metrics("key", ["x", "y", "z"], session=s)
    assert len(s.calls) == 1 and got == {"x": {"views": 900}, "z": {"views": 50, "likes": 3}}
    assert gone == {"y"} and problems == []


# ---- just in time ------------------------------------------------------------------------

def make_day(project, monkeypatch, city="omaha"):
    c = cfg_for(project, city)
    day = fl.build_day(c, fl.demo_raw(date(2026, 11, 4), c))

    def fake_render(cfg_, post_date, **kw):
        f = cfg_.media_file(post_date, "stats.json"); f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"source": "opensky", "complete": True,
                                 "posts": {k: v["stats"] for k, v in day["posts"].items()}}))
        for s in cfg_.slots:
            cfg_.media_file(post_date, s.file).write_bytes(b"x")
        return True
    monkeypatch.setattr(engine, "run_render", fake_render)
    for key in ("OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET", "BUFFER_API_KEY"):
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("BUFFER_CHANNEL_OMAHA_FACEBOOK", "a" * 24)
    monkeypatch.setenv("BUFFER_CHANNEL_OMAHA_TIKTOK", "b" * 24)
    monkeypatch.setattr(cloud, "snapshot_board", lambda *a, **k: 0)
    monkeypatch.setattr(cloud, "compare_with_board", lambda *a, **k: "")
    from skybird.publishers import pages_host
    monkeypatch.setattr(pages_host, "publish", lambda files, d, **k: {f.name: f"https://pages/{f.name}" for f in files})
    sent = []

    def fake_post(video, caps, secrets, due=None, video_url=None, **k):
        sent.append((due, video_url, {p: secrets[f"buffer_channel_{p}"] for p in caps}))
        return {p: PublishResult(p, True, f"id{len(sent)}{p}", "", "Scheduled") for p in caps}
    monkeypatch.setattr(buffer, "post", fake_post)
    import skybird.report as rp
    monkeypatch.setattr(rp, "daily_numbers", lambda *a, **k: (0, []))
    return c, sent


def test_posts_go_into_buffer_about_two_hours_ahead(project, monkeypatch):
    c, sent = make_day(project, monkeypatch)
    tz = c.tz
    out = cloud.run(c, now=datetime(2026, 11, 6, 3, 40, tzinfo=tz))
    assert sent == [] and len(out["planned"]) == 3                         # nothing in Buffer at 3:40 AM
    cloud.check_posts(c, now=datetime(2026, 11, 6, 6, 35, tzinfo=tz))
    assert len(sent) == 1 and sent[0][0].hour == 9                         # the 9 AM post, ~2.5 h ahead
    assert sent[0][1].startswith("https://pages/")                         # from its GitHub Pages link
    assert sent[0][2] == {"facebook": "a" * 24, "tiktok": "b" * 24}
    cloud.check_posts(c, now=datetime(2026, 11, 6, 7, 35, tzinfo=tz))
    assert len(sent) == 1                                                  # not twice
    cloud.check_posts(c, now=datetime(2026, 11, 6, 8, 35, tzinfo=tz))
    assert [d.hour for d, *_ in sent] == [9, 11]
    assert cloud.waiting_in_buffer(c, datetime(2026, 11, 6, 8, 40, tzinfo=tz)) == 4   # 2 posts x 2 platforms
    cloud.check_posts(c, now=datetime(2026, 11, 6, 16, 5, tzinfo=tz))
    assert [d.hour for d, *_ in sent] == [9, 11, 18]


def test_never_more_than_ten_waiting_in_buffer(project, monkeypatch):
    c, sent = make_day(project, monkeypatch)
    tz = c.tz
    cloud.run(c, now=datetime(2026, 11, 6, 3, 40, tzinfo=tz))
    path = c.shared_dir / "buffer_queue.json"
    path.write_text(json.dumps([{"id": f"k{i}", "city": "kc", "platform": "tiktok",
                                 "due": "2026-11-06T12:00:00-06:00"} for i in range(9)]))
    lines = cloud.check_posts(c, now=datetime(2026, 11, 6, 6, 35, tzinfo=tz))
    assert sent == [] and any("waiting posts" in ln for ln in lines)     # 9 + 2 would be 11
    path.write_text(json.dumps([]))
    cloud.check_posts(c, now=datetime(2026, 11, 6, 6, 40, tzinfo=tz))
    assert len(sent) == 1


def test_a_late_check_run_still_gets_the_post_out(project, monkeypatch):
    c, sent = make_day(project, monkeypatch)
    tz = c.tz
    cloud.run(c, now=datetime(2026, 11, 6, 3, 40, tzinfo=tz))
    cloud.check_posts(c, now=datetime(2026, 11, 6, 9, 30, tzinfo=tz))     # GitHub ran 3 hours late
    assert [(d.hour, d.minute) for d, *_ in sent] == [(9, 40), (11, 0)]
    cloud.check_posts(c, now=datetime(2026, 11, 6, 23, 0, tzinfo=tz))     # after the last post time
    assert len(sent) == 2
    from skybird.state import load_state
    assert load_state(c, date(2026, 11, 6))["posts"]["all"].get("jit_skipped")


def test_kc_dry_run_never_contacts_buffer(project, monkeypatch):
    c, sent = make_day(project, monkeypatch, "kc")
    monkeypatch.setenv("BUFFER_CHANNEL_KC_TIKTOK", "c" * 24)
    monkeypatch.setattr(cloud, "connect_buffer", lambda *a, **k: pytest.fail("Buffer contacted in a dry run"))
    out = cloud.run(c, now=datetime(2026, 11, 6, 3, 40, tzinfo=c.tz))
    cloud.check_posts(c, now=datetime(2026, 11, 6, 8, 35, tzinfo=c.tz))
    assert sent == [] and len(out["scheduled"]) == 3
