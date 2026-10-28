"""TTS 适配层工厂：每个 bot 一个 livekit `tts.TTS` 实例（provider + voice_id 来自 VoiceConfig.bots）。

provider:
  volc   豆包语音合成 2.0 双向流式 WebSocket（首选，需 VOLC_API_KEY 或 VOLC_APP_ID+VOLC_ACCESS_TOKEN）
  aliyun 阿里 cosyvoice-v3-flash（可选骨架，需 DASHSCOPE_API_KEY，未实现）
  local  sherpa-onnx + Kokoro-82M v1.1-zh（Apache-2.0，CPU 本地，无 key）
缺凭证时 resolve_tts_provider() 自动回退 local。
"""
from __future__ import annotations

from typing import Optional

from livekit.agents import tts as lk_tts

from ..config import VoiceConfig


def make_tts(cfg: VoiceConfig, bot_id: str, provider: Optional[str] = None) -> lk_tts.TTS:
    v = cfg.bot_voice(bot_id)
    p = provider or cfg.resolve_tts_provider(bot_id)
    if p == "volc":
        from .volc import VolcTTS
        return VolcTTS(speaker=v.volc_speaker, resource_id=cfg.volc_tts_resource_id,
                       sample_rate=cfg.tts_sample_rate, speed=v.speed)
    if p == "aliyun":
        from .aliyun import AliyunCosyVoiceTTS
        return AliyunCosyVoiceTTS(voice=v.aliyun_voice, sample_rate=cfg.tts_sample_rate)
    if p == "openai":
        from providers.openai_speech import make_openai_tts
        return make_openai_tts(cfg, v)
    from .local_kokoro import KokoroTTS
    return KokoroTTS(models_dir=cfg.models_dir, sid=v.local_sid, speed=v.speed)
