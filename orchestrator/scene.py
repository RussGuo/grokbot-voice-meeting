"""Scene documents: the thing `meet` puts in LiveKit job metadata and the worker reads back.

A scene is JSON with who / topic / agenda / briefs / voices. It is not the sample
chief-of-staff scenario. The moderator defaults to a neutral host id `moderator`.
"""
from __future__ import annotations

import json
import os
from typing import Any

from .config import human_identity
from .i18n import human_name_default, moderator_name_default, normalize, unknown
from .models import Brief, Meeting

DEFAULT_MODERATOR_ID = "moderator"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


_BOT_INSTRUCTIONS = {
    "zh": "你是{name}的语音分身，代表它参加这场会。只根据会前 brief 说话；brief 里写着未知的，就说不知道，不要编。",
    "en": "You are the voice stand-in for {name}. Speak only from the pre-meeting brief. If a field says unknown, say you don't know. Do not invent facts.",
}


def bot_participant(bid: str, meta: dict, voice: str, language: str = "zh") -> dict[str, Any]:
    """一个成员 bot 的参会者 dict。build_scene 与会中加 bot（meet/live_controls.py）共用，保证两处一致。"""
    lang = normalize(language)
    name = meta.get("display_name") or bid
    aliases = list(dict.fromkeys([name, bid, *(meta.get("aliases") or [])]))
    return {
        "id": bid,
        "kind": "bot",
        "display_name": name,
        "role": "member",
        "aliases": aliases,
        "principal_ref": {"bot_id": meta.get("principal") or bid, "channel": "message"},
        "agent_profile": {
            "tts_voice": voice,
            "llm": "openai",
            "instructions": _BOT_INSTRUCTIONS[lang].format(name=name),
        },
    }


def build_scene(*, who: list[str], topic: str, agenda: list[str] | None, briefs: dict[str, dict],
                voices: dict[str, str], registry: dict[str, dict], language: str = "zh",
                human_id: str | None = None, human_name: str | None = None,
                moderator_id: str | None = None, moderator_name: str | None = None) -> dict[str, Any]:
    """Assemble the scene dict `make_link` stores as room and dispatch metadata."""
    lang = normalize(language or _env("MEETING_LANG", "zh"))
    hid = human_id or human_identity()
    hname = human_name or _env("MEETING_HUMAN_NAME") or human_name_default(lang)
    mid = moderator_id or _env("MEETING_MODERATOR_ID", DEFAULT_MODERATOR_ID)
    mname = moderator_name or _env("MEETING_MODERATOR_NAME") or moderator_name_default(lang)
    titles = [t.strip() for t in (agenda or []) if t and t.strip()] or [topic]
    owners = who or [mid]
    agenda_items = []
    for i, title in enumerate(titles, 1):
        agenda_items.append({
            "id": f"ag-{i}",
            "title": title,
            "owner_id": owners[(i - 1) % len(owners)],
            "timebox_ms": 180_000,
            "goal": "discuss",
            "keywords": [],
        })
    mod_instructions = {
        "zh": "你是这场会议的中立主持人。按议程推进、点名还没说话的人、小结时不要站队。",
        "en": "You are the neutral host. Move through the agenda, invite people who have not spoken, and keep summaries neutral.",
    }[lang]
    participants: list[dict[str, Any]] = [{
        "id": hid,
        "kind": "human",
        "display_name": hname,
        "role": "member",
        "aliases": [hname],
    }, {
        "id": mid,
        "kind": "bot",
        "display_name": mname,
        "role": "moderator",
        "aliases": [mname, "host", "主持人"] if lang == "zh" else [mname, "host"],
        "agent_profile": {"tts_voice": voices.get(mid, ""), "llm": "openai", "instructions": mod_instructions},
    }]
    for bid in who:
        participants.append(bot_participant(bid, registry.get(bid, {}), voices.get(bid, ""), lang))
    return {
        "version": 1,
        "topic": topic,
        "title": topic,
        "language": lang,
        "who": list(who),
        "agenda": agenda_items,
        "human": {"id": hid, "display_name": hname},
        "moderator": {"id": mid, "display_name": mname, "mode": "neutral"},
        "participants": participants,
        "briefs": briefs,
        "voices": voices,
    }


def _agenda_from(data: dict) -> list[dict]:
    raw = data.get("agenda") or [data.get("topic") or data.get("title") or "Meeting"]
    who = data.get("who") or []
    out = []
    for i, item in enumerate(raw, 1):
        if isinstance(item, str):
            owners = who or ["moderator"]
            out.append({
                "id": f"ag-{i}", "title": item, "owner_id": owners[(i - 1) % len(owners)],
                "timebox_ms": 180_000, "goal": "discuss", "keywords": [],
            })
        else:
            out.append(item)
    return out


