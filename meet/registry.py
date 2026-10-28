"""Bot registry: who can be invited, and where their context lives.

Looked up in this order:
  --registry PATH
  ./bots.yaml (or bots.json)
  $MEETING_BOTS
  ~/.config/grokbot-voice-meeting/bots.yaml
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def registry_path(explicit: str | None = None) -> Path | None:
    if explicit:
        return Path(explicit)
    env = os.environ.get("MEETING_BOTS")
    if env:
        return Path(env)
    for candidate in (Path.cwd() / "bots.yaml", Path.cwd() / "bots.json",
                      Path.home() / ".config" / "grokbot-voice-meeting" / "bots.yaml"):
        if candidate.is_file():
            return candidate
    return None


def load_registry(path: str | Path | None) -> dict[str, dict]:
    if path is None:
        return {}
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        data = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as e:
            raise RuntimeError("reading a YAML registry needs pyyaml (pip install pyyaml)") from e
        data = yaml.safe_load(text) or {}
    bots = data.get("bots") if isinstance(data, dict) else None
    if not isinstance(bots, dict):
        raise ValueError(f"{path} must contain a 'bots' map")
    out: dict[str, dict] = {}
    for bid, meta in bots.items():
        if not isinstance(meta, dict):
            raise ValueError(f"bot {bid} must be a map")
        item = dict(meta)
        item.setdefault("display_name", bid)
        item["_registry_dir"] = str(path.parent)
        out[str(bid)] = item
    if isinstance(data.get("moderator"), dict):
        out["__moderator__"] = dict(data["moderator"])
    return out


def read_context(bot: dict, context_dir: str | None = None) -> str:
    """Context the operator or a Grok Bot exported. Missing context is an empty string, not a guess."""
    inline = bot.get("context")
    if isinstance(inline, str) and inline.strip():
        return inline.strip()
    rel = bot.get("context_file")
    if rel:
        path = Path(rel)
        if not path.is_absolute():
            path = Path(bot.get("_registry_dir") or ".") / path
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
        raise FileNotFoundError(path)
    return ""


def context_from_dir(bot_id: str, context_dir: str | None, fallback: str) -> str:
    if not context_dir:
        return fallback
    for name in (f"{bot_id}.md", f"{bot_id}.txt"):
        path = Path(context_dir) / name
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    return fallback
