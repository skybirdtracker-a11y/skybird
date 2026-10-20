"""Instagram Reels + Facebook Page Reels via the Meta Graph API.

Both upload the local .mp4 straight to Meta (resumable upload), so the videos
never need to be hosted on a public website.

Secrets used (written by `python -m skybird meta-setup`):
  fb_page_id, fb_page_token, ig_user_id
"""
from __future__ import annotations

from pathlib import Path

import requests

from .base import PublishError, PublishResult, check, poll, read_video

GRAPH = "https://graph.facebook.com"
RUPLOAD = "https://rupload.facebook.com"


def _need(secrets: dict, *keys: str) -> None:
    missing = [k for k in keys if not secrets.get(k)]
    if missing:
        raise PublishError(f"Missing in secrets.json: {', '.join(missing)} (run: python -m skybird meta-setup)")


def post_instagram_reel(video: Path, caption: str, secrets: dict, *, version: str = "v23.0",
                        share_to_feed: bool = True, session: requests.Session | None = None,
                        poll_every: float = 5) -> PublishResult:
    _need(secrets, "ig_user_id", "fb_page_token")
    s = session or requests.Session()
    token, ig = secrets["fb_page_token"], secrets["ig_user_id"]
    data = read_video(video)

    # 1) create a resumable Reels container
    body = check(s.post(f"{GRAPH}/{version}/{ig}/media", data={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption,
        "share_to_feed": "true" if share_to_feed else "false", "access_token": token,
    }, timeout=60), "Instagram create container")
    container = body["id"]

    # 2) upload the file bytes
    check(s.post(f"{RUPLOAD}/ig-api-upload/{version}/{container}", headers={
        "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(len(data)),
    }, data=data, timeout=600), "Instagram video upload")

    # 3) wait for Instagram to process the video
    def ready():
        st = check(s.get(f"{GRAPH}/{version}/{container}", params={
            "fields": "status_code,status", "access_token": token}, timeout=30), "Instagram status")
        code = st.get("status_code")
        if code == "FINISHED":
            return True
        if code in ("ERROR", "EXPIRED"):
            raise PublishError(f"Instagram processing {code}: {st.get('status')}")
        return False
    poll(ready, every=poll_every, what="Instagram processing")

    # 4) publish
    pub = check(s.post(f"{GRAPH}/{version}/{ig}/media_publish", data={
        "creation_id": container, "access_token": token}, timeout=60), "Instagram publish")
    media_id = pub["id"]
    url = ""
    try:
        url = s.get(f"{GRAPH}/{version}/{media_id}", params={
            "fields": "permalink", "access_token": token}, timeout=30).json().get("permalink", "")
    except Exception:
        pass
    return PublishResult("instagram", True, media_id, url)


def post_facebook_reel(video: Path, caption: str, secrets: dict, *, version: str = "v23.0",
                       session: requests.Session | None = None) -> PublishResult:
    _need(secrets, "fb_page_id", "fb_page_token")
    s = session or requests.Session()
    token, page = secrets["fb_page_token"], secrets["fb_page_id"]
    data = read_video(video)

    # 1) start
    start = check(s.post(f"{GRAPH}/{version}/{page}/video_reels", data={
        "upload_phase": "start", "access_token": token}, timeout=60), "Facebook start upload")
    video_id = start["video_id"]
    upload_url = start.get("upload_url") or f"{RUPLOAD}/video-upload/{version}/{video_id}"

    # 2) upload bytes
    check(s.post(upload_url, headers={
        "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(len(data)),
    }, data=data, timeout=600), "Facebook video upload")

    # 3) finish + publish
    check(s.post(f"{GRAPH}/{version}/{page}/video_reels", data={
        "upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED",
        "description": caption, "access_token": token}, timeout=60), "Facebook publish")
    return PublishResult("facebook", True, video_id, f"https://www.facebook.com/reel/{video_id}")


# ---- one-time setup helper -------------------------------------------------

def setup_tokens(app_id: str, app_secret: str, short_user_token: str, *, version: str = "v23.0",
                 page_name: str | None = None, session: requests.Session | None = None) -> dict:
    """Short-lived user token (from Graph API Explorer) -> long-lived user token ->
    never-expiring Page token, plus the Page id and linked Instagram account id."""
    s = session or requests.Session()
    ll = check(s.get(f"{GRAPH}/{version}/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": app_secret,
        "fb_exchange_token": short_user_token}, timeout=30), "Exchange for long-lived token")
    user_token = ll["access_token"]

    pages = check(s.get(f"{GRAPH}/{version}/me/accounts", params={
        "access_token": user_token, "fields": "id,name,access_token"}, timeout=30), "List Pages")["data"]
    if not pages:
        raise PublishError("No Facebook Pages found for this login. Create the Page first and grant pages_show_list.")
    if page_name:
        match = [p for p in pages if p["name"].lower() == page_name.lower()]
        if not match:
            raise PublishError(f"Page '{page_name}' not found. Pages: {[p['name'] for p in pages]}")
        page = match[0]
    else:
        page = pages[0]

    ig = check(s.get(f"{GRAPH}/{version}/{page['id']}", params={
        "fields": "instagram_business_account", "access_token": page["access_token"]},
        timeout=30), "Find linked Instagram account").get("instagram_business_account", {})
    return {
        "meta_app_id": app_id,
        "fb_page_id": page["id"],
        "fb_page_name": page["name"],
        "fb_page_token": page["access_token"],
        "ig_user_id": ig.get("id", ""),
    }
