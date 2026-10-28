"""语音层抽象接口（对应设计页 VoiceLayer）。voice/ 的真实实现以此为准。

约定：
- 语音层把实时事件以 dict 推给 subscribe 的回调，type 取值见 VOICE_EVENT_TYPES。
- 所有 bot 侧输出事件（tts.first_audio / bot.utterance / tts.done）必须带 token_id，
  编排器据此丢弃过期令牌的输出。
- 时间 at 一律毫秒，由语音层的时钟给出（真实实现用 time.monotonic()*1000）。
- 原型补充了 speak()（设计页未列）：令牌授予后让当前 Agent 用最新 chat_ctx 生成并播放。
"""
from __future__ import annotations

import abc
import heapq
import itertools
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .chunker import SentenceChunker
from .models import ID, ChatCtxRecipe, Millis

VOICE_EVENT_TYPES = {
    # 人类侧（只来自 RoomIO 唯一链接的用户）
    "vad.start", "vad.end", "asr.partial", "asr.final", "eot",
    # 打断判定（语音层也可以自己判定后上报；编排器默认自己用定时器判定）
    "bargein.confirmed", "bargein.rejected",
    # bot 侧
    "tts.first_audio", "tts.segment", "bot.utterance", "tts.done",
}

VoiceEvent = dict[str, Any]

SPEAKER_TOPIC = "meeting.speaker"
ROSTER_TOPIC = "meeting.roster"


class VoiceLayer(abc.ABC):
    @abc.abstractmethod
    def subscribe(self, cb: Callable[[VoiceEvent], None]) -> Callable[[], None]: ...

    @abc.abstractmethod
    async def switch_agent(self, participant_id: ID, ctx: ChatCtxRecipe) -> dict[str, float]:
        """封装 session.update_agent(agents[pid]) 并注入 chat_ctx；返回 {"switch_ms": 实测耗时}。"""

    @abc.abstractmethod
    async def speak(self, token_id: ID, participant_id: ID, ctx: ChatCtxRecipe) -> None:
        """令牌授予后开始生成与播放；之后经回调上报 tts.first_audio / bot.utterance / tts.done（带 token_id）。"""

    @abc.abstractmethod
    def duck(self, gain_db: float) -> None:
        """软打断：在发送端立即把 TTS 增益压到 gain_db（不能放到浏览器做，否则多一个下行延迟）。"""

    @abc.abstractmethod
    def restore(self) -> None:
        """误触发：恢复音量继续播。"""

    @abc.abstractmethod
    def interrupt(self) -> dict[str, int]:
        """真打断：停止 TTS，返回 {"played_chars": 已播字符数}。"""

    @abc.abstractmethod
    async def publish_data(self, topic: str, payload: dict[str, Any]) -> None:
        """房间数据消息，reliable 通道。topic 见 SPEAKER_TOPIC / ROSTER_TOPIC。"""

    async def publish_current_speaker(self, speaker_id: ID, display_name: str, state: str) -> None:
        await self.publish_data(SPEAKER_TOPIC, {"speaker_id": speaker_id, "display_name": display_name, "state": state})

    async def publish_roster(self, participants: list[dict[str, str]]) -> None:
        await self.publish_data(ROSTER_TOPIC, {"participants": participants})

    # 设计页里的 onFirstAudio / onDone：这里统一走 subscribe 回调里的 tts.first_audio / tts.done 事件；
    # 保留两个便捷方法以对齐接口名。
    def on_first_audio(self, cb: Callable[[VoiceEvent], None]) -> Callable[[], None]:
        return self.subscribe(lambda e: cb(e) if e.get("type") == "tts.first_audio" else None)

    def on_done(self, cb: Callable[[VoiceEvent], None]) -> Callable[[], None]:
        return self.subscribe(lambda e: cb(e) if e.get("type") == "tts.done" else None)


# ———————————————————— 测试 / 模拟用实现 ————————————————————

class FakeClock:
    def __init__(self, start: Millis = 0):
        self.now: Millis = start

    def __call__(self) -> Millis:
        return self.now


@dataclass(order=True)
class _Scheduled:
    at: Millis
    seq: int
    event: VoiceEvent = field(compare=False)


