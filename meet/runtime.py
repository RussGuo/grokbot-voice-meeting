"""Small, livekit-free helpers for the worker: where run artifacts live, run ids, the join wait."""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

log = logging.getLogger("voice-meeting")

DEFAULT_JOIN_WAIT_S = 1800.0   # matches the 30 min room empty_timeout set by scripts/mklink.py
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def runs_root() -> Path:
    """`runs/` under MEETING_HOME if set, else the current working directory (never the package location)."""
    home = os.environ.get("MEETING_HOME")
    return (Path(home) if home else Path.cwd()) / "runs"


def make_run_id(room: str, now: float | None = None, env: dict | None = None) -> str:
    """`<room>-<YYYYmmdd-HHMMSS>`. MEETING_RUN_ID, when set, is used verbatim (scripts/run_sim.sh relies on it).

    `meet followup --room ROOM` resolves the newest `runs/<room>-*`, so keep this format stable."""
    env = os.environ if env is None else env
    explicit = (env.get("MEETING_RUN_ID") or "").strip()
    if explicit:
        return explicit
    slug = _UNSAFE.sub("-", room or "").strip("-") or "room"
    return f"{slug}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}"


def join_wait_s(env: dict | None = None) -> float:
    """MEETING_JOIN_WAIT_S seconds to wait for the human to open the link (default 1800)."""
    env = os.environ if env is None else env
    raw = (env.get("MEETING_JOIN_WAIT_S") or "").strip()
    if not raw:
        return DEFAULT_JOIN_WAIT_S
    try:
        v = float(raw)
    except ValueError:
        v = 0.0
    if v <= 0:
        log.warning("MEETING_JOIN_WAIT_S=%r is not a positive number; using %ds", raw, int(DEFAULT_JOIN_WAIT_S))
        return DEFAULT_JOIN_WAIT_S
    return v
