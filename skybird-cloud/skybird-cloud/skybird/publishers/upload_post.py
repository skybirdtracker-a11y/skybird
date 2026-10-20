"""Upload-Post relay (upload-post.com): posts to Instagram, Facebook and TikTok
without your own Meta/TikTok developer apps. You link the three accounts once in
Upload-Post's website; SkyBird sends the video + captions with an API key.

Secrets used (written by `python -m skybird uploadpost-setup`):
  upload_post_api_key
Config [upload_post]: profile, facebook_page_id, tiktok_post_mode, share_to_feed
"""
from __future__ import annotations

import time
from pathlib import Path

import requests

from .base import PublishError, PublishResult

API = "https://api.upload-post.com/api"


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Apikey {api_key}", "User-Agent": "skybird-autopost/1.0"}


def _json(resp: requests.Response, what: str) -> dict:
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:400]}
    if resp.status_code == 429:
        raise PublishError(f"{what}: Upload-Post rate limit reached. Wait a few minutes; nothing was retried automatically.")
    if resp.status_code >= 400 or (isinstance(body, dict) and body.get("success") is False and not body.get("results")):
        msg = body.get("message") or body.get("detail") or body.get("error") or body if isinstance(body, dict) else body
        raise PublishError(f"{what} failed (HTTP {resp.status_code}): {msg}")
    return body


# ---- response parsing (kept tolerant: Upload-Post adds fields over time) ----------

