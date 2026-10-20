"""Engagement numbers for posted videos, stored in data/metrics.json.

Key: "<post_date>|<slot>|<platform>" -> {views, likes, comments, shares, saves, reach, fetched_at, error}
The scheduler refreshes posts from the last 30 days every 3 hours (numbers come
from Buffer for posts it sent, or the platforms' own APIs when connected); the
Engagement tab has a Refresh button too.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import requests

from .config import Config
from .publishers.base import check
from .state import load_state, log

GRAPH = "https://graph.facebook.com"
TT = "https://open.tiktokapis.com/v2"
FIELDS = ("views", "likes", "comments", "shares", "saves", "reach")
# Older posts barely change, so after 30 days they stop being refreshed.
REFRESH_DAYS = 30


def path(cfg: Config):
    return cfg.data_dir / "metrics.json"


def load_metrics(cfg: Config) -> dict:
    try:
        return json.loads(path(cfg).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_metrics(cfg: Config, m: dict) -> None:
    tmp = path(cfg).with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=2), encoding="utf-8")
    tmp.replace(path(cfg))


# ---- per platform --------------------------------------------------------------

def instagram(media_id: str, token: str, version: str, s=requests) -> dict:
    out = {}
    try:
        body = check(s.get(f"{GRAPH}/{version}/{media_id}/insights", params={
            "metric": "views,reach,likes,comments,shares,saved", "access_token": token}, timeout=30), "IG insights")
        for row in body.get("data", []):
            val = (row.get("values") or [{}])[0].get("value", row.get("total_value", {}).get("value"))
            out["saves" if row["name"] == "saved" else row["name"]] = val
    except Exception:
        # Older API versions / not enough data yet: fall back to basic counts
        body = check(s.get(f"{GRAPH}/{version}/{media_id}", params={
            "fields": "like_count,comments_count", "access_token": token}, timeout=30), "IG counts")
        out = {"likes": body.get("like_count"), "comments": body.get("comments_count")}
    return out


def facebook(video_id: str, token: str, version: str, s=requests) -> dict:
    out = {}
    try:
        body = check(s.get(f"{GRAPH}/{version}/{video_id}/video_insights", params={
            "metric": "blue_reels_play_count,post_impressions_unique", "access_token": token}, timeout=30), "FB insights")
        for row in body.get("data", []):
            val = (row.get("values") or [{}])[0].get("value")
            if row["name"] == "blue_reels_play_count":
                out["views"] = val
            elif row["name"] == "post_impressions_unique":
                out["reach"] = val
    except Exception:
        pass
    body = check(s.get(f"{GRAPH}/{version}/{video_id}", params={
        "fields": "likes.summary(true).limit(0),comments.summary(true).limit(0)",
        "access_token": token}, timeout=30), "FB counts")
    out["likes"] = (body.get("likes") or {}).get("summary", {}).get("total_count")
    out["comments"] = (body.get("comments") or {}).get("summary", {}).get("total_count")
    return out


def tiktok_recent(token: str, s=requests) -> list[dict]:
    """Most recent videos on the account (needs the video.list scope)."""
    body = check(s.post(f"{TT}/video/list/", params={
        "fields": "id,create_time,title,share_url,view_count,like_count,comment_count,share_count"},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"max_count": 20}, timeout=30), "TikTok video list")
    return body.get("data", {}).get("videos", [])


def _tt_row(v: dict) -> dict:
    return {"views": v.get("view_count"), "likes": v.get("like_count"),
            "comments": v.get("comment_count"), "shares": v.get("share_count"),
            "url": v.get("share_url", "")}


# ---- refresh ---------------------------------------------------------------------

def refresh(cfg: Config, days: int = REFRESH_DAYS, today: date | None = None) -> dict:
    """Fetch fresh numbers for everything posted in the last `days` days."""
    m = load_metrics(cfg)
    if cfg.dry_run:
        return m
    sec = cfg.secrets
    ig_v = cfg.section("instagram").get("graph_version", "v23.0")
    fb_v = cfg.section("facebook").get("graph_version", "v23.0")
    today = today or datetime.now(cfg.tz).date()
    tt_videos, tt_claimed = None, {v.get("id") for v in m.values() if v.get("tiktok_id")}
    stamp = datetime.now(cfg.tz).isoformat(timespec="seconds")
    up_cache: dict[str, dict] = {}

    for i in range(days):
        d = today - timedelta(days=i)
        st = load_state(cfg, d)
        if not st:
            continue
        for slot, post in st["posts"].items():
            for platform, r in post.get("results", {}).items():
                if not r.get("ok") or r.get("id") in ("", "dry-run", None):
                    continue
                key = f"{d.isoformat()}|{slot}|{platform}"
                row = dict(m.get(key, {}))
                try:
                    if r.get("request_id") and sec.get("upload_post_api_key"):
                        rid = r["request_id"]
                        if rid not in up_cache:
                            from .publishers import upload_post
                            up_cache[rid] = upload_post.post_analytics(sec["upload_post_api_key"], rid)
                        row.update(up_cache[rid].get(platform, {}))
                    elif sec.get("buffer_api_key") and "Buffer" in (r.get("detail") or ""):
                        from .publishers import buffer
                        got = buffer.post_metrics(sec["buffer_api_key"], r["id"])
                        if not got:
                            row["error"] = "Buffer has no numbers for this one yet"
                            row.update(fetched_at=stamp, post_date=d.isoformat(), slot=slot, platform=platform,
                                       title=post["title"], url=row.get("url") or r.get("url", ""))
                            m[key] = row
                            continue
                        row.update(got)
                    elif r.get("manual"):
                        continue  # posted by hand: no numbers available
                    elif platform == "instagram":
                        row.update(instagram(r["id"], sec["fb_page_token"], ig_v))
                    elif platform == "facebook":
                        row.update(facebook(r["id"], sec["fb_page_token"], fb_v))
                    elif platform == "tiktok":
                        if tt_videos is None:
                            from .publishers import tiktok as tk
                            if tk.ensure_fresh_token(sec):
                                cfg.save_secrets()
                            tt_videos = tiktok_recent(sec["tiktok_access_token"])
                        match = next((v for v in tt_videos if v.get("id") in (row.get("tiktok_id"), r["id"])), None)
                        if not match:  # inbox mode: the first new video after we sent it
                            sent = datetime.fromisoformat(r["at"]).timestamp()
                            cands = sorted((v for v in tt_videos if v.get("id") not in tt_claimed
                                            and sent - 600 <= v.get("create_time", 0) <= sent + 12 * 3600),
                                           key=lambda v: v["create_time"])
                            match = cands[0] if cands else None
                        if match:
                            row.update(_tt_row(match), tiktok_id=match["id"])
                            tt_claimed.add(match["id"])
                        else:
                            row["error"] = "Not found on the account yet (still in your inbox?)"
                            m[key] = row
                            continue
                    row.pop("error", None)
                except Exception as e:
                    row["error"] = str(e)[:300]
                row.update(fetched_at=stamp, post_date=d.isoformat(), slot=slot, platform=platform,
                           title=post["title"], url=row.get("url") or r.get("url", ""))
                m[key] = row
    save_metrics(cfg, m)
    log(cfg, f"Engagement refreshed ({len(m)} post/platform rows)")
    return m


def summary(cfg: Config, days: int, platforms: list[str] | None = None, today: date | None = None) -> dict:
    """Numbers for the Engagement tab."""
    today = today or datetime.now(cfg.tz).date()
    start = today - timedelta(days=days - 1)
    rows = [dict(v, key=k) for k, v in load_metrics(cfg).items()
            if v.get("post_date") and start.isoformat() <= v["post_date"] <= today.isoformat()
            and (not platforms or v.get("platform") in platforms)]
    num = lambda v: v if isinstance(v, (int, float)) else 0
    totals = {f: sum(num(r.get(f)) for r in rows) for f in FIELDS}
    daily = {}
    for r in rows:
        daily.setdefault(r["post_date"], {}).setdefault(r["platform"], 0)
        daily[r["post_date"]][r["platform"]] += num(r.get("views"))
    by_slot = {}
    for r in rows:
        b = by_slot.setdefault(r["slot"], {"title": r.get("title", r["slot"]), "views": 0, "posts": 0})
        b["views"] += num(r.get("views"))
        b["posts"] += 1
    for b in by_slot.values():
        b["avg"] = round(b["views"] / b["posts"]) if b["posts"] else 0
    dates = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    top = sorted(rows, key=lambda r: -num(r.get("views")))[:15]
    return {"totals": totals, "posts": len(rows), "dates": dates, "daily": daily,
            "by_slot": by_slot, "top": top, "errors": [r for r in rows if r.get("error")]}
