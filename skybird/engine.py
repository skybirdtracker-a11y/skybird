"""Morning prep, publishing, and the scheduler tick."""
from __future__ import annotations

import json
import shlex
import subprocess
from datetime import date, datetime, timedelta

from . import captions
from .config import PLATFORMS, Config
from .events import events_for
from . import notify
from .publishers import buffer, meta, tiktok, upload_post
from .settings import load_plan, load_settings
from .publishers.base import PublishError, PublishResult
from .state import (APPROVED, FAILED, MISSED, PARTIAL, PENDING, POSTED, POSTING, READY, SKIPPED,
                    load_state, locked, log, save_state)


def now_local(cfg: Config) -> datetime:
    return datetime.now(cfg.tz)


def scheduled_at(cfg: Config, post_date: date, slot_time) -> datetime:
    return datetime.combine(post_date, slot_time, tzinfo=cfg.tz)


def slot_when(cfg: Config, post_date: date, slot, post: dict | None) -> datetime:
    """The post's time today: the usual slot time, or a test time (see insights.py)."""
    if post and post.get("time_test"):
        return scheduled_at(cfg, post_date, datetime.strptime(post["time"], "%H:%M").time())
    return scheduled_at(cfg, post_date, slot.time)


# ---- morning prep ------------------------------------------------------------

def rendering_flag(cfg: Config):
    return cfg.data_dir / ".rendering"


RETRY_EVERY_MIN, RETRY_MAX = 30, 8
RETRY_UNTIL = datetime.strptime("11:30", "%H:%M").time()


def _render_status(cfg: Config, post_date: date, ok: bool | None = None, error: str = "") -> dict:
    """Per-day record of video-making attempts (data/render_status.json)."""
    path = cfg.data_dir / "render_status.json"
    try:
        allst = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        allst = {}
    st = allst.get(post_date.isoformat(), {"attempts": 0, "ok": False})
    if ok is not None:
        st = {"attempts": st["attempts"] + (0 if ok else 1), "ok": ok, "error": error[:400],
              "at": now_local(cfg).isoformat(timespec="seconds")}
        allst[post_date.isoformat()] = st
        path.write_text(json.dumps(allst, indent=1), encoding="utf-8")
    return st


def really_posted(post: dict) -> bool:
    """True if any platform actually received this post (dry runs don't count)."""
    return any(r.get("ok") and r.get("id") != "dry-run" for r in (post.get("results") or {}).values())


def dry_run_only(post: dict) -> bool:
    """Marked posted, but only by a dry run: nothing really uploaded."""
    res = post.get("results") or {}
    return post["status"] == POSTED and bool(res) and all(r.get("id") == "dry-run" for r in res.values())


def post_for_real(cfg: Config, post_date: date, keys: list[str] | None = None) -> list[str]:
    """Turn dry-run 'posted' posts back into approved ones, so the scheduler
    posts them live (late ones catch up an hour apart). Returns the keys changed."""
    if cfg.dry_run:
        raise ValueError("SkyBird is still in dry run. Turn on Live in Settings first.")
    done = []
    with locked(cfg):
        state = load_state(cfg, post_date)
        if not state:
            raise ValueError(f"No posts for {post_date}")
        stamp = now_local(cfg).isoformat(timespec="seconds")
        for key, post in state["posts"].items():
            if (keys is None or key in keys) and dry_run_only(post):
                post["status"] = APPROVED
                post["approved_at"] = stamp
                post["results"] = {}
                post.pop("posted_at", None)
                done.append(key)
        save_state(cfg, post_date, state)
    for key in done:
        log(cfg, f"{post_date} {state['posts'][key]['title']}: only went out as a dry run; queued to post for real")
    return done


