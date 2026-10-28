"""真实语音栈适配：在语音队友的 LiveKitVoiceLayer（voice/voice_layer/livekit_layer.py）之上加编排器需要的「说什么」。

不改 voice/ 的文件，只继承并覆盖 speak()：
  - 「说什么 + 什么意图」交给 Planner（orchestrator/planner.py）：真实 LLM 流式输出「JSON 头 + 口语正文」，
    头里的 act / commit / confidence / provenance / addressees 经确定性护栏校验后成为 bot.utterance 的字段，
    头不会被念出来；mustAsk 预检、relay 转述、主持人固定话术不经 LLM；任何失败回退 FakeLLM 规划；
  - planner 参数既可以是 Planner（有 async plan），也可以是旧的同步可调用对象（如 FakeLLM，会被包成静态规划）；
  - 正文按句切段（chunker）送 TTS，令牌失效即停并关掉上游 LLM 流；
  - 事件顺序：bot.utterance 必须先于 tts.done（编排器在 tts.done 释放令牌后会丢弃迟到的 utterance）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from voice_layer.events import make_event  # type: ignore
from voice_layer.livekit_layer import LiveKitVoiceLayer  # type: ignore

from .chunker import pump_segments
from .planner import FIELD_KEYS, PlanStream, Planner, as_planner, safe_ask_line, static_stream

log = logging.getLogger("live_voice")


class MeetingVoiceLayer(LiveKitVoiceLayer):
    def __init__(self, *a, planner: Any, chat=None, moderator_id: str = "",
                 max_tokens: int = 120, language: str = "zh", **kw):
        super().__init__(*a, llm_factory=lambda _pid: None, **kw)
        self.planner = planner
        self._planner: Planner = as_planner(planner)
        self.chat = chat                     # 保留给调用方；LLM 调用由 planner 持有的 chat 完成
        self.moderator_id = moderator_id
        self.max_tokens = max_tokens
        self.language = language or "zh"
        self.token_valid: Callable[[str], bool] = lambda _t: True
        # 每轮一条：pid / llm / header_ms / first_seg_ms / total_ms / ok / fallback / guard ...
        self.llm_stats: list[dict] = []
        self._bg: set[asyncio.Task] = set()  # 持有后台 task 的引用，避免被 GC 提前回收

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    async def _plan(self, participant_id: str, ctx: Any) -> PlanStream:
        try:
            return await self._planner.plan(participant_id, ctx)
        except Exception as e:  # noqa: BLE001  规划器本身不该抛；抛了就说安全话术并转问本体，别让话轮卡死
            log.exception("planner 异常")
            meta = getattr(ctx, "turn_meta", {}) or {}
            q = meta.get("directive_text") or meta.get("last_text") or ""
            plan = {"text": safe_ask_line(self.language), "provenance": "model", "confidence": 0.3,
                    "commitments": [], "addressees": []}
            if q and participant_id != self.moderator_id:
                plan["ask_principal"] = q
            return static_stream(plan, {"path": "planner_error", "fallback": f"planner:{type(e).__name__}"})

    async def speak(self, token_id: str, participant_id: str, ctx: Any) -> None:
        if self.current is None or self.current.bot_id != participant_id:
            await self.switch_agent(participant_id, ctx)
        t0 = time.perf_counter()
        ps = await self._plan(participant_id, ctx)
        self._token, self._speaking_pid, self._played_s = token_id, participant_id, 0.0
        q: asyncio.Queue = asyncio.Queue()
        spoken: list[str] = []
        stat: dict[str, Any] = {"pid": participant_id, "token_id": token_id}
        finished = False          # 语音已结束（含被打断）：之后一段都不再发

        def still_valid(t: str) -> bool:
            # 只靠 task.cancel() 不够：3.11 的 wait_for 可能吞掉取消，上游还会再吐几块
            return not finished and self.token_valid(t)

        def send(seg) -> None:
            if not spoken:
                stat["first_seg_ms"] = (time.perf_counter() - t0) * 1000
            spoken.append(seg.text)
            q.put_nowait(seg.text)
            self.bus.emit(make_event("tts.segment", participant_id, token_id=token_id, seq=seg.seq, text=seg.text))

        async def producer():
            try:
                await pump_segments(ps, token_id, still_valid, send)
            except Exception:  # noqa: BLE001
                log.exception("producer")
            finally:
                await ps.aclose()                  # 令牌失效 / 出错时关掉上游 LLM 流
                q.put_nowait(None)

        async def tts_text():
            while True:
                t = await q.get()
                if t is None or not self.token_valid(token_id):
                    return
                yield t

        task = self._spawn(producer())
        handle = self.session.say(tts_text(), add_to_chat_ctx=False)

        def _done(h) -> None:
            nonlocal finished
            finished = True
            if not task.done():                    # 被打断：语音先结束，别让上游继续烧 token
                task.cancel()
            stat["total_ms"] = (time.perf_counter() - t0) * 1000
            st = ps.stats
            stat.update(llm=bool(st.get("llm")), path=st.get("path"), ok=not st.get("fallback"))
            for k in ("header_ms", "fallback", "guard", "act", "truncated", "inline", "inline_wait_ms"):
                if k in st:
                    stat[k] = st[k]
            if "header_ms" in st:
                stat["ttft_ms"] = st["header_ms"]  # 兼容旧字段名
            self.llm_stats.append(stat)
            text = "".join(spoken) or ps.planned_text
            fields = {k: ps.fields[k] for k in FIELD_KEYS if k in ps.fields}
            self.bus.emit(make_event("bot.utterance", participant_id, token_id=token_id, text=text,
                                     interrupted=bool(h.interrupted), **fields))
            self.bus.emit(make_event("tts.done", participant_id, token_id=token_id, interrupted=bool(h.interrupted)))
            if self._token == token_id:
                self._speaking_pid = None

        handle.add_done_callback(_done)
