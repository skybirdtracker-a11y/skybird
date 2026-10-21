"""Keep each day's videos on GitHub itself (as files attached to a "release" in this
repository), so Buffer can fetch them any time, for as long as needed.

Buffer doesn't keep its own copy of a scheduled video: it goes back to the link when
the post is published. Free file hosts delete files after a few hours, so a video
uploaded at 3:35 AM was gone before its 9 AM post. A release file's link is permanent.

Needs a PUBLIC repository (a private one's files need a login Buffer doesn't have) and
the workflow's built-in token with `contents: write`. Releases older than KEEP_DAYS are
removed so they don't pile up.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import requests

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
KEEP_DAYS = 14
TAG = "videos-{day}"


class NotAvailable(Exception):
    """GitHub storage can't be used (not on GitHub, no token, or the repo is private)."""


def _ctx():
    repo, token = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", "")
    if not repo or not token:
        raise NotAvailable("not running on GitHub with a token")
    return repo, {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                  "X-GitHub-Api-Version": "2022-11-28"}


def public(session=requests) -> bool:
    repo, h = _ctx()
    r = session.get(f"{API}/repos/{repo}", headers=h, timeout=30)
    if r.status_code != 200:
        raise NotAvailable(f"couldn't read the repository (HTTP {r.status_code})")
    return not r.json().get("private", True)


def _release(repo, h, day: date, session) -> dict:
    tag = TAG.format(day=day.isoformat())
    r = session.get(f"{API}/repos/{repo}/releases/tags/{tag}", headers=h, timeout=30)
    if r.status_code == 200:
        return r.json()
    r = session.post(f"{API}/repos/{repo}/releases", headers=h, timeout=30, json={
        "tag_name": tag, "name": f"SkyBird videos {day.isoformat()}",
        "body": "The day's SkyBird videos, kept here so Buffer can fetch them. Removed automatically after "
                f"{KEEP_DAYS} days.", "prerelease": True})
    if r.status_code not in (200, 201):
        raise NotAvailable(f"couldn't create the release (HTTP {r.status_code}: {r.text[:200]})")
    return r.json()


def upload(videos: list[Path], day: date, session=None) -> dict[str, str]:
    """Attach the videos to the day's release; returns {file name: public link}."""
    s = session or requests.Session()
    repo, h = _ctx()
    if not public(s):
        raise NotAvailable("the repository is private, so Buffer can't fetch its files. "
                           "Make it public: GitHub → Settings → General → Danger Zone → Change visibility")
    rel = _release(repo, h, day, s)
    existing = {a["name"]: a for a in rel.get("assets", [])}
    out = {}
    for v in videos:
        if v.name in existing:   # remade video: replace the old file
            s.delete(f"{API}/repos/{repo}/releases/assets/{existing[v.name]['id']}", headers=h, timeout=30)
        with open(v, "rb") as f:
            r = s.post(f"{UPLOADS}/repos/{repo}/releases/{rel['id']}/assets", params={"name": v.name},
                       headers=dict(h, **{"Content-Type": "video/mp4"}), data=f, timeout=600)
        if r.status_code not in (200, 201):
            raise NotAvailable(f"couldn't upload {v.name} (HTTP {r.status_code}: {r.text[:200]})")
        out[v.name] = r.json()["browser_download_url"]
    return out


def cleanup(today: date, session=None) -> int:
    """Remove video releases (and their tags) older than KEEP_DAYS. Returns how many."""
    s = session or requests.Session()
    repo, h = _ctx()
    r = s.get(f"{API}/repos/{repo}/releases", headers=h, params={"per_page": 100}, timeout=30)
    if r.status_code != 200:
        return 0
    n = 0
    for rel in r.json():
        tag = rel.get("tag_name", "")
        try:
            d = date.fromisoformat(tag.removeprefix("videos-"))
        except ValueError:
            continue
        if tag.startswith("videos-") and d < today - timedelta(days=KEEP_DAYS):
            s.delete(f"{API}/repos/{repo}/releases/{rel['id']}", headers=h, timeout=30)
            s.delete(f"{API}/repos/{repo}/git/refs/tags/{tag}", headers=h, timeout=30)
            n += 1
    return n