def run_render(cfg: Config, post_date: date, *, demo: bool = False, force: bool = False) -> bool:
    """Make the day's 4 videos. Uses render_command if you set one, otherwise
    SkyBird's built-in renderer (OpenSky flight data). Returns True on success."""
    cmd = (cfg.section("media").get("render_command") or "").strip()
    folder = cfg.media_folder(post_date)
    have_all = all(cfg.media_file(post_date, s.file).exists() for s in cfg.slots)
    try:
        if json.loads(cfg.media_file(post_date, "stats.json").read_text(encoding="utf-8")).get("source") == "sample":
            have_all = False  # sample videos never count as the real ones
    except (FileNotFoundError, ValueError):
        pass
    if not cmd:
        if have_all and not demo and not force:
            return True  # you (or another tool) already dropped the videos in
        if not demo and not (cfg.secrets.get("opensky_client_id") and cfg.section("render").get("enabled", True)):
            log(cfg, "Videos not made: connect OpenSky in Settings (or drop the 4 videos into the folder)")
            return False
        flag = rendering_flag(cfg)
        flag.write_text(post_date.isoformat())
        try:
            from . import render as _render
            _render.render_day(cfg, post_date, demo=demo)
            if not demo:
                _render_status(cfg, post_date, ok=True)
            return True
        except Exception as e:
            log(cfg, f"Making videos failed: {e}")
            st = _render_status(cfg, post_date, ok=False, error=str(e))
            if not demo and st["attempts"] == 1:
                notify.send(cfg, "SkyBird: couldn't make today's videos yet",
                            f"{str(e)[:220]} SkyBird will try again every {RETRY_EVERY_MIN} minutes until "
                            f"{RETRY_UNTIL.strftime('%I:%M %p').lstrip('0')}, or tap Make videos now.",
                            priority="high", path="/")
            elif not demo and st["attempts"] >= RETRY_MAX:
                notify.send(cfg, "SkyBird: gave up on today's videos",
                            f"OpenSky still isn't answering after {st['attempts']} tries. You can skip today or try "
                            "Make videos now later.", priority="high", path="/")
            return False
        finally:
            flag.unlink(missing_ok=True)
    cmd = cmd.format(post_date=post_date.isoformat(), data_date=cfg.data_date(post_date).isoformat())
    log(cfg, f"Rendering videos: {cmd}")
    result = subprocess.run(cmd, shell=True, cwd=cfg.root, capture_output=True, text=True)
    if result.returncode != 0:
        log(cfg, f"Render command failed ({result.returncode}): {result.stderr[-800:]}")
        return False
    return True