def _first(d: dict, *keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def parse_results(body: dict, platforms: list[str]) -> dict[str, dict]:
    """-> {platform: {"done": bool, "ok": bool, "id", "url", "error"}} for platforms found."""
    out: dict[str, dict] = {}
    res = body.get("results") if isinstance(body, dict) else None
    if res is None and isinstance(body, dict):
        res = (body.get("data") or {}).get("results") if isinstance(body.get("data"), dict) else None
    items = []
    if isinstance(res, dict):
        items = [(k, v) for k, v in res.items() if isinstance(v, dict)]
    elif isinstance(res, list):
        items = [(v.get("platform"), v) for v in res if isinstance(v, dict)]
    for platform, v in items:
        if platform not in platforms:
            continue
        status = str(v.get("status", "")).lower()
        ok = v.get("success")
        if ok is None and status:
            ok = True if status in ("success", "completed", "published", "done", "posted") else None
        failed = ok is False or status in ("failed", "error")
        done = bool(ok) or failed or status in ("success", "completed", "published", "failed", "error")
        out[platform] = {
            "done": done, "ok": bool(ok) and not failed,
            "id": str(_first(v, "platform_post_id", "post_id", "publish_id", "id", "video_id") or ""),
            "url": _first(v, "url", "post_url", "permalink", "share_url") or "",
            "error": _first(v, "error", "error_message", "message") if failed else "",
        }
    return out


# ---- posting ------------------------------------------------------------------------

def post(video: Path, captions: dict[str, str], secrets: dict, opts: dict,
         session: requests.Session | None = None, poll_every: float = 10,
         timeout: float = 1200) -> dict[str, PublishResult]:
    """One request for every platform in `captions` ({platform: caption}).
    Returns {platform: PublishResult}; platforms that failed come back ok=False."""
    key = secrets.get("upload_post_api_key")
    if not key:
        raise PublishError("Upload-Post isn't connected (run: python -m skybird uploadpost-setup)")
    profile = opts.get("profile")
    if not profile:
        raise PublishError("Set [upload_post] profile in config.toml to your Upload-Post profile name")
    if not video.exists():
        raise PublishError(f"Video not found: {video}")
    s = session or requests.Session()
    platforms = list(captions)
    first = captions[platforms[0]]

    data: list[tuple] = [("user", profile), ("title", first), ("async_upload", "true")]
    for p in platforms:
        data.append(("platform[]", p))
    if "instagram" in captions:
        data += [("instagram_title", captions["instagram"]), ("media_type", "REELS"),
                 ("share_to_feed", "true" if opts.get("share_to_feed", True) else "false")]
    if "facebook" in captions:
        data += [("facebook_title", captions["facebook"]), ("facebook_description", captions["facebook"]),
                 ("facebook_media_type", "REELS")]
        if opts.get("facebook_page_id"):
            data.append(("facebook_page_id", str(opts["facebook_page_id"])))
    if "tiktok" in captions:
        data += [("tiktok_title", captions["tiktok"]),
                 ("post_mode", opts.get("tiktok_post_mode", "DIRECT_POST"))]
        if opts.get("tiktok_privacy_level"):
            data.append(("privacy_level", opts["tiktok_privacy_level"]))

    with open(video, "rb") as f:
        body = _json(s.post(f"{API}/upload", headers=_headers(key), data=data,
                            files=[("video", (video.name, f, "video/mp4"))], timeout=900), "Upload-Post upload")

    request_id = _first(body, "request_id", "requestId") or ""
    results = parse_results(body, platforms)
    deadline = time.monotonic() + timeout
    while request_id and not all(results.get(p, {}).get("done") for p in platforms):
        if time.monotonic() > deadline:
            break
        time.sleep(poll_every)
        st = _json(s.get(f"{API}/uploadposts/status", headers=_headers(key),
                         params={"request_id": request_id}, timeout=30), "Upload-Post status")
        results.update(parse_results(st, platforms))
        if str(st.get("status", "")).lower() in ("completed", "failed", "error") and not st.get("results"):
            break

    out = {}
    for p in platforms:
        r = results.get(p)
        if r and r["ok"]:
            out[p] = PublishResult(p, True, r["id"] or request_id, r["url"], "via Upload-Post")
        elif r and r["done"]:
            out[p] = PublishResult(p, False, detail=str(r["error"] or "failed"))
        else:
            out[p] = PublishResult(p, False, detail="Still processing at Upload-Post; check their dashboard, then Retry here only if it never appeared")
        out[p].request_id = request_id
    return out


# ---- setup / checks / analytics -------------------------------------------------------

def list_profiles(api_key: str, session=requests) -> list[dict]:
    body = _json(session.get(f"{API}/uploadposts/users", headers=_headers(api_key), timeout=30), "List profiles")
    profiles = body.get("profiles") or body.get("users") or body.get("data") or []
    return profiles if isinstance(profiles, list) else []


def connected_platforms(profile: dict) -> dict[str, str]:
    """{platform: display name} for accounts linked to an Upload-Post profile."""
    accts = profile.get("social_accounts") or profile.get("accounts") or {}
    out = {}
    if isinstance(accts, dict):
        for p, v in accts.items():
            if v:
                name = v.get("display_name") or v.get("username") or v.get("name") if isinstance(v, dict) else str(v)
                out[p] = str(name or "connected")
    return out


def facebook_pages(api_key: str, profile: str, session=requests) -> list[dict]:
    body = _json(session.get(f"{API}/uploadposts/facebook/pages", headers=_headers(api_key),
                             params={"profile": profile}, timeout=30), "List Facebook Pages")
    pages = body.get("pages") or body.get("data") or []
    return pages if isinstance(pages, list) else []


METRIC_KEYS = {
    "views": ("views", "view_count", "video_views", "plays", "play_count", "impressions"),
    "likes": ("likes", "like_count", "reactions"),
    "comments": ("comments", "comment_count", "comments_count"),
    "shares": ("shares", "share_count"),
    "saves": ("saves", "saved", "save_count"),
    "reach": ("reach",),
}


def _pick_metrics(d: dict) -> dict:
    flat = dict(d)
    for k in ("post_metrics", "metrics", "stats"):
        if isinstance(d.get(k), dict):
            flat.update(d[k])
    out = {}
    for name, keys in METRIC_KEYS.items():
        for k in keys:
            v = flat.get(k)
            if isinstance(v, (int, float)):
                out[name] = v
                break
    return out


def post_analytics(api_key: str, request_id: str, session=requests) -> dict[str, dict]:
    """{platform: {views, likes, comments, shares, saves, reach}} for one upload."""
    body = _json(session.get(f"{API}/uploadposts/post-analytics/{request_id}", headers=_headers(api_key),
                             timeout=30), "Upload-Post analytics")
    src = body.get("platforms") or body.get("results") or body.get("data") or body
    out = {}
    if isinstance(src, dict):
        for p, v in src.items():
            if p in ("instagram", "facebook", "tiktok") and isinstance(v, dict):
                out[p] = _pick_metrics(v)
    elif isinstance(src, list):
        for v in src:
            if isinstance(v, dict) and v.get("platform") in ("instagram", "facebook", "tiktok"):
                out[v["platform"]] = _pick_metrics(v)
    return out
