"""The context bundle: every stand-in's full exported context, chunked, for in-meeting search.

A brief is ~800 characters; the full context is what the bot's principal actually exported. `meet` chunks it
into passages and writes `links/<room>.context.json` (chmod 600, like the link and scene files). The scene only
carries `{"context_bundle": {"path", "sha256"}}`; the worker loads the file, verifies the hash, and builds a
per-bot BM25 index (orchestrator/retrieval.py). Contexts never travel through LiveKit metadata.

    {"v": 1, "bots": {"<bot_id>": {"chunks": [{"id": "<bot_id>#1", "text": "..."}]}}}

Caps: ~200 KB of text per bot and ~2 MB in total. Past a cap the tail is dropped and the caller prints a warning.
Bots without context get no entry.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from orchestrator.retrieval import BUNDLE_VERSION, PER_BOT_BYTES, TOTAL_BYTES, chunk_bot


@dataclass
class Bundle:
    bots: dict[str, list[dict]] = field(default_factory=dict)     # bot_id -> [{"id", "text"}]
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> bytes:
        data = {"v": BUNDLE_VERSION, "bots": {b: {"chunks": c} for b, c in self.bots.items()}}
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def __bool__(self) -> bool:
        return bool(self.bots)


def build_bundle(contexts: dict[str, str], *, per_bot_bytes: int = PER_BOT_BYTES,
                 total_bytes: int = TOTAL_BYTES) -> Bundle:
    """contexts = {bot_id: full context text}. Bots with empty context are skipped."""
    out = Bundle()
    left = total_bytes
    for bid, text in contexts.items():
        if not (text or "").strip():
            continue
        if left <= 0:
            out.warnings.append(f"{bid}: context dropped, bundle already holds {total_bytes // 1000} KB")
            continue
        limit = min(per_bot_bytes, left)
        chunks, cut = chunk_bot(bid, text, limit)
        if not chunks:
            out.warnings.append(f"{bid}: context dropped, nothing fits in the remaining {limit // 1000} KB")
            continue
        if cut:
            why = "per-bot" if per_bot_bytes <= left else "total"
            out.warnings.append(f"{bid}: context truncated to {limit // 1000} KB ({why} limit)")
        left -= sum(len(c.text.encode("utf-8")) for c in chunks)
        out.bots[bid] = [{"id": c.id, "text": c.text} for c in chunks]
    return out


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def scene_ref(path: str, data: bytes) -> dict:
    """What goes into the scene: where the file is and the hash the worker must see."""
    return {"path": path, "sha256": sha256_hex(data)}
