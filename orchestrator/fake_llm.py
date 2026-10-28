"""确定性的 fake LLM：单测与 sim.py 用，不依赖任何 key 或网络。

输入：participant_id + ChatCtxRecipe；输出 dict：
  text, provenance, confidence, ask_principal(可选), commitments(list[str]), addressees(list)
真实 LLM（DeepSeek，OpenAI 兼容）在 main.py 里挂到各 Agent 上；ask_principal 与承诺
届时应改成 function tool 调用（TODO，待验证），由编排器统一校验 mandate。
"""
from __future__ import annotations

from typing import Any

from .i18n import member_lines, moderator_lines, normalize, notes_line
from .models import ID, Brief, ChatCtxRecipe, Meeting
from .retrieval import turn_query

CONFIDENCE_THRESHOLD = 0.6


class FakeLLM:
    def __init__(self, meeting: Meeting, briefs: dict[ID, Brief], context_index: Any = None):
        self.meeting = meeting
        self.briefs = briefs
        self.context_index = context_index     # retrieval.ContextIndex | None：没有快照事实命中时引用自己的笔记
        self._lines = member_lines(getattr(meeting, "language", "zh"))     # 成员台词模板，按会议语言

    def __call__(self, pid: ID, ctx: ChatCtxRecipe) -> dict[str, Any]:
        meta = ctx.turn_meta
        intent = meta.get("intent", "answer")
        if pid == self.meeting.moderator.participant_id:
            return self._moderator(intent, meta)
        if intent == "relay":
            return {"text": self._lines["relay"].format(answer=meta.get("answer", "")), "provenance": "principal",
                    "confidence": 0.95, "commitments": [], "addressees": []}
        return self._member(pid, meta)

    def _moderator(self, intent: str, meta: dict) -> dict[str, Any]:
        item = meta.get("agenda_title", "")
        human = self.meeting.voice.linked_human_id
        lang = normalize(getattr(self.meeting, "language", "zh"))
        cos = self.meeting.moderator.mode == "chiefOfStaff"
        texts = moderator_lines(lang, chief=cos)
        text = texts.get(intent, self._lines["okay"])
        text = text.format(item=item, recap=meta.get("recap_item", ""), target=meta.get("target_name", ""))
        return {"text": text, "provenance": "model", "confidence": 1.0,
                "commitments": [], "addressees": [human] if intent == "ask_human" else []}

    def _member(self, pid: ID, meta: dict) -> dict[str, Any]:
        brief = self.briefs[pid]
        last = meta.get("directive_text") or meta.get("last_text", "")
        gist = meta.get("gist", "")
        # mustAsk 命中 → 慢路径
        for term in brief.mandate.must_ask:
            if term and term in last:
                return {"text": self._lines["ask_term"].format(term=term), "provenance": "model",
                        "confidence": 0.3, "ask_principal": last, "commitments": [], "addressees": []}
        facts = [f for f in brief.memory_snapshot.facts if f.key.split(".")[-1] in last or f.value[:4] in last]
        if not facts and gist:
            facts = [f for f in brief.memory_snapshot.facts if f.value == gist]
        hit = None
        if not facts and self.context_index:
            main, extra = turn_query(meta)
            hits = self.context_index.search(pid, main, k=1, extra=extra)
            hit = hits[0] if hits else None
        if facts:
            f = facts[0]
            text = self._lines["snapshot"].format(key=f.key, value=f.value)
            conf = 0.8
            prov = "snapshot"
        elif hit is not None:            # 快照没命中但自己的笔记里有相关原文：念首句，标 context 并带上来源段落 id
            text = notes_line(getattr(self.meeting, "language", "zh")).format(text=hit.first_sentence())
            conf = 0.7
            prov = "context"
            sources = [hit.id]
        else:
            pos = next((p for p in brief.positions if p.agenda_item_id == meta.get("agenda_item_id")), None)
            stance = pos.stance if pos else self._lines["no_stance"]
            text = self._lines["stance"].format(stance=stance)
            conf = 0.65 if not meta.get("addressed") else 0.5
            prov = "brief"
        commitments = [c for c in brief.mandate.can_commit if c and c in last]
        if commitments:      # mandate.canCommit 范围内：当场答应（决策 1：binding）
            return {"text": self._lines["commit"].format(commitment=commitments[0]), "provenance": "brief",
                    "confidence": 0.85, "commitments": commitments[:1], "addressees": []}
        if meta.get("addressed") and conf < CONFIDENCE_THRESHOLD:
            return {"text": self._lines["beyond"], "provenance": "model",
                    "confidence": conf, "ask_principal": last, "commitments": [], "addressees": []}
        out = {"text": text, "provenance": prov, "confidence": conf, "commitments": [], "addressees": []}
        if prov == "context":
            out["sources"] = sources
        return out
