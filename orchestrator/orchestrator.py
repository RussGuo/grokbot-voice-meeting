"""编排器：单线程 asyncio 事件循环，唯一持有会议状态与发言令牌。

处理顺序（设计页 SCHED_CODE）：每个事件先落 MeetingEvent，再改内存状态。
- vad.start：持令牌者是 bot → duck + 启动 barge-in 判定定时器；无人持令牌 → 给用户
- bargein.confirmed / rejected：真打断 / 恢复音量
- asr.final：追加 Utterance，分发给各分身打分器（举手）
- eot：用户说完 → 释放令牌 → schedule()
- tts.first_audio / bot.utterance / tts.done：bot 输出（带 token_id，过期令牌的输出丢弃）
- 定时器：barge-in 判定、单次时长告警与收回、冷场、议程 timebox、本体答案 TTL、cue 超时
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import time
from typing import Any, Callable, Optional

from .brief_compact import compact_brief, compact_snapshot
from .commitments import commitments_in_speech
from .events import EventLog
from .i18n import cut_off as i18n_cut_off
from .i18n import end_button as i18n_end_button
from .i18n import minutes_tag as i18n_minutes_tag
from .i18n import normalize as i18n_normalize
from .i18n import rules as i18n_rules
from .i18n import speaker_line as i18n_speaker_line
from .i18n import turn as i18n_turn
from .models import (
    ID, Brief, ChatCtxRecipe, Commitment, FloorToken, Meeting, Millis,
    PrincipalAnswer, PrincipalQuery, Utterance,
)
from .principal import PrincipalClient, deliver_to
from .scheduler import Candidate, Cue, Scheduler, cooldown_penalty
from .scoring import BotCtx, ScoreRequest, build_scorer
from .state_machine import Floor, MeetingStateMachine, Phase
from .summarize import SummaryRequest, fallback_text
from .voice_iface import VoiceLayer

F = Floor
log = logging.getLogger("orchestrator")


class Orchestrator:
    def __init__(self, meeting: Meeting, briefs: dict[ID, Brief], voice: VoiceLayer,
                 principal_factory: Callable[["Orchestrator"], PrincipalClient],
                 clock: Callable[[], Millis], log_path=None, summarizer=None, scorer_chat: Any = None):
        self.m = meeting
        self.briefs = briefs
        self.voice = voice
        self.clock = clock
        self.rules = meeting.rules
        self.human_id = meeting.voice.linked_human_id
        self.moderator_id = meeting.moderator.participant_id
        self.log = EventLog(meeting.id, log_path)
        self.sm = MeetingStateMachine(on_change=self._on_state_change)
        humans = [p.id for p in meeting.participants if p.kind == "human"]
        if self.human_id not in humans:
            raise ValueError(f"linked_human_id={self.human_id} 不在参会者里（必须等于用户的 LiveKit identity）")
        self.sched = Scheduler(self.rules, humans, self.moderator_id)
        # 打分器：MEETING_SCORER=rule（默认）| llm。llm 需要 scorer_chat（有 astream_text 的 LLM 客户端），
        # 缺省则退回规则版；结果以 scorer.update 事件投回 inbox（与 principal.answered 同一条路）。
        self.scorer = build_scorer(briefs, {p.id: p.aliases + [p.display_name] for p in meeting.participants},
                                   chat=scorer_chat)
        self.scorer.bind(deliver_to(self))
        self._scorer_ctx: Optional[tuple[int, ID, str, ID]] = None   # (seq, 议程项, 发言, 说话人)：最近一次 fan-out
        self._rule_hands: dict[ID, ID] = {}                          # bot -> 该次 fan-out 里规则分举的手
        self._scorer_applied = -1
        self.principal = principal_factory(self)

        self.token: Optional[FloorToken] = None
        self._tok_ids = itertools.count(1)
        self._utt_ids = itertools.count(1)
        self._q_ids = itertools.count(1)
        self._c_ids = itertools.count(1)
        self._timers: list[tuple[Millis, int, dict]] = []
        self._timer_seq = itertools.count()
        self.human_vad_active = False
        self._human_turn_start: Optional[Millis] = None
        self._turn_started: Optional[Millis] = None
        self._first_audio_at: Optional[Millis] = None
        self._idle_gen = 0
        self._silence_strikes = 0
        self._agenda_due = False
        self._end_confirm: Optional[str] = None   # None / "asking" / "waiting"
        self._end_confirmed = False
        self._human_ended = False                   # 用户口头宣布结束：最高优先级，立即散会
        self._fills_this_item = 0                   # 本项主持人「小结并邀请补充」次数（每项最多 1 次）
        self._end_asks = 0                          # 「是否结束」已问次数（最多 2 次，再冷场即结束）
        self._end_gen = 0
        self._after_recap: Optional[Candidate] = None
        self._pending_cue_human: Optional[int] = None
        self.minutes: list[str] = []
        self.utterances: list[Utterance] = []
        self.queries: dict[ID, PrincipalQuery] = {}
        self.commitments: dict[ID, Commitment] = {}
        self.started_at: Millis = 0
        self.metrics: dict[str, list[float]] = {"switch_ms": [], "bargein_stop_ms": []}
        self._ducks: list[dict] = []
        self._vad_started_at: Millis = 0
        self._directive_seen: set[str] = set()
        self._transcript_after_eot = False
        self.summarizer = summarizer                    # async (SummaryRequest) -> str|None；None → 只用兜底结论
        self._summary_tasks: dict[ID, asyncio.Task] = {}   # 议程项 id → 后台总结任务

    # ———————————————————— 基础设施 ————————————————————
    def now(self) -> Millis:
        return self.clock()

    def emit(self, type_: str, **data: Any):
        return self.log.append(type_, self.now(), **data)

    def _on_state_change(self, layer: str, a: str, b: str) -> None:
        self.log.append("PhaseChanged", self.now(), layer=layer, **{"from": a, "to": b})

    def schedule_timer(self, delay_ms: Millis, ev: dict) -> None:
        ev = dict(ev)
        at = self.now() + delay_ms
        ev.setdefault("at", at)
        heapq.heappush(self._timers, (at, next(self._timer_seq), ev))

    def next_timer_at(self) -> Optional[Millis]:
        return self._timers[0][0] if self._timers else None

    async def fire_due(self, now: Millis) -> None:
        while self._timers and self._timers[0][0] <= now:
            _, _, ev = heapq.heappop(self._timers)
            await self.handle(ev)

    def name(self, pid: ID) -> str:
        return self.m.participant(pid).display_name

    def is_bot(self, pid: ID) -> bool:
        return self.m.participant(pid).kind == "bot"

    @property
    def agenda_id(self) -> ID:
        a = self.m.current_agenda
        return a.id if a else ""

    async def _publish(self, pid: ID, state: str) -> None:
        payload = {"speaker_id": pid, "display_name": self.name(pid), "state": state}
        self.emit("SpeakerPublished", topic="meeting.speaker", payload=payload)
        await self.voice.publish_current_speaker(pid, self.name(pid), state)

    async def publish_roster(self) -> None:
        parts = [{"speaker_id": p.id, "display_name": p.display_name} for p in self.m.participants
                 if p.invite == "accepted"]
        self.emit("SpeakerPublished", topic="meeting.roster", payload={"participants": parts})
        await self.voice.publish_roster(parts)

    # ———————————————————— 会议生命周期 ————————————————————
    async def start(self) -> None:
        """Inviting → Preparing → OpeningSync → InAgenda；发 roster；主持人开场。"""
        self.started_at = self.now()
        self.emit("MeetingCreated", payload={
            "title": self.m.title, "initiator": self.m.initiator, "language": getattr(self.m, "language", "zh"),
            "moderator": self.m.moderator.to_dict(), "linked_human_id": self.human_id,
            "agenda": [{"id": a.id, "title": a.title, "timebox_ms": a.timebox_ms} for a in self.m.agenda],
            "participants": [{"id": p.id, "kind": p.kind, "display_name": p.display_name, "role": p.role,
                              "tts_voice": p.agent_profile.tts_voice if p.agent_profile else None}
                             for p in self.m.participants]})
        for p in self.m.participants:
            self.emit("InviteSent", payload={"participant_id": p.id})
            self.emit("InviteAnswered", payload={"participant_id": p.id, "answer": p.invite})
        self.sm.to_phase(Phase.PREPARING)
        self.m.status = "preparing"
        for pid, b in self.briefs.items():
            self.emit("BriefSubmitted", brief=b)
        self.sm.to_phase(Phase.OPENING_SYNC)
        await self.publish_roster()
        self.m.status = "live"
        self.sm.to_phase(Phase.IN_AGENDA)
        self._start_agenda_item(first=True)
        await self.schedule()

    async def add_participant(self, p) -> None:
        self.m.participants.append(p)
        await self.publish_roster()

    def _start_agenda_item(self, first: bool = False) -> None:
        item = self.m.current_agenda
        item.status = "active"
        self.emit("AgendaItemStarted", agenda_item_id=item.id)
        self.sched.on_agenda_changed()
        for h in self.sched.expire_hands(self.now(), item.id):
            self.emit("HandExpired", hand=h)
        self._agenda_due = False
        self._silence_strikes = 0
        self._fills_this_item = 0
        self.schedule_timer(item.timebox_ms, {"type": "timer.agenda", "agenda_item_id": item.id})
        self.sched.cue(Cue(self.moderator_id, "open" if first else "open_item", self.now()))
        if first and self.rules.opening_round:      # 开场同步：主持人依次 cue 各成员 bot 一句话立场
            for p in self.m.participants:
                if p.kind == "bot" and p.id != self.moderator_id and p.id in self.briefs:
                    self.sched.cue(Cue(p.id, "cue", self.now(), note="opening"))

    async def advance_agenda(self) -> None:
        """InAgenda → Deciding → 下一项 / Closing。"""
        item = self.m.current_agenda
        self.sm.to_phase(Phase.DECIDING)
        lang = getattr(self.m, "language", "zh")
        mine = [u for u in self.utterances if u.agenda_item_id == item.id]
        ids = {u.id for u in mine}
        binding = [c.text for c in self.commitments.values() if c.status == "binding" and c.utterance_id in ids]
        # 先写确定性兜底并立即推进；LLM 总结在后台跑，落地后再发 DecisionSummarized（不 await：慢调用会拖住用户语音事件）
        text = fallback_text(lang, binding)
        item.status = "decided"
        self.emit("DecisionRecorded", payload={"agenda_item_id": item.id, "title": item.title, "text": text,
                                               "method": "pending" if self.summarizer else "fallback"})
        self.emit("AgendaItemClosed", agenda_item_id=item.id)
        self.minutes.append(self._decision_line(lang, item.title, text))
        if self.summarizer:
            req = SummaryRequest(
                item_title=item.title, language=lang, commitments=binding,
                utterances=[(self.name(u.speaker_id), u.text) for u in mine if not u.interrupted])
            self._summary_tasks[item.id] = asyncio.create_task(self._summarize(item.id, item.title, text, req))
        if self.m.current_agenda_idx + 1 < len(self.m.agenda) and not self._human_ended:
            self.m.current_agenda_idx += 1
            self.sm.to_phase(Phase.IN_AGENDA)
            self._start_agenda_item()
            await self.schedule()
        else:
            await self.close()

    def _decision_line(self, lang: str, title: str, text: str) -> str:
        return f"{i18n_minutes_tag(lang)} \"{title}\": {text}" if i18n_normalize(lang) == "en" else f"{i18n_minutes_tag(lang)} 「{title}」：{text}"

    async def _summarize(self, item_id: ID, title: str, fallback: str, req: SummaryRequest) -> None:
        """后台任务：总结完成后发 DecisionSummarized，并把共享纪要里的兜底行原位换掉（bot 之后看到的是总结）。"""
        try:
            text = await self.summarizer(req)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("议程总结失败：%s", type(e).__name__)
            return
        if not text or text == fallback:
            return
        self.emit("DecisionSummarized", payload={"agenda_item_id": item_id, "title": title, "text": text,
                                                 "method": "llm"})
        lang = getattr(self.m, "language", "zh")
        old = self._decision_line(lang, title, fallback)
        for i in range(len(self.minutes) - 1, -1, -1):
            if self.minutes[i] == old:
                self.minutes[i] = self._decision_line(lang, title, text)
                break

    SUMMARY_WAIT_S = 6.0        # close() 等后台总结的总上限（秒，真实时间）

    async def _drain_summaries(self) -> None:
        tasks = [t for t in self._summary_tasks.values() if not t.done()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self.SUMMARY_WAIT_S)
            for t in pending:                      # 超时的保留兜底结论，不再等
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                log.warning("%d 个议程总结超时，纪要保留兜底结论", len(pending))
        self._summary_tasks.clear()

    async def close(self) -> None:
        if self.sm.phase == Phase.IN_AGENDA:
            if self.token:
                await self._revoke("meeting_end")
            self.sm.to_phase(Phase.DECIDING)
        self.sm.to_phase(Phase.CLOSING)
        self.m.status = "closing"
        # 没回或没来得及插回的本体答案 → 纪要「会后补充」
        for q in self.queries.values():
            if q.status in ("pending", "answered"):
                if q.status == "pending":
                    self.sm.pending_count = max(0, self.sm.pending_count - 1)
                q.status = "deferredToMinutes"
                self.emit("PrincipalDeferred", query=q, reason="meeting_end")
        await self._drain_summaries()          # 纪要从事件流生成：总结要赶在 MeetingEnded 之前落地
        self.emit("MeetingEnded", minutesId=f"minutes-{self.m.id}")
        self.sm.to_phase(Phase.POST_MEETING)
        self.m.status = "post"

    async def reconcile(self, participant_id: ID, items: list[dict]) -> None:
        """会后对账：本体逐条 confirm / correct / retract。"""
        for it in items:
            cid = it.get("commitment_id")
            if cid and cid in self.commitments:
                c = self.commitments[cid]
                c.status = {"confirm": "confirmed", "correct": "corrected", "retract": "retracted"}[it["verdict"]]
                c.correction = it.get("correction")
        self.emit("ReconcileResult", participantId=participant_id, items=items)

    def switch_moderator(self, to: ID, reason: str) -> None:
        """决策 3：中立主持 ⇄ Chief of Staff 兼任。v0 只做接口，默认关闭。"""
        frm = self.moderator_id
        self.moderator_id = to
        self.sched.moderator_id = to
        self.m.moderator.participant_id = to
        self.m.moderator.mode = "neutral" if to == "moderator" else "chiefOfStaff"
        self.m.moderator.switched_at = self.now()
        self.emit("ModeratorSwitched", **{"from": frm, "to": to, "reason": reason})

    # ———————————————————— 事件分发 ————————————————————
    async def handle(self, ev: dict) -> None:
        t = ev["type"]
        h = getattr(self, "_on_" + t.replace(".", "_"), None)
        if h is None:
            raise ValueError(f"未知事件 {t}")
        await h(ev)

    def token_valid(self, token_id: str) -> bool:
        """给语音层的分段推送用：令牌失效后停止发送后续段。"""
        return self.token is not None and self.token.id == token_id and self.sm.bot_speaking

    # —— 人类侧 ——
    async def _on_vad_start(self, ev):
        if ev.get("participant_id", self.human_id) != self.human_id:
            return      # RoomIO 只链接用户；其他来源一律忽略
        self.human_vad_active = True
        self._transcript_after_eot = False
        self._vad_started_at = self.now()
        self.emit("HumanVad", stage="start", participant_id=self.human_id)
        self.sched.on_human_spoke()
        if self.sm.phase != Phase.IN_AGENDA:
            return
        tok = self.token
        if tok and self.is_bot(tok.holder_id):
            if self.sm.floor == F.PLAYING:
                self.sm.to_floor(F.DUCKING)
                self.voice.duck(self.rules.duck_gain_db)       # 发送端立即压低
                self.emit("Ducked", speakerId=tok.holder_id)
                self._mark_duck(tok)
                await self._publish(tok.holder_id, "ducking")
                self.schedule_timer(self.rules.bargein_min_duration_ms,
                                    {"type": "timer.bargein", "token_id": tok.id})
            elif self.sm.floor == F.SWITCHING:
                await self._revoke("human_priority")
                await self._grant_human()
            # DUCKING 中再次 vad.start：判定定时器已在跑，不重复
        elif tok is None:
            if self.sm.can_floor(F.HUMAN_SPEAKING):
                await self._grant_human()

    async def _on_vad_end(self, ev):
        if ev.get("participant_id", self.human_id) != self.human_id:
            return
        self.human_vad_active = False
        self.emit("HumanVad", stage="end", participant_id=self.human_id)
        if self.sm.floor == F.DUCKING:
            await self._on_bargein_rejected({"type": "bargein.rejected", "token_id": self.token.id})

    async def _on_timer_bargein(self, ev):
        tok = self.token
        if tok and tok.id == ev["token_id"] and self.sm.floor == F.DUCKING and self.human_vad_active:
            await self._on_bargein_confirmed({"type": "bargein.confirmed", "token_id": tok.id})

    async def _on_bargein_rejected(self, ev):
        tok = self.token
        if not tok or self.sm.floor != F.DUCKING or ev.get("token_id", tok.id) != tok.id:
            return
        self.voice.restore()
        self.sm.to_floor(F.PLAYING)
        self.emit("DuckRestored", speakerId=tok.holder_id)
        self._mark_duck(tok, restored=True)
        await self._publish(tok.holder_id, "speaking")

    async def _on_bargein_confirmed(self, ev):
        tok = self.token
        if not tok or not self.sm.bot_speaking or ev.get("token_id", tok.id) != tok.id:
            return
        played = self.voice.interrupt()["played_chars"]
        if tok.intent == "confirm_end" and self._end_confirm == "asking":
            self._end_confirm = "waiting"            # 用户打断「是否结束」= 正在回答
            self._end_gen += 1
            self.schedule_timer(self.rules.end_confirm_timeout_ms, {"type": "timer.endConfirm", "gen": self._end_gen})
        self.metrics["bargein_stop_ms"].append(self.now() - getattr(self, "_vad_started_at", self.now()))
        self.sm.to_floor(F.INTERRUPTED)
        self.emit("Interrupted", speakerId=tok.holder_id, by=self.human_id, atChar=played)
        self._record_bot_partial(tok, played)
        self.emit("FloorRevoked", token=tok, reason="interrupted")
        self.sched.on_turn_end(tok.holder_id, self.agenda_id, self._turn_started or self.now(), self.now(),
                               cooldown=False)
        await self._publish(tok.holder_id, "idle")
        self.token = None
        await self._grant_human(from_interrupted=True)

    async def _on_asr_partial(self, ev):
        pass

    async def _on_asr_final(self, ev):
        if ev.get("participant_id", self.human_id) != self.human_id or self.sm.phase != Phase.IN_AGENDA:
            return
        # 晚到的 final（eot 已经把话轮交还）不能再抢回发言权，否则点名指令会一直挂着。
        # human_vad 仍开着，或这不是「刚说完」的补转写，才按 VAD 漏报兜底。
        if (self.token is None and self.sm.can_floor(F.HUMAN_SPEAKING)
                and not self._transcript_after_eot):
            await self._grant_human()
        if self._transcript_after_eot and not self.human_vad_active:
            self._transcript_after_eot = False
        text = ev["text"]
        u = Utterance(id=f"utt-{next(self._utt_ids)}", speaker_id=self.human_id, agenda_item_id=self.agenda_id,
                      text=text, started_at=self._human_turn_start or self.now(), ended_at=self.now(),
                      token_id=self.token.id if self.token else None,
                      addressees=[p.id for p in self.m.participants if p.kind == "bot" and self.scorer.addressed(p.id, text)])
        self._append_utterance(u)
        self._silence_strikes = 0
        await self._queue_directive(text)
        if is_end_command(text, getattr(self.m, "language", "zh")):
            await self._human_end(text)
            return
        if self._end_confirm == "waiting":
            if is_end_affirmation(text, getattr(self.m, "language", "zh")):
                self._end_confirmed = True
                self.emit("EndConfirm", stage="confirmed", text=text)
            else:                                   # 用户还有话说：取消结束询问，继续讨论
                self._end_confirm = None
                self._end_gen += 1
                self._agenda_due = False
                self.emit("EndConfirm", stage="continue", text=text)

    async def _human_end(self, text: str) -> None:
        """硬规则：人的介入优先级最高。用户说「结束吧/散会」或点结束按钮 = 任何状态下立即结束。"""
        if self.sm.phase != Phase.IN_AGENDA:
            return
        self._human_ended = True
        self._end_confirmed = True
        self._end_confirm = None
        self._end_gen += 1
        self.sched.s.cues.clear()
        self.emit("EndConfirm", stage="human_command", text=text)
        if self.token is not None and self.is_bot(self.token.holder_id):
            await self._revoke("meeting_end")
            self.sm.floor = F.FLOOR_IDLE
        if self.token is None:
            await self.advance_agenda()

    # —— 网页控制（meeting.control，main.py 已校验发送方是用户）——
    async def _on_control_end(self, ev):
        await self._human_end(ev.get("text") or i18n_end_button(getattr(self.m, "language", "zh")))

    async def _on_control_interrupt(self, ev):
        """打断按钮 = 用户开口打断：bot 立即停，令牌给用户；他若不说话，6s 后自动释放。"""
        tok = self.token
        if self.sm.phase != Phase.IN_AGENDA:
            return
        if tok and self.is_bot(tok.holder_id) and self.sm.bot_speaking:
            await self._on_bargein_confirmed({"type": "bargein.confirmed", "token_id": tok.id})
        elif tok is None and self.sm.can_floor(F.HUMAN_SPEAKING):
            await self._grant_human()
        else:
            return
        self.emit("HumanDirective", stage="button", action="interrupt")
        if self.token is not None:
            self.schedule_timer(6_000, {"type": "timer.controlRelease", "token_id": self.token.id})

    async def _on_timer_controlRelease(self, ev):
        tok = self.token
        if tok and tok.id == ev["token_id"] and tok.holder_id == self.human_id and not self.human_vad_active:
            await self._on_eot({"type": "eot", "participant_id": self.human_id})

    async def _on_eot(self, ev):
        if ev.get("participant_id", self.human_id) != self.human_id:
            return
        # LiveKit often commits the turn (eot) before the final transcript event.
        # The turn message already carries the text; parse it here or the directive is skipped.
        said = ev.get("text") or ""
        if said:
            await self._queue_directive(said)
        tok = self.token
        if tok and tok.holder_id == self.human_id and self.sm.floor == F.HUMAN_SPEAKING:
            self.emit("FloorReleased", token=tok)
            self.sched.on_turn_end(self.human_id, self.agenda_id, self._human_turn_start or self.now(), self.now())
            self.token = None
            self.sm.to_floor(F.FLOOR_IDLE)
            self._transcript_after_eot = True
            await self._publish(self.human_id, "idle")
        await self.schedule()

    def _directive_aliases(self) -> dict[str, list[str]]:
        return _alias_map(self.m.participants, self.moderator_id)

    async def _queue_directive(self, text: str) -> None:
        """Record a spoken directive once, then run it if the floor is already free.

        If the human still holds the floor, the following eot calls schedule().
        If eot already ran and someone else was granted, preempt that turn:
        the transcript arrived late, which is the usual barge-in race.
        """
        text = (text or "").strip()
        if not text or self.sm.phase != Phase.IN_AGENDA:
            return
        target = parse_directive(text, self._directive_aliases())
        if not target:
            return
        key = f"{target}:{text}"
        if key in self._directive_seen:
            return
        self._directive_seen.add(key)
        did = f"dir-{next(self._q_ids)}"
        self.emit("HumanDirective", stage="parsed", directive_id=did, target=target, text=text)
        for h in self.sched.withdraw(target):
            self.emit("HandWithdrawn", hand=h)
        self.sched.s.cues.insert(0, Cue(target, "directive", self.now(), note=text, ref_query_id=did))
        await self._run_directive_if_floor_free()

    async def _run_directive_if_floor_free(self) -> None:
        if self.sm.phase != Phase.IN_AGENDA or self.human_vad_active or self.sm.floor == F.HUMAN_SPEAKING:
            return
        directive = next((c for c in self.sched.s.cues if c.intent == "directive"), None)
        if directive is None:
            return
        tok = self.token
        if tok and self.is_bot(tok.holder_id) and tok.intent != "directive" and directive.target != tok.holder_id:
            if self.sm.floor in (F.PLAYING, F.SWITCHING, F.GRANTING, F.CUEING):
                await self._revoke("directive")
                if self.sm.floor != F.FLOOR_IDLE:
                    self.sm.to_floor(F.FLOOR_IDLE)
                await self.schedule()
            return
        if tok is None and self.sm.floor == F.FLOOR_IDLE:
            await self.schedule()

    # —— bot 侧 ——
    def _stale(self, ev) -> bool:
        tok = self.token
        if tok is None or ev.get("token_id") != tok.id:
            self.emit("OutputDropped", token_id=ev.get("token_id"), kind=ev["type"])
            return True
        return False

    async def _on_tts_first_audio(self, ev):
        if self._stale(ev):
            return
        self._first_audio_at = self.now()
        self.emit("BotFirstAudio", token_id=ev["token_id"], speakerId=self.token.holder_id)

    async def _on_tts_segment(self, ev):
        """分段播放回报；过期令牌的段丢弃（真实语音层在令牌失效后本就不该再发）。"""
        if self._stale(ev):
            return
        self.metrics.setdefault("segments", []).append(len(ev.get("text", "")))

    async def _on_bot_utterance(self, ev):
        if self._stale(ev):
            return
        tok = self.token
        pid = tok.holder_id
        u = Utterance(id=f"utt-{next(self._utt_ids)}", speaker_id=pid, agenda_item_id=self.agenda_id,
                      text=ev["text"], started_at=self._first_audio_at or self._turn_started or self.now(),
                      ended_at=self.now(), token_id=tok.id, provenance=ev.get("provenance"),
                      confidence=ev.get("confidence"), addressees=ev.get("addressees", []),
                      ducked=list(getattr(self, "_ducks", [])))
        planned = [t for t in (ev.get("commitments") or []) if t]
        spoken = commitments_in_speech(ev.get("text") or "", self.briefs[pid].mandate.can_commit if pid in self.briefs else [])
        merged: list[str] = []
        for text in planned + spoken:
            if text not in merged:
                merged.append(text)
        for text in self._guard_commitments(pid, merged):
            c = Commitment(id=f"cmt-{next(self._c_ids)}", text=text, owner_id=pid, made_by=pid, utterance_id=u.id)
            u.commitments.append(c)
            self.commitments[c.id] = c
        self._append_utterance(u)
        for c in u.commitments:
            self.emit("CommitmentMade", commitment=c)   # 决策 1：当场生效 binding
        if ev.get("ask_principal") and self.sm.floor in (F.PLAYING, F.DUCKING):
            await self._ask_principal(pid, ev["ask_principal"])

    def _guard_commitments(self, pid: ID, texts: list[str]) -> list[str]:
        """编排器兜底校验 mandate：命中 mustAsk 的承诺不生效（分身应转慢路径）。"""
        b = self.briefs.get(pid)
        if not b:
            return []
        return [t for t in texts if not any(m and m in t for m in b.mandate.must_ask)]

    async def _ask_principal(self, pid: ID, question: str) -> None:
        mine = [q for q in self.queries.values() if q.asked_by == pid and q.status == "pending"]
        if len(mine) >= self.rules.max_pending_per_bot:
            return
        p = self.m.participant(pid)
        remaining = max(1, self.rules.total_ms - (self.now() - self.started_at))
        q = PrincipalQuery(id=f"pq-{next(self._q_ids)}", asked_by=pid,
                           principal_bot_id=p.principal_ref.bot_id if p.principal_ref else pid,
                           question=question, agenda_item_id=self.agenda_id,
                           context_event_seq=self.log.last_seq, asked_at=self.now(), ttl_ms=remaining)
        self.queries[q.id] = q
        self.sm.to_floor(F.ASKING_PRINCIPAL)
        self.sm.pending_asked()
        self.emit("PrincipalAsked", query=q)
        self.schedule_timer(q.ttl_ms, {"type": "timer.principalTtl", "query_id": q.id})
        await self.principal.ask(q)

    async def _on_tts_done(self, ev):
        if self._stale(ev):
            return
        tok = self.token
        if self.sm.floor == F.DUCKING:
            self.voice.restore()
        self.emit("FloorReleased", token=tok)
        self.sched.on_turn_end(tok.holder_id, self.agenda_id, self._turn_started or self.now(), self.now())
        if tok.holder_id == self.moderator_id and tok.intent == "confirm_end" and self._end_confirm == "asking":
            self._end_confirm = "waiting"
            self._end_gen += 1
            self.schedule_timer(self.rules.end_confirm_timeout_ms, {"type": "timer.endConfirm", "gen": self._end_gen})
        if tok.holder_id == self.moderator_id and tok.intent == "fill_silence":
            self._silence_strikes += 1
        elif tok.holder_id != self.moderator_id:
            self._silence_strikes = 0
        self.token = None
        self.sm.to_floor(F.FLOOR_IDLE)
        await self._publish(tok.holder_id, "idle")
        if self.human_vad_active:                   # bot 播完时用户还在说
            await self._grant_human()
            return
        if self._after_recap is not None:           # 主持人回溯完 → 立刻给本体答案
            c, self._after_recap = self._after_recap, None
            self.sm.to_floor(F.GRANTING)
            await self._switch_and_grant(c)
            return
        await self.schedule()

    # —— 本体慢路径 ——
    async def _on_principal_answered(self, ev):
        q = self.queries.get(ev["query_id"])
        if not q or q.status != "pending":
            return
        q.status = "answered"
        q.answer = PrincipalAnswer(text=ev["text"], received_at=self.now())
        q.round_trip_ms = self.now() - q.asked_at
        self.sm.pending_resolved()
        self.emit("PrincipalAnswered", query=q)
        self.sched.answer_ready(q)      # 决策 4：作为 pendingAnswer 举手入队，可跨议程插回
        await self._yield_moderator_filler("pendingAnswer")
        await self.schedule()

    async def _yield_moderator_filler(self, reason: str) -> None:
        """主持人正在说程序性填充话（小结/问是否结束）时，本体答案到了 → 立刻让出，在这个空档插回。"""
        tok = self.token
        if not tok or tok.holder_id != self.moderator_id or tok.intent not in ("fill_silence", "confirm_end"):
            return
        if not self.sm.bot_speaking:
            return
        played = self.voice.interrupt()["played_chars"]
        self._record_bot_partial(tok, played)
        if tok.intent == "confirm_end":
            self._end_confirm = None
            self._end_gen += 1
            self.emit("EndConfirm", stage="preempted", reason=reason)
        self.emit("FloorRevoked", token=tok, reason=reason)
        self.sched.on_turn_end(tok.holder_id, self.agenda_id, self._turn_started or self.now(), self.now(), cooldown=False)
        self.token = None
        self.sm.to_floor(F.FLOOR_IDLE)
        await self._publish(tok.holder_id, "idle")

    async def _on_timer_principalTtl(self, ev):
        q = self.queries.get(ev["query_id"])
        if q and q.status == "pending":
            q.status = "expired"
            self.sm.pending_resolved()
            self.emit("PrincipalExpired", query=q)

    # —— 定时器 ——
    async def _on_timer_floorWarn(self, ev):
        if self.token and self.token.id == ev["token_id"]:
            self.emit("FloorWarned", token=self.token)

    async def _on_timer_floorTtl(self, ev):
        tok = self.token
        if not tok or tok.id != ev["token_id"] or not self.sm.bot_speaking:
            return
        played = self.voice.interrupt()["played_chars"]
        self._record_bot_partial(tok, played)
        self.sm.to_floor(F.OVERTIME)
        self.emit("FloorRevoked", token=tok, reason="overtime")
        self.sched.on_turn_end(tok.holder_id, self.agenda_id, self._turn_started or self.now(), self.now())
        self.token = None
        self.sm.to_floor(F.FLOOR_IDLE)
        await self._publish(tok.holder_id, "idle")
        await self.schedule()

    async def _on_timer_silence(self, ev):
        if ev["gen"] != self._idle_gen or self.token or self.sm.floor != F.FLOOR_IDLE or self.sm.phase != Phase.IN_AGENDA:
            return
        if self._end_confirm is not None:
            return                                   # 正在等用户确认是否结束，由 timer.endConfirm 处理
        if self._silence_strikes >= 1 or self._agenda_due or self._fills_this_item >= 1:
            # 冷场累计 ≥ 8s（4s 小结后又 4s 无人接）→ 推进议程；最后一项不直接结束，先问用户
            self.sm.to_floor(F.SILENCE)
            if self._is_last_item() and not self._end_confirmed:
                await self._ask_end()
            else:
                await self.advance_agenda()
            return
        self.sm.to_floor(F.SILENCE)
        self._fills_this_item += 1
        c = Candidate(self.moderator_id, "moderatorCue", 1.0, Cue(self.moderator_id, "fill_silence", self.now()))
        self.sm.to_floor(F.CUEING)
        await self._switch_and_grant(c)

    async def _on_timer_agenda(self, ev):
        if ev["agenda_item_id"] == self.agenda_id:
            self._agenda_due = True
            if self.token is None and self.sm.floor == F.FLOOR_IDLE:
                await self.schedule()

    async def _on_timer_endConfirm(self, ev):
        """问完「是否结束」后再冷场 15s → 结束会议。"""
        if ev["gen"] != self._end_gen or self._end_confirm != "waiting":
            return
        if self.token is not None or self.human_vad_active or self.sm.floor != F.FLOOR_IDLE:
            return                                   # 用户正在说话：等他的 asr.final 决定
        self.emit("EndConfirm", stage="timeout")
        self._end_confirmed = True
        await self.advance_agenda()

    def _is_last_item(self) -> bool:
        return self.m.current_agenda_idx >= len(self.m.agenda) - 1

    async def _ask_end(self) -> None:
        if self._end_asks >= self.rules.max_end_asks:   # 已问过两次仍冷场 → 直接结束，不再重复问
            self.emit("EndConfirm", stage="timeout", reason="max_asks")
            self._end_confirmed = True
            await self.advance_agenda()
            return
        self._end_asks += 1
        self._end_confirm = "asking"
        self.emit("EndConfirm", stage="asked")
        c = Candidate(self.moderator_id, "moderatorCue", 1.0, Cue(self.moderator_id, "confirm_end", self.now()))
        self.sm.to_floor(F.CUEING)
        await self._switch_and_grant(c)

    async def _on_timer_cue(self, ev):
        if self._pending_cue_human == ev["gen"] and self.sm.floor == F.CUEING:
            self._pending_cue_human = None
            self.sm.to_floor(F.FLOOR_IDLE)
            await self.schedule()

    # ———————————————————— 调度与授予 ————————————————————
    async def schedule(self) -> None:
        if self.sm.phase != Phase.IN_AGENDA or self.token is not None or self.sm.floor != F.FLOOR_IDLE:
            return
        for h in self.sched.expire_hands(self.now(), self.agenda_id):
            self.emit("HandExpired", hand=h)
        if self._end_confirmed:
            await self.advance_agenda()               # 用户确认结束（或超时）
            return
        if self._end_confirm == "waiting" and not self._end_confirmed and self.sched.s.ready_answers:
            # 等确认期间本体答案到了：pendingAnswer 优先级高于「结束」，先插回，之后重新问
            self._end_confirm = None
            self._end_gen += 1
            self.emit("EndConfirm", stage="preempted", reason="pendingAnswer")
        if self._end_confirm is not None:
            return                                   # 等用户回答「是否结束」，bot 不接话
        ready_here = any(q.agenda_item_id == self.agenda_id for q in self.sched.s.ready_answers)
        if self._agenda_due and not ready_here:   # 本项已有到达的本体答案：先插回再推进（免得跨议程回溯）
            if self._is_last_item():
                await self._ask_end()
            else:
                await self.advance_agenda()
            return
        best, actions = self.sched.pick(self.now(), self.agenda_id, floor_free=True,
                                        human_vad_active=self.human_vad_active)
        if best is None:
            self._idle_gen += 1
            self.schedule_timer(self.rules.silence_ms, {"type": "timer.silence", "gen": self._idle_gen})
            return
        if "ask_human" in actions:
            self.sm.to_floor(F.CUEING)
            await self._switch_and_grant(best)
            return
        self.sched.consume(best, self.agenda_id)
        if best.source == "pendingAnswer" and best.ref.agenda_item_id != self.agenda_id:
            # 决策 4：跨议程插回前，主持人一句话回溯原议题
            self.emit("ModeratorRecap", query=best.ref)
            self._after_recap = best
            recap = Candidate(self.moderator_id, "moderatorCue", 1.0,
                              Cue(self.moderator_id, "recap", self.now(), ref_query_id=best.ref.id))
            self.sm.to_floor(F.CUEING)
            await self._switch_and_grant(recap)
            return
        if best.source == "moderatorCue":
            cue: Cue = best.ref
            self.sm.to_floor(F.CUEING)
            if cue.target == self.human_id:
                self._idle_gen += 1
                self._pending_cue_human = self._idle_gen
                self.schedule_timer(self.rules.cue_timeout_ms, {"type": "timer.cue", "gen": self._idle_gen})
                return
            await self._switch_and_grant(best)
            return
        self.sm.to_floor(F.GRANTING)
        await self._switch_and_grant(best)

    def assemble_chat_ctx(self, pid: ID, intent: str, meta: dict) -> ChatCtxRecipe:
        """update_agent 会清空历史，必须重组：brief + 共享主题快照 + 最近 8 条共享纪要。"""
        p = self.m.participant(pid)
        lang = getattr(self.m, "language", "zh")
        rules_text = i18n_rules(lang)
        system = (p.agent_profile.instructions if p.agent_profile else "") + "\n" + rules_text
        brief = self.briefs.get(pid)
        snapshots = {bid: b.memory_snapshot.to_dict() for bid, b in self.briefs.items()}  # 决策 2：共享可见
        item = self.m.current_agenda
        last_text = self.utterances[-1].text if self.utterances else ""
        meta = {"intent": intent, "agenda_title": item.title if item else "", "agenda_item_id": item.id if item else "", "last_text": last_text, **meta}
        instr = i18n_turn(lang, intent)
        brief_text, snaps_text = "", {}
        if brief:
            brief_text, cut, full_len = compact_brief(brief.to_dict())
            if cut:
                log.warning("brief 超长被截断：%s %d→%d 字", pid, full_len, len(brief_text))
                self.emit("ContextTruncated", participant_id=pid, part="brief", full=full_len, kept=len(brief_text))
        for bid, b in self.briefs.items():
            if bid == pid:
                continue                      # 自己的快照已在 brief 的「关键数字」里
            t, cut = compact_snapshot(b.memory_snapshot.to_dict())
            snaps_text[bid] = t
            if cut:
                self.emit("ContextTruncated", participant_id=pid, part=f"snapshot:{bid}", full=-1, kept=len(t))
        return ChatCtxRecipe(participant_id=pid, system=system.strip(),
                             brief=brief.to_dict() if brief else None, topic_snapshots=snapshots,
                             shared_minutes=self.minutes[-self.rules.minutes_window:],
                             max_turns=self.rules.minutes_window, turn_instruction=instr, turn_meta=meta,
                             brief_text=brief_text, snapshots_text=snaps_text)

    async def _switch_and_grant(self, c: Candidate) -> None:
        pid = c.participant_id
        meta: dict[str, Any] = {}
        intent = "answer"
        if c.source == "moderatorCue":
            intent = c.ref.intent
            if intent == "directive":
                meta["addressed"] = True
                meta["directive_text"] = c.ref.note
            if intent == "recap":
                q = self.queries[c.ref.ref_query_id]
                meta["recap_item"] = next(a.title for a in self.m.agenda if a.id == q.agenda_item_id)
        elif c.source == "pendingAnswer":
            intent = "relay"
            meta["answer"] = c.ref.answer.text
            meta["query_id"] = c.ref.id
        elif c.source == "handRaise":
            meta["gist"] = c.ref.gist
            meta["addressed"] = c.ref.score.addressed
        self.sm.to_floor(F.SWITCHING)
        tok = FloorToken(id=f"tok-{next(self._tok_ids)}", holder_id=pid, source=c.source,
                         granted_at=self.now(), ttl_ms=self.rules.floor_ttl_ms, intent=intent)
        self.token = tok
        self._idle_gen += 1
        self._ducks = []
        ctx = self.assemble_chat_ctx(pid, intent, meta)
        res = await self.voice.switch_agent(pid, ctx)
        tok.switch_ms = res.get("switch_ms")
        self.metrics["switch_ms"].append(tok.switch_ms or 0)
        p = self.m.participant(pid)
        self.emit("AgentSwitched", to=pid, switchMs=tok.switch_ms,
                  tts_voice=p.agent_profile.tts_voice if p.agent_profile else None)
        self.emit("FloorGranted", token=tok)
        if c.source == "moderatorCue" and c.ref.intent == "directive":
            self.emit("HumanDirective", stage="executed", directive_id=c.ref.ref_query_id, target=pid, token_id=tok.id)
        if c.source == "pendingAnswer":
            c.ref.status = "deliveredInMeeting"
            self.emit("PrincipalAnswered", query=c.ref)
        self.sm.to_floor(F.PLAYING)
        self._turn_started = self.now()
        self._first_audio_at = None
        await self._publish(pid, "speaking")
        self.schedule_timer(self.rules.floor_warn_ms, {"type": "timer.floorWarn", "token_id": tok.id})
        self.schedule_timer(self.rules.floor_ttl_ms, {"type": "timer.floorTtl", "token_id": tok.id})
        await self.voice.speak(tok.id, pid, ctx)   # 拿到令牌后才用最新 chat_ctx 生成，不播旧草稿

    async def _grant_human(self, from_interrupted: bool = False) -> None:
        if self.sm.floor in (F.GRANTING, F.SWITCHING, F.SILENCE, F.OFF_TOPIC, F.CUEING, F.FLOOR_IDLE, F.INTERRUPTED):
            self.sm.to_floor(F.HUMAN_SPEAKING)
        self._pending_cue_human = None
        tok = FloorToken(id=f"tok-{next(self._tok_ids)}", holder_id=self.human_id, source="human",
                         granted_at=self.now(), ttl_ms=10 ** 9, interruptible=False)
        self.token = tok
        self._idle_gen += 1
        self._human_turn_start = self.now()
        self.emit("FloorGranted", token=tok)
        await self._publish(self.human_id, "speaking")

    async def _revoke(self, reason: str) -> None:
        tok = self.token
        if not tok:
            return
        if self.is_bot(tok.holder_id):
            self.voice.interrupt()
        self.emit("FloorRevoked", token=tok, reason=reason)
        await self._publish(tok.holder_id, "idle")
        self.token = None

    # ———————————————————— 记录 ————————————————————
    def _append_utterance(self, u: Utterance) -> None:
        self.utterances.append(u)
        self.emit("UtteranceFinal", utterance=u)
        self.minutes.append(i18n_speaker_line(getattr(self.m, "language", "zh"), self.name(u.speaker_id), u.text))
        self._fan_out(u)

    def _said_in_item(self, agenda_item_id: ID) -> list[str]:
        # 「已说内容」只算成员 bot 的实质发言（主持人的程序性发言、人类提问不算）
        return [x.text for x in self.utterances if x.agenda_item_id == agenda_item_id
                and x.speaker_id not in (self.human_id, self.moderator_id) and not x.interrupted]

    def _fan_out(self, u: Utterance) -> None:
        """把最新发言推给各分身打分器；达到阈值即举手。LLM 模式下另发一次异步二次打分（不阻塞）。"""
        said = self._said_in_item(u.agenda_item_id)
        seq = self.log.last_seq
        self._scorer_ctx = (seq, u.agenda_item_id, u.text, u.speaker_id)
        self._rule_hands = {}
        scored: list[BotCtx] = []
        for p in self.m.participants:
            if p.kind != "bot" or p.id in (u.speaker_id, self.moderator_id) or p.id not in self.briefs:
                continue
            pen = cooldown_penalty(self.sched.s.last_spoke_end.get(p.id), self.now(), self.rules)
            score, gist = self.scorer.score(p.id, u.text, said, pen, u.agenda_item_id)
            hand, replaced = self.sched.raise_hand(p.id, self.agenda_id, score, gist, self.now(), self.log.last_seq)
            if replaced:
                self.emit("HandWithdrawn", hand=replaced)
            if hand:
                self.emit("HandRaised", hand=hand)
                self._rule_hands[p.id] = hand.id
            if not score.addressed:           # 被点名的直接举手，不需要二次打分
                scored.append(BotCtx(p.id, p.display_name, score, gist))
        if self.scorer.async_updates and scored:
            recent = [(self.name(x.speaker_id), x.text) for x in self.utterances[-4:]
                      if x.agenda_item_id == u.agenda_item_id]
            self.scorer.submit(ScoreRequest(seq, u.agenda_item_id, u.text, u.speaker_id, scored, said, recent))

    async def _on_scorer_update(self, ev):
        """LLM 批量打分结果：丢弃过期的（已有更新的发言 / 议程项已换），否则重算 S，补举手、替换或撤回。"""
        ctx = self._scorer_ctx
        if (ctx is None or ev.get("based_on_seq") != ctx[0] or ev.get("agenda_item_id") != ctx[1]
                or ctx[1] != self.agenda_id or ctx[0] == self._scorer_applied
                or self.sm.phase != Phase.IN_AGENDA):
            return
        self._scorer_applied = ctx[0]
        _, agenda, text, speaker = ctx
        said = self._said_in_item(agenda)
        raised = False
        for pid, upd in (ev.get("scores") or {}).items():
            if (pid not in self.briefs or pid in (speaker, self.moderator_id) or not self.is_bot(pid)
                    or (self.token is not None and self.token.holder_id == pid)):
                continue
            old = next((h for h in self.sched.s.hands if h.id == self._rule_hands.get(pid)), None)
            if pid in self._rule_hands and old is None:
                continue                      # 规则分那次的手已被授予 / 过期：这个 bot 对这句话已经行动过了
            pen = cooldown_penalty(self.sched.s.last_spoke_end.get(pid), self.now(), self.rules)
            rule, rgist = self.scorer.score(pid, text, said, pen, agenda)
            if rule.addressed:
                continue
            score, gist = self.scorer.blend(rule, rgist, upd, pen)
            if old is not None:
                self.sched.withdraw(pid)      # 旧手按新分数重新裁决，避免去重分支保留更高的规则分
            hand, replaced = self.sched.raise_hand(pid, agenda, score, gist, self.now(), self.log.last_seq)
            gone = old or replaced
            if gone is not None:
                self.emit("HandWithdrawn", hand=gone)
            if hand:
                self.emit("HandRaised", hand=hand)
                self._rule_hands[pid] = hand.id
                raised = True
        if raised and self.token is None:
            await self.schedule()             # 话轮空闲时新举的手要立刻参与挑选

    def _mark_duck(self, tok: FloorToken, restored: bool = False) -> None:
        if restored and self._ducks:
            self._ducks[-1]["restored"] = True
        elif not restored:
            self._ducks.append({"at": self.now(), "restored": False})

    def _record_bot_partial(self, tok: FloorToken, played_chars: int) -> None:
        u = Utterance(id=f"utt-{next(self._utt_ids)}", speaker_id=tok.holder_id, agenda_item_id=self.agenda_id,
                      text=i18n_cut_off(getattr(self.m, "language", "zh"), played_chars), started_at=self._turn_started or self.now(),
                      ended_at=self.now(), token_id=tok.id,
                      interrupted={"by": self.human_id, "at_char": played_chars}, ducked=list(self._ducks))
        self.utterances.append(u)
        self.emit("UtteranceFinal", utterance=u)

    # ———————————————————— 实时运行（main.py 用） ————————————————————
    async def run(self, stop: asyncio.Event, *, start: bool = True) -> None:
        """单线程事件循环：语音层事件入队，按定时器最早到期时间等待。所有状态变更都只在这个协程里发生。"""
        q: asyncio.Queue = asyncio.Queue()
        self.inbox = q                      # main.py 往这里投 control.* 事件（网页按钮）
        unsub = self.voice.subscribe(q.put_nowait)
        try:
            if start:
                await self.start()
            while not stop.is_set():
                nxt = self.next_timer_at()
                timeout = None if nxt is None else max(0, nxt - self.now()) / 1000
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=timeout)
                    await self.handle(ev)
                except asyncio.TimeoutError:
                    pass
                await self.fire_due(self.now())
        finally:
            unsub()


_DIRECTIVE_CUES = ("先让", "让", "请")
_EN_DIRECTIVE_CUES = ("let ", "ask ", "have ")
_EN_DIRECTIVE_VERBS = ("speak", "talk", "say", "go ahead", "start", "weigh in")


def _alias_map(participants, moderator_id: str) -> dict[str, list[str]]:
    return {p.id: p.aliases + [p.display_name] for p in participants
            if p.kind == "bot" and p.id != moderator_id}


def parse_directive(text: str, aliases: dict[str, list[str]]) -> Optional[str]:
    """Spot "先让工程说说成本" / "let Eng speak about cost". Returns the target bot id."""
    zh = _parse_directive_zh(text, aliases)
    if zh:
        return zh
    return _parse_directive_en(text, aliases)


def _parse_directive_zh(text: str, aliases: dict[str, list[str]]) -> Optional[str]:
    for cue in _DIRECTIVE_CUES:
        i = text.find(cue)
        if i < 0:
            continue
        rest = text[i + len(cue):]
        if "说" not in rest and "讲" not in rest:
            continue
        best = None
        for pid, names in aliases.items():
            for n in names:
                j = rest.find(n)
                if n and j >= 0 and (best is None or j < best[0]):
                    best = (j, pid)
        if best:
            return best[1]
    return None


def _parse_directive_en(text: str, aliases: dict[str, list[str]]) -> Optional[str]:
    low = text.lower()
    for cue in _EN_DIRECTIVE_CUES:
        i = low.find(cue)
        if i < 0:
            continue
        rest_l = low[i + len(cue):]
        if not any(v in rest_l for v in _EN_DIRECTIVE_VERBS):
            continue
        best = None
        for pid, names in aliases.items():
            for n in names:
                if not n:
                    continue
                j = rest_l.find(n.lower())
                if j >= 0 and (best is None or j < best[0]):
                    best = (j, pid)
        if best:
            return best[1]
    return None


_AFFIRM = ("结束", "散会", "可以了", "就这样", "没有了", "没问题", "好的", "行")
_AFFIRM_EN = ("yes", "we're done", "we are done", "sounds good", "that's all", "that is all")
_NEGATE = ("不", "别", "还有", "等等", "等一下", "再")
_NEGATE_EN = ("don't", "do not", "not yet", "wait", "another")


def is_end_affirmation(text: str, lang: str = "zh") -> bool:
    """用户对「是否结束」的回答是否为肯定。规则版。"""
    if any(n in text for n in _NEGATE):
        return False
    if any(a in text for a in _AFFIRM):
        return True
    if lang == "en":
        low = text.lower()
        if any(n in low for n in _NEGATE_EN):
            return False
        return any(a in low for a in _AFFIRM_EN)
    return False


_END_CMD = ("结束吧", "散会", "结束会议", "会议结束", "今天就到这", "先到这", "就到这里")
_END_CMD_EN = ("end the meeting", "adjourn", "that's a wrap", "lets wrap up", "let's wrap up")


def is_end_command(text: str, lang: str = "zh") -> bool:
    """用户主动宣布结束（不必等主持人问）。带否定词不算。"""
    if any(n in text for n in ("不", "别", "没", "等等", "等一下")):
        return False
    if any(c in text for c in _END_CMD):
        return True
    if lang == "en":
        low = text.lower()
        return any(c in low for c in _END_CMD_EN)
    return False


def monotonic_ms() -> Millis:
    return int(time.monotonic() * 1000)
