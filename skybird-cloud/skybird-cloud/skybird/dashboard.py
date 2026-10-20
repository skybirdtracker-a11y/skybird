"""SkyBird control center: approvals, upcoming posts, engagement, history & logs,
settings, plus the built-in scheduler.

    python -m skybird serve      ->  http://127.0.0.1:8787
"""
from __future__ import annotations

import secrets as _secrets
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, render_template_string, request, send_file, session

from . import engine, insights, metrics, phone
from .accounts import cached_accounts, check_accounts
from .config import PLATFORMS, Config
from .events import events_for
from .settings import load_plan, load_settings, save_settings, set_plan
from .state import APPROVED, PENDING, SKIPPED, load_state, log, recent_log

PLATFORM_LABEL = {"instagram": "Instagram", "facebook": "Facebook", "tiktok": "TikTok"}


def _fmt_time(hhmm: str) -> str:
    h, m = map(int, hhmm.split(":"))
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def create_app(cfg: Config) -> Flask:
    app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
    app.secret_key = cfg.secrets.get("dashboard_secret") or _secrets.token_hex(16)
    if not cfg.secrets.get("dashboard_secret"):
        cfg.secrets["dashboard_secret"] = app.secret_key  # keeps phone logins across restarts
        try:
            cfg.save_secrets()
        except OSError:
            pass
    @app.before_request
    def _auth():
        # The PC itself never needs the PIN; any other device does (when phone access is on)
        if request.endpoint in ("login", "touch_icon") or session.get("ok") or phone.is_local(request.remote_addr):
            return None
        if phone.has_pin(cfg):
            return redirect("/login")
        if phone.enabled(cfg):
            abort(403)   # phone access on but no PIN yet: refuse rather than leave it open

    @app.route("/login", methods=["GET", "POST"])
    def login():
        ip, msg = request.remote_addr or "?", ""
        if request.method == "POST":
            wait = phone.locked_out(ip)
            if wait:
                msg = f"Too many wrong PINs. Try again in {wait // 60 + 1} min."
            elif phone.check_pin(cfg, request.form.get("pin", "")):
                session["ok"] = True
                session.permanent = True
                return redirect("/")
            else:
                phone.failed(ip)
                msg = "That PIN isn't right."
        return render_template_string(LOGIN_HTML, msg=msg)

    @app.get("/apple-touch-icon.png")
    def touch_icon():
        return send_file(Path(__file__).parent / "assets" / "apple-touch-icon.png", mimetype="image/png")

    def today() -> date:
        return engine.now_local(cfg).date()

    def _date(s: str | None) -> date:
        return date.fromisoformat(s) if s else today()

    def page(template: str, tab: str, **ctx):
        st = load_state(cfg, today())
        pending = sum(1 for p in (st or {}).get("posts", {}).values() if p["status"] == PENDING)
        return render_template(template, tab=tab, cfg=cfg, brand=cfg.general.get("brand_name", "SkyBird - Omaha"),
                               dry_run=cfg.dry_run, autopost=cfg.autopost, pending_count=pending,
                               platforms=PLATFORMS, label=PLATFORM_LABEL, fmt_time=_fmt_time,
                               backend=engine.backend(cfg), caption_for=engine.caption_for, tiktok_tags=engine.tiktok_tags,
                               rendering=engine.rendering_flag(cfg).exists(),
                               opensky_on=bool(cfg.secrets.get("opensky_client_id")),
                               version=__import__("skybird").VERSION, disk_version=version_on_disk(),
                               install_dir=str(Path(__file__).resolve().parent.parent), **ctx)

    # ---- pages --------------------------------------------------------------
    @app.get("/")
    def index():
        d = _date(request.args.get("date"))
        state = load_state(cfg, d)
        planned = {}
        if state:
            for key, t in engine.planned_times(cfg, state, d, engine.now_local(cfg)).items():
                p = state["posts"][key]
                if p["status"] == APPROVED:
                    sched = datetime.strptime(p["time"], "%H:%M")
                    late = (t.hour, t.minute) != (sched.hour, sched.minute)
                    planned[key] = {"at": _fmt_time(t.strftime("%H:%M")), "catchup": late,
                                    "past_cutoff": t.time() > cfg.last_post_time}
        dry_posts = [k for k, p in (state or {}).get("posts", {}).items() if engine.dry_run_only(p)]
        return page("today.html", "today", d=d, state=state, planned=planned, dry_posts=dry_posts, today_date=today(),
                    can_redo=d == today() and not cfg.dry_run,
                    prev=(d - timedelta(days=1)).isoformat(), next=(d + timedelta(days=1)).isoformat())

    @app.get("/upcoming")
    def upcoming():
        t, plan = today(), load_plan(cfg.data_dir)
        auto = load_settings(cfg.data_dir).get("auto_approve", {})
        days = []
        for i in range(14):
            d = t + timedelta(days=i)
            st = load_state(cfg, d)
            slots = []
            for s in cfg.slots:
                post = (st or {}).get("posts", {}).get(s.key)
                res = ""
                if post and post.get("results"):
                    res = " ".join(f"{PLATFORM_LABEL[p]} {'✓' if r.get('ok') else '✕'}" for p, r in post["results"].items())
                slots.append({"key": s.key, "title": s.title, "time": _fmt_time(s.time.strftime("%H:%M")),
                              "status": post["status"] if post else None, "results": res,
                              "plan": plan.get(d.isoformat(), {}).get(s.key), "auto": auto.get(s.key)})
            days.append({"date": d.isoformat(), "label": f"{d.strftime('%a %b')} {d.day}",
                         "rel": "Today" if i == 0 else "Tomorrow" if i == 1 else "",
                         "is_today": i == 0, "built": st is not None, "slots": slots,
                         "events": events_for(cfg, d)})
        return page("upcoming.html", "upcoming", days=days)

    @app.get("/engagement")
    def engagement():
        return page("engagement.html", "engagement")

    @app.get("/history")
    def history():
        n = 30
        rows, counts = [], {k: 0 for k in ("posted", "partial", "failed", "skipped", "missed")}
        for i in range(n):
            d = today() - timedelta(days=i)
            st = load_state(cfg, d)
            if not st:
                continue
            for key, p in st["posts"].items():
                if p["status"] in counts:
                    counts[p["status"]] += 1
                rows.append({"date": d.isoformat(), "label": f"{d.strftime('%a %b')} {d.day}",
                             "time": _fmt_time(p["time"]), "title": p["title"], "status": p["status"],
                             "results": p.get("results", {})})
        return page("history.html", "history", rows=rows, counts=counts, history_days=n,
                    log_lines=recent_log(cfg, 400))

    @app.get("/settings")
    def settings_page():
        s = load_settings(cfg.data_dir)
        accounts = cached_accounts(cfg)
        mentions = {p: list(cfg.section("mentions").get(p, [])) + list(s.get("mentions", {}).get(p, []))
                    for p in PLATFORMS}
        return page("settings.html", "settings", slots=cfg.slots, auto_approve=s.get("auto_approve", {}),
                    accounts=accounts, any_connected=any(accounts.get(p, {}).get("ok") for p in PLATFORMS),
                    mentions=mentions, tiktok_mode=cfg.section("tiktok").get("mode", "inbox"),
                    port=cfg.section("server").get("port", 8787),
                    notify_on=bool(cfg.section("notify").get("ntfy_topic")),
                    buffer_on=bool(cfg.secrets.get("buffer_api_key")),
                    media_template=(s.get("media_folder") or cfg.section("media").get("folder", "")),
                    media_today=str(cfg.media_folder(today())),
                    media_example=cfg.media_file(today(), "post1_commercial.mp4").name,
                    notify_topic=cfg.section("notify").get("ntfy_topic", ""),
                    tag_accounts=_tag_accounts(), phone_on=phone.enabled(cfg), phone_pin=phone.has_pin(cfg),
                    phone_urls=[dict(u, qr=phone.qr_svg(u["url"])) for u in phone.urls(cfg)] if phone.enabled(cfg) else [],
                    phone_listening=request.host.split(":")[0] not in ("127.0.0.1", "localhost") or phone.host(cfg) == "0.0.0.0")

    def _tag_accounts():
        from .captions import ACCOUNTS, NEWS, account_handle
        yours = load_settings(cfg.data_dir).get("account_handles", {})
        out = []
        for key, a in ACCOUNTS.items():
            use = ("Commercial posts; the 6 PM post only on event days" if key == "eppley" else
                   "6 PM post Tue / Thu / Sat, at most 3x a week" if key == "visitomaha" else
                   "Only big events (Berkshire, CWS, a home opener…): one outlet per day, taking turns" if key in NEWS else
                   "Commercial posts whenever it's a top-2 airline that day" if a.get("airline") else "")
            out.append({"key": key, "name": a["name"], "use": use, "confirmed": bool(a.get("confirmed")),
                        "handles": {p: yours.get(key, {}).get(p) or (a.get(p, "") if a.get("confirmed") else "") for p in PLATFORMS},
                        "likely": {p: a.get(p, "") for p in PLATFORMS},
                        "live": {p: bool(account_handle(cfg, key, p)) for p in PLATFORMS}})
        return out

    @app.get("/video/<d>/<slot>")
    def video(d, slot):
        state = load_state(cfg, _date(d))
        if not state or slot not in state["posts"]:
            abort(404)
        p = Path(state["posts"][slot]["video"])
        if not p.exists():
            abort(404)
        return send_file(p, mimetype="video/mp4", conditional=True,
                         as_attachment=bool(request.args.get("download")),
                         download_name=f"skybird-{d}-{slot}.mp4")

    # ---- API ------------------------------------------------------------------
    def _err(e):
        return jsonify(ok=False, error=str(e)), 400

    @app.get("/api/<d>/state")
    def api_state(d):
        return jsonify(load_state(cfg, _date(d)) or {})

    @app.post("/api/<d>/prepare")
    def api_prepare(d):
        force = bool((request.get_json(silent=True) or {}).get("force"))
        engine.prepare(cfg, _date(d), force=force)
        return jsonify(ok=True)

    @app.post("/api/<d>/<slot>/<action>")
    def api_slot(d, slot, action):
        day, edits = _date(d), (request.get_json(silent=True) or {})
        try:
            if action == "save":
                engine.set_status(cfg, day, slot, "", edits)
            elif action == "approve":
                engine.set_status(cfg, day, slot, APPROVED, edits)
            elif action == "skip":
                engine.set_status(cfg, day, slot, SKIPPED, edits)
            elif action == "reset":
                engine.set_status(cfg, day, slot, PENDING, edits)
            elif action == "post-now":
                engine.set_status(cfg, day, slot, APPROVED, edits)
                threading.Thread(target=_safe_publish, args=(cfg, day, slot), daemon=True).start()
            elif action == "mark-posted":
                if edits.get("platform") not in PLATFORMS:
                    return _err("Unknown platform")
                engine.mark_posted(cfg, day, slot, edits["platform"], edits.get("url", ""))
            elif action == "for-real":
                if not engine.post_for_real(cfg, day, [slot]):
                    return _err("That post wasn't a dry run")
            elif action == "retry":
                threading.Thread(target=_safe_publish, args=(cfg, day, slot), daemon=True).start()
            else:
                abort(404)
        except (ValueError, KeyError) as e:
            return _err(e)
        return jsonify(ok=True)

    @app.post("/api/<d>/approve-all")
    def api_approve_all(d):
        day = _date(d)
        body = request.get_json(silent=True) or {}
        done = []
        for slot, edits in (body.get("posts") or {}).items():
            state = load_state(cfg, day)
            if state and state["posts"].get(slot, {}).get("status") == PENDING:
                engine.set_status(cfg, day, slot, APPROVED, edits)
                done.append(slot)
        return jsonify(ok=True, approved=done)

    @app.post("/api/<d>/for-real")
    def api_for_real(d):
        try:
            return jsonify(ok=True, queued=engine.post_for_real(cfg, _date(d)))
        except ValueError as e:
            return _err(e)

    @app.post("/api/<d>/make-videos")
    def api_make_videos(d):
        day = _date(d)
        demo = bool((request.get_json(silent=True) or {}).get("demo"))
        if engine.rendering_flag(cfg).exists():
            return _err("Videos are already being made. Give it a few minutes.")
        if not demo and not cfg.secrets.get("opensky_client_id") and not (cfg.section("media").get("render_command") or "").strip():
            return _err("Connect OpenSky in Settings first (it's free), or try the sample videos.")
        st = load_state(cfg, day)
        live = [p["title"] for p in (st or {}).get("posts", {}).values() if engine.really_posted(p)]
        if live:
            return _err(f"{', '.join(live)} already went out live, so this day's videos stay as they are.")
        if st and any(p["status"] == "posting" for p in st["posts"].values()):
            return _err("A post is uploading right now. Try again in a minute.")
        engine.rendering_flag(cfg).write_text(day.isoformat())
        log(cfg, f"{day}: {'sample' if demo else 'new'} videos requested from the dashboard")

        def work():
            try:
                engine.rendering_flag(cfg).unlink(missing_ok=True)
                if engine.run_render(cfg, day, demo=demo, force=True) or demo:
                    engine.prepare(cfg, day, force=True, render=False)
            except Exception as e:
                log(cfg, f"Making videos failed: {e}")
        threading.Thread(target=work, daemon=True).start()
        return jsonify(ok=True)

    @app.post("/api/restart")
    def api_restart():
        if engine.rendering_flag(cfg).exists():
            return _err("Videos are being made right now. Restart after they finish.")
        log(cfg, "Restarting SkyBird from the dashboard")
        threading.Thread(target=_restart_self, args=(cfg,), daemon=True).start()
        return jsonify(ok=True)

    @app.post("/api/buffer")
    def api_buffer():
        from .publishers import buffer
        key = str((request.get_json(silent=True) or {}).get("api_key", "")).strip().strip('"')
        if not key:
            return _err("Paste the Buffer API key first.")
        try:
            orgs = buffer.organizations(key)
            if not orgs:
                return _err("Buffer accepted the key but found no account. Log in at buffer.com once, then try again.")
            chs = buffer.channels(key, orgs[0]["id"])
        except Exception as e:
            return _err(f"Buffer didn't accept that: {e}")
        found = buffer.match_channels(chs)
        cfg.secrets.update(buffer_api_key=key, buffer_org_id=orgs[0]["id"])
        for p in buffer.PLATFORMS:
            if p in found:
                cfg.secrets[f"buffer_channel_{p}"] = found[p]["id"]
        cfg.save_secrets()
        save_settings(cfg.data_dir, {"backend": "buffer"})
        check_accounts(cfg)
        log(cfg, "Buffer connected: " + ", ".join(f"{p}={'yes' if p in found else 'missing'}" for p in buffer.PLATFORMS))
        return jsonify(ok=True, channels={p: (found[p].get("name") if p in found else None) for p in buffer.PLATFORMS})

    @app.post("/api/opensky")
    def api_opensky():
        b = request.get_json(silent=True) or {}
        cid, csec = str(b.get("client_id", "")).strip(), str(b.get("client_secret", "")).strip()
        if not cid or not csec:
            return _err("Paste both the client id and the client secret.")
        cfg.secrets.update(opensky_client_id=cid, opensky_client_secret=csec)
        cfg.secrets.pop("_opensky_token", None)
        try:
            from . import flights
            import requests as _rq
            flights._token(cfg, _rq.Session())
        except Exception as e:
            cfg.secrets.pop("opensky_client_id", None), cfg.secrets.pop("opensky_client_secret", None)
            return _err(str(e))
        cfg.save_secrets()
        log(cfg, "OpenSky connected")
        return jsonify(ok=True)

    @app.post("/api/plan")
    def api_plan():
        b = request.get_json(silent=True) or {}
        if b.get("decision") not in ("approve", "skip", None) or b.get("slot") not in [s.key for s in cfg.slots]:
            return _err("Unknown choice")
        d = date.fromisoformat(b["date"])
        if d < today():
            return _err("That day has already passed")
        set_plan(cfg.data_dir, d.isoformat(), b["slot"], b.get("decision"))
        log(cfg, f"{d} {b['slot']}: plan set to {b.get('decision') or 'ask me'}")
        return jsonify(ok=True)

    @app.post("/api/settings")
    def api_settings():
        b = request.get_json(silent=True) or {}
        allowed = {k: v for k, v in b.items() if k in ("autopost", "dry_run", "auto_approve", "times", "mentions", "backend", "media_folder",
                                                       "time_tests", "auto_best_times", "account_handles")}
        if "account_handles" in allowed:
            import re
            from .captions import ACCOUNTS, _blocked
            clean = {}
            for key, per in (allowed["account_handles"] or {}).items():
                if key not in ACCOUNTS or not isinstance(per, dict):
                    continue
                for p, h in per.items():
                    h = str(h or "").strip()
                    if p not in PLATFORMS:
                        continue
                    if h and not h.startswith("@"):
                        h = "@" + h
                    if h and (not re.fullmatch(r"@[A-Za-z0-9._]{1,30}", h) or _blocked(h)):
                        return _err(f"'{h}' doesn't look like a handle")
                    clean.setdefault(key, {})[p] = h
            allowed["account_handles"] = clean
        if "media_folder" in allowed:
            mf = str(allowed["media_folder"] or "").strip().strip('"').replace("\\", "/").rstrip("/")
            allowed["media_folder"] = mf or None
        if "backend" in allowed and allowed["backend"] not in ("buffer", "upload_post", "manual", "direct"):
            return _err("Unknown posting method")
        if "times" in allowed:
            for k, v in allowed["times"].items():
                try:
                    datetime.strptime(v, "%H:%M")
                except (TypeError, ValueError):
                    return _err(f"'{v}' is not a time like 09:00")
        save_settings(cfg.data_dir, allowed)
        log(cfg, "Settings changed: " + ", ".join(f"{k}={v}" for k, v in allowed.items()))
        return jsonify(ok=True)

    @app.post("/api/phone")
    def api_phone():
        b = request.get_json(silent=True) or {}
        pin = str(b.get("pin") or "").strip()
        if pin:
            if not pin.isdigit() or not 4 <= len(pin) <= 8:
                return _err("The PIN must be 4 to 8 digits.")
            phone.set_pin(cfg, pin)
        on = bool(b.get("on"))
        if on and not phone.has_pin(cfg):
            return _err("Choose a PIN first, so nobody else on your Wi-Fi can open SkyBird.")
        was = phone.enabled(cfg)
        save_settings(cfg.data_dir, {"phone_access": on})
        log(cfg, f"Phone access {'on' if on else 'off'}{' (PIN changed)' if pin else ''}")
        return jsonify(ok=True, restart=was != on, urls=phone.urls(cfg))

    @app.post("/api/accounts/check")
    def api_accounts():
        check_accounts(cfg)
        return jsonify(ok=True)

    @app.get("/api/engagement")
    def api_engagement():
        days = max(1, min(365, int(request.args.get("days", 30))))
        plats = [p for p in request.args.get("platforms", "").split(",") if p in PLATFORMS] or None
        data = metrics.summary(cfg, days, plats)
        fetched = [v.get("fetched_at") for v in metrics.load_metrics(cfg).values() if v.get("fetched_at")]
        data["updated"] = datetime.fromisoformat(max(fetched)).strftime("%b %d, %I:%M %p") if fetched else ""
        return jsonify(data)

    @app.get("/api/insights")
    def api_insights():
        return jsonify(insights.report(cfg))

    @app.post("/api/engagement/refresh")
    def api_refresh():
        try:
            metrics.refresh(cfg)
        except Exception as e:
            return _err(e)
        return jsonify(ok=True)

    return app


