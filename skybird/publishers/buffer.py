"""Buffer (buffer.com): posts to Instagram, Facebook and TikTok through Buffer's
approved apps. Free plan = 3 channels, API included. No developer accounts.

Buffer's API takes a public video URL instead of a file, so SkyBird first uploads
the video to a free file host (litterbox.catbox.moe, auto-deletes after 72 h;
catbox.moe, uguu.se and tmpfiles.org as backups) and hands Buffer that link. The video is going public on your pages anyway.

Secrets (written by `python -m skybird buffer-setup`):
  buffer_api_key, buffer_org_id, buffer_channel_instagram / _facebook / _tiktok
"""
from __future__ import annotations

from datetime import datetime, timezone
from itertools import product
from pathlib import Path

import requests

from .base import PublishError, PublishResult

API = "https://api.buffer.com"
LITTERBOX = "https://litterbox.catbox.moe/resources/internals/api.php"
PLATFORMS = ("instagram", "facebook", "tiktok")

CREATE_POST = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id } }
    ... on MutationError { message }
  }
}"""

ORGS = "query { account { organizations { id name } } }"
CHANNELS = """
query GetChannels($input: ChannelsInput!) {
  channels(input: $input) { id name service }
}"""


def gql(api_key: str, query: str, variables: dict | None = None, session=requests) -> dict:
    r = session.post(API, json={"query": query, "variables": variables or {}},
                     headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                     timeout=60)
    try:
        body = r.json()
    except ValueError:
        raise PublishError(f"Buffer returned HTTP {r.status_code}: {r.text[:300]}")
    if r.status_code == 401 or r.status_code == 403:
        raise PublishError("Buffer rejected the API key. Create a new one in Buffer → Settings → API and run buffer-setup again.")
    if r.status_code == 429:
        raise PublishError("Buffer rate limit reached. Wait a few minutes; nothing was retried automatically.")
    if body.get("errors"):
        raise PublishError("Buffer: " + "; ".join(e.get("message", str(e)) for e in body["errors"]))
    if r.status_code >= 400:
        raise PublishError(f"Buffer returned HTTP {r.status_code}: {body}")
    return body.get("data") or {}


# ---- setup ------------------------------------------------------------------------

def organizations(api_key: str, session=requests) -> list[dict]:
    return (gql(api_key, ORGS, session=session).get("account") or {}).get("organizations") or []


def channels(api_key: str, org_id: str, session=requests) -> list[dict]:
    return gql(api_key, CHANNELS, {"input": {"organizationId": org_id}}, session=session).get("channels") or []


def match_channels(chs: list[dict]) -> dict[str, dict]:
    """{platform: channel} using Buffer's `service` names."""
    out = {}
    for ch in chs:
        svc = str(ch.get("service", "")).lower()
        for p in PLATFORMS:
            if svc.startswith(p) and p not in out:
                out[p] = ch
    return out


# ---- hosting ----------------------------------------------------------------------

def _litterbox(r):
    return (r.text or "").strip()


def _uguu(r):
    return ((r.json().get("files") or [{}])[0]).get("url", "")


def _tmpfiles(r):
    # The page link is https://tmpfiles.org/123/x.mp4; the file itself is under /dl/
    return (r.json().get("data") or {}).get("url", "").replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)


# Tried in order until one works. (name, url, form data, file field, reply parser)
# uguu.se first: in Oct 2026 it was the only one accepting uploads from GitHub's
# servers (catbox blocks them, 0x0.st closed uploads, litterbox timed out).
# How long each keeps the file (hours); scheduled posts skip ones that expire too soon.
LIFE_HOURS = {"litterbox.catbox.moe": 72, "catbox.moe": 10 ** 6, "0x0.st": 720, "uguu.se": 3, "tmpfiles.org": 1}
HOSTS = [
    ("uguu.se", "https://uguu.se/upload", {}, "files[]", _uguu),
    ("litterbox.catbox.moe", LITTERBOX, {"reqtype": "fileupload", "time": "72h"}, "fileToUpload", _litterbox),
    ("catbox.moe", "https://catbox.moe/user/api.php", {"reqtype": "fileupload"}, "fileToUpload", _litterbox),
    ("0x0.st", "https://0x0.st", {}, "file", _litterbox),
    ("tmpfiles.org", "https://tmpfiles.org/api/v1/upload", {}, "file", _tmpfiles),
]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def _short(text: str) -> str:
    """'412 Precondition Failed' instead of a page of nginx HTML."""
    import re
    m = re.search(r"<title>(.*?)</title>", text or "", re.S | re.I)
    return (m.group(1) if m else (text or "")).strip()[:120]


