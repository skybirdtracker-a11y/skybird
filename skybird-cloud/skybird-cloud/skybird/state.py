"""Per-day state: what was prepared, what you approved, what got posted.

Everything for a post date lives in data/<post_date>/state.json so the dashboard,
the scheduler and the CLI all read and write the same file.
"""
from __future__ import annotations

import json
import os
import threading
import time as _time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from .config import Config

_thread_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()

# Post statuses
PENDING = "pending"        # prepared, waiting for your approval
APPROVED = "approved"      # approved, waits for its time slot
SKIPPED = "skipped"        # you chose not to post it today
POSTING = "posting"
POSTED = "posted"          # every selected platform succeeded
READY = "ready"            # manual mode: post kit is ready for you to post from your phone
PARTIAL = "partial"        # some platforms failed (retry from the dashboard)
FAILED = "failed"
MISSED = "missed"          # never approved / past the late window


def day_dir(cfg: Config, post_date: date) -> Path:
    p = cfg.data_dir / post_date.isoformat()
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_path(cfg: Config, post_date: date) -> Path:
    return day_dir(cfg, post_date) / "state.json"


def load_state(cfg: Config, post_date: date) -> dict | None:
    p = state_path(cfg, post_date)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def save_state(cfg: Config, post_date: date, state: dict) -> None:
    p = state_path(cfg, post_date)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(p)


@contextmanager
def locked(cfg: Config, name: str = "state", timeout: float = 600):
    """Cross-process lock (works on Windows and Linux) so the dashboard, the
    scheduler thread and a manual CLI run never post the same thing twice."""
    lock_file = cfg.data_dir / f".{name}.lock"
    with _guard:
        tlock = _thread_locks.setdefault(name, threading.Lock())
    with tlock:
        start = _time.monotonic()
        while True:
            try:
                fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                break
            except FileExistsError:
                # Stale lock from a crashed process (older than timeout)?
                try:
                    if _time.time() - lock_file.stat().st_mtime > timeout:
                        lock_file.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if _time.monotonic() - start > timeout:
                    raise TimeoutError(f"Could not get lock {lock_file}")
                _time.sleep(0.2)
        try:
            yield
        finally:
            lock_file.unlink(missing_ok=True)


def log(cfg: Config, message: str) -> None:
    stamp = datetime.now(cfg.tz).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    with open(cfg.data_dir / "activity.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def recent_log(cfg: Config, n: int = 40) -> list[str]:
    p = cfg.data_dir / "activity.log"
    if not p.exists():
        return []
    return p.read_text(encoding="utf-8").splitlines()[-n:][::-1]