def version_on_disk() -> str:
    """The version in the files on disk (an update may have replaced them since SkyBird started)."""
    import re
    try:
        txt = (Path(__file__).parent / "__init__.py").read_text(encoding="utf-8")
        m = re.search(r'VERSION\s*=\s*"([^"]+)"', txt)
        return m.group(1) if m else ""
    except OSError:
        return ""


def _restart_self(cfg):
    """Start a fresh SkyBird (which waits for this one to let go of the port), then exit."""
    import os
    import subprocess
    import sys
    time.sleep(0.5)
    exe = sys.executable
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        pyw = Path(exe).with_name("pythonw.exe")
        if pyw.exists():
            exe = str(pyw)
    subprocess.Popen([exe, "-m", "skybird", "serve", "--wait", "4"], cwd=str(cfg.root), creationflags=flags,
                     close_fds=True)
    os._exit(0)


def _safe_publish(cfg, day, slot):
    try:
        engine.publish(cfg, day, slot)
    except Exception as e:
        log(cfg, f"{day} {slot}: publish error {e}")


def scheduler_loop(cfg: Config, every: int = 30) -> None:
    opened_for, last_metrics = None, 0.0
    while True:
        try:
            actions = engine.tick(cfg)
            today = engine.now_local(cfg).date()
            if "prepared" in actions and cfg.section("server").get("open_browser_on_prepare", True) and opened_for != today:
                opened_for = today
                _open_browser(cfg)
            if time.monotonic() - last_metrics > 3 * 3600:
                last_metrics = time.monotonic()
                metrics.refresh(cfg)
            insights.daily(cfg)   # once a day: best-time switches + Monday summary
        except Exception as e:
            log(cfg, f"scheduler error: {e}")
        time.sleep(every)


