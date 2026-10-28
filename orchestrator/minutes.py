"""会后产物：从事件流（而不是编排器内存）生成纪要与按 bot 拆分的行动项。

输出到 run_dir：
  events.jsonl            事件日志
  minutes.md              纪要（议程、决策、各方发言要点、行动项、会后补充；语言取 MeetingCreated.payload.language）
  actions/<owner_id>.json 每个 owner 一份行动项（分发给对应本体的待办 / 对账）
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .events import EventLog, MeetingEvent
from .i18n import inline_tag, minutes_labels, normalize


def build(events: Iterable[MeetingEvent]) -> dict:
    events = list(events)
    meta = next((e.data["payload"] for e in events if e.type == "MeetingCreated"), {})
    names = {p["id"]: p["display_name"] for p in meta.get("participants", [])}
    utt, decisions, actions, deferred = [], [], {}, []
    status: dict[str, str] = {}
    src: dict[str, str] = {}                     # token_id → 授予来源（moderatorCue/handRaise/pendingAnswer/human…）
    hands: dict[str, dict] = {}                  # hand_id → 举手记录
    granted_hands: list[dict] = []
    end_by = None
    added_items: list[dict] = []                 # 会中新增的议程项 / 改名 / 入会的 bot（网页控制）
    renamed: dict[str, str] = {}
    joined: list[dict] = []
    inline_toks: set[str] = set()                # 本体在本轮内当场答复的话轮（PrincipalAnswered inline）
    for e in events:
        d = e.data
        if e.type == "AgendaItemAdded":
            added_items.append(dict(d["agenda_item"]))
        elif e.type == "AgendaItemRenamed":
            renamed[d["agenda_item_id"]] = d["title"]
        elif e.type == "BotJoined":
            joined.append(dict(d["participant"]))
        if e.type == "FloorGranted":
            src[d["token"]["id"]] = d["token"].get("source", "")
            if d["token"].get("source") == "handRaise":
                granted_hands.append({"at": e.at, "who": d["token"]["holder_id"]})
        elif e.type in ("HandRaised", "HandExpired", "HandWithdrawn"):
            h = d["hand"]
            r = hands.setdefault(h["id"], {"who": h["participant_id"], "at": e.at, "gist": h.get("gist", ""),
                                           "score": (h.get("score") or {}).get("total"), "result": "queued"})
            if e.type != "HandRaised":
                r["result"] = {"HandExpired": "expired", "HandWithdrawn": "withdrawn"}[e.type]
        elif e.type == "PrincipalAnswered" and d.get("token_id"):
            (inline_toks.add if d.get("inline") else inline_toks.discard)(d["token_id"])    # requeued 的撤销标记
        elif e.type == "EndConfirm" and d.get("stage") in ("human_command", "confirmed", "timeout"):
            end_by = (e.at, d.get("stage"), d.get("text", ""))
        if e.type == "UtteranceFinal" and not d["utterance"].get("interrupted"):
            u = d["utterance"]
            tok = u.get("token_id") or ""
            utt.append((e.at, u["speaker_id"], u["agenda_item_id"], u["text"], u.get("provenance"),
                        src.get(tok, "") + ("+inline" if tok in inline_toks else "")))
        elif e.type == "DecisionRecorded":
            decisions.append(dict(d["payload"]))
        elif e.type == "DecisionSummarized":     # 后台总结落地：覆盖同一议程项先前记录的兜底文本
            p = d["payload"]
            cur = next((x for x in reversed(decisions) if x.get("agenda_item_id") == p.get("agenda_item_id")), None)
            if cur is None:
                decisions.append(dict(p))
            else:
                cur.update(text=p["text"], method=p.get("method", "summary"))
        elif e.type == "CommitmentMade":
            c = d["commitment"]
            actions.setdefault(c["owner_id"], []).append({
                "id": c["id"], "title": c["text"], "owner_id": c["owner_id"], "made_by": c["made_by"],
                "source_utterance_ids": [c["utterance_id"]], "status": c["status"], "at_ms": e.at})
            status[c["id"]] = c["status"]
        elif e.type == "ReconcileResult":
            for it in d.get("items", []):
                if it.get("commitment_id"):
                    status[it["commitment_id"]] = {"confirm": "confirmed", "correct": "corrected",
                                                   "retract": "retracted"}[it["verdict"]]
        elif e.type == "PrincipalDeferred":
            deferred.append(d["query"])
    for items in actions.values():
        for a in items:
            a["status"] = status.get(a["id"], a["status"])
    # 举手结果：与随后同一 bot 的 handRaise 授予配对
    for g in granted_hands:
        cand = [h for h in hands.values() if h["who"] == g["who"] and h["at"] <= g["at"] and h["result"] == "queued"]
        if cand:
            cand[-1]["result"] = "granted"
            cand[-1]["granted_at"] = g["at"]
    for h in hands.values():
        if h["result"] == "queued":
            h["result"] = "not_reached"
    meta = {**meta, "language": normalize(meta.get("language"))}      # 复制一份，不改事件里的 payload
    if added_items or renamed or joined:
        meta["agenda"] = [{**a, "title": renamed.get(a["id"], a["title"])} for a in [*meta.get("agenda", []), *added_items]]
        meta["participants"] = [*meta.get("participants", []), *joined]
        names.update({p["id"]: p["display_name"] for p in joined})
    return {"meta": meta, "names": names, "utterances": utt, "decisions": decisions,
            "actions": actions, "deferred": deferred, "hands": list(hands.values()),
            "hand_grants": len(granted_hands), "end_by": end_by}


def render_md(b: dict) -> str:
    m, n = b["meta"], b["names"]
    L = minutes_labels(m.get("language"))
    titles = {a["id"]: a["title"] for a in m.get("agenda", [])}
    out = [L["title"].format(title=m.get("title", "")), "", L["note"], "", L["agenda"]]
    out += [f"- {a['title']}" for a in m.get("agenda", [])]
    out += ["", L["decisions"]]
    out += [L["decision_item"].format(title=d.get("title") or titles.get(d.get("agenda_item_id"), ""), text=d["text"])
            for d in b["decisions"]] or [L["none"]]
    out += ["", L["transcript"]]
    src_tag = {"handRaise": L["tag_hand"], "pendingAnswer": L["tag_pending"], "human": ""}
    for at, sp, ag, text, prov, source in b["utterances"]:
        tag = L["prov"].format(prov=prov) if prov and prov != "model" else ""
        src, _, inline = source.partition("+")
        tag = src_tag.get(src, "") + (inline_tag(m.get("language")) if inline else "") + tag
        out.append(L["utt"].format(t=at / 1000, ag=ag, name=n.get(sp, sp), tag=tag, text=text))
    out += ["", L["floor"]]
    out.append(L["hands"].format(n=len(b["hands"]), m=b["hand_grants"]))
    for h in b["hands"]:
        sc = L["score"].format(s=h["score"]) if isinstance(h.get("score"), (int, float)) else ""
        res = L["r_granted"].format(t=h.get("granted_at", 0) / 1000) if h["result"] == "granted" else \
            L[{"expired": "r_expired", "withdrawn": "r_withdrawn", "not_reached": "r_queued"}[h["result"]]]
        gist = (h["gist"] or L["no_gist"]).rstrip("。，.,")
        out.append(L["hand"].format(t=h["at"] / 1000, name=n.get(h["who"], h["who"]), gist=gist, score=sc, result=res))
    if b.get("end_by"):
        at, stage, text = b["end_by"]
        how = L[{"human_command": "end_human_command", "confirmed": "end_confirmed", "timeout": "end_timeout"}[stage]]
        out.append(L["end"].format(t=at / 1000, how=how, quote=L["end_quote"].format(text=text) if text else ""))
    out += ["", L["actions"]]
    if not b["actions"]:
        out.append(L["none"])
    for owner, items in b["actions"].items():
        for a in items:
            out.append(L["action"].format(owner=n.get(owner, owner), title=a["title"], status=a["status"]))
    out += ["", L["followup"]]
    out += [L["deferred"].format(who=q["asked_by"], q=q["question"]) for q in b["deferred"]] or [L["none"]]
    return "\n".join(out) + "\n"


def export(log: EventLog, run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    (run_dir / "actions").mkdir(parents=True, exist_ok=True)
    log.dump(run_dir / "events.jsonl")
    b = build(log)
    (run_dir / "minutes.md").write_text(render_md(b), encoding="utf-8")
    for owner, items in b["actions"].items():
        (run_dir / "actions" / f"{owner}.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return b
