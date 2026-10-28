import os
import sys

import pytest


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for k in ("VOLC_API_KEY", "VOLC_APP_ID", "VOLC_ACCESS_TOKEN", "DASHSCOPE_API_KEY",
              "VOICE_ASR_PROVIDER", "VOICE_TTS_PROVIDER", "VOICE_CONFIG"):
        monkeypatch.delenv(k, raising=False)
    from voice_layer import adapters
    adapters._cfg = None
    yield
    adapters._cfg = None


def test_alias_mapping():
    from voice_layer import adapters
    assert adapters.resolve_bot_id("voice_pm_female_warm") == "pm_bot"
    assert adapters.resolve_bot_id("eng_bot") == "eng_bot"
    assert adapters.config().bot_voice("voice_eng_male_calm").volc_speaker == "zh_male_m191_uranus_bigtts"


def test_make_tts_without_keys_falls_back_local():
    from voice_layer import adapters
    from voice_layer.tts.local_kokoro import KokoroTTS
    t = adapters.make_tts("voice_moderator_neutral")
    assert isinstance(t, KokoroTTS)


def test_make_tts_volc_when_key_present(monkeypatch):
    monkeypatch.setenv("VOLC_API_KEY", "dummy")
    from voice_layer import adapters
    from voice_layer.tts.volc import VolcTTS
    t = adapters.make_tts("voice_pm_female_warm")
    assert isinstance(t, VolcTTS)


def test_make_stt_without_keys_local():
    from voice_layer import adapters
    s = adapters.make_stt()
    assert type(s).__name__ == "SherpaStreamingSTT"


def test_main_import_path():
    """main.py 在仓库根目录 `from voice import adapters`。"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, root)
    try:
        import importlib
        mod = importlib.import_module("voice.adapters")
        assert callable(mod.make_stt) and callable(mod.make_tts)
    finally:
        sys.path.remove(root)