def scene_to_meeting(data: dict, *, human_id: str | None = None) -> tuple[Meeting, dict[str, Brief]]:
    """Turn a scene dict into the orchestrator's Meeting + briefs."""
    lang = normalize(data.get("language") or _env("MEETING_LANG", "zh"))
    if _env("MEETING_LANG") in ("zh", "en"):
        lang = normalize(_env("MEETING_LANG"))
    human = dict(data.get("human") or {})
    hid = human_id or human.get("id") or human_identity()
    hname = _env("MEETING_HUMAN_NAME") or human.get("display_name") or human_name_default(lang)
    mod = dict(data.get("moderator") or {})
    mid = mod.get("id") or _env("MEETING_MODERATOR_ID", DEFAULT_MODERATOR_ID)
    mname = mod.get("display_name") or _env("MEETING_MODERATOR_NAME") or moderator_name_default(lang)
    mode = mod.get("mode") or "neutral"
    if mode not in ("neutral", "chiefOfStaff"):
        mode = "neutral"
    participants = data.get("participants")
    if not participants:
        raise ValueError("scene has no participants")
    people = []
    for p in participants:
        q = dict(p)
        if q.get("kind") == "human" or q.get("id") in ("__HUMAN__", human.get("id")):
            q["id"] = hid
            q["kind"] = "human"
            q["display_name"] = hname
        elif q.get("role") == "moderator":
            q["id"] = mid
            q["display_name"] = mname
        people.append(q)
    # Guarantee the linked human and the moderator exist.
    if not any(p["id"] == hid for p in people):
        people.insert(0, {"id": hid, "kind": "human", "display_name": hname, "role": "member", "aliases": [hname]})
    if not any(p["id"] == mid for p in people):
        people.insert(1, {
            "id": mid, "kind": "bot", "display_name": mname, "role": "moderator", "aliases": [mname],
            "agent_profile": {"tts_voice": (data.get("voices") or {}).get(mid, ""), "llm": "openai", "instructions": ""},
        })
    meeting_raw = {
        "id": data.get("id") or "mtg-scene",
        "title": data.get("title") or data.get("topic") or "Meeting",
        "initiator": hid,
        "language": lang,
        "agenda": _agenda_from({**data, "who": data.get("who") or [p["id"] for p in people if p.get("kind") == "bot" and p.get("id") != mid]}),
        "participants": people,
        "moderator": {"mode": mode, "participant_id": mid},
        "voice": {"provider": "livekitCloud", "room": data.get("room") or "meeting", "linked_human_id": hid},
    }
    meeting = Meeting.from_dict(meeting_raw)
    meeting.language = lang
    meeting.voice.linked_human_id = hid
    briefs: dict[str, Brief] = {}
    raw_briefs = dict(data.get("briefs") or {})
    for p in data.get("participants") or []:
        if isinstance(p, dict) and p.get("brief") and p.get("id"):
            raw_briefs.setdefault(p["id"], p["brief"])
    for pid, blob in raw_briefs.items():
        if not isinstance(blob, dict):
            continue
        briefs[pid] = brief_from_blob(pid, blob, meeting.title, [a.id for a in meeting.agenda])
    if mid not in briefs:
        briefs[mid] = _empty_moderator_brief(mid, meeting, lang)
    return meeting, briefs


def brief_from_blob(pid: str, blob: dict, topic: str, agenda_ids: list[str]) -> Brief:
    """brief 的 dict（来自场景或 meet.briefs.summarize）补齐缺省字段后转成 Brief。"""
    blob = dict(blob)
    blob.setdefault("participant_id", pid)
    blob.setdefault("agenda_item_ids", list(agenda_ids))
    blob.setdefault("positions", [])
    blob.setdefault("memory_snapshot", {"topic": topic, "facts": []})
    blob.setdefault("mandate", {"can_commit": [], "must_ask": []})
    return Brief.from_dict(blob)


def _empty_moderator_brief(mid: str, meeting: Meeting, lang: str) -> Brief:
    reason = "the host does not take a side" if lang == "en" else "主持人不预设立场"
    return Brief.from_dict({
        "participant_id": mid,
        "agenda_item_ids": [a.id for a in meeting.agenda],
        "positions": [{"agenda_item_id": a.id, "stance": unknown(lang, reason), "evidence": []} for a in meeting.agenda],
        "memory_snapshot": {
            "topic": meeting.title,
            "facts": [{"key": "role", "value": unknown(lang, reason), "source": "unspecified", "as_of": "unspecified"}],
        },
        "mandate": {"can_commit": [], "must_ask": []},
    })


def load_scene_metadata(*blobs: str | None) -> tuple[Meeting, dict[str, Brief], dict[str, str]] | None:
    """First non-empty JSON object that looks like a scene wins. Returns meeting, briefs, voices."""
    for raw in blobs:
        if not raw or not str(raw).strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if not (data.get("topic") or data.get("title") or data.get("participants") or data.get("who")):
            continue
        if not data.get("participants") and not data.get("who"):
            continue
        meeting, briefs = scene_to_meeting(data)
        voices = data.get("voices") or {}
        if not isinstance(voices, dict):
            voices = {}
        return meeting, briefs, {str(k): str(v) for k, v in voices.items()}
    return None
