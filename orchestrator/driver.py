"""确定性驱动器：把 FakeVoiceLayer 的到期事件与编排器定时器按时间顺序喂给编排器。

单测与 sim.py 共用。同一时刻：语音层事件先于编排器定时器。
"""
from __future__ import annotations

from typing import Optional

from .bots import load_example
from .fake_llm import FakeLLM
from .models import Millis
from .orchestrator import Orchestrator
from .principal import MockPrincipal
from .voice_iface import FakeClock, FakeVoiceLayer


class SimDriver:
    def __init__(self, *, human_id: str = "host", principal_delay_ms: Millis = 20_000,
                 principal_answers: Optional[dict] = None, never_answer: bool = False,
                 rules_override: Optional[dict] = None, moderator_mode: Optional[str] = None,
                 scenario: str = "example", **voice_kw):
        self.clock = FakeClock(0)
        meeting, briefs = load_example(human_id, moderator_mode, scenario)
        for k, v in (rules_override or {}).items():
            setattr(meeting.rules, k, v)
        self.llm = FakeLLM(meeting, briefs)
        self.voice = FakeVoiceLayer(self.clock, self.llm, **voice_kw)
        self.orch = Orchestrator(
            meeting, briefs, self.voice,
            principal_factory=lambda o: MockPrincipal(o.schedule_timer, delay_ms=principal_delay_ms,
                                                      answers=principal_answers, never_answer=never_answer),
            clock=self.clock,
        )
        self.human = human_id

    async def start(self):
        await self.orch.start()

    def say(self, at: Millis, text: str, dur_ms: Optional[Millis] = None, eot_delay: Millis = 400):
        """脚本化用户发言：vad.start → vad.end → asr.final → eot。"""
        dur = dur_ms if dur_ms is not None else max(600, len(text) * 200)
        h = self.human
        self.voice.inject({"type": "vad.start", "participant_id": h}, at=at)
        self.voice.inject({"type": "vad.end", "participant_id": h}, at=at + dur)
        self.voice.inject({"type": "asr.final", "participant_id": h, "text": text}, at=at + dur + 150)
        self.voice.inject({"type": "eot", "participant_id": h, "probability": 0.9}, at=at + dur + eot_delay)

    def blip(self, at: Millis, dur_ms: Millis = 120):
        """误触发：短促噪声 / “嗯”。"""
        self.voice.inject({"type": "vad.start", "participant_id": self.human}, at=at)
        self.voice.inject({"type": "vad.end", "participant_id": self.human}, at=at + dur_ms)

    async def run_until(self, t_end: Millis, max_steps: int = 100_000):
        for _ in range(max_steps):
            cands = [t for t in (self.voice.next_due_at(), self.orch.next_timer_at()) if t is not None]
            if not cands or min(cands) > t_end:
                self.clock.now = t_end
                return
            self.clock.now = max(self.clock.now, min(cands))
            for ev in self.voice.pop_due(self.clock.now):
                await self.orch.handle(ev)
            await self.orch.fire_due(self.clock.now)
        raise RuntimeError("步数超限，疑似死循环")