def prepare(cfg: Config, post_date: date, *, force: bool = False, render: bool = True, quiet: bool = False) -> dict:
    """Build the day's approval package: captions, hashtags, video paths."""
    existing = load_state(cfg, post_date)
    if existing and not force and any(p["status"] != PENDING for p in existing["posts"].values()):
        log(cfg, f"{post_date}: already has approvals/posts; not rebuilding (use --force)")
        return existing
    if render:
        run_render(cfg, post_date)  # outside the lock: takes a few minutes
    with locked(cfg):
        existing = load_state(cfg, post_date)

        folder = cfg.media_folder(post_date)
        try:
            folder.mkdir(parents=True, exist_ok=True)  # so you can see where the videos go
        except OSError:
            pass
        stats_file = cfg.media_file(post_date, cfg.section("media").get("stats_file", "stats.json"))
        stats = json.loads(stats_file.read_text(encoding="utf-8")) if stats_file.exists() else {}
        data_date = cfg.data_date(post_date)

        state = {"post_date": post_date.isoformat(), "data_date": data_date.isoformat(),
                 "prepared_at": now_local(cfg).isoformat(timespec="seconds"),
                 "media_folder": str(folder), "stats_found": stats_file.exists(),
                 "sample": stats.get("source") == "sample",
                 "incomplete": "" if stats.get("complete", True) else (stats.get("incomplete_reason") or "The flight data looks incomplete."),
                 "events": [], "posts": {}}
        day_events = events_for(cfg, post_date)
        state["events"] = [{"name": e["name"], "phase": e["phase"], "note": e.get("note", ""),
                            "bonus_posts": e.get("bonus_posts", []), "confirmed": e.get("confirmed", True)}
                           for e in day_events]

        from . import insights
        tests = insights.test_times(cfg, post_date)
        for slot in cfg.slots:
            s_stats = (stats.get("posts") or {}).get(slot.key, {})
            evs = events_for(cfg, post_date, slot.key)
            options = captions.build_options(cfg, slot.key, s_stats, data_date, evs)
            video = cfg.media_file(post_date, slot.file)
            prev = (existing or {}).get("posts", {}).get(slot.key, {})
            state["posts"][slot.key] = {
                "title": slot.title,
                "time": tests.get(slot.key, slot.time.strftime("%H:%M")),
                "time_test": slot.key in tests,
                "usual_time": slot.time.strftime("%H:%M"),
                "video": str(video),
                "video_exists": video.exists(),
                "status": PENDING,
                "options": options,
                "choice": options[0]["id"],
                "caption": options[0]["text"],
                "hashtags": captions.hashtag_choices(cfg, slot.key, s_stats, evs),
                "tiktok_priority": captions.tiktok_priority(cfg, slot.key),
                "mentions": {p: captions.mention_choices(cfg, p, evs, slot.key, s_stats, post_date) for p in PLATFORMS},
                "cta": options[0].get("cta", ""),
                "platforms": {p: (p in slot.platforms and cfg.platform_enabled(p)) for p in PLATFORMS},
                "results": prev.get("results", {}),
            }
            if not state["incomplete"]:  # never auto/pre-approve a day with incomplete data
                _apply_plan(cfg, post_date, slot.key, state["posts"][slot.key])
        save_state(cfg, post_date, state)
    missing = [k for k, p in state["posts"].items() if not p["video_exists"]]
    waiting = [p["title"] for p in state["posts"].values() if p["status"] == PENDING]
    if state.get("incomplete") and post_date == now_local(cfg).date() and not quiet:
        notify.send(cfg, "SkyBird: yesterday's data looks incomplete", state["incomplete"] +
                    (" SkyBird will fetch it again every hour this morning and remake the videos; you'll get a "
                     "note when they're fixed. Hold off approving until then." if cfg.secrets.get("opensky_client_id")
                     else " Consider skipping today."),
                    priority="high", path="/")
    if waiting and post_date == now_local(cfg).date() and len(missing) < len(state["posts"]) and not quiet:
        ev = f" Today: {state['events'][0]['name']}." if state["events"] else ""
        notify.send(cfg, "SkyBird: today's posts are ready",
                    f"{len(waiting)} post(s) waiting for your approval.{ev} Tap to review." +
                    (f" Missing video: {', '.join(missing)}" if missing else ""), path="/")
    log(cfg, f"{post_date}: prepared {len(state['posts'])} posts"
             + (f" (videos missing: {', '.join(missing)})" if missing else "")
             + (f" · events: {', '.join(e['name'] for e in state['events'])}" if state["events"] else ""))
    return state


def _apply_plan(cfg: Config, post_date: date, slot_key: str, post: dict) -> None:
    """Pre-approvals / skips set on the Upcoming tab, and daily auto-approve."""
    decision = load_plan(cfg.data_dir).get(post_date.isoformat(), {}).get(slot_key)
    if decision is None and load_settings(cfg.data_dir).get("auto_approve", {}).get(slot_key):
        decision = "auto"
    if not decision:
        return
    stamp = now_local(cfg).isoformat(timespec="seconds")
    if decision == "skip":
        post["status"], post["skipped_at"], post["decided_by"] = SKIPPED, stamp, "plan"
    elif post["video_exists"]:  # never auto-approve a post whose video is missing
        post["status"], post["approved_at"] = APPROVED, stamp
        post["decided_by"] = "pre-approved" if decision == "approve" else "auto-approve"


