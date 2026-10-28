"""发言调度：举手打分、队列、冷却去重、防死循环、本体答案插回。

纯内存规则，不调 LLM（设计目标：调度决策 < 50ms）。所有时间由调用方传入 now（毫秒）。

优先级：human 100 > pendingAnswer 70 > moderatorCue 60 > handRaise 40 > free 10
候选综合分 = PRIORITY[source] + 20·score − 15·recentShare
举手分 S = 0.35·相关度 + 0.25·置信度 + 0.30·新信息量 + 0.10·被点名 − 冷却惩罚，阈值 0.6
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .models import (
    ID, PRIORITY, Brief, HandRaise, HandScore, MeetingRules, Millis, PrincipalQuery,
)

W_RELEVANCE, W_CONFIDENCE, W_NOVELTY, W_ADDRESSED = 0.35, 0.25, 0.30, 0.10
COOLDOWN_MAX_PENALTY = 0.3   # 刚说完时的最大冷却惩罚，15s 线性衰减到 0（初值，待调）


# ———————————————————— 打分 ————————————————————

def _tokens(text: str) -> set[str]:
    """中文按字二元组 + 英文按词，粗粒度相似度够规则打分用。"""
    text = text.lower()
    words = set(re.findall(r"[a-z0-9_]+", text))
    han = re.sub(r"[^\u4e00-\u9fff]", "", text)
    bigrams = {han[i:i + 2] for i in range(len(han) - 1)}
    return words | bigrams


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def cooldown_penalty(last_spoke_end: Optional[Millis], now: Millis, rules: MeetingRules) -> float:
    if last_spoke_end is None:
        return 0.0
    dt = now - last_spoke_end
    if dt >= rules.cooldown_ms:
        return 0.0
    return COOLDOWN_MAX_PENALTY * (1 - dt / rules.cooldown_ms)


def hand_score(relevance: float, confidence: float, novelty: float, addressed: bool,
               penalty: float = 0.0) -> HandScore:
    """公式 (1)。被点名时设计规定“命中即直接举手，分数记 1”。"""
    if addressed:
        return HandScore(relevance, confidence, novelty, True, 1.0)
    total = (W_RELEVANCE * relevance + W_CONFIDENCE * confidence + W_NOVELTY * novelty
             + W_ADDRESSED * 0.0 - penalty)
    return HandScore(relevance, confidence, novelty, False, round(total, 4))


def should_raise(score: HandScore, rules: MeetingRules) -> bool:
    if score.addressed:
        return True
    if score.novelty < rules.novelty_floor:   # 防复读
        return False
    return score.total >= rules.hand_threshold


class RuleScorer:
    """规则打分器：编排器内对每个分身批量打分（单 session 下不为每个 bot 跑主 LLM）。

    TODO(待验证)：相关度可换成小模型 / embedding；这里用字二元组重叠做原型。
    """

    def __init__(self, briefs: dict[ID, Brief], aliases: dict[ID, list[str]]):
        self.briefs = briefs
        self.aliases = aliases

    def addressed(self, bot_id: ID, text: str) -> bool:
        return any(a and a in text for a in self.aliases.get(bot_id, []))

    def score(self, bot_id: ID, text: str, said_in_item: list[str], penalty: float,
              agenda_item_id: ID = "") -> tuple[HandScore, str]:
        brief = self.briefs[bot_id]
        pos = next((p for p in brief.positions if p.agenda_item_id == agenda_item_id), None)
        corpus = " ".join(brief.keywords + [p.stance for p in brief.positions])
        kw_hits = sum(1 for k in brief.keywords if k in text)
        relevance = min(1.0, 0.5 * similarity(text, corpus) + 0.25 * kw_hits)
        facts = brief.memory_snapshot.facts
        matched = [f for f in facts if any(tok in text for tok in _fact_terms(f.key, f.value))]
        # 置信度：能引用快照 fact 最高；只有本议程项的 brief 立场次之；都没有则低于阈值
        confidence = min(1.0, 0.4 + 0.3 * len(matched)) if matched else (0.6 if pos else 0.3)
        # gist：这个 bot 准备说的要点 = 命中的第一条事实，否则用本议程项立场
        gist = matched[0].value if matched else (pos.stance if pos else "")
        novelty = 1.0 - max([similarity(gist, s) for s in said_in_item] or [0.0])
        return hand_score(relevance, confidence, novelty, self.addressed(bot_id, text), penalty), gist


def _fact_terms(key: str, value: str) -> list[str]:
    terms = [t for t in re.split(r"[._\s/，,]", key) if len(t) >= 2]
    return terms


# ———————————————————— 候选与调度 ————————————————————

@dataclass
class Candidate:
    participant_id: ID
    source: str
    score: float
    ref: object = None          # HandRaise / PrincipalQuery / Cue
    recent_share: float = 0.0

    @property
    def rank(self) -> float:
        return PRIORITY[self.source] + 20 * self.score - 15 * self.recent_share


@dataclass
class Cue:
    target: ID
    intent: str                 # "cue" / "recap" / "fill_silence" / "ask_human" / "open" / "decide"
    created_at: Millis
    note: str = ""
    ref_query_id: Optional[ID] = None


@dataclass
class SchedulerState:
    hands: list[HandRaise] = field(default_factory=list)
    ready_answers: list[PrincipalQuery] = field(default_factory=list)   # 已回答、待插回
    cues: list[Cue] = field(default_factory=list)
    last_spoke_end: dict[ID, Millis] = field(default_factory=dict)
    talk_ms: dict[ID, dict[ID, int]] = field(default_factory=dict)      # agenda -> speaker -> ms
    bot_chain: int = 0
    awaiting_human: bool = False
    reinserted: dict[ID, int] = field(default_factory=dict)            # 当前议程项已插回次数


class Scheduler:
    def __init__(self, rules: MeetingRules, human_ids: Iterable[ID], moderator_id: ID):
        self.rules = rules
        self.human_ids = set(human_ids)
        self.moderator_id = moderator_id
        self.s = SchedulerState()
        self._ids = itertools.count(1)

    # —— 举手 ——
    def raise_hand(self, participant_id: ID, agenda_item_id: ID, score: HandScore, gist: str,
                   now: Millis, based_on_seq: int, kind: str = "normal",
                   query_id: Optional[ID] = None) -> tuple[Optional[HandRaise], Optional[HandRaise]]:
        """返回 (新举手, 被替换的旧举手)。同一人只保留一个普通举手（去重），分高者留下。"""
        if kind == "normal" and not should_raise(score, self.rules):
            return None, None
        hand = HandRaise(id=f"hand-{next(self._ids)}", participant_id=participant_id,
                         agenda_item_id=agenda_item_id, kind=kind, score=score, gist=gist,
                         based_on_event_seq=based_on_seq, created_at=now,
                         expires_at=now + self.rules.hand_ttl_ms, query_id=query_id)
        replaced = None
        for h in list(self.s.hands):
            if h.participant_id == participant_id and h.kind == "normal" and kind == "normal":
                if h.score.total >= score.total and similarity(h.gist, gist) > 0.8:
                    return None, None            # 重复举手：忽略
                self.s.hands.remove(h)
                replaced = h
        self.s.hands.append(hand)
        return hand, replaced

    def expire_hands(self, now: Millis, current_agenda_id: ID) -> list[HandRaise]:
        """举手 20s 或议题切换后过期。"""
        gone = [h for h in self.s.hands if h.kind == "normal" and (h.expires_at <= now or h.agenda_item_id != current_agenda_id)]
        for h in gone:
            self.s.hands.remove(h)
        return gone

    def withdraw(self, participant_id: ID) -> list[HandRaise]:
        gone = [h for h in self.s.hands if h.participant_id == participant_id and h.kind == "normal"]
        for h in gone:
            self.s.hands.remove(h)
        return gone

    # —— 本体答案 ——
    def answer_ready(self, q: PrincipalQuery) -> None:
        self.s.ready_answers.append(q)

    def cue(self, c: Cue) -> None:
        self.s.cues.append(c)

    # —— 发言记账 ——
    def on_turn_end(self, speaker_id: ID, agenda_item_id: ID, started: Millis, ended: Millis,
                    *, cooldown: bool = True) -> None:
        """cooldown=False：被用户打断的 bot 不进冷却，它的举手可以加回补充（设计页时序图 bargein）。"""
        if cooldown:
            self.s.last_spoke_end[speaker_id] = ended
        per = self.s.talk_ms.setdefault(agenda_item_id, {})
        per[speaker_id] = per.get(speaker_id, 0) + max(0, ended - started)
        if speaker_id in self.human_ids:
            self.s.bot_chain = 0
            self.s.awaiting_human = False
        elif speaker_id != self.moderator_id:
            self.s.bot_chain += 1

    def on_human_spoke(self) -> None:
        self.s.bot_chain = 0
        self.s.awaiting_human = False

    def on_agenda_changed(self) -> None:
        self.s.reinserted = {}
        self.s.bot_chain = 0
        self.s.cues = [c for c in self.s.cues if c.intent in ("recap", "directive")]

    def recent_share(self, pid: ID, agenda_item_id: ID) -> float:
        per = self.s.talk_ms.get(agenda_item_id, {})
        total = sum(per.values())
        return per.get(pid, 0) / total if total else 0.0

    def in_cooldown(self, pid: ID, now: Millis) -> bool:
        t = self.s.last_spoke_end.get(pid)
        return t is not None and now - t < self.rules.cooldown_ms

    # —— 选下一位 ——
    def pick(self, now: Millis, current_agenda_id: ID, *, floor_free: bool, human_vad_active: bool
             ) -> tuple[Optional[Candidate], list[str]]:
        """返回 (候选, 附带动作)。附带动作："ask_human"（防 bot 链）、"defer:<qid>"（插回配额用完）。"""
        actions: list[str] = []
        if not floor_free or human_vad_active:     # 半双工 + 人类优先
            return None, actions
        for c in self.s.cues:          # 用户刚点名：高于本体插回和普通举手
            if c.intent == "directive":
                return Candidate(c.target, "moderatorCue", 1.0, c, 0.0), actions
        for c in self.s.cues:          # 议程项开场优先于一切 bot 发言（人类除外）
            if c.intent in ("open", "open_item"):
                return Candidate(c.target, "moderatorCue", 1.0, c, 0.0), actions
        cands: list[Candidate] = []
        for q in list(self.s.ready_answers):
            if q.agenda_item_id != current_agenda_id:
                used = self.s.reinserted.get(current_agenda_id, 0)
                if used >= self.rules.max_reinsert_per_item:
                    continue                      # 本议程项插回配额用完，留到下一项或会后
            cands.append(Candidate(q.asked_by, "pendingAnswer", 1.0, q,
                                   self.recent_share(q.asked_by, current_agenda_id)))
        for c in self.s.cues:
            cands.append(Candidate(c.target, "moderatorCue", 1.0, c, 0.0))
        if self.s.bot_chain >= self.rules.max_bot_chain and not self.s.awaiting_human:
            # 防 bot 互聊死循环：主持人把话轮交还用户
            self.s.awaiting_human = True
            actions.append("ask_human")
            return Candidate(self.moderator_id, "moderatorCue", 1.0,
                             Cue(self.moderator_id, "ask_human", now), 0.0), actions
        if not self.s.awaiting_human:
            for h in self.s.hands:
                if h.kind != "normal" or h.expires_at <= now or h.agenda_item_id != current_agenda_id:
                    continue
                if self.in_cooldown(h.participant_id, now) and not h.score.addressed:
                    continue
                cands.append(Candidate(h.participant_id, "handRaise", h.score.total, h,
                                       self.recent_share(h.participant_id, current_agenda_id)))
        if not cands:
            return None, actions
        best = max(cands, key=lambda c: c.rank)
        return best, actions

    def consume(self, c: Candidate, current_agenda_id: ID) -> None:
        """候选被授予令牌后，从队列移除。"""
        if c.source == "pendingAnswer":
            self.s.ready_answers.remove(c.ref)  # type: ignore[arg-type]
            if c.ref.agenda_item_id != current_agenda_id:  # type: ignore[union-attr]
                self.s.reinserted[current_agenda_id] = self.s.reinserted.get(current_agenda_id, 0) + 1
        elif c.source == "moderatorCue":
            if c.ref in self.s.cues:
                self.s.cues.remove(c.ref)  # type: ignore[arg-type]
        elif c.source == "handRaise":
            if c.ref in self.s.hands:
                self.s.hands.remove(c.ref)  # type: ignore[arg-type]
