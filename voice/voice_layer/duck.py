"""发送端软打断：在推出去的 TTS 音频帧上做增益（duck → stop / restore）。

挂点（以 livekit-agents 1.8.9 源码为准）：
  - Agent.tts_node 是官方可覆盖节点，输出 rtc.AudioFrame。这里包一层：先限速再乘增益。
  - 为什么要限速：RoomIO 的 _ParticipantAudioOutput.capture_frame 把帧推进一个无界队列
    （voice/room_io/_output.py `_audio_buf`），TTS 比实时快时整句音频会先全部入队，
    之后再改增益已经来不及。所以 tts_node 里按实时节奏放帧，只领先播放头 pace_lead_s，
    duck 生效延迟上限 ≈ pace_lead_s（+ AudioSource 内部已入队部分，同样 ≤ pace_lead_s）。
  - 真停止仍走框架：session.interrupt(force=True) 会清 AudioSource 队列（_wait_for_playout 里 clear_queue）。
    这里的 stop() 只是额外保险：立刻把增益置 0，并让限速器丢弃剩余帧（切换中的旧 TTS 也会被截断）。
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import AsyncIterable, AsyncIterator, Callable, Optional

import numpy as np
from livekit import rtc

from .config import DuckConfig


def db_to_gain(db: float) -> float:
    return float(10 ** (db / 20.0))


class GainController:
    """全局唯一（所有 bot 的 Agent 共用一个），因此无论当前是哪个 Agent 在播、是否在切换中，duck/stop 都生效。"""

    def __init__(self, cfg: DuckConfig, clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self._clock = clock
        self._current = 1.0
        self._target = 1.0
        self.stopped = False          # stop() 后为 True，直到下一次 begin_turn()
        self.ducked = False
        self.human_active = False     # 由 VAD 维护；新一轮 TTS 开始时如果人还在说，直接以 duck 增益起播
        self.generation = 0           # 每次 stop() +1，限速器据此丢弃旧帧
        self.log: list[tuple[float, str, float]] = []   # (monotonic, action, target_gain) 供打点

    # —— 控制面 ——
    def duck(self, gain_db: Optional[float] = None) -> None:
        if self.stopped:
            return
        self._target = db_to_gain(self.cfg.gain_db if gain_db is None else gain_db)
        self.ducked = True
        self.log.append((self._clock(), "duck", self._target))

    def restore(self) -> None:
        if self.stopped:
            return
        self._target = 1.0
        self.ducked = False
        self.log.append((self._clock(), "restore", 1.0))

    def stop(self) -> None:
        self._target = 0.0
        self._current = 0.0
        self.stopped = True
        self.ducked = False
        self.generation += 1
        self.log.append((self._clock(), "stop", 0.0))

    def begin_turn(self) -> None:
        """新一段 TTS 开始（tts_node 被调用）时调用。"""
        self.stopped = False
        self._target = db_to_gain(self.cfg.gain_db) if self.human_active else 1.0
        self.ducked = self.human_active
        self._current = self._target

    @property
    def target(self) -> float:
        return self._target

    @property
    def current(self) -> float:
        return self._current

    # —— 数据面 ——
    def apply(self, frame: rtc.AudioFrame) -> rtc.AudioFrame:
        """返回乘过增益的新帧；current→target 在 ramp_ms 内线性过渡。"""
        tgt, cur = self._target, self._current
        if cur == 1.0 and tgt == 1.0:
            return frame
        n = frame.samples_per_channel
        ch = frame.num_channels
        data = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32).reshape(n, ch)
        ramp_n = max(1, int(frame.sample_rate * self.cfg.ramp_ms / 1000.0))
        if cur != tgt:
            k = min(n, ramp_n)
            env = np.full(n, tgt, dtype=np.float32)
            env[:k] = np.linspace(cur, tgt, k, endpoint=True, dtype=np.float32)
            # 如果 ramp 比一帧长，按比例推进 current
            if ramp_n > n:
                cur_next = cur + (tgt - cur) * (n / ramp_n)
                env = np.linspace(cur, cur_next, n, dtype=np.float32)
                self._current = cur_next
            else:
                self._current = tgt
        else:
            env = np.full(n, tgt, dtype=np.float32)
        out = np.clip(data * env[:, None], -32768, 32767).astype(np.int16)
        return rtc.AudioFrame(out.tobytes(), frame.sample_rate, ch, n)


class Pacer:
    """把 TTS 帧按实时节奏放出去，最多领先 lead_s。支持 hold（框架 pause 输出时暂停计时）。"""

    def __init__(self, lead_s: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], "asyncio.Future"] = asyncio.sleep) -> None:
        self.lead_s = lead_s
        self._clock = clock
        self._sleep = sleep
        self._flowing = asyncio.Event()
        self._flowing.set()
        self._held_since: Optional[float] = None
        self._held_total = 0.0

    def hold(self) -> None:
        if self._flowing.is_set():
            self._flowing.clear()
            self._held_since = self._clock()

    def release(self) -> None:
        if not self._flowing.is_set():
            if self._held_since is not None:
                self._held_total += self._clock() - self._held_since
            self._held_since = None
            self._flowing.set()

    async def run(self, frames: AsyncIterable[rtc.AudioFrame], gain: GainController,
                  on_first: Optional[Callable[[], None]] = None) -> AsyncIterator[rtc.AudioFrame]:
        self.release()          # 新一段 TTS：清掉上一段遗留的 hold
        gain.begin_turn()
        gen = gain.generation
        t0: Optional[float] = None
        held_at_start = self._held_total
        played = 0.0
        async for f in frames:
            if t0 is None:
                t0 = self._clock()
                held_at_start = self._held_total
                if on_first:
                    on_first()
            while True:
                if gain.stopped or gain.generation != gen:
                    return   # 硬停止：剩余帧全部丢弃
                if not self._flowing.is_set():
                    await self._flowing.wait()
                    continue
                due = t0 + (self._held_total - held_at_start) + played - self.lead_s
                wait = due - self._clock()
                if wait <= 0:
                    break
                await self._sleep(min(wait, 0.02))
            played += f.samples_per_channel / f.sample_rate
            yield gain.apply(f)


class BargeInTracker:
    """VAD 事件 → duck / confirmed / rejected。不看会议状态（硬规则：人声任何时候都处理）。

    on_start(): 人开口（silero START_OF_SPEECH）=> 立刻 duck（auto_duck 时）
    on_speech_duration(d): VAD 推理持续上报语音时长，d ≥ min_duration => confirmed（只触发一次）
    on_end(): 人停了；如果没到 min_duration => rejected => restore
    """

    def __init__(self, min_duration: float, gain: GainController, auto_duck: bool = True,
                 on_confirmed: Optional[Callable[[], None]] = None,
                 on_rejected: Optional[Callable[[], None]] = None) -> None:
        self.min_duration = min_duration
        self.gain = gain
        self.auto_duck = auto_duck
        self.on_confirmed = on_confirmed
        self.on_rejected = on_rejected
        self.active = False
        self.confirmed = False

    def on_start(self) -> None:
        self.active = True
        self.confirmed = False
        self.gain.human_active = True
        if self.auto_duck:
            self.gain.duck()

    def on_speech_duration(self, speech_duration: float) -> None:
        if not self.active:
            # 某些情况下 START 事件可能晚于推理事件：补一次 start
            self.on_start()
        if not self.confirmed and speech_duration >= self.min_duration:
            self.confirmed = True
            if self.on_confirmed:
                self.on_confirmed()

    def on_end(self) -> None:
        was_confirmed = self.confirmed
        self.active = False
        self.confirmed = False
        self.gain.human_active = False
        if not was_confirmed:
            if self.auto_duck:
                self.gain.restore()
            if self.on_rejected:
                self.on_rejected()


def rms_dbfs(frame: rtc.AudioFrame) -> float:
    a = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32)
    if a.size == 0:
        return -120.0
    r = math.sqrt(float(np.mean(a * a))) / 32768.0
    return 20 * math.log10(max(r, 1e-6))