def _readable(link: str, session) -> str:
    """'' if the link serves the video file itself, else why not. Buffer (and
    TikTok behind it) must be able to download it, not get a web page."""
    try:
        r = session.get(link, headers={"User-Agent": UA, "Range": "bytes=0-1023"}, timeout=60, stream=True)
        ctype = (r.headers.get("Content-Type") or "").lower()
        r.close()
    except requests.RequestException as e:
        return f"link didn't open ({type(e).__name__})"
    if r.status_code not in (200, 206):
        return f"link gave HTTP {r.status_code}"
    if ctype.startswith("text/") or "html" in ctype:
        return f"link opens a web page ({ctype}), not the video"
    return ""


OFFLINE = ("This PC couldn't reach the internet when it tried to post (Wi-Fi down, PC waking from sleep, "
           "or a VPN/antivirus blocking Python). Nothing was sent. SkyBird tries again on its own every 10 minutes.")


def online(session=requests) -> bool:
    """Can this PC reach the internet at all? (Buffer's own server is the test.)"""
    for url in (API, "https://www.google.com"):
        try:
            session.get(url, headers={"User-Agent": UA}, timeout=15)
            return True
        except requests.RequestException:
            continue
    return False


def _net_why(e: Exception) -> str:
    """A short, plain reason for a network error instead of just its name."""
    t = str(e)
    if any(w in t for w in ("NameResolution", "getaddrinfo", "Name or service not known", "nodename nor servname")):
        return "couldn't look up the address (internet or DNS down?)"
    if any(w in t for w in ("10054", "Connection reset", "ConnectionResetError", "RemoteDisconnected", "Connection aborted")):
        return "the connection was cut off partway (upload too big or blocked?)"
    if "timed out" in t.lower() or isinstance(e, requests.Timeout):
        return "timed out"
    if any(w in t for w in ("10061", "refused")):
        return "connection refused"
    if "SSL" in t or "certificate" in t.lower():
        return "secure-connection (SSL) error, often antivirus/VPN"
    return type(e).__name__


def host_video(video: Path, session=requests, skip: tuple[str, ...] = (), min_hours: float = 0) -> tuple[str, str]:
    """Upload to a free public file host Buffer can download from; returns
    (url, host name). If a host refuses or its link doesn't serve the video
    (hosts come and go), the next one is tried."""
    errors = []
    if not online(session):
        raise PublishError(OFFLINE)
    for name, url, data, field, parse in HOSTS:
        if name in skip or LIFE_HOURS.get(name, 0) < min_hours:
            continue
        try:
            with open(video, "rb") as f:
                r = session.post(url, data=data, files={field: (video.name, f, "video/mp4")},
                                 headers={"User-Agent": UA}, timeout=(15, 180))
            link = parse(r) if r.status_code == 200 else ""
        except (requests.RequestException, ValueError, AttributeError, IndexError) as e:
            errors.append(f"{name}: {_net_why(e)}")
            continue
        if not link.startswith("https://"):
            msg = _short(r.text)
            errors.append(f"{name}: {msg if msg.startswith(str(r.status_code)) else f'HTTP {r.status_code} {msg}'.strip()}")
            continue
        why = _readable(link, session)
        if why:
            errors.append(f"{name}: {why}")
            continue
        return link, name
    if not errors:
        raise PublishError(f"No free file host keeps videos {min_hours:.0f}+ hours (until the post time), so Buffer "
                           "would lose the video. Make the GitHub repository public so SkyBird can keep them there.")
    raise PublishError("Couldn't upload the video to any temporary host (" + "; ".join(errors) + ")")