# ---- dashboard edits -----------------------------------------------------------

def apply_edits(post: dict, edits: dict) -> None:
    if "choice" in edits:
        post["choice"] = edits["choice"]
    if "caption" in edits:
        post["caption"] = str(edits["caption"])
    if "hashtags" in edits:
        chosen = {t.lower() for t in edits["hashtags"]}
        known = {h["tag"].lower() for h in post["hashtags"]}
        for h in post["hashtags"]:
            h["checked"] = h["tag"].lower() in chosen
        for t in edits["hashtags"]:  # custom tags typed in the dashboard
            if t.lower() not in known:
                post["hashtags"].append({"tag": t, "checked": True, "source": "custom"})
    if "mentions" in edits:
        for platform, handles in edits["mentions"].items():
            chosen = {h.lower() for h in handles}
            lst = post["mentions"].setdefault(platform, [])
            known = {m["handle"].lower() for m in lst}
            for m in lst:
                m["checked"] = m["handle"].lower() in chosen
            for h in handles:
                if h.lower() not in known:
                    lst.append({"handle": h, "checked": True})
    if "platforms" in edits:
        for p, on in edits["platforms"].items():
            if p in post["platforms"]:
                post["platforms"][p] = bool(on)


def set_status(cfg: Config, post_date: date, slot_key: str, status: str, edits: dict | None = None) -> dict:
    with locked(cfg):
        state = load_state(cfg, post_date)
        if not state:
            raise ValueError(f"No package for {post_date}. Run prepare first.")
        post = state["posts"][slot_key]
        if post["status"] in (POSTED, POSTING):
            raise ValueError(f"{post['title']} is already {post['status']}")
        if edits:
            apply_edits(post, edits)
        if status:
            post["status"] = status
            post[f"{status}_at"] = now_local(cfg).isoformat(timespec="seconds")
        save_state(cfg, post_date, state)
    if status:
        log(cfg, f"{post_date} {post['title']}: {status}")
    return state


# ---- publishing ------------------------------------------------------------------

def tiktok_tags(post: dict) -> list[str]:
    return captions.pick_tiktok(post["hashtags"], post.get("tiktok_priority") or captions.TIKTOK_TOP["all"])


def caption_for(post: dict, platform: str) -> str:
    tags = [h["tag"] for h in post["hashtags"] if h["checked"]]
    if platform == "tiktok":
        tags = tiktok_tags(post)
    mentions = [m["handle"] for m in post["mentions"].get(platform, []) if m["checked"]]
    return captions.final_caption(post["caption"], tags, mentions, platform)


def backend(cfg: Config) -> str:
    """buffer (default): via buffer.com's free plan, no developer accounts needed
    upload_post: via upload-post.com, no developer accounts needed
    direct: your own Meta + TikTok developer apps
    manual: SkyBird readies a phone 'post kit' at the slot time; you post it yourself"""
    return load_settings(cfg.data_dir).get("backend") or cfg.section("publisher").get("backend", "buffer")


def _publish_direct(cfg: Config, platform: str, video, caption: str) -> PublishResult:
    sec = cfg.secrets
    if platform == "instagram":
        ig = cfg.section("instagram")
        return meta.post_instagram_reel(video, caption, sec, version=ig.get("graph_version", "v23.0"),
                                        share_to_feed=ig.get("share_to_feed", True))
    if platform == "facebook":
        return meta.post_facebook_reel(video, caption, sec,
                                       version=cfg.section("facebook").get("graph_version", "v23.0"))
    if platform == "tiktok":
        if tiktok.ensure_fresh_token(sec):
            cfg.save_secrets()
        return tiktok.post_video(video, caption, sec, cfg.section("tiktok"))
    raise PublishError(f"Unknown platform {platform}")


