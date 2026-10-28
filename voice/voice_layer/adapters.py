"""给 main.py（编排器入口）用的兼容工厂：

    from voice import adapters        # voice/adapters.py 转发到这里
    stt = adapters.make_stt()          # livekit stt.STT（火山流式 ASR；缺凭证回退本地 sherpa）
    tts = adapters.make_tts("voice_pm_female_warm")   # livekit tts.TTS

voice_name 可以是：
  - meeting_example.json 里 agent_profile.tts_voice 的别名（voice_moderator_neutral / voice_pm_female_warm / voice_eng_male_calm）
  - VoiceConfig.bots 里的 bot_id（moderator / pm_bot / eng_bot / bot_c）
  - 其他字符串：按名字稳定分配一个本地音色（火山无对应音色时回退 local）
配置来自 VoiceConfig（可用 VOICE_CONFIG=xx.json 覆盖），provider 由 VOICE_ASR_PROVIDER / VOICE_TTS_PROVIDER 或凭证自动决定。
不读取、不打印任何 key 值。
"""
from __future__ import annotations

from typing import Optional

from .config import VOICE_ALIASES, VoiceConfig, load_config

_cfg: Optional[VoiceConfig] = None


def config() -> VoiceConfig:
    global _cfg
    if _cfg is None:
        _cfg = load_config()
    return _cfg


def resolve_bot_id(voice_name: str) -> str:
    return VOICE_ALIASES.get(voice_name, voice_name)


def make_stt(provider: Optional[str] = None):
    from .asr import make_stt as _make
    return _make(config(), provider)


def make_tts(voice_name: str, provider: Optional[str] = None):
    from .tts import make_tts as _make
    return _make(config(), resolve_bot_id(voice_name), provider)


def make_vad():
    from livekit.plugins import silero
    v = config().vad
    return silero.VAD.load(min_speech_duration=v.min_speech_duration, min_silence_duration=v.min_silence_duration,
                           activation_threshold=v.activation_threshold)