# ---- numbers ----------------------------------------------------------------------

# Buffer's API reports each sent post's numbers as a list of {type, value}.
# Two query shapes are tried (Buffer's API is in beta); a wrong shape is just an error.
POST_QUERIES = ("query { post(input: {id: %s}) { id metrics { type value } } }",
                "query { post(id: %s) { id metrics { type value } } }")
METRIC_NAMES = {"views": "views", "plays": "views", "videoviews": "views", "impressions": "impressions",
                "reach": "reach", "reactions": "likes", "likes": "likes", "comments": "comments",
                "shares": "shares", "reposts": "shares", "saves": "saves", "clicks": "clicks",
                "engagementrate": "engagement_rate", "follows": "follows"}


def _metrics_of(post: dict, field: str = "metrics") -> dict:
    """Buffer's numbers as {"views": .., "likes": ..}. They come either as a list of
    {type, value} or as one object of named numbers."""
    raw = post.get(field)
    pairs = []
    if isinstance(raw, list):
        for m in raw:
            if isinstance(m, dict):
                pairs.append((m.get("type") or m.get("name") or m.get("metric") or "", m.get("value")))
    elif isinstance(raw, dict):
        pairs = list(raw.items())
    out = {}
    for typ, val in pairs:
        name = METRIC_NAMES.get(str(typ).replace("_", "").lower())
        if name and isinstance(val, (int, float)) and not isinstance(val, bool):
            # "reactions" and "likes" can both be reported for one post: keep the larger
            out[name] = max(out.get(name, 0), val) if name == "likes" else \
                out.get(name, 0) + val if name == "shares" else val
    if not out.get("views"):   # Facebook reports impressions/reach, not plays
        out["views"] = out.get("impressions") or out.get("reach") or out.get("views")
    if out.get("views") is None:
        out.pop("views", None)
    return out


def _fatal(e: Exception) -> bool:
    return "rejected the API key" in str(e) or "rate limit" in str(e)


# Buffer's API is in beta and its query shapes have changed under us, so SkyBird asks
# the API itself (GraphQL introspection) how one post is looked up and what it reports.
_TYPE = """query($n: String!) { __type(name: $n) { fields { name args { name type { ...T } } type { ...T } }
  inputFields { name type { ...T } } } }
fragment T on __Type { kind name ofType { kind name ofType { kind name ofType { kind name } } } }"""
PLAIN_FIELDS = ("id", "status", "sentAt", "dueAt", "externalLink", "serviceLink", "error")
METRIC_WORDS = ("metric", "statistic", "analytic", "insight", "engagement")
_SHAPE: dict = {}


def _named(t: dict) -> tuple[str, str]:
    """(kind, name) of a GraphQL type with its NON_NULL / LIST wrappers removed."""
    while t and t.get("ofType") and t.get("kind") in ("NON_NULL", "LIST"):
        t = t["ofType"]
    return (t or {}).get("kind", ""), (t or {}).get("name", "")


def _type(api_key: str, name: str, session) -> dict:
    return gql(api_key, _TYPE, {"n": name}, session=session).get("__type") or {}


