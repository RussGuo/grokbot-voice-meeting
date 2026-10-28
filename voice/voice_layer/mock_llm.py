"""无 key 的脚本化 LLM（livekit llm.LLM 接口），用于延迟探针和端到端自测。
ttft_s 模拟首 token 延迟；按 token_interval_s 逐字吐出 reply。
"""
from __future__ import annotations

import asyncio
import uuid

from livekit.agents import llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions


class ScriptedLLM(llm.LLM):
    def __init__(self, reply: str = "好的，我这边补充一点：原型已经能按令牌轮流发言了。", *,
                 ttft_s: float = 0.0, token_interval_s: float = 0.02, chars_per_token: int = 2) -> None:
        super().__init__()
        self.reply, self.ttft_s, self.token_interval_s, self.cpt = reply, ttft_s, token_interval_s, chars_per_token

    @property
    def model(self) -> str:
        return "scripted"

    @property
    def provider(self) -> str:
        return "mock"

    def chat(self, *, chat_ctx, tools=None, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
             parallel_tool_calls=NOT_GIVEN, tool_choice=NOT_GIVEN, extra_kwargs=NOT_GIVEN) -> "ScriptedStream":
        return ScriptedStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


class ScriptedStream(llm.LLMStream):
    async def _run(self) -> None:
        l: ScriptedLLM = self._llm  # type: ignore[assignment]
        rid = str(uuid.uuid4())
        await asyncio.sleep(l.ttft_s)
        for i in range(0, len(l.reply), l.cpt):
            self._event_ch.send_nowait(llm.ChatChunk(id=rid, delta=llm.ChoiceDelta(role="assistant", content=l.reply[i:i + l.cpt])))
            await asyncio.sleep(l.token_interval_s)
