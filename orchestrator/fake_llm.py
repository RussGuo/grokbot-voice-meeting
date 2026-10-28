"""确定性的 fake LLM：单测与 sim.py 用，不依赖任何 key 或网络。

输入：participant_id + ChatCtxRecipe；输出 dict：
  text, provenance, confidence, ask_principal(可选), commitments(list[str]), addressees(list)
真实 LLM（DeepSeek，OpenAI 兼容）在 main.py 里挂到各 Agent 上；ask_principal 与承诺
届时应改成 function tool 调用（TODO，待验证），由编排器统一校验 mandate。
"""
from __future__ import annotations

from typing import Any

from .models import ID, Brief, ChatCtxRecipe, Meeting

CONFIDENCE_THRESHOLD = 0.6


class FakeLLM:
    def __init__(self, meeting: Meeting, briefs: dict[ID, Brief]):
        self.meeting = meeting
        self.briefs = briefs

    def __call__(self, pid: ID, ctx: ChatCtxRecipe) -> dict[str, Any]:
        meta = ctx.turn_meta
        intent = meta.get("intent", "answer")
        if pid == self.meeting.moderator.participant_id:
            return self._moderator(intent, meta)
        if intent == "relay":
            return {"text": f"本体回复了：{meta.get('answer', '')}", "provenance": "principal",
                    "confidence": 0.95, "commitments": [], "addressees": []}
        return self._member(pid, meta)

    def _moderator(self, intent: str, meta: dict) -> dict[str, Any]:
        item = meta.get("agenda_title", "")
        human = self.meeting.voice.linked_human_id
        cos = self.meeting.moderator.mode == "chiefOfStaff"
        texts = {
            "open": (("我是 Chief of Staff，兼任本场主持，我有自己的立场，小结会尽量中立。" if cos else "欢迎。")
                     + f"今天的议题是「{item}」。规则：用户随时可以打断，分身承诺当场生效、会后对账。"),
            "confirm_end": "议程都过完了。用户，我们是否结束？",
            "open_item": f"进入下一项：「{item}」。",
            "recap": f"插一句，回到刚才「{meta.get('recap_item', '')}」那一项，有本体的答复要补充。",
            "fill_silence": f"我小结一下「{item}」目前的讨论，还有谁要补充？",
            "ask_human": "bot 们已经连续讨论了好几轮，用户，你怎么看？",
            "cue": f"请{meta.get('target_name', '')}说说。",
            "warn": "还剩 10 秒。",
        }
        return {"text": texts.get(intent, "好的。"), "provenance": "model", "confidence": 1.0,
                "commitments": [], "addressees": [human] if intent == "ask_human" else []}

    def _member(self, pid: ID, meta: dict) -> dict[str, Any]:
        brief = self.briefs[pid]
        last = meta.get("directive_text") or meta.get("last_text", "")
        gist = meta.get("gist", "")
        # mustAsk 命中 → 慢路径
        for term in brief.mandate.must_ask:
            if term and term in last:
                return {"text": f"「{term}」这个我需要问一下本体，稍后补充。", "provenance": "model",
                        "confidence": 0.3, "ask_principal": last, "commitments": [], "addressees": []}
        facts = [f for f in brief.memory_snapshot.facts if f.key.split(".")[-1] in last or f.value[:4] in last]
        if not facts and gist:
            facts = [f for f in brief.memory_snapshot.facts if f.value == gist]
        if facts:
            f = facts[0]
            text = f"按会前快照（{f.key}）：{f.value}"
            conf = 0.8
            prov = "snapshot"
        else:
            pos = next((p for p in brief.positions if p.agenda_item_id == meta.get("agenda_item_id")), None)
            stance = pos.stance if pos else "我暂时没有补充。"
            text = f"我的立场是：{stance}"
            conf = 0.65 if not meta.get("addressed") else 0.5
            prov = "brief"
        commitments = [c for c in brief.mandate.can_commit if c and c in last]
        if commitments:      # mandate.canCommit 范围内：当场答应（决策 1：binding）
            return {"text": f"可以，这件事我当场答应：{commitments[0]}。", "provenance": "brief",
                    "confidence": 0.85, "commitments": commitments[:1], "addressees": []}
        if meta.get("addressed") and conf < CONFIDENCE_THRESHOLD:
            return {"text": "这个超出我的会前快照，我问一下本体，稍后补充。", "provenance": "model",
                    "confidence": conf, "ask_principal": last, "commitments": [], "addressees": []}
        return {"text": text, "provenance": prov, "confidence": conf,
                "commitments": [], "addressees": []}