def post_shape(api_key: str, session=requests) -> dict | None:
    """{"arg": 'input: {id: %s}', "fields": "id status ...", "metrics": field or ""}, or
    None if Buffer won't describe itself (then the fixed guesses below are used)."""
    if api_key in _SHAPE:
        return _SHAPE[api_key]
    try:
        query = _type(api_key, "Query", session)
        field = next((f for f in query.get("fields") or [] if f["name"] == "post"), None)
        if not field:
            return None
        arg = ""
        for a in field.get("args") or []:
            kind, tname = _named(a["type"])
            if a["name"] in ("id", "postId") and kind == "SCALAR":
                arg = f"{a['name']}: %s"
            elif kind == "INPUT_OBJECT":
                names = [f["name"] for f in _type(api_key, tname, session).get("inputFields") or []]
                key = next((n for n in ("id", "postId") if n in names), "")
                arg = f"{a['name']}: {{{key}: %s}}" if key else ""
            if arg:
                break
        if not arg:
            return None
        fields, metrics = [], ""
        for f in _type(api_key, _named(field["type"])[1], session).get("fields") or []:
            if any(a["type"].get("kind") == "NON_NULL" for a in f.get("args") or []):
                continue   # needs arguments we can't guess
            kind, tname = _named(f["type"])
            if kind in ("SCALAR", "ENUM") and f["name"] in PLAIN_FIELDS:
                fields.append(f["name"])
            elif kind == "OBJECT" and not metrics and any(w in f["name"].lower() for w in METRIC_WORDS):
                subs = [g["name"] for g in _type(api_key, tname, session).get("fields") or []
                        if _named(g["type"])[0] in ("SCALAR", "ENUM")
                        and not any(a["type"].get("kind") == "NON_NULL" for a in g.get("args") or [])]
                if subs:
                    metrics = f"{f['name']} {{ {' '.join(subs)} }}"
        _SHAPE[api_key] = {"arg": arg, "fields": " ".join(fields or ["id"]), "metrics": metrics}
    except PublishError as e:
        if _fatal(e):
            raise
        return None
    return _SHAPE[api_key]


def _read_post(api_key: str, post_id: str, guesses: tuple[str, ...], want_metrics: bool, session) -> tuple[dict | None, str]:
    """(the post as Buffer returns it, name of its numbers field). Every error is kept,
    so a failure says what Buffer actually objected to."""
    import json
    shape = post_shape(api_key, session)
    if shape:
        sel = shape["fields"] + (" " + shape["metrics"] if want_metrics and shape["metrics"] else "")
        guesses = (f"query {{ post({shape['arg']}) {{ {sel} }} }}",)
    errors = []
    for q in guesses:
        try:
            data = gql(api_key, q % json.dumps(post_id), session=session)
        except PublishError as e:
            if _fatal(e):
                raise
            errors.append(str(e).replace("Buffer: ", "", 1))
            continue
        field = shape["metrics"].split(" ", 1)[0] if shape and shape["metrics"] else "metrics"
        return data.get("post"), field
    raise PublishError("Buffer: " + " | ".join(dict.fromkeys(errors)) if errors else "Buffer didn't answer")


def post_metrics(api_key: str, post_id: str, session=requests) -> dict:
    """{"views": .., "likes": .., ...} for a post Buffer sent; {} if it has no numbers yet."""
    post, field = _read_post(api_key, post_id, POST_QUERIES, True, session)
    return _metrics_of(post or {}, field)


# Used only if Buffer won't describe its API (see post_shape).
STATUS_QUERIES = ("query { post(input: {id: %s}) { id status sentAt } }",
                  "query { post(input: {id: %s}) { id status } }",
                  "query { post(id: %s) { id status } }")


def post_status(api_key: str, post_id: str, session=requests) -> str:
    """'sent', 'failed', 'waiting' (still queued) or 'unknown' for a post Buffer has."""
    p, _ = _read_post(api_key, post_id, STATUS_QUERIES, False, session)
    if p is None:
        return "unknown"
    st = str(p.get("status") or "").lower()
    if p.get("sentAt") or st in ("sent", "published", "posted"):
        return "sent"
    if any(w in st for w in ("error", "fail", "rejected")):
        return "failed"
    return "waiting" if st in ("buffer", "scheduled", "queued", "pending", "sending", "draft", "") else "unknown"


# ---- posting ----------------------------------------------------------------------

