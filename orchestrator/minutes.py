"""会后产物：从事件流（而不是编排器内存）生成纪要与按 bot 拆分的行动项。

输出到 run_dir：
  events.jsonl            事件日志
  minutes.md              纪要（议程、决策、各方发言要点、行动项、会后补充）
  actions/<owner_id>.json 每个 owner 一份行动项（分发给对应本体的待办 / 对账）
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .events import EventLog, MeetingEvent


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
    for e in events:
        d = e.data
        if e.type == "FloorGranted":
            src[d["token"]["id"]] = d["token"].get("source", "")
            if d["token"].get("source") == "handRaise":
                granted_hands.append({"at": e.at, "who": d["token"]["holder_id"]})
        elif e.type in ("HandRaised", "HandExpired", "HandWithdrawn"):
            h = d["hand"]
            r = hands.setdefault(h["id"], {"who": h["participant_id"], "at": e.at, "gist": h.get("gist", ""),
                                           "score": (h.get("score") or {}).get("total"), "result": "排队"})
            if e.type != "HandRaised":
                r["result"] = {"HandExpired": "过期", "HandWithdrawn": "撤回"}[e.type]
        elif e.type == "EndConfirm" and d.get("stage") in ("human_command", "confirmed", "timeout"):
            end_by = (e.at, d.get("stage"), d.get("text", ""))
        if e.type == "UtteranceFinal" and not d["utterance"].get("interrupted"):
            u = d["utterance"]
            utt.append((e.at, u["speaker_id"], u["agenda_item_id"], u["text"], u.get("provenance"),
                        src.get(u.get("token_id") or "", "")))
        elif e.type == "DecisionRecorded":
            decisions.append(d["payload"])
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
        cand = [h for h in hands.values() if h["who"] == g["who"] and h["at"] <= g["at"] and h["result"] == "排队"]
        if cand:
            cand[-1]["result"] = f"{g['at'] / 1000:.1f}s 获准发言"
    for h in hands.values():
        if h["result"] == "排队":
            h["result"] = "未轮到"
    return {"meta": meta, "names": names, "utterances": utt, "decisions": decisions,
            "actions": actions, "deferred": deferred, "hands": list(hands.values()),
            "hand_grants": len(granted_hands), "end_by": end_by}


def render_md(b: dict) -> str:
    m, n = b["meta"], b["names"]
    out = [f"# 纪要：{m.get('title', '')}", "", "（由事件流生成；分身承诺当场生效，会后由本体对账）", "", "## 议程"]
    out += [f"- {a['title']}" for a in m.get("agenda", [])]
    out += ["", "## 决策"] + [f"- {d['text']}" for d in b["decisions"]] or ["- 无"]
    out += ["", "## 发言记录"]
    src_tag = {"handRaise": "【举手】", "pendingAnswer": "【本体答复插回】", "human": ""}
    for at, sp, ag, text, prov, source in b["utterances"]:
        tag = f"（{prov}）" if prov and prov != "model" else ""
        tag = src_tag.get(source, "") + tag
        out.append(f"- {at / 1000:6.1f}s [{ag}] {n.get(sp, sp)}{tag}：{text}")
    out += ["", "## 发言秩序（举手申请）"]
    out.append(f"- 举手 {len(b['hands'])} 次，获准发言 {b['hand_grants']} 次")
    for h in b["hands"]:
        sc = f"，得分 {h['score']:.2f}" if isinstance(h.get("score"), (int, float)) else ""
        out.append(f"- {h['at'] / 1000:6.1f}s {n.get(h['who'], h['who'])} 举手：{(h['gist'] or '（无摘要）').rstrip('。，')}{sc} → {h['result']}")
    if b.get("end_by"):
        at, stage, text = b["end_by"]
        how = {"human_command": "用户宣布结束", "confirmed": "用户确认结束", "timeout": "无人回应超时结束"}[stage]
        out.append(f"- 散会：{at / 1000:.1f}s {how}" + (f"（「{text}」）" if text else ""))
    out += ["", "## 行动项（按 owner）"]
    if not b["actions"]:
        out.append("- 无")
    for owner, items in b["actions"].items():
        for a in items:
            out.append(f"- {n.get(owner, owner)}：{a['title']}（{a['status']}）")
    out += ["", "## 会后补充（本体答案未在会上送达）"]
    out += [f"- {q['asked_by']}：{q['question']}" for q in b["deferred"]] or ["- 无"]
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
