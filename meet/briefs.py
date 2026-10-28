"""Turn a bot's exported context into a meeting brief.

The model is instructed to copy facts, not invent them. A second pass drops any fact
whose numbers do not appear in the source, and rewrites it as unknown.
With no API key the heuristic brief stores the excerpt and marks every stance unknown.
Results are cached by hash of (model, topic, agenda, context).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from orchestrator.i18n import is_unknown, normalize, unknown

PROMPT_VERSION = "1"


def cache_dir() -> Path:
    root = os.environ.get("MEETING_BRIEF_CACHE")
    path = Path(root) if root else Path.home() / ".cache" / "grokbot-voice-meeting" / "briefs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_key(*, bot_id: str, context: str, topic: str, agenda: list[str], model: str, lang: str) -> str:
    blob = json.dumps({
        "v": PROMPT_VERSION, "bot": bot_id, "context": context, "topic": topic,
        "agenda": agenda, "model": model, "lang": lang,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def list_unknowns(brief: dict) -> list[str]:
    found = []
    for fact in (brief.get("memory_snapshot") or {}).get("facts") or []:
        if is_unknown(str(fact.get("value") or "")):
            found.append(str(fact.get("key") or "fact"))
    for pos in brief.get("positions") or []:
        if is_unknown(str(pos.get("stance") or "")):
            found.append(str(pos.get("agenda_item_id") or "position"))
    return found


def heuristic_brief(*, bot_id: str, context: str, topic: str, agenda: list[dict], lang: str) -> dict:
    lang = normalize(lang)
    facts = []
    text = (context or "").strip()
    if text:
        facts.append({
            "key": "context.excerpt",
            "value": text[:800],
            "source": "provided context",
            "as_of": "unspecified",
        })
    else:
        facts.append({
            "key": "context.missing",
            "value": unknown(lang, "no context was provided for this bot"),
            "source": "unspecified",
            "as_of": "unspecified",
        })
    positions = []
    for item in agenda:
        positions.append({
            "agenda_item_id": item["id"],
            "stance": unknown(lang, f"the context does not state a position on {item['title']}"),
            "evidence": [],
        })
    return {
        "participant_id": bot_id,
        "agenda_item_ids": [a["id"] for a in agenda],
        "positions": positions,
        "memory_snapshot": {
            "topic": topic,
            "facts": facts,
            "open_todos": [],
            "recent_decisions": [],
            "red_lines": [],
            "exported_at": "",
            "token_estimate": max(1, len(text) // 4),
        },
        "mandate": {"can_commit": _labeled_list(text, ("can commit", "可承诺")), "must_ask": _labeled_list(text, ("must ask", "必须问"))},
        "keywords": [],
        "questions_for_others": [],
    }


def _labeled_list(text: str, labels: tuple[str, ...]) -> list[str]:
    """Only lines under an explicit heading. No heading means no commitments, not a guess."""
    if not text:
        return []
    lines = text.splitlines()
    capture = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip().lower().rstrip(":")
        if any(stripped == label for label in labels):
            capture = True
            continue
        if capture and stripped.endswith(":") and not line.startswith((" ", "\t", "-", "*")):
            break
        if capture:
            raw = line.strip()
            if not raw:
                continue
            if not (raw.startswith("-") or raw.startswith("*") or re.match(r"\d+[.)]\s+", raw)):
                break
            item = raw.lstrip("-*").strip()
            item = re.sub(r"^\d+[.)]\s*", "", item)
            if item:
                out.append(item)
    return out


_SUMMARY_SYSTEM = """You write a meeting brief as JSON.
Rules:
- Use ONLY facts that appear in the context. Do not invent numbers, dates, names, or decisions.
- If the context does not say something, set the value to "unknown: <what is missing>" (or "未知：…" when language is zh).
- source is "provided context" when the fact is copied, otherwise "unspecified".
- can_commit and must_ask stay empty unless the context has an explicit list.
Return one JSON object with keys participant_id, positions, memory_snapshot, mandate.
positions is a list of {agenda_item_id, stance, evidence}.
memory_snapshot is {topic, facts: [{key, value, source, as_of}], open_todos, recent_decisions, red_lines}.
mandate is {can_commit: [], must_ask: []}.
"""


def _parse_json(text: str) -> dict:
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j < i:
        raise ValueError("model did not return JSON")
    data = json.loads(raw[i:j + 1])
    if not isinstance(data, dict):
        raise ValueError("brief JSON must be an object")
    return data


def guard_brief(brief: dict, context: str, lang: str) -> dict:
    """Drop facts the model was not given. Numbers must occur in the context verbatim."""
    lang = normalize(lang)
    ctx = context or ""
    facts = (brief.get("memory_snapshot") or {}).get("facts") or []
    for fact in facts:
        value = str(fact.get("value") or "")
        if is_unknown(value):
            fact["source"] = "unspecified"
            continue
        numbers = re.findall(r"\d+(?:\.\d+)?%?", value)
        if any(n not in ctx for n in numbers):
            fact["value"] = unknown(lang, "a number in the draft was not in the context")
            fact["source"] = "unspecified"
            continue
        snippet = value.strip()
        if len(snippet) >= 12 and snippet[:24] not in ctx and snippet not in ctx:
            # Short overlap: at least 8 consecutive characters of the claim must be in the source.
            if not any(snippet[i:i + 8] in ctx for i in range(0, max(1, len(snippet) - 7))):
                fact["value"] = unknown(lang, "the draft claim was not found in the context")
                fact["source"] = "unspecified"
    return brief


def summarize(*, bot_id: str, context: str, topic: str, agenda: list[dict], lang: str, chat=None,
              model: str = "heuristic") -> dict:
    lang = normalize(lang)
    key = cache_key(bot_id=bot_id, context=context, topic=topic,
                    agenda=[a["title"] for a in agenda], model=model if chat else "heuristic", lang=lang)
    path = cache_dir() / f"{key}.json"
    if path.is_file():
        cached = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(cached, dict) and cached.get("participant_id") == bot_id:
            return cached
    if chat is None:
        brief = heuristic_brief(bot_id=bot_id, context=context, topic=topic, agenda=agenda, lang=lang)
    else:
        user = json.dumps({
            "language": lang, "participant_id": bot_id, "topic": topic, "agenda": agenda, "context": context,
        }, ensure_ascii=False)
        try:
            draft = _parse_json(chat.complete([
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user", "content": user},
            ]))
        except (ValueError, json.JSONDecodeError):
            draft = heuristic_brief(bot_id=bot_id, context=context, topic=topic, agenda=agenda, lang=lang)
        draft["participant_id"] = bot_id
        draft.setdefault("agenda_item_ids", [a["id"] for a in agenda])
        draft.setdefault("positions", [])
        draft.setdefault("memory_snapshot", {"topic": topic, "facts": []})
        draft.setdefault("mandate", {"can_commit": [], "must_ask": []})
        # Fill stances the model skipped so the orchestrator still has one per item.
        have = {p.get("agenda_item_id") for p in draft["positions"]}
        for item in agenda:
            if item["id"] not in have:
                draft["positions"].append({
                    "agenda_item_id": item["id"],
                    "stance": unknown(lang, f"no position was stated for {item['title']}"),
                    "evidence": [],
                })
        brief = guard_brief(draft, context, lang)
    path.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    return brief
