"""阿里百炼 cosyvoice-v3-flash 适配器骨架（可选，用户已定首选火山）。

环境变量 DASHSCOPE_API_KEY。文档：https://help.aliyun.com/zh/model-studio/cosyvoice-websocket-api
未实现：构造即可（便于配置校验），实际合成抛 NotImplementedError；工厂在缺 key 时根本不会选到这里。
"""
from __future__ import annotations

from livekit.agents import tts as lk_tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions


class AliyunCosyVoiceTTS(lk_tts.TTS):
    def __init__(self, *, voice: str, sample_rate: int = 24000, model: str = "cosyvoice-v3-flash") -> None:
        super().__init__(capabilities=lk_tts.TTSCapabilities(streaming=False), sample_rate=sample_rate, num_channels=1)
        self.voice, self._model = voice, model

    @property
    def provider(self) -> str:
        return "aliyun-dashscope"

    def synthesize(self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS):
        raise NotImplementedError("阿里 cosyvoice 适配器只留了骨架；MVP 用火山豆包 TTS（VOICE_TTS_PROVIDER=volc）")
