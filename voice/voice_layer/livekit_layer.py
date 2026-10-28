"""LiveKitVoiceLayer：orchestrator/voice_iface.VoiceLayer 的真实实现（livekit-agents 1.8.9）。

MVP 拓扑：一个 AgentSession，RoomIO 只链接用户（participant_identity），每个 bot 一个 MeetingAgent
（各自 tts 音色、llm），编排器发令牌 → switch_agent() → session.update_agent() → speak()。

硬规则「人声优先」在这里的落实：
  1. 独立的人声 VAD 通道（HumanVad）：单独订阅用户的音轨跑一路 Silero，和 AgentActivity 生命周期无关，
     所以在空闲、bot 在说、update_agent 切换中都照样产生 vad.start/vad.end，且不做任何状态过滤。
  2. 一个全局 GainController 被所有 Agent 的 tts_node 共用：duck/stop 对「当前在播的任何 TTS」生效，
     包括切换过程中上一个 Agent 的尾音。
  3. interrupt() = 增益置 0 + 限速器丢弃剩余帧 + session.interrupt(force=True) 清 AudioSource 队列。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, AsyncIterable, Callable, Optional

from livekit import rtc
from livekit.agents import Agent, AgentSession, llm as lk_llm, vad as lk_vad
from livekit.agents.llm.tool_context import StopResponse
from livekit.agents.voice import ModelSettings
from livekit.agents.voice.room_io import RoomOptions

from .asr import make_stt
from .config import VoiceConfig
from .duck import BargeInTracker, GainController, Pacer
from .levels import LEVEL_TOPIC, LevelMeter
from .events import EventBus, VoiceEvent, make_event, mono_ms
from .tts import make_tts

log = logging.getLogger("voice_layer")

# 接口以 orchestrator/voice_iface.py 为准；独立运行（无 orchestrator 包）时退化为本地抽象
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
try:
    from orchestrator.voice_iface import VoiceLayer as _VoiceLayerBase, SPEAKER_TOPIC  # type: ignore
except Exception:  # pragma: no cover
    import abc

    SPEAKER_TOPIC = "meeting.speaker"

    class _VoiceLayerBase(abc.ABC):  # type: ignore[no-redef]
        pass


def recipe_to_chat_ctx(ctx: Any) -> lk_llm.ChatContext:
    """ChatCtxRecipe（或已是 ChatContext / messages 列表）→ livekit ChatContext。"""
    if isinstance(ctx, lk_llm.ChatContext):
        return ctx
    msgs = ctx.to_messages() if hasattr(ctx, "to_messages") else (ctx or [])
    c = lk_llm.ChatContext.empty()
    for m in msgs:
        role = m.get("role", "user")
        c.add_message(role="system" if role == "system" else ("assistant" if role == "assistant" else "user"),
                      content=m.get("content", ""))
    return c


class MeetingAgent(Agent):
    """一个 bot 的语音分身。不自动接话（on_user_turn_completed 抛 StopResponse），发言权只来自编排器。"""

    def __init__(self, bot_id: str, layer: "LiveKitVoiceLayer", *, instructions: str,
                 tts, llm, chat_ctx: Optional[lk_llm.ChatContext] = None) -> None:
        super().__init__(instructions=instructions, tts=tts, llm=llm, chat_ctx=chat_ctx, id=bot_id)
        self.bot_id = bot_id
        self._layer = layer
        self.entered = asyncio.Event()

    async def on_enter(self) -> None:
        self.entered.set()
        self._layer._on_agent_enter(self)

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        self._layer._emit_eot(new_message)
        raise StopResponse()

    async def llm_node(self, chat_ctx, tools, model_settings):
        layer = self._layer
        layer._mark("llm_start")
        first = True
        async for chunk in Agent.default.llm_node(self, chat_ctx, tools, model_settings):
            if first:
                layer._mark("llm_first_token")
                first = False
            yield chunk

    async def tts_node(self, text: AsyncIterable[str], model_settings: ModelSettings) -> AsyncIterable[rtc.AudioFrame]:
        layer = self._layer
        src = Agent.default.tts_node(self, text, model_settings)
        async for f in layer.pacer.run(src, layer.gain, on_first=layer._on_first_frame):
            layer._played_s += f.samples_per_channel / f.sample_rate
            layer.levels.add(self.bot_id, f.data)
            yield f


class HumanVad:
    """独立人声 VAD：一路 Silero，帧来源可以是 LiveKit 音轨，也可以是离线 push_frame（测试/探针）。"""

    def __init__(self, vad: lk_vad.VAD, on_start: Callable[[float], None], on_duration: Callable[[float], None],
                 on_end: Callable[[float], None], on_frame: Optional[Callable[[rtc.AudioFrame], None]] = None) -> None:
        self._stream = vad.stream()
        self._on_frame = on_frame
        self._on_start, self._on_duration, self._on_end = on_start, on_duration, on_end
        self._task = asyncio.create_task(self._loop())
        self._track_task: Optional[asyncio.Task] = None

    def push_frame(self, frame: rtc.AudioFrame) -> None:
        self._stream.push_frame(frame)
        if self._on_frame:
            self._on_frame(frame)

    def attach_track(self, track: rtc.Track) -> None:
        async def pump() -> None:
            async for ev in rtc.AudioStream(track, sample_rate=16000, num_channels=1):
                self.push_frame(ev.frame)
        if self._track_task:
            self._track_task.cancel()
        self._track_task = asyncio.create_task(pump())

    async def _loop(self) -> None:
        async for ev in self._stream:
            t = mono_ms()
            if ev.type == lk_vad.VADEventType.START_OF_SPEECH:
                self._on_start(t)
            elif ev.type == lk_vad.VADEventType.INFERENCE_DONE and ev.speaking:
                self._on_duration(ev.speech_duration)
            elif ev.type == lk_vad.VADEventType.END_OF_SPEECH:
                self._on_end(t)

    async def aclose(self) -> None:
        if self._track_task:
            self._track_task.cancel()
        self._task.cancel()
        await self._stream.aclose()


def build_session(cfg: VoiceConfig, vad: lk_vad.VAD, *, stt=None) -> AgentSession:
    """推荐的 AgentSession 参数（打断参数全部来自 cfg.interruption）。"""
    ic = cfg.interruption
    return AgentSession(
        stt=stt if stt is not None else make_stt(cfg),
        vad=vad,
        turn_handling={
            "turn_detection": "vad",
            "endpointing": {"min_delay": 0.4, "max_delay": 2.5},
            "interruption": ({"enabled": True, "mode": ic.framework_mode, "min_duration": ic.min_duration,
                              "false_interruption_timeout": ic.false_interruption_timeout,
                              "resume_false_interruption": ic.resume_false_interruption,
                              "discard_audio_if_uninterruptible": ic.discard_audio_if_uninterruptible}
                             if ic.framework_enabled else
                             {"enabled": False, "discard_audio_if_uninterruptible": ic.discard_audio_if_uninterruptible}),
            "preemptive_generation": {"enabled": False},
        },
        user_away_timeout=None,
    )


def room_options(cfg: VoiceConfig) -> RoomOptions:
    return RoomOptions(participant_identity=cfg.human_identity, close_on_disconnect=False)


class LiveKitVoiceLayer(_VoiceLayerBase):
    def __init__(self, cfg: VoiceConfig, session: AgentSession, *, vad: lk_vad.VAD,
                 llm_factory: Callable[[str], Any], instructions: Optional[dict[str, str]] = None,
                 room: Optional[rtc.Room] = None, human_id: Optional[str] = None) -> None:
        self.cfg = cfg
        self.session = session
        self.room = room
        self.human_id = human_id or cfg.human_identity
        self.bus = EventBus()
        self.gain = GainController(cfg.duck)
        self.pacer = Pacer(cfg.duck.pace_lead_s)
        self.tracker = BargeInTracker(cfg.interruption.min_duration, self.gain, auto_duck=cfg.interruption.auto_duck,
                                      on_confirmed=self._on_bargein_confirmed, on_rejected=self._on_bargein_rejected)
        self._llm_factory = llm_factory
        self._instructions = instructions or {}
        self._tts_cache: dict[str, Any] = {}
        self._llm_cache: dict[str, Any] = {}
        self.current: Optional[MeetingAgent] = None
        self._token: Optional[str] = None
        self._speaking_pid: Optional[str] = None
        self._played_s = 0.0
        self._first_frame_cb: Optional[Callable[[], None]] = None
        self.levels = LevelMeter()
        self.human_vad = HumanVad(vad, self._vad_start, self.tracker.on_speech_duration, self._vad_end,
                                  on_frame=lambda fr: self.levels.add(self.human_id, fr.data))
        self._level_task: Optional[asyncio.Task] = asyncio.create_task(self._level_loop()) if room is not None else None
        self.timings: list[dict[str, Any]] = []
        self.marks: list[dict[str, Any]] = []
        session.on("user_input_transcribed", self._on_transcribed)
        session.on("agent_state_changed", self._on_agent_state)
        if room is not None:
            room.on("track_subscribed", self._on_track_subscribed)
            for p in room.remote_participants.values():
                if p.identity == cfg.human_identity:
                    for pub in p.track_publications.values():
                        if pub.track is not None and pub.kind == rtc.TrackKind.KIND_AUDIO:
                            self.human_vad.attach_track(pub.track)

    # —— VoiceLayer 接口 ——
    def subscribe(self, cb: Callable[[VoiceEvent], None]) -> Callable[[], None]:
        return self.bus.subscribe(cb)

    def agent_for(self, pid: str, ctx: Any = None) -> MeetingAgent:
        if pid not in self._tts_cache:
            self._tts_cache[pid] = make_tts(self.cfg, pid)
            try:
                self._tts_cache[pid].prewarm()
            except Exception:
                pass
        if pid not in self._llm_cache:
            self._llm_cache[pid] = self._llm_factory(pid)
        system = getattr(ctx, "system", None) or self._instructions.get(pid) or f"你是会议中的 {pid}，用简短口语中文发言。"
        return MeetingAgent(pid, self, instructions=system, tts=self._tts_cache[pid], llm=self._llm_cache[pid],
                            chat_ctx=recipe_to_chat_ctx(ctx) if ctx is not None else None)

    async def switch_agent(self, participant_id: str, ctx: Any) -> dict[str, float]:
        t0 = time.perf_counter()
        if self.session.agent_state == "speaking" or self._speaking_pid is not None:
            self.interrupt()   # 切换前先截断旧 TTS（设计页风险表「切换时旧 TTS 未截断」）
        agent = self.agent_for(participant_id, ctx)
        self.session.update_agent(agent)
        try:
            await asyncio.wait_for(agent.entered.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("update_agent: on_enter 5s 未触发")
        ms = (time.perf_counter() - t0) * 1000
        self.timings.append({"kind": "switch", "pid": participant_id, "ms": ms})
        return {"switch_ms": ms}

    async def speak(self, token_id: str, participant_id: str, ctx: Any) -> None:
        if self.current is None or self.current.bot_id != participant_id:
            await self.switch_agent(participant_id, ctx)
        self._token = token_id
        self._speaking_pid = participant_id
        self._played_s = 0.0
        instr = getattr(ctx, "turn_instruction", None) or None
        handle = self.session.generate_reply(instructions=instr) if instr else self.session.generate_reply()

        def _done(h) -> None:
            text = ""
            try:
                for it in h.chat_items:
                    if getattr(it, "role", None) == "assistant":
                        text = it.text_content or ""
            except Exception:
                pass
            self.bus.emit(make_event("bot.utterance", participant_id, token_id=token_id, text=text,
                                     interrupted=bool(h.interrupted)))
            self.bus.emit(make_event("tts.done", participant_id, token_id=token_id, interrupted=bool(h.interrupted)))
            if self._token == token_id:
                self._speaking_pid = None

        handle.add_done_callback(_done)

    def say(self, token_id: str, participant_id: str, text: str):
        """不经 LLM 直接念一段文本（探针 / 主持人固定话术用）。"""
        self._token = token_id
        self._speaking_pid = participant_id
        self._played_s = 0.0
        h = self.session.say(text)

        def _done(hh) -> None:
            self.bus.emit(make_event("tts.done", participant_id, token_id=token_id, interrupted=bool(hh.interrupted)))
            if self._token == token_id:
                self._speaking_pid = None

        h.add_done_callback(_done)
        return h

    def duck(self, gain_db: float) -> None:
        self.gain.duck(gain_db)

    def restore(self) -> None:
        self.gain.restore()

    def interrupt(self) -> dict[str, int]:
        self.gain.stop()
        try:
            self.session.interrupt(force=True)
        except Exception as e:  # 切换窗口里 activity 可能为 None
            log.debug("session.interrupt: %s", e)
        # 中文口语约 4.5 字/秒：用已放出的音频时长估算已播字数（精确值以 bot.utterance 的截断文本为准）
        return {"played_chars": int(self._played_s * 4.5)}

    async def publish_data(self, topic: str, payload: dict[str, Any]) -> None:
        if self.room is None:
            return
        await self.room.local_participant.publish_data(json.dumps(payload, ensure_ascii=False).encode(),
                                                       reliable=True, topic=topic)

    async def publish_current_speaker(self, speaker_id: str, display_name: str = "", state: str = "speaking") -> None:
        await self.publish_data(SPEAKER_TOPIC, {"speaker_id": speaker_id, "display_name": display_name or speaker_id,
                                                "state": state})

    def on_first_audio(self, cb):
        return self.subscribe(lambda e: cb(e) if e.get("type") == "tts.first_audio" else None)

    def on_done(self, cb):
        return self.subscribe(lambda e: cb(e) if e.get("type") == "tts.done" else None)

    # —— 离线/测试入口 ——
    def push_human_audio(self, frame: rtc.AudioFrame) -> None:
        self.human_vad.push_frame(frame)

    async def _level_loop(self) -> None:
        """~10Hz 把各参会者电平和开口/闭口推到 meeting.level；电平走 lossy，开口/闭口走 reliable。"""
        while True:
            await asyncio.sleep(0.1)
            for m in self.levels.tick():
                try:
                    await self.room.local_participant.publish_data(
                        json.dumps(m, ensure_ascii=False).encode(), reliable=m["type"] != "level", topic=LEVEL_TOPIC)
                except Exception as e:
                    log.debug("level publish: %s", e)

    async def aclose(self) -> None:
        if self._level_task:
            self._level_task.cancel()
        await self.human_vad.aclose()
        for t in list(self._tts_cache.values()):
            try:
                await t.aclose()
            except Exception:
                pass
        self._tts_cache.clear()

    # —— 内部回调 ——
    def _on_track_subscribed(self, track, pub, participant) -> None:
        if participant.identity == self.cfg.human_identity and track.kind == rtc.TrackKind.KIND_AUDIO:
            self.human_vad.attach_track(track)

    def _vad_start(self, t: float) -> None:
        # 不看任何状态：先 duck（若在播）再上报
        self.tracker.on_start()
        self.bus.emit(make_event("vad.start", self.human_id, at=t))

    def _vad_end(self, t: float) -> None:
        self.tracker.on_end()
        self.bus.emit(make_event("vad.end", self.human_id, at=t))

    def _on_bargein_confirmed(self) -> None:
        if self.cfg.interruption.auto_stop:
            self.interrupt()
        self.bus.emit(make_event("bargein.confirmed", self.human_id, token_id=self._token))

    def _on_bargein_rejected(self) -> None:
        self.bus.emit(make_event("bargein.rejected", self.human_id, token_id=self._token))

    def _on_transcribed(self, ev) -> None:
        self.bus.emit(make_event("asr.final" if ev.is_final else "asr.partial", self.human_id,
                                 text=ev.transcript, lang=str(ev.language) if getattr(ev, "language", None) else None))

    def _emit_eot(self, msg) -> None:
        self.bus.emit(make_event("eot", self.human_id, probability=1.0, text=getattr(msg, "text_content", None)))

    def _on_agent_state(self, ev) -> None:
        if ev.new_state == "speaking":
            self.pacer.release()
            if self._speaking_pid:
                self.bus.emit(make_event("tts.first_audio", self._speaking_pid, token_id=self._token))
        elif ev.old_state == "speaking" and self._speaking_pid is not None:
            # 框架 pause（假打断）时暂停限速时钟，避免恢复后积压
            self.pacer.hold()
            asyncio.get_event_loop().call_later(0.05, self._maybe_release)

    def _maybe_release(self) -> None:
        if self._speaking_pid is None:
            self.pacer.release()

    def _mark(self, name: str) -> None:
        """打点（monotonic 毫秒），延迟探针读取 layer.marks。"""
        self.marks.append({"name": name, "at": mono_ms(), "token_id": self._token, "pid": self._speaking_pid})

    def _on_first_frame(self) -> None:
        self._mark("tts_first_frame")
        if self._first_frame_cb:
            self._first_frame_cb()

    def _on_agent_enter(self, agent: MeetingAgent) -> None:
        self.current = agent
