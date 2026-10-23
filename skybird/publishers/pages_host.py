"""Serve each day's videos from GitHub Pages: https://<owner>.github.io/<repo>/videos/<day>/<file>.mp4

Buffer fetches a scheduled post's video again when it publishes, and needs a link
that plays as a video. GitHub Pages serves the files as real MP4s (video/mp4) at
permanent addresses. (Release files didn't work: their link redirects to a download
that expires after 30 minutes, which Buffer can't preview.)

The videos live on a separate `gh-pages` branch that is rewritten each time with only
the last KEEP_DAYS days (plus the small weekly reports), so the repository doesn't grow. Needs a public repository
with Pages turned on (Settings -> Pages -> Deploy from a branch -> gh-pages).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import requests

BRANCH = "gh-pages"
KEEP_DAYS = 3
WAIT = 1800   # seconds to wait for GitHub to publish the new files (usually 1-2 min)


class NotAvailable(Exception):
    """GitHub Pages can't be used right now (not on GitHub, Pages off, ...)."""


def base_url() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        raise NotAvailable("not running on GitHub")
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}"


def _git(*args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise NotAvailable(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def _rewrite(day: date, add, message: str) -> None:
    """Rebuild the gh-pages branch: the last KEEP_DAYS days of videos and every report
    already there, plus whatever `add(folder)` writes; then force-push it."""
    repo, token = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", "")
    if not repo or not token:
        raise NotAvailable("not running on GitHub with a token")
    remote = os.environ.get("SKYBIRD_PAGES_REMOTE") or f"https://x-access-token:{token}@github.com/{repo}.git"
    with tempfile.TemporaryDirectory() as tmp:
        old, new = Path(tmp) / "old", Path(tmp) / "new"
        old.mkdir(), new.mkdir()
        # keep the recent days already there (today's posts may still need yesterday's files)
        _git("init", "-q", cwd=old)
        fetched = subprocess.run(["git", "fetch", "-q", "--depth", "1", remote, BRANCH], cwd=old,
                                 capture_output=True, text=True).returncode == 0
        if fetched:
            _git("checkout", "-q", "FETCH_HEAD", cwd=old)
        keep_from = day - timedelta(days=KEEP_DAYS - 1)
        for d in sorted((old / "videos").glob("*")) if (old / "videos").exists() else []:
            try:
                if keep_from <= date.fromisoformat(d.name) <= day:
                    shutil.copytree(d, new / "videos" / d.name)
            except ValueError:
                continue
        if (old / "reports").exists():   # weekly reports stay for good (a few KB each)
            shutil.copytree(old / "reports", new / "reports")
        add(new)
        (new / ".nojekyll").write_text("")
        (new / "index.html").write_text("<!doctype html><title>SkyBird</title>"
                                        "<p>Video files for SkyBird's scheduled posts.</p>"
                                        "<p><a href='reports/latest.html'>Latest weekly report</a></p>")
        _git("init", "-q", "-b", BRANCH, cwd=new)
        _git("add", "-A", cwd=new)
        _git("-c", "user.name=SkyBird", "-c", "user.email=skybird@users.noreply.github.com",
             "commit", "-q", "-m", message, cwd=new)
        _git("push", "-q", "--force", remote, f"{BRANCH}:{BRANCH}", cwd=new)


def publish(videos: list[Path], day: date, session=None, wait: int = WAIT) -> dict[str, str]:
    """Put the videos on GitHub Pages; returns {file name: playable link} once they're live."""
    s = session or requests.Session()

    def add(new):
        target = new / "videos" / day.isoformat()
        if target.exists():
            shutil.rmtree(target)   # a remake replaces that day's files
        target.mkdir(parents=True, exist_ok=True)
        for v in videos:
            shutil.copy2(v, target / v.name)
    _rewrite(day, add, f"Videos for {day.isoformat()}")
    links = {v.name: f"{base_url()}/videos/{day.isoformat()}/{v.name}" for v in videos}
    _wait_live(list(links.values()), s, wait)
    return links


def publish_report(page: str, day: date) -> str:
    """Put a weekly report on GitHub Pages; returns its permanent link (reports/latest.html
    always shows the newest). Doesn't wait for GitHub to publish it."""
    def add(new):
        (new / "reports").mkdir(exist_ok=True)
        (new / "reports" / f"{day.isoformat()}.html").write_text(page, encoding="utf-8")
        (new / "reports" / "latest.html").write_text(page, encoding="utf-8")
    _rewrite(day, add, f"Weekly report {day.isoformat()}")
    return f"{base_url()}/reports/latest.html"


def _wait_live(urls: list[str], s, wait: int) -> None:
    """GitHub takes a minute or so to publish; wait until each link plays as a video."""
    deadline = time.time() + wait
    pending = list(urls)
    last = ""
    while pending and time.time() < deadline:
        u = pending[0]
        try:
            r = s.get(u, headers={"Range": "bytes=0-1023"}, timeout=30, stream=True)
            ctype = (r.headers.get("Content-Type") or "").lower()
            r.close()
            if r.status_code in (200, 206) and ctype.startswith("video/"):
                pending.pop(0)
                continue
            last = f"HTTP {r.status_code} {ctype}"
        except requests.RequestException as e:
            last = type(e).__name__
        time.sleep(15)
    if pending:
        hint = (" Turn on GitHub Pages: repository Settings -> Pages -> Build and deployment -> "
                "Deploy from a branch -> gh-pages / (root) -> Save.") if "404" in last else ""
        raise NotAvailable(f"the videos didn't appear on GitHub Pages within {wait // 60} min ({last}).{hint}")