class FakeVoiceLayer(VoiceLayer):
    """纯内存语音层：时间与事件都可注入，便于确定性测试。

    - inject(ev, at=)：注入人类侧事件（vad/asr/eot）。
    - speak() 调用注入的 generator（fake LLM）得到台词，再按 chars_per_sec 排定
      tts.first_audio / bot.utterance / tts.done。
    - pop_due(now) 由驱动方取出到期事件喂给编排器。
    """

    def __init__(self, clock: FakeClock, generator: Callable[[ID, ChatCtxRecipe], dict[str, Any]],
                 *, switch_ms: float = 30.0, first_audio_ms: Millis = 600, chars_per_sec: float = 5.0,
                 segments: bool = True):
        self.clock = clock
        self.generator = generator
        self.switch_ms = switch_ms
        self.first_audio_ms = first_audio_ms
        self.chars_per_sec = chars_per_sec
        self.segments = segments
        self._subs: list[Callable[[VoiceEvent], None]] = []
        self._q: list[_Scheduled] = []
        self._seq = itertools.count()
        self.calls: list[tuple] = []                 # 审计：所有接口调用
        self.published: list[tuple[str, dict]] = []  # 数据消息
        self.gain_db = 0.0
        self.current_agent: Optional[ID] = None
        self._playing: Optional[dict[str, Any]] = None  # {token_id, text, started_at}

    def subscribe(self, cb):
        self._subs.append(cb)
        return lambda: self._subs.remove(cb)

    def inject(self, ev: VoiceEvent, at: Optional[Millis] = None) -> None:
        ev = dict(ev)
        ev.setdefault("at", self.clock() if at is None else at)
        heapq.heappush(self._q, _Scheduled(ev["at"], next(self._seq), ev))

    def next_due_at(self) -> Optional[Millis]:
        return self._q[0].at if self._q else None

    def pop_due(self, now: Millis) -> list[VoiceEvent]:
        out = []
        while self._q and self._q[0].at <= now:
            out.append(heapq.heappop(self._q).event)
        return out

    async def switch_agent(self, participant_id, ctx):
        self.calls.append(("switch_agent", participant_id, len(ctx.shared_minutes)))
        self.current_agent = participant_id
        self.last_ctx = ctx
        return {"switch_ms": self.switch_ms}

    async def speak(self, token_id, participant_id, ctx):
        self.calls.append(("speak", token_id, participant_id))
        out = self.generator(participant_id, ctx)
        text = out["text"]
        now = self.clock()
        t_first = now + self.first_audio_ms
        dur = int(len(text) / self.chars_per_sec * 1000)
        self._playing = {"token_id": token_id, "text": text, "started_at": t_first, "pid": participant_id}
        self.inject({"type": "tts.first_audio", "token_id": token_id, "participant_id": participant_id}, at=t_first)
        if self.segments:                      # 按句切段送 TTS，每段带 token_id
            ch = SentenceChunker()
            segs = ch.feed(text) + ch.flush()
            t = t_first
            for i, seg in enumerate(segs):
                self.inject({"type": "tts.segment", "token_id": token_id, "participant_id": participant_id,
                             "seq": i, "text": seg}, at=t)
                t += int(len(seg) / self.chars_per_sec * 1000)
        self.inject({"type": "bot.utterance", "token_id": token_id, "participant_id": participant_id, **out}, at=t_first + dur)
        self.inject({"type": "tts.done", "token_id": token_id, "participant_id": participant_id}, at=t_first + dur)

    def duck(self, gain_db):
        self.calls.append(("duck", gain_db))
        self.gain_db = gain_db

    def restore(self):
        self.calls.append(("restore",))
        self.gain_db = 0.0

    def interrupt(self):
        self.calls.append(("interrupt",))
        played = 0
        if self._playing:
            tok = self._playing["token_id"]
            elapsed = max(0, self.clock() - self._playing["started_at"])
            played = min(len(self._playing["text"]), int(elapsed / 1000 * self.chars_per_sec))
            # 已排定的该令牌后续输出不再产生（真实实现：TTS 已停）
            self._q = [s for s in self._q if s.event.get("token_id") != tok]
            heapq.heapify(self._q)
            self._playing = None
        self.gain_db = 0.0
        return {"played_chars": played}

    async def publish_data(self, topic, payload):
        self.published.append((topic, dict(payload)))
