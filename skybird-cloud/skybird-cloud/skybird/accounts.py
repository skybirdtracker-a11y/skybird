"""Checks that each social account is connected and the token works."""
from __future__ import annotations

import json
from datetime import datetime

import requests

from .config import Config


def check_upload_post(cfg: Config) -> dict:
    from .publishers import upload_post
    key, out = cfg.secrets.get("upload_post_api_key"), {}
    want = cfg.upload_post_opts().get("profile", "")
    if not key:
        out = {p: {"ok": False, "error": "Upload-Post not connected yet (python -m skybird uploadpost-setup)"}
               for p in ("instagram", "facebook", "tiktok")}
    else:
        try:
            profiles = upload_post.list_profiles(key)
            prof = next((p for p in profiles if p.get("username") == want), None)
            if not prof:
                names = [p.get("username") for p in profiles]
                out = {p: {"ok": False, "error": f"No Upload-Post profile named '{want}'. Profiles: {names}"}
                       for p in ("instagram", "facebook", "tiktok")}
            else:
                linked = upload_post.connected_platforms(prof)
                for p in ("instagram", "facebook", "tiktok"):
                    out[p] = ({"ok": True, "name": linked[p] + " (via Upload-Post)"} if p in linked else
                              {"ok": False, "error": f"Not linked in Upload-Post profile '{want}'. Link it on upload-post.com"})
        except Exception as e:
            out = {p: {"ok": False, "error": str(e)[:200]} for p in ("instagram", "facebook", "tiktok")}
    out["backend"] = "upload_post"
    return out


def check_buffer(cfg: Config) -> dict:
    from .publishers import buffer
    sec = cfg.secrets
    if not sec.get("buffer_api_key"):
        return {p: {"ok": False, "error": "Buffer not connected yet (scripts\\connect_accounts.bat)"} for p in buffer.PLATFORMS}
    try:
        chs = {c["id"]: c for c in buffer.channels(sec["buffer_api_key"], sec.get("buffer_org_id", ""))}
    except Exception as e:
        return {p: {"ok": False, "error": str(e)[:200]} for p in buffer.PLATFORMS}
    out = {}
    for p in buffer.PLATFORMS:
        ch = chs.get(sec.get(f"buffer_channel_{p}", ""))
        out[p] = ({"ok": True, "name": f"{ch.get('name')} (via Buffer)"} if ch else
                  {"ok": False, "error": "Not connected in Buffer yet. Add the channel in Buffer, then run connect_accounts.bat again"})
    return out


def check_accounts(cfg: Config) -> dict:
    from .engine import backend as _backend
    backend = _backend(cfg)
    if backend in ("upload_post", "manual", "buffer"):
        out = check_upload_post(cfg) if backend == "upload_post" else check_buffer(cfg) if backend == "buffer" else {
            p: {"ok": True, "name": "Manual mode: you post from the post kit"} for p in ("instagram", "facebook", "tiktok")}
        out["backend"] = backend
        out["checked_at"] = datetime.now(cfg.tz).strftime("%b %d, %I:%M %p")
        (cfg.data_dir / "accounts.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out
    sec, out = cfg.secrets, {}
    v = cfg.section("facebook").get("graph_version", "v23.0")
    g = f"https://graph.facebook.com/{v}"
    if sec.get("fb_page_token"):
        try:
            r = requests.get(f"{g}/{sec['fb_page_id']}", params={"fields": "name", "access_token": sec["fb_page_token"]}, timeout=20).json()
            out["facebook"] = {"ok": "name" in r, "name": r.get("name", ""), "error": (r.get("error") or {}).get("message", "")}
        except Exception as e:
            out["facebook"] = {"ok": False, "error": str(e)}
        if sec.get("ig_user_id"):
            try:
                r = requests.get(f"{g}/{sec['ig_user_id']}", params={"fields": "username", "access_token": sec["fb_page_token"]}, timeout=20).json()
                out["instagram"] = {"ok": "username" in r, "name": "@" + r["username"] if "username" in r else "",
                                    "error": (r.get("error") or {}).get("message", "")}
            except Exception as e:
                out["instagram"] = {"ok": False, "error": str(e)}
        else:
            out["instagram"] = {"ok": False, "error": "No Instagram professional account is linked to the Facebook Page"}
    else:
        out["facebook"] = out["instagram"] = {"ok": False, "error": "Not connected yet"}
    if sec.get("tiktok_refresh_token"):
        try:
            from .publishers import tiktok
            if tiktok.ensure_fresh_token(sec):
                cfg.save_secrets()
            r = requests.get("https://open.tiktokapis.com/v2/user/info/", params={"fields": "display_name"},
                             headers={"Authorization": f"Bearer {sec['tiktok_access_token']}"}, timeout=20).json()
            name = r.get("data", {}).get("user", {}).get("display_name", "")
            out["tiktok"] = {"ok": bool(name), "name": name, "error": (r.get("error") or {}).get("message", "") if not name else "",
                             "mode": cfg.section("tiktok").get("mode", "inbox")}
        except Exception as e:
            out["tiktok"] = {"ok": False, "error": str(e)}
    else:
        out["tiktok"] = {"ok": False, "error": "Not connected yet"}
    out["checked_at"] = datetime.now(cfg.tz).strftime("%b %d, %I:%M %p")
    (cfg.data_dir / "accounts.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def cached_accounts(cfg: Config) -> dict:
    try:
        return json.loads((cfg.data_dir / "accounts.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