def _open_browser(cfg: Config) -> None:
    import webbrowser
    srv = cfg.section("server")
    host = srv.get("host", "127.0.0.1")
    webbrowser.open(f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{srv.get('port', 8787)}/")


SKYBIRD_VIDEOS = "C:/Users/joshu/OneDrive/Desktop/Sky Bird/videos"


def _migrate_video_folder(cfg: Config) -> None:
    """One-time: move older setups (Flight Visibility/out/social or a per-day
    subfolder under Sky Bird\videos) to the single Sky Bird\videos folder."""
    cur = cfg._media_template()
    old = ("out/social" in cur) or cur.rstrip("/").endswith("Sky Bird/videos/{data_date}")
    if old and Path(SKYBIRD_VIDEOS).parent.exists():
        save_settings(cfg.data_dir, {"media_folder": SKYBIRD_VIDEOS})
        Path(SKYBIRD_VIDEOS).mkdir(parents=True, exist_ok=True)
        log(cfg, f"Video folder set to {SKYBIRD_VIDEOS}")


def serve(cfg: Config, with_scheduler: bool = True) -> None:
    _migrate_video_folder(cfg)
    srv = cfg.section("server")
    if with_scheduler:
        threading.Thread(target=scheduler_loop, args=(cfg,), daemon=True).start()
    from . import VERSION
    log(cfg, f"SkyBird {VERSION} starting from {Path(__file__).resolve().parent.parent}")
    log(cfg, f"Dashboard on http://{phone.host(cfg)}:{srv.get('port', 8787)} "
             f"(scheduler {'on' if with_scheduler else 'off'}, {'DRY RUN' if cfg.dry_run else 'LIVE'})")
    create_app(cfg).run(host=phone.host(cfg), port=int(srv.get("port", 8787)),
                        debug=False, use_reloader=False, threaded=True)


LOGIN_HTML = """<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=apple-mobile-web-app-capable content=yes><meta name=apple-mobile-web-app-title content=SkyBird>
<link rel=apple-touch-icon href="/apple-touch-icon.png"><title>SkyBird</title>
<style>body{background:#F6F2E7;color:#0A2540;font-family:system-ui,-apple-system,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;padding:16px}
form{background:#fff;border:1px solid #D9DEE5;border-radius:16px;padding:24px;display:flex;flex-direction:column;gap:12px;width:min(100%,320px);text-align:center}
img{width:96px;height:96px;margin:0 auto;border-radius:50%}h1{font:italic 900 26px/1 system-ui;color:#014291;margin:0}
input,button{font:inherit;font-size:20px;padding:12px 16px;border-radius:10px;border:1px solid #D9DEE5;min-height:48px;text-align:center}
input{letter-spacing:6px}button{background:#014291;color:#fff;border:0;font-weight:700}.m{color:#C0392B;font-size:14px;min-height:1em}</style>
<form method=post><img src="/apple-touch-icon.png" alt=""><h1>SkyBird</h1>
<input name=pin type=password inputmode=numeric autocomplete=current-password placeholder=PIN aria-label=PIN autofocus>
<button>Open</button><div class=m role=status>{{ msg }}</div></form></html>"""