def _publish_batch(cfg: Config, video, captions: dict[str, str]) -> dict[str, PublishResult | Exception]:
    """Post to several platforms; returns {platform: PublishResult or the exception}."""
    if cfg.dry_run:
        return {p: PublishResult(p, True, "dry-run", "", f"DRY RUN via {backend(cfg)}: would upload {video.name} ({len(c)} chars)")
                for p, c in captions.items()}
    if backend(cfg) == "buffer":
        try:
            return dict(buffer.post(video, captions, cfg.secrets))
        except Exception as e:
            return {p: e for p in captions}
    if backend(cfg) == "upload_post":
        up = cfg.upload_post_opts()
        opts = dict(up, share_to_feed=cfg.section("instagram").get("share_to_feed", True),
                    tiktok_privacy_level=up.get("tiktok_privacy_level") or None)
        try:
            return dict(upload_post.post(video, captions, cfg.secrets, opts))
        except Exception as e:
            return {p: e for p in captions}
    out = {}
    for p, c in captions.items():
        try:
            out[p] = _publish_direct(cfg, p, video, c)
        except Exception as e:  # keep going with the other platforms
            out[p] = e
    return out


def publish(cfg: Config, post_date: date, slot_key: str, *, force: bool = False) -> dict:
    """Post one slot to every selected platform that hasn't succeeded yet."""
    from pathlib import Path
    with locked(cfg, "publish", timeout=1800):
        with locked(cfg):
            state = load_state(cfg, post_date)
            post = state["posts"][slot_key]
            if post["status"] not in (APPROVED, PARTIAL, FAILED) and not force:
                raise ValueError(f"{post['title']} is {post['status']}, not approved")
            if state.get("sample") and not cfg.dry_run:
                post["status"] = FAILED
                post["note"] = "These are SAMPLE videos (fake flights), so they can't be posted live. Make the real videos first."
                save_state(cfg, post_date, state)
                raise ValueError("These are sample videos with fake flights, so they can't be posted live. Make the real videos first.")
            post["status"] = POSTING
            post["tracking"] = tracking(cfg, post_date, slot_key, post)
            save_state(cfg, post_date, state)

        video = Path(post["video"])
        results = dict(post.get("results", {}))
        todo = [p for p, on in post["platforms"].items() if on and not results.get(p, {}).get("ok")]
        stamp = now_local(cfg).isoformat(timespec="seconds")
        if backend(cfg) == "manual" and not cfg.dry_run:
            for p in todo:
                results[p] = {"ok": False, "manual": True, "at": stamp,
                              "detail": "Ready: open the post kit on your phone and post it"}
            log(cfg, f"{post_date} {post['title']}: post kit ready for {', '.join(todo)}")
            notify.send(cfg, f"Time to post: {post['title']}",
                        "Video and captions are ready. Tap, save the video, copy the caption, post.",
                        priority="high", path=f"/?date={post_date}")
        elif todo:
            outcome = _publish_batch(cfg, video, {p: caption_for(post, p) for p in todo})
            for p, r in outcome.items():
                if isinstance(r, PublishResult) and r.ok:
                    results[p] = {"ok": True, "id": r.post_id, "url": r.url, "detail": r.detail,
                                  "request_id": r.request_id, "at": stamp}
                    log(cfg, f"{post_date} {post['title']} -> {p}: OK {r.detail or r.post_id}")
                else:
                    err = r.detail if isinstance(r, PublishResult) else str(r)
                    results[p] = {"ok": False, "error": err[:600], "at": stamp,
                                  "request_id": getattr(r, "request_id", "")}
                    log(cfg, f"{post_date} {post['title']} -> {p}: FAILED {err}")

        with locked(cfg):
            state = load_state(cfg, post_date)
            post = state["posts"][slot_key]
            post["results"] = results
            _set_final_status(post)
            post["posted_at"] = stamp
            save_state(cfg, post_date, state)
        failed = [p for p, r in results.items() if not r.get("ok") and not r.get("manual")]
        if failed and not cfg.dry_run:
            tries = post.get("auto_retries", 0)
            if net_failure(post) and tries == 0:
                notify.send(cfg, f"SkyBird: no internet for {post['title']}",
                            f"The PC couldn't reach the internet, so nothing was sent. SkyBird retries every "
                            f"{NET_RETRY_MIN} minutes for the next hour, or tap Retry failed.", path=f"/?date={post_date}")
            elif not net_failure(post) or tries >= NET_RETRY_MAX:
                notify.send(cfg, f"SkyBird: {post['title']} failed on {', '.join(failed)}",
                            "Open the dashboard to see why and tap Retry failed.", priority="high",
                            path=f"/?date={post_date}")
        return post


