"""The OpenSky ledger: daily caps per city and overall, OpenSky's own count, 429s."""
import json
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from skybird import budget, flights as fl  # noqa: E402
from skybird.config import load_config  # noqa: E402

NOW = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "cloud").mkdir()
    shutil.copy(ROOT / "cloud" / "config.toml", tmp_path / "cloud" / "config.toml")
    shutil.copytree(ROOT / "cities", tmp_path / "cities")
    shutil.copytree(ROOT / "events", tmp_path / "events")
    return tmp_path


def ledger(project, city="omaha"):
    return budget.Ledger(load_config(project / "cloud" / "config.toml", city), now=NOW)


def fill(lg, city, cat, n):
    d = lg.day()
    d["cities"].setdefault(city, {c: 0 for c in budget.CATEGORIES})[cat] += n
    d["total"] += n
    lg.save()


def test_city_cap_blocks_request_1601(project, monkeypatch):
    monkeypatch.setattr("skybird.notify.send", lambda *a, **k: None)
    lg = ledger(project, "kc")
    ledger(project, "omaha").lists_done()
    fill(lg, "kc", "lists", 40)
    fill(lg, "kc", "types", 400)
    fill(lg, "kc", "backfill", 60)
    # the categories add up to 1,100 (the other 500 is unassigned headroom), so raise
    # fill-in's own cap to show the city cap on its own
    lg.caps["fill_in"] = 1200
    fill(lg, "kc", "fill_in", 1099)
    lg.spend("fill_in")                                    # request 1,600: allowed
    assert lg.used("kc") == 1600
    with pytest.raises(budget.BudgetError, match="Kansas City's daily OpenSky limit"):
        lg.spend("fill_in")
    monkeypatch.setenv("SKYBIRD_USE_RESERVE", "1")        # a manual run may use the shared reserve
    lg.spend("fill_in")
    assert lg.used("kc") == 1601


def test_total_cap_blocks_request_4000(project, monkeypatch):
    monkeypatch.setattr("skybird.notify.send", lambda *a, **k: None)
    monkeypatch.setenv("SKYBIRD_USE_RESERVE", "1")
    lg = ledger(project)
    d = lg.day()
    d["total"] = 3998
    lg.save()
    lg.spend("lists")                                      # request 3,999: allowed
    assert lg.total() == 3999
    with pytest.raises(budget.BudgetError, match="3999"):
        lg.spend("lists")                                  # request 4,000: never
    cfg = load_config(project / "cloud" / "config.toml")
    cfg.raw["opensky_budget"]["total"] = 9999              # a config typo can't lift it past the account
    assert budget.Ledger(cfg, now=NOW).caps["total"] == 3999


def test_category_caps_and_lists_kept_for_the_other_city(project, monkeypatch):
    monkeypatch.setattr("skybird.notify.send", lambda *a, **k: None)
    om = ledger(project)
    fill(om, "omaha", "types", 400)
    with pytest.raises(budget.BudgetError, match="aircraft types limit"):
        om.spend("types")
    om.spend("lists")                                      # lists still fine
    d = om.day(); d["total"] = 3999 - 40; om.save()        # nearly out, and KC hasn't pulled its lists
    with pytest.raises(budget.BudgetError, match="kept back for kc"):
        om.spend("fill_in")
    kc = ledger(project, "kc")
    kc.spend("lists")                                      # KC's core lists still go through


def test_low_remaining_header_stops_lookups(project, monkeypatch):
    monkeypatch.setattr("skybird.notify.send", lambda *a, **k: None)
    lg = ledger(project)
    lg.note_headers({"X-Rate-Limit-Remaining": "350"})
    for cat in ("types", "fill_in"):
        with pytest.raises(budget.BudgetError, match="350"):
            lg.spend(cat)
    lg.spend("lists")
    lg.note_headers({"X-Rate-Limit-Remaining": "80"})
    lg.spend("lists")                                      # Omaha doesn't have its day yet: lists only
    with pytest.raises(budget.BudgetError):
        lg.spend("backfill")
    lg.lists_done()
    with pytest.raises(budget.BudgetError, match="80"):
        lg.spend("lists")                                  # it has its day: nothing more under 100


def test_429_stops_the_day_and_lines_up_the_reset(project, monkeypatch):
    monkeypatch.setattr("skybird.notify.send", lambda *a, **k: None)
    lg = ledger(project)
    lg.spend("lists")
    lg.note_429(str(5 * 3600))                             # OpenSky resets in 5 hours: 14:00 UTC
    with pytest.raises(budget.BudgetError, match="429"):
        lg.spend("lists")
    assert lg.data["_reset_minute_utc"] == 14 * 60
    before = budget.Ledger(lg.cfg, now=datetime(2026, 11, 6, 13, 30, tzinfo=timezone.utc))
    assert before.day_key() == "2026-11-05" and before.day().get("blocked") and before.used() == 1
    after = budget.Ledger(lg.cfg, now=datetime(2026, 11, 6, 14, 30, tzinfo=timezone.utc))
    assert after.day_key() == "2026-11-06" and not after.day().get("blocked") and after.used() == 0
    after.spend("lists")                                   # a new OpenSky day: requests work again