# Instagram and Facebook need to be told it's a Reel. Tried in order; a shape
# Buffer's schema doesn't know is rejected before anything posts.
METADATA = {
    "instagram": ({"instagram": {"type": "reel", "shouldShareToFeed": True}}, {"instagram": {"type": "reel"}}),
    "facebook": ({"facebook": {"type": "reel"}},),
}
SCHEMA_WORDS = ("videos", "video", "AssetInput", "AssetsInput", "not defined", "Unknown field",
                "invalid value", "Expected type", "required type", "shouldShareToFeed", "metadata")


def _create(api_key: str, channel_id: str, caption: str, video_url: str, platform: str, session,
            due: datetime | None = None) -> str:
    base = {"channelId": channel_id, "text": caption, "schedulingType": "automatic", "mode": "shareNow"}
    if due:   # goes into Buffer's queue for that time; you can still edit or delete it in Buffer
        base.update(mode="customScheduled", dueAt=due.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    # Buffer's docs describe the video asset two ways; try the list form first and
    # fall back on a schema error (schema errors are rejected before anything posts).
    last = None
    shapes = ({"videos": [{"url": video_url}]}, [{"video": {"url": video_url}}])
    for meta, assets in product(METADATA.get(platform, (None,)), shapes):
        inp = dict(base, assets=assets)
        if meta:
            inp["metadata"] = meta
        try:
            data = gql(api_key, CREATE_POST, {"input": inp}, session=session)
        except PublishError as e:
            if any(w in str(e) for w in SCHEMA_WORDS):
                last = e
                continue
            raise
        res = data.get("createPost") or {}
        if res.get("__typename") == "MutationError" or res.get("message"):
            raise PublishError(f"Buffer ({platform}): {res.get('message', res)}")
        post_id = ((res.get("post") or {}).get("id")) or ""
        if not post_id:
            raise PublishError(f"Buffer ({platform}) gave an unexpected reply: {res}")
        return post_id
    raise last or PublishError("Buffer rejected the video format")


def post(video: Path, captions: dict[str, str], secrets: dict,
         session: requests.Session | None = None, due: datetime | None = None,
         video_url: str | None = None) -> dict[str, PublishResult]:
    """Post now, or with `due` schedule it in Buffer's queue for that time.
    `video_url`: a permanent link to the video (GitHub); otherwise a free file host is used."""
    key = secrets.get("buffer_api_key")
    if not key:
        raise PublishError("Buffer isn't connected (run: python -m skybird buffer-setup)")
    if not video.exists():
        raise PublishError(f"Video not found: {video}")
    s = session or requests.Session()
    # Buffer does NOT keep its own copy of a scheduled video: it fetches the link again
    # when it publishes. So the link must outlive the wait until the post time.
    life = max(0.0, (due - datetime.now(timezone.utc)).total_seconds() / 3600) + 1 if due else 0
    if video_url:
        url, host = video_url, "GitHub"
    else:
        url, host = host_video(video, s, min_hours=life)
    used = [host]
    out = {}
    for platform, caption in captions.items():
        ch = secrets.get(f"buffer_channel_{platform}")
        if not ch:
            out[platform] = PublishResult(platform, False, detail=f"No Buffer channel saved for {platform}. Connect it in Buffer, then run buffer-setup.")
            continue
        while True:
            try:
                pid = _create(key, ch, caption, url, platform, s, due)
                what = (f"Scheduled in Buffer for {due.strftime('%I:%M %p').lstrip('0')} (edit or delete it in the Buffer app)"
                        if due else "Sent via Buffer")
                out[platform] = PublishResult(platform, True, pid, "", f"{what} (video from {host}; check Buffer's Sent tab)")
            except PublishError as e:
                if "read from its URL" in str(e) or "could not be read" in str(e).lower():
                    try:  # Buffer couldn't download from this host: re-upload to the next one
                        url, host = host_video(video, s, skip=tuple(used), min_hours=life)
                        used.append(host)
                        continue
                    except PublishError:
                        pass
                out[platform] = PublishResult(platform, False, detail=f"{e} (video host: {', '.join(used)})")
            break
    return out
