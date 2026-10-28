"""真实语音栈适配：在语音队友的 LiveKitVoiceLayer（voice/voice_layer/livekit_layer.py）之上加编排器需要的「说什么」。

不改 voice/ 的文件，只继承并覆盖 speak()：
  - 结构化意图（ask_principal / commitments / provenance / confidence）由规则策略（FakeLLM，按 brief + mandate）决定，
    保证慢路径、承诺、主持流程可预期；
  - 成员 bot 的口语台词交给 DeepSeek 流式生成（thinking 关闭），按句切段（chunker）送 TTS，令牌失效即停；
  - 主持人固定话术、转述本体答案、「我问一下本体」不调 LLM（省调用、保证关键信息不走样）；
  - DeepSeek 不可用或报错时回退到策略台词。
事件顺序：bot.utterance 必须先于 tts.done（编排器在 tts.done 释放令牌后会丢弃迟到的 utterance）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from voice_layer.events import make_event  # type: ignore
from voice_layer.livekit_layer import LiveKitVoiceLayer  # type: ignore

from .chunker import pump_segments
from .i18n import style as i18n_style

log = logging.getLogger("live_voice")


class MeetingVoiceLayer(LiveKitVoiceLayer):
    def __init__(self, *a, planner: Callable[[str, Any], dict], chat=None, moderator_id: str = "",
                 max_tokens: int = 120, language: str = "zh", **kw):
        super().__init__(*a, llm_factory=lambda _pid: None, **kw)
        self.planner = planner
        self.chat = chat
        self.moderator_id = moderator_id
        self.max_tokens = max_tokens
        self.language = language or "zh"
        self.token_valid: Callable[[str], bool] = lambda _t: True
        self.llm_stats: list[dict] = []      # 每次 LLM 调用：ttft_ms / first_seg_ms / total_ms / ok

    def _use_llm(self, pid: str, plan: dict) -> bool:
        if self.chat is None or pid == self.moderator_id:
            return False
        if plan.get("ask_principal") or plan.get("provenance") == "principal":
            return False
        return True

    def _messages(self, ctx, plan: dict) -> list[dict]:
        msgs = ctx.to_messages()
        must = plan["text"]
        extra = f"{i18n_style(self.language)}\n这一轮你要表达的要点（可以换说法，但数字和结论不能改）：{must}"
        if self.language == "en":
            extra = f"{i18n_style(self.language)}\nSay this, in your own words, without changing numbers or conclusions: {must}"
        if plan.get("commitments"):
            if self.language == "en":
                extra += f"\nSay explicitly that you commit to: {plan['commitments'][0]}."
            else:
                extra += f"\n必须明确说你当场答应：{plan['commitments'][0]}。"
        msgs.append({"role": "user", "content": extra})
        return msgs

    async def speak(self, token_id: str, participant_id: str, ctx: Any) -> None:
        if self.current is None or self.current.bot_id != participant_id:
            await self.switch_agent(participant_id, ctx)
        plan = self.planner(participant_id, ctx)
        use_llm = self._use_llm(participant_id, plan)
        self._token, self._speaking_pid, self._played_s = token_id, participant_id, 0.0
        q: asyncio.Queue = asyncio.Queue()
        spoken: list[str] = []
        stat = {"pid": participant_id, "token_id": token_id, "llm": use_llm, "t0": time.perf_counter()}

        def send(seg) -> None:
            if not spoken:
                stat["first_seg_ms"] = (time.perf_counter() - stat["t0"]) * 1000
            spoken.append(seg.text)
            q.put_nowait(seg.text)
            self.bus.emit(make_event("tts.segment", participant_id, token_id=token_id, seq=seg.seq, text=seg.text))

        async def source():
            if not use_llm:
                yield plan["text"]
                return
            got = False
            try:
                async for piece in self.chat.astream_text(self._messages(ctx, plan), max_tokens=self.max_tokens,
                                                          thinking="disabled"):
                    if not got:
                        stat["ttft_ms"] = (time.perf_counter() - stat["t0"]) * 1000
                        got = True
                    yield piece
                stat["ok"] = got
            except Exception as e:  # noqa: BLE001
                stat["ok"] = False
                stat["error"] = type(e).__name__
                log.warning("DeepSeek 调用失败（%s），回退策略台词", type(e).__name__)
            if not got:
                yield plan["text"]

        async def producer():
            try:
                await pump_segments(source(), token_id, self.token_valid, send)
            except Exception:  # noqa: BLE001
                log.exception("producer")
            finally:
                q.put_nowait(None)

        async def tts_text():
            while True:
                t = await q.get()
                if t is None or not self.token_valid(token_id):
                    return
                yield t

        asyncio.create_task(producer())
        handle = self.session.say(tts_text(), add_to_chat_ctx=False)

        def _done(h) -> None:
            stat["total_ms"] = (time.perf_counter() - stat["t0"]) * 1000
            self.llm_stats.append({k: v for k, v in stat.items() if k != "t0"})
            text = "".join(spoken) or plan["text"]
            fields = {k: plan[k] for k in ("provenance", "confidence", "commitments", "addressees", "ask_principal")
                      if k in plan}
            self.bus.emit(make_event("bot.utterance", participant_id, token_id=token_id, text=text,
                                     interrupted=bool(h.interrupted), **fields))
            self.bus.emit(make_event("tts.done", participant_id, token_id=token_id, interrupted=bool(h.interrupted)))
            if self._token == token_id:
                self._speaking_pid = None

        handle.add_done_callback(_done)