def tracking(cfg: Config, post_date: date, slot_key: str, post: dict) -> dict:
    """What this post used, saved with it so the Engagement tab can compare later:
    caption style, call to action, @mentions per platform, and the event (if any)."""
    cta = next((c for c in captions.CTAS if c[0] == post.get("cta")), None)
    return {
        "caption_style": post.get("choice", ""),
        "cta": cta[0] if cta and cta[1] in post.get("caption", "") else "",
        "mentions": {p: [m["handle"] for m in post["mentions"].get(p, []) if m["checked"]]
                     for p, on in post["platforms"].items() if on},
        "event": ", ".join(e["name"] for e in events_for(cfg, post_date, slot_key)),
    }


NET_RETRY_MIN, NET_RETRY_MAX = 10, 6   # internet down: retry every 10 min for an hour


def net_failure(post: dict) -> bool:
    """Every failure was the PC not reaching the internet before anything got to Buffer,
    so trying again can't double-post."""
    errs = [r.get("error", "") for r in (post.get("results") or {}).values() if not r.get("ok") and not r.get("manual")]
    return bool(errs) and all(
        e.startswith(buffer.OFFLINE[:40]) or (e.startswith("Couldn't upload the video to any temporary host")
                                               and "HTTP" not in e and "web page" not in e)
        for e in errs)


def _net_retry_due(post: dict, now: datetime, cutoff: datetime) -> bool:
    if post["status"] not in (FAILED, PARTIAL) or not net_failure(post) or now > cutoff:
        return False
    if post.get("auto_retries", 0) >= NET_RETRY_MAX:
        return False
    last = _ts(post.get("posted_at"))
    return last is None or (now - last).total_seconds() >= NET_RETRY_MIN * 60


def _set_final_status(post: dict) -> None:
    wanted = [p for p, on in post["platforms"].items() if on]
    res = post.get("results", {})
    oks = [p for p in wanted if res.get(p, {}).get("ok")]
    manual = [p for p in wanted if res.get(p, {}).get("manual") and not res[p].get("ok")]
    if len(oks) == len(wanted):
        post["status"] = POSTED
    elif manual:
        post["status"] = READY
    else:
        post["status"] = PARTIAL if oks else FAILED


def mark_posted(cfg: Config, post_date: date, slot_key: str, platform: str, url: str = "") -> dict:
    """Manual backend: you posted it yourself from the post kit."""
    with locked(cfg):
        state = load_state(cfg, post_date)
        post = state["posts"][slot_key]
        post.setdefault("results", {})[platform] = {
            "ok": True, "manual": True, "url": url, "detail": "Posted by you",
            "at": now_local(cfg).isoformat(timespec="seconds")}
        _set_final_status(post)
        save_state(cfg, post_date, state)
    log(cfg, f"{post_date} {post['title']} -> {platform}: marked as posted by hand")
    return post


# ---- scheduler -------------------------------------------------------------------

