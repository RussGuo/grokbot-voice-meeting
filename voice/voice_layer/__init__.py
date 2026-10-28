"""voice/ 语音层：ASR / TTS 适配、Silero VAD、发送端 duck、打断参数、LiveKit 单 session 封装。

对 orchestrator 暴露的入口：
    from voice_layer.livekit_layer import LiveKitVoiceLayer, build_session
"""
from .config import VoiceConfig, BotVoice, InterruptionConfig, DuckConfig, load_config
from .events import EventBus, make_event, VOICE_EVENT_TYPES

__all__ = [
    "VoiceConfig", "BotVoice", "InterruptionConfig", "DuckConfig", "load_config",
    "EventBus", "make_event", "VOICE_EVENT_TYPES",
]
