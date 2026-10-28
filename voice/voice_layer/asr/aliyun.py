"""阿里 Paraformer 实时识别适配器骨架（可选）。DASHSCOPE_API_KEY。
文档：https://help.aliyun.com/zh/model-studio/websocket-for-paraformer-real-time-service
"""
from __future__ import annotations

from livekit.agents import stt as lk_stt
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN


class AliyunParaformerSTT(lk_stt.STT):
    def __init__(self, model: str = "paraformer-realtime-v2") -> None:
        super().__init__(capabilities=lk_stt.STTCapabilities(streaming=True, interim_results=True, offline_recognize=False))
        self._model = model

    async def _recognize_impl(self, buffer, *, language=NOT_GIVEN, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        raise NotImplementedError

    def stream(self, **kw):
        raise NotImplementedError("阿里 Paraformer 只留了骨架；MVP 用火山（VOICE_ASR_PROVIDER=volc）")
