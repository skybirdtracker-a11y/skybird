"""SkyBird auto-poster.

  python -m skybird serve                 dashboard + scheduler (leave running)
  python -m skybird prepare [--date D]    build a day's approval package now
  python -m skybird approve [--date D]    approve every pending post (no edits)
  python -m skybird post SLOT [--date D]  post one slot now (commercial/private/military/all)
  python -m skybird tick                  one scheduler pass (for cron / Task Scheduler)
  python -m skybird status [--date D]
  python -m skybird meta-setup            connect Facebook Page + Instagram
  python -m skybird tiktok-auth           connect TikTok
  python -m skybird check                 verify tokens / accounts
  python -m skybird cloud-run             GitHub Actions: make the videos + schedule them in Buffer
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
import urllib.parse
from datetime import date

from . import engine
from .config import load_config
from .state import APPROVED, PENDING, load_state


def _date(cfg, s):
    return date.fromisoformat(s) if s else engine.now_local(cfg).date()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="skybird", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve"); p.add_argument("--no-scheduler", action="store_true")
    p.add_argument("--wait", type=float, default=0, help="seconds to wait first (used by Restart)")
    p = sub.add_parser("prepare"); p.add_argument("--date"); p.add_argument("--force", action="store_true")
    p.add_argument("--no-render", action="store_true")
    p = sub.add_parser("approve"); p.add_argument("--date")
    p = sub.add_parser("post"); p.add_argument("slot"); p.add_argument("--date")
    p.add_argument("--force", action="store_true", help="post even if not approved")
    sub.add_parser("tick")
    p = sub.add_parser("status"); p.add_argument("--date")
    p = sub.add_parser("meta-setup"); p.add_argument("--page-name")
    sub.add_parser("tiktok-auth")
    sub.add_parser("uploadpost-setup")
    sub.add_parser("buffer-setup")
    p = sub.add_parser("render"); p.add_argument("--date", help="post date (the video shows the day before)")
    p.add_argument("--demo", action="store_true", help="sample flights, for previewing only")
    sub.add_parser("opensky-setup")
    p = sub.add_parser("recreate", help="remake a day's videos and rebuild its posts"); p.add_argument("--date")
    sub.add_parser("check")
    p = sub.add_parser("cloud-run", help="GitHub Actions: make the videos and schedule them in Buffer")
    p.add_argument("--force", action="store_true", help="run even if today was already scheduled")
    p.add_argument("--dry-run", action="store_true", help="make everything but schedule nothing")
    p.add_argument("--only", default="", help="remake + schedule just these posts, e.g. all or commercial,private")
    p.add_argument("--day", default="", help="post date to remake (YYYY-MM-DD; default today = yesterday's flights)")
    p.add_argument("--at", default="", help="post time HH:MM for --only posts (default: their usual time)")
    p.add_argument("--check-hosts", action="store_true", help="only test which video hosts accept uploads")
    p.add_argument("--test-notify", action="store_true", help="only send a test phone notification")
    p.add_argument("--check-posts", action="store_true", help="only ask Buffer whether today's posts went out")
    p.add_argument("--probe-board", action="store_true", help="only look at Eppley's arrivals/departures page")
    p.add_argument("--fresh-data", action="store_true", help="pull the flights from OpenSky again even if today's are saved")
    a = ap.parse_args(argv)
    if a.cmd == "cloud-run" and a.dry_run:
        import os
        os.environ["SKYBIRD_DRY_RUN"] = "1"
    if a.cmd == "cloud-run" and a.fresh_data:
        import os
        os.environ["SKYBIRD_FRESH_DATA"] = "1"
    cfg = load_config(a.config)
    if sys.stdout is None or sys.stderr is None:  # started via pythonw (no console window)
        out = open(cfg.data_dir / "server.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or out
        sys.stderr = sys.stderr or out

    if a.cmd == "serve":
        if a.wait:
            import time as _t
            _t.sleep(a.wait)
        from .dashboard import serve
        serve(cfg, with_scheduler=not a.no_scheduler)
    elif a.cmd == "cloud-run":
        from .cloud import run
        if a.probe_board:
            from .cloud import probe_board
            probe_board()
            return
        if a.check_posts:
            from .cloud import check_posts
            check_posts(cfg)
            return
        if a.test_notify:
            from .cloud import test_notify
            test_notify(cfg)
            return
        if a.check_hosts:
            from .cloud import check_hosts
            check_hosts()
            return
        only = [x.strip() for x in a.only.replace("all posts", "").split(",") if x.strip()] or None
        import os
        import traceback
        try:
            run(cfg, force=a.force, only=only, day=date.fromisoformat(a.day) if a.day.strip() else None,
                at=a.at.strip() or None)
        except BaseException as e:   # show the reason on the GitHub run page (Annotations)
            if os.environ.get("GITHUB_ACTIONS") and not (isinstance(e, SystemExit) and not e.code):
                last = traceback.format_exception_only(type(e), e)[-1].strip().replace("\n", " ")
                where = traceback.extract_tb(e.__traceback__)[-1] if e.__traceback__ else None
                print(f"::error title=SkyBird stopped::{last[:900]}" + (f" (at {where.filename.split('/')[-1]}:{where.lineno})" if where else ""))
                from . import notify
                notify.send(cfg, "SkyBird (cloud): the morning run failed", last[:400], priority="high",
                            click="https://github.com/" + os.environ.get("GITHUB_REPOSITORY", "") + "/actions")
            raise
    elif a.cmd == "prepare":
        engine.prepare(cfg, _date(cfg, a.date), force=a.force, render=not a.no_render)
    elif a.cmd == "approve":
        d = _date(cfg, a.date)
        st = load_state(cfg, d) or engine.prepare(cfg, d)
        for k, post in st["posts"].items():
            if post["status"] == PENDING:
                engine.set_status(cfg, d, k, APPROVED)
    elif a.cmd == "post":
        d = _date(cfg, a.date)
        if a.force:
            engine.set_status(cfg, d, a.slot, APPROVED)
        post = engine.publish(cfg, d, a.slot, force=a.force)
        print(json.dumps(post["results"], indent=2))
    elif a.cmd == "tick":
        print(engine.tick(cfg) or "nothing to do")
    elif a.cmd == "status":
        d = _date(cfg, a.date)
        st = load_state(cfg, d)
        if not st:
            print(f"No package for {d}")
            return
        print(f"{d}  ({'DRY RUN' if cfg.dry_run else 'LIVE'})")
        for k, post in st["posts"].items():
            res = " ".join(f"{p}:{'ok' if r.get('ok') else 'FAIL'}" for p, r in post.get("results", {}).items())
            print(f"  {post['time']}  {post['title']:<26} {post['status']:<9} {res}")
    elif a.cmd == "meta-setup":
        meta_setup(cfg, a.page_name)
    elif a.cmd == "render":
        from .render import render_day
        print(render_day(cfg, _date(cfg, a.date), demo=a.demo))
    elif a.cmd == "recreate":
        d = _date(cfg, a.date)
        st = load_state(cfg, d)
        live = [p["title"] for p in (st or {}).get("posts", {}).values() if engine.really_posted(p)]
        if live:
            raise SystemExit(f"Not remaking {d}: {', '.join(live)} already went out live.")
        print(f"Making new videos for {d} (about 10 minutes)...")
        if not engine.run_render(cfg, d, force=True):
            raise SystemExit("Couldn't make the videos. See the activity log in the dashboard (History & logs).")
        engine.prepare(cfg, d, force=True, render=False)
        print("Done. Refresh the dashboard to see the new videos.")
    elif a.cmd == "opensky-setup":
        cfg.secrets["opensky_client_id"] = input("OpenSky client id: ").strip()
        cfg.secrets["opensky_client_secret"] = getpass.getpass("OpenSky client secret (hidden): ").strip()
        import requests as _rq
        from .flights import _token
        _token(cfg, _rq.Session())
        cfg.save_secrets()
        print("OpenSky connected.")
    elif a.cmd == "buffer-setup":
        buffer_setup(cfg)
    elif a.cmd == "uploadpost-setup":
        uploadpost_setup(cfg)
    elif a.cmd == "tiktok-auth":
        tiktok_auth(cfg)
    elif a.cmd == "check":
        check(cfg)


def meta_setup(cfg, page_name=None):
    from .publishers.meta import setup_tokens
    print(__import__("textwrap").dedent("""
    Connect Facebook Page + Instagram (see README, section 2).
    1. developers.facebook.com > your app > Tools > Graph API Explorer
    2. Pick your app, "Get User Access Token" with permissions:
       pages_show_list, pages_read_engagement, pages_manage_posts,
       instagram_basic, instagram_content_publish, instagram_manage_insights,
       read_insights, business_management
    3. Copy the token and paste it below.
    """))
    app_id = input("Meta App ID: ").strip()
    app_secret = getpass.getpass("Meta App Secret (hidden): ").strip()
    token = getpass.getpass("User access token (hidden): ").strip()
    version = cfg.section("facebook").get("graph_version", "v23.0")
    info = setup_tokens(app_id, app_secret, token, version=version, page_name=page_name)
    cfg.secrets.update(info)
    cfg.save_secrets()
    print(f"\nSaved. Page: {info['fb_page_name']} ({info['fb_page_id']})")
    if info["ig_user_id"]:
        print(f"Instagram business account: {info['ig_user_id']}")
    else:
        print("WARNING: no Instagram account is linked to this Page. In the Instagram app switch to a "
              "Professional (Business/Creator) account and link it to the Facebook Page, then rerun.")


def buffer_setup(cfg):
    from .publishers import buffer
    print(__import__("textwrap").dedent("""
    Connect through Buffer (free plan, no developer accounts).
    1. At buffer.com connect Instagram, the Facebook Page and TikTok.
    2. In Buffer open Settings > API and create a personal API key.
    3. Paste it below (right-click to paste), then press Enter.
    (Easier: paste it in the dashboard under Settings > Accounts > Connect Buffer.)
    """))
    key = input("Buffer API key: ").strip().strip('"') or cfg.secrets.get("buffer_api_key", "")
    orgs = buffer.organizations(key)
    if not orgs:
        raise SystemExit("Buffer accepted the key but returned no organization. Log in at buffer.com once, then retry.")
    org = orgs[0]
    if len(orgs) > 1:
        for i, o in enumerate(orgs, 1):
            print(f"  {i}. {o.get('name')}")
        org = orgs[int(input("Which one is SkyBird? ").strip() or "1") - 1]
    chs = buffer.channels(key, org["id"])
    found = buffer.match_channels(chs)
    cfg.secrets.update(buffer_api_key=key, buffer_org_id=org["id"])
    for p in buffer.PLATFORMS:
        if p in found:
            cfg.secrets[f"buffer_channel_{p}"] = found[p]["id"]
    cfg.save_secrets()
    print("\nSaved. Channels found in Buffer:")
    for p in buffer.PLATFORMS:
        print(f"  {p:<10} {found[p].get('name') if p in found else 'NOT CONNECTED: add it in Buffer, then run this again'}")
    others = [c for c in chs if c not in found.values()]
    if others:
        print("  (ignored: " + ", ".join(f"{c.get('service')} {c.get('name')}" for c in others) + ")")


def uploadpost_setup(cfg):
    from .publishers import upload_post
    print(__import__("textwrap").dedent("""
    Connect through Upload-Post (no Meta/TikTok developer accounts needed).
    1. Sign up at https://app.upload-post.com
    2. Create a profile (e.g. "skybird") and, inside it, connect Instagram,
       Facebook and TikTok (log in to each and approve).
    3. Open "API Keys", create a key and paste it below.
    """))
    key = getpass.getpass("Upload-Post API key (hidden): ").strip() or cfg.secrets.get("upload_post_api_key", "")
    profiles = upload_post.list_profiles(key)
    if not profiles:
        raise SystemExit("No profiles found. Create one on upload-post.com and connect your accounts first.")
    for i, p in enumerate(profiles, 1):
        linked = ", ".join(upload_post.connected_platforms(p)) or "nothing linked yet"
        print(f"  {i}. {p.get('username')}  ({linked})")
    pick = input(f"Which profile is SkyBird? [1-{len(profiles)}]: ").strip() or "1"
    prof = profiles[int(pick) - 1]
    cfg.secrets.update(upload_post_api_key=key, upload_post_profile=prof.get("username"))
    linked = upload_post.connected_platforms(prof)
    if "facebook" in linked:
        try:
            pages = upload_post.facebook_pages(key, prof.get("username"))
        except Exception:
            pages = []
        if len(pages) == 1:
            cfg.secrets["upload_post_facebook_page_id"] = str(pages[0].get("id") or pages[0].get("page_id") or "")
        elif pages:
            for i, pg in enumerate(pages, 1):
                print(f"  {i}. {pg.get('name')}")
            n = int(input("Which Facebook Page is SkyBird? ").strip() or "1")
            cfg.secrets["upload_post_facebook_page_id"] = str(pages[n - 1].get("id") or pages[n - 1].get("page_id") or "")
    cfg.save_secrets()
    missing = [p for p in ("instagram", "facebook", "tiktok") if p not in linked]
    print(f"\nSaved. Profile '{prof.get('username')}'. Linked: {', '.join(linked) or 'none'}")
    if missing:
        print(f"Still to link on upload-post.com: {', '.join(missing)}")


def tiktok_auth(cfg):
    from .publishers import tiktok
    sec = cfg.secrets
    for key, prompt in [("tiktok_client_key", "TikTok Client Key: "),
                        ("tiktok_client_secret", "TikTok Client Secret: "),
                        ("tiktok_redirect_uri", "Redirect URI (exactly as registered in the TikTok app): ")]:
        if not sec.get(key):
            sec[key] = (getpass.getpass(prompt) if "Secret" in prompt else input(prompt)).strip()
    cfg.save_secrets()
    print("\nOpen this link, log in as the SkyBird TikTok account and press Authorize:\n")
    print(tiktok.authorize_url(sec["tiktok_client_key"], sec["tiktok_redirect_uri"]))
    print("\nYou'll land on your redirect page. Copy the WHOLE address from the browser bar.")
    back = input("Paste it here: ").strip()
    code = urllib.parse.parse_qs(urllib.parse.urlparse(back).query).get("code", [back])[0]
    tiktok.exchange_code(sec, code)
    cfg.save_secrets()
    print("TikTok connected. The access token refreshes itself automatically (the refresh token lasts 1 year).")


def check(cfg):
    from .accounts import check_accounts
    print(f"Mode: {'DRY RUN (nothing posts)' if cfg.dry_run else 'LIVE'}")
    res = check_accounts(cfg)
    for p in ("facebook", "instagram", "tiktok"):
        r = res[p]
        print(f"{p.title():<10} {'OK  ' + r.get('name', '') if r['ok'] else 'NOT READY  ' + r.get('error', '')}")

if __name__ == "__main__":
    sys.exit(main())