def _should_retry_videos(cfg: Config, state: dict, today: date, now: datetime) -> bool:
    if all(p["video_exists"] for p in state["posts"].values()):
        return False
    if any(p["status"] not in (PENDING,) for p in state["posts"].values()):
        return False  # you've already acted on this day
    st = _render_status(cfg, today)
    if st.get("ok") or not st.get("attempts") or st["attempts"] >= RETRY_MAX or now.time() > RETRY_UNTIL:
        return False
    if rendering_flag(cfg).exists():
        return False
    last = datetime.fromisoformat(st["at"]) if st.get("at") else None
    return last is None or (now - last).total_seconds() >= RETRY_EVERY_MIN * 60


REDO_EVERY_MIN, REDO_MAX = 60, 4   # incomplete data: remake the videos hourly, up to 4 times


def _redo_marks(cfg: Config) -> dict:
    try:
        return json.loads((cfg.data_dir / "redo_incomplete.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _should_redo_incomplete(cfg: Config, state: dict, today: date, now: datetime) -> bool:
    """OpenSky's data for yesterday looked unfinished: try again later in the morning,
    as long as you haven't approved anything yet."""
    if not state.get("incomplete") or state.get("sample") or now.time() > RETRY_UNTIL:
        return False
    if any(p["status"] != PENDING for p in state["posts"].values()) or rendering_flag(cfg).exists():
        return False
    if not cfg.secrets.get("opensky_client_id"):
        return False
    m = _redo_marks(cfg).get(today.isoformat(), {"n": 0})
    if m["n"] >= REDO_MAX:
        return False
    last = _ts(m.get("at")) or _ts(state.get("prepared_at"))
    return last is None or (now - last).total_seconds() >= REDO_EVERY_MIN * 60


def redo_incomplete(cfg: Config, today: date, now: datetime) -> dict:
    marks = _redo_marks(cfg)
    m = marks.get(today.isoformat(), {"n": 0})
    m = {"n": m["n"] + 1, "at": now.isoformat(timespec="seconds")}
    marks[today.isoformat()] = m
    (cfg.data_dir / "redo_incomplete.json").write_text(json.dumps(marks, indent=1), encoding="utf-8")
    log(cfg, f"{today}: flight data looked incomplete; fetching it again and remaking the videos (try {m['n']} of {REDO_MAX})")
    if run_render(cfg, today, force=True):
        st = load_state(cfg, today)
        if st and any(p["status"] != PENDING for p in st["posts"].values()):
            log(cfg, f"{today}: you approved posts meanwhile, so the captions were left as they were")
            return st
        st = prepare(cfg, today, force=True, render=False, quiet=True)
        if not st.get("incomplete"):
            notify.send(cfg, "SkyBird: the videos are fixed",
                        "OpenSky finished yesterday's flights, so the videos were remade with the full day. Tap to review.",
                        path="/")
        elif m["n"] >= REDO_MAX:
            notify.send(cfg, "SkyBird: yesterday's data is still incomplete",
                        st["incomplete"] + " Consider skipping today.", priority="high", path="/")
        return st
    return load_state(cfg, today)


def _ts(v):
    try:
        return datetime.fromisoformat(v) if v else None
    except (TypeError, ValueError):
        return None


def planned_times(cfg: Config, state: dict, day: date, now: datetime | None = None) -> dict:
    """When each approved post will actually go out.

    On-time approvals keep their slot time. Late approvals catch up: the first one
    goes when you approve it, and every post after it waits at least
    `catchup_gap_minutes` (default 60) after the one before, in slot order."""
    gap = cfg.catchup_gap
    plan, prev = {}, None
    for slot in sorted(cfg.slots, key=lambda s: s.time):
        post = state["posts"].get(slot.key)
        if not post:
            continue
        sched = slot_when(cfg, day, slot, post)
        st = post["status"]
        if st in (POSTED, PARTIAL, FAILED, READY, POSTING):
            prev = _ts(post.get("posted_at")) or max(sched, prev or sched)
            if now is not None:
                prev = min(prev, now)
            plan[slot.key] = prev
            continue
        if st != APPROVED:
            continue  # pending / skipped / missed don't hold a place in line
        approved = _ts(post.get("approved_at")) or sched
        if now is not None:
            approved = min(approved, now)  # an approval can't be in the future
        t = max(sched, approved)
        if prev is not None:
            t = max(t, prev + gap)
        plan[slot.key] = t
        prev = t
    return plan


def tick(cfg: Config, now: datetime | None = None) -> list[str]:
    """Run every minute or so: builds the morning package and posts anything
    approved whose time has come. Safe to call as often as you like."""
    now = now or now_local(cfg)
    today = now.date()
    actions = []

    state = load_state(cfg, today)
    if state is None and now.time() >= cfg.prepare_time:
        prepare(cfg, today)
        actions.append("prepared")
        state = load_state(cfg, today)
    elif state and _should_retry_videos(cfg, state, today, now):
        log(cfg, f"{today}: trying again to make the videos")
        prepare(cfg, today, force=True)
        actions.append("retried-videos")
        state = load_state(cfg, today)
    elif state and _should_redo_incomplete(cfg, state, today, now):
        state = redo_incomplete(cfg, today, now)
        actions.append("redo-incomplete")
    if not state:
        return actions

    cutoff = datetime.combine(today, cfg.last_post_time, tzinfo=cfg.tz)
    plan = planned_times(cfg, state, today, now)
    slots = sorted(cfg.slots, key=lambda s: s.time)

    # First pass: anything that can't go out today is marked missed up front,
    # so it doesn't wait behind the one-post-per-pass rule below.
    missed = set()
    for slot in slots:
        post = state["posts"].get(slot.key)
        if not post:
            continue
        sched = slot_when(cfg, today, slot, post)
        why = None
        if post["status"] == APPROVED:
            approved = min(_ts(post.get("approved_at")) or sched, now)
            due = plan.get(slot.key, sched)
            if cfg.late_approval == "skip" and approved > sched:
                why = "approved after its time and late approvals are set to skip"
            elif due > cutoff:
                why = (f"catch-up time {due.strftime('%I:%M %p').lstrip('0')} is past "
                       f"{cutoff.strftime('%I:%M %p').lstrip('0')}, so it won't post today")
        elif post["status"] == PENDING and now > cutoff:
            why = "never approved"
        if why:
            log(cfg, f"{today} {slot.title}: {why}")
            set_status(cfg, today, slot.key, MISSED)
            actions.append(f"missed:{slot.key}")
            missed.add(slot.key)

    for slot in slots:
        post = state["posts"].get(slot.key)
        if not post or slot.key in missed:
            continue
        sched = slot_when(cfg, today, slot, post)
        if post["status"] == APPROVED:
            due = plan.get(slot.key, sched)
            if not cfg.autopost or now < due:
                continue
            if due > sched + timedelta(minutes=1):
                log(cfg, f"{today} {slot.title}: posting as a catch-up (scheduled {sched.strftime('%I:%M %p').lstrip('0')})")
            try:
                publish(cfg, today, slot.key)
                actions.append(f"posted:{slot.key}")
            except Exception as e:
                log(cfg, f"{today} {slot.title}: publish error {e}")
            break  # one post per pass, so the next one is timed from this one's real post time
        elif cfg.autopost and _net_retry_due(post, now, cutoff):
            with locked(cfg):
                st = load_state(cfg, today)
                st["posts"][slot.key]["auto_retries"] = st["posts"][slot.key].get("auto_retries", 0) + 1
                save_state(cfg, today, st)
            log(cfg, f"{today} {slot.title}: internet was down; trying again (try {st['posts'][slot.key]['auto_retries']} of {NET_RETRY_MAX})")
            try:
                publish(cfg, today, slot.key)
                actions.append(f"retried:{slot.key}")
            except Exception as e:
                log(cfg, f"{today} {slot.title}: publish error {e}")
            break
    return actions
