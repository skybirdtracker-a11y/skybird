from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import requests


class PublishError(Exception):
    pass


@dataclass
class PublishResult:
    platform: str
    ok: bool
    post_id: str = ""
    url: str = ""
    detail: str = ""
    request_id: str = ""   # Upload-Post request (used to fetch engagement later)


def check(resp: requests.Response, what: str) -> dict:
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:500]}
    err = body.get("error") if isinstance(body, dict) else None
    # TikTok always returns an "error" object; code "ok" means success.
    tiktok_ok = isinstance(err, dict) and err.get("code") == "ok"
    if resp.status_code >= 400 or (err and not tiktok_ok):
        raise PublishError(f"{what} failed (HTTP {resp.status_code}): {body}")
    return body


def poll(fn, *, every: float = 5, timeout: float = 600, what: str = "processing"):
    """Call fn() until it returns a truthy value; fn raises PublishError to abort."""
    deadline = time.monotonic() + timeout
    while True:
        result = fn()
        if result:
            return result
        if time.monotonic() > deadline:
            raise PublishError(f"Timed out waiting for {what}")
        time.sleep(every)


def read_video(path: Path) -> bytes:
    if not path.exists():
        raise PublishError(f"Video not found: {path}")
    return path.read_bytes()
