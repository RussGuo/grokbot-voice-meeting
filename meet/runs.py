"""Locate run directories: by path, bare run id, or by LiveKit room name."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

_STAMP = re.compile(r"\d{8}-\d{6}")


def search_roots() -> list[Path]:
    roots: list[Path] = []
    env = os.environ.get("MEETING_HOME")
    if env:
        roots.append(Path(env))
    roots.append(Path.cwd())
    out: list[Path] = []
    for r in roots:
        if r not in out:
            out.append(r)
    return out


def resolve_run(arg: str | Path) -> Path | None:
    p = Path(arg)
    if p.is_dir():
        return p
    for root in search_roots():
        cand = root / "runs" / str(arg)
        if cand.is_dir():
            return cand
    return None


def _clock_room(d: Path) -> str | None:
    try:
        return json.loads((d / "clock.json").read_text(encoding="utf-8")).get("room")
    except (OSError, ValueError, AttributeError):
        return None


def find_run_by_room(room: str) -> Path | None:
    """Newest runs/<room>-<YYYYmmdd-HHMMSS> dir; falls back to clock.json `room` fields."""
    named: list[Path] = []
    scanned: list[Path] = []
    for root in search_roots():
        runs = root / "runs"
        if not runs.is_dir():
            continue
        for d in runs.iterdir():
            if not d.is_dir():
                continue
            suffix = d.name[len(room) + 1:] if d.name.startswith(room + "-") else None
            if suffix is not None and _STAMP.fullmatch(suffix):
                named.append(d)
            elif d.name == room or _clock_room(d) == room:
                scanned.append(d)
    if named:
        return max(named, key=lambda d: d.name[len(room) + 1:])
    if scanned:
        return max(scanned, key=_mtime)
    return None


def _mtime(d: Path) -> float:
    for name in ("clock.json", "events.jsonl"):
        try:
            return (d / name).stat().st_mtime
        except OSError:
            continue
    return d.stat().st_mtime
