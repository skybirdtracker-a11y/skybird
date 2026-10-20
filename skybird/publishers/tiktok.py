"""TikTok Content Posting API.

mode "direct": publishes to the profile (public only after TikTok approves the
               app audit; before that TikTok only allows SELF_ONLY posts).
mode "inbox":  uploads to your TikTok inbox; you open the app and tap Post.

Secrets used (written by `python -m skybird tiktok-auth`):
  tiktok_client_key, tiktok_client_secret, tiktok_redirect_uri,
  tiktok_access_token, tiktok_refresh_token, tiktok_expires_at
"""
from __future__ import annotations

import math
import secrets as _secrets
import time
import urllib.parse
from pathlib import Path

import requests

from .base import PublishError, PublishResult, check, poll, read_video

API = "https://open.tiktokapis.com/v2"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
SCOPES = "user.info.basic,video.publish,video.upload,video.list"
CHUNK = 10 * 1024 * 1024  # 10 MB (TikTok: 5–64 MB per chunk; files < 5 MB go in one piece)


# ---- auth ------------------------------------------------------------------

def authorize_url(client_key: str, redirect_uri: str, state: str | None = None) -> str:
    q = {"client_key": client_key, "scope": SCOPES, "response_type": "code",
         "redirect_uri": redirect_uri, "state": state or _secrets.token_urlsafe(12)}
    return AUTH_URL + "?" + urllib.parse.urlencode(q)


def _save_token(secrets: dict, body: dict) -> dict:
    if "access_token" not in body:
        raise PublishError(f"TikTok token response had no access_token: {body}")
    secrets["tiktok_access_token"] = body["access_token"]
    secrets["tiktok_refresh_token"] = body.get("refresh_token", secrets.get("tiktok_refresh_token"))
    secrets["tiktok_expires_at"] = int(time.time()) + int(body.get("expires_in", 86400))
    secrets["tiktok_open_id"] = body.get("open_id", secrets.get("tiktok_open_id", ""))
    return secrets


def exchange_code(secrets: dict, code: str, session: requests.Session | None = None) -> dict:
    s = session or requests.Session()
    body = check(s.post(f"{API}/oauth/token/", data={
        "client_key": secrets["tiktok_client_key"], "client_secret": secrets["tiktok_client_secret"],
        "code": code, "grant_type": "authorization_code", "redirect_uri": secrets["tiktok_redirect_uri"],
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30), "TikTok code exchange")
    return _save_token(secrets, body)


def ensure_fresh_token(secrets: dict, session: requests.Session | None = None) -> bool:
    """Refresh the 24h access token when it has < 1h left. Returns True if refreshed
    (caller should save secrets.json)."""
    if not secrets.get("tiktok_refresh_token"):
        raise PublishError("TikTok not connected (run: python -m skybird tiktok-auth)")
    if int(secrets.get("tiktok_expires_at", 0)) - time.time() > 3600:
        return False
    s = session or requests.Session()
    body = check(s.post(f"{API}/oauth/token/", data={
        "client_key": secrets["tiktok_client_key"], "client_secret": secrets["tiktok_client_secret"],
        "grant_type": "refresh_token", "refresh_token": secrets["tiktok_refresh_token"],
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30), "TikTok token refresh")
    _save_token(secrets, body)
    return True


# ---- posting ---------------------------------------------------------------

def _chunks(size: int) -> tuple[int, int]:
    if size <= CHUNK:
        return size, 1
    return CHUNK, math.floor(size / CHUNK)  # last chunk carries the remainder


def post_video(video: Path, caption: str, secrets: dict, opts: dict,
               session: requests.Session | None = None, poll_every: float = 5) -> PublishResult:
    s = session or requests.Session()
    headers = {"Authorization": f"Bearer {secrets['tiktok_access_token']}",
               "Content-Type": "application/json; charset=UTF-8"}
    data = read_video(video)
    size = len(data)
    chunk_size, count = _chunks(size)
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size,
              "total_chunk_count": count}
    mode = opts.get("mode", "inbox")

    if mode == "direct":
        info = check(s.post(f"{API}/post/publish/creator_info/query/", headers=headers,
                            json={}, timeout=30), "TikTok creator info")["data"]
        allowed = info.get("privacy_level_options", [])
        privacy = opts.get("privacy_level", "PUBLIC_TO_EVERYONE")
        if allowed and privacy not in allowed:
            privacy = "SELF_ONLY" if "SELF_ONLY" in allowed else allowed[0]
        body = {"post_info": {
                    "title": caption[:2200], "privacy_level": privacy,
                    "disable_comment": bool(opts.get("disable_comment", False)),
                    "disable_duet": bool(opts.get("disable_duet", False)),
                    "disable_stitch": bool(opts.get("disable_stitch", False)),
                    "video_cover_timestamp_ms": 1000},
                "source_info": source}
        init = check(s.post(f"{API}/post/publish/video/init/", headers=headers, json=body,
                            timeout=60), "TikTok init")["data"]
    else:
        init = check(s.post(f"{API}/post/publish/inbox/video/init/", headers=headers,
                            json={"source_info": source}, timeout=60), "TikTok inbox init")["data"]
    publish_id, upload_url = init["publish_id"], init["upload_url"]

    for i in range(count):
        start = i * chunk_size
        end = size - 1 if i == count - 1 else start + chunk_size - 1
        r = s.put(upload_url, data=data[start:end + 1], headers={
            "Content-Type": "video/mp4", "Content-Length": str(end - start + 1),
            "Content-Range": f"bytes {start}-{end}/{size}"}, timeout=600)
        if r.status_code not in (200, 201, 206):
            raise PublishError(f"TikTok chunk {i + 1}/{count} upload failed: HTTP {r.status_code} {r.text[:300]}")

    done_states = {"PUBLISH_COMPLETE"} if mode == "direct" else {"SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"}

    def finished():
        st = check(s.post(f"{API}/post/publish/status/fetch/", headers=headers,
                          json={"publish_id": publish_id}, timeout=30), "TikTok status")["data"]
        status = st.get("status")
        if status in done_states:
            return st
        if status == "FAILED":
            raise PublishError(f"TikTok processing failed: {st.get('fail_reason')}")
        return None
    st = poll(finished, every=poll_every, what="TikTok processing")
    ids = st.get("publicaly_available_post_id") or st.get("publicly_available_post_id") or []
    detail = "Sent to your TikTok inbox: open TikTok and tap Post" if mode == "inbox" else ""
    return PublishResult("tiktok", True, str(ids[0]) if ids else publish_id, "", detail)
