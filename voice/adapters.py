"""main.py 约定的入口 `from voice import adapters`；实现见 voice_layer/adapters.py。"""
from voice_layer.adapters import config, make_stt, make_tts, make_vad, resolve_bot_id  # noqa: E402

__all__ = ["config", "make_stt", "make_tts", "make_vad", "resolve_bot_id"]