class FakeResp:
    def __init__(self, body, status=200, headers=None):
        self._b, self.status_code, self.headers = body, status, headers or {}
        self.text = json.dumps(body)

    def json(self):
        return self._b


def test_a_429_falls_back_to_the_saved_flight_list(project, monkeypatch):
    from skybird import render
    monkeypatch.setattr("skybird.notify.send", lambda *a, **k: None)
    cfg = load_config(project / "cloud" / "config.toml")
    cfg.secrets.update(opensky_client_id="id", opensky_client_secret="s")
    flight_day = date(2026, 11, 4)
    master = cfg.data_dir / "flights" / f"{flight_day}.json"
    master.parent.mkdir(parents=True)
    saved = fl.demo_raw(flight_day, cfg)
    master.write_text(json.dumps({"flights": saved, "fetched_at": 1}))   # saved, but not "complete"

    class Session:
        def post(self, url, **kw):
            return FakeResp({"access_token": "t", "expires_in": 1800})

        def get(self, url, **kw):
            return FakeResp({}, 429, {"X-Rate-Limit-Retry-After-Seconds": "3600"})
    monkeypatch.setattr("requests.Session", Session)
    made = []
    monkeypatch.setattr(render.Reel, "render", lambda self, out: (made.append(self.slot), out.write_bytes(b"x")))
    render.render_day(cfg, date(2026, 11, 6))
    st = json.loads(cfg.media_file(date(2026, 11, 6), "stats.json").read_text())
    assert st["posts"]["all"]["flights"] > 100 and made                 # made from the saved list
    lg = budget.Ledger(cfg)
    assert lg.day().get("blocked")                                      # and nothing more today


def test_kc_simulated_day_stays_under_its_caps(project, monkeypatch):
    """A whole KC day against a fake OpenSky: lists, fill-in and type lookups all counted."""
    from skybird import render
    monkeypatch.setattr("skybird.notify.send", lambda *a, **k: None)
    k = load_config(project / "cloud" / "config.toml", "kc")
    k.secrets.update(opensky_client_id="id", opensky_client_secret="s")
    flight_day = date(2026, 11, 4)
    raw = fl.demo_raw(flight_day, k)
    arrivals_missing = [f for f in raw if f["estArrivalAirport"] != "KMCI"]   # most arrivals missing: fill-in runs
    asked = []

    class Session:
        def post(self, url, **kw):
            return FakeResp({"access_token": "t", "expires_in": 1800})

        def get(self, url, **kw):
            asked.append(url)
            if "/flights/departure" in url:
                return FakeResp([f for f in arrivals_missing if f["estDepartureAirport"] == kw["params"]["airport"]],
                                headers={"X-Rate-Limit-Remaining": str(4000 - len(asked))})
            if "/flights/arrival" in url:
                return FakeResp([], 404)
            if "/flights/aircraft" in url:
                return FakeResp([], 404)
            return FakeResp({"icaoAircraftClass": "L2J", "model": "Citation"})
    monkeypatch.setattr("requests.Session", Session)
    monkeypatch.setattr(render.Reel, "render", lambda self, out: out.write_bytes(b"x"))
    render.render_day(k, date(2026, 11, 6))
    lg = budget.Ledger(k)
    mine = lg.day()["cities"]["kc"]
    assert 0 < sum(mine.values()) < 1600
    assert all(mine[c] <= lg.caps[c] for c in budget.CATEGORIES)
    assert mine["lists"] <= 40 and mine["fill_in"] > 0 and mine["types"] > 0
    assert "kc" in lg.day()["lists_done"]
    assert not any("KSZL" in u or "KOFF" in u for u in asked)          # never asks for Whiteman or Offutt


def test_credits_are_measured_from_opensky_count(project, monkeypatch):
    """OpenSky's allowance is in credits; a request can cost several. The drops in its
    remaining count are kept per city next to the request count."""
    monkeypatch.setattr("skybird.notify.send", lambda *a, **k: None)
    om, kc = ledger(project), ledger(project, "kc")
    om.note_headers({"X-Rate-Limit-Remaining": "4000"})
    om.note_headers({"X-Rate-Limit-Remaining": "3970"})                 # Omaha: 30 credits
    kc.note_headers({"X-Rate-Limit-Remaining": "3900"})                 # KC: 70 credits
    kc.note_headers({"X-Rate-Limit-Remaining": "3880"})
    assert om.credits() == 30 and kc.credits() == 90 and kc.day()["remaining"] == 3880
    kc.note_headers({"X-Rate-Limit-Remaining": "3999"})                 # OpenSky reset: not a use
    assert kc.credits() == 90
