"""OpenAI speech adapters via livekit-plugins-openai.

Works against api.openai.com or any compatible base URL (OPENAI_BASE_URL / LLM_BASE_URL).
Selected with VOICE_ASR_PROVIDER=openai and VOICE_TTS_PROVIDER=openai.
The key is OPENAI_API_KEY, or LLM_API_KEY if that is unset.
"""
from __future__ import annotations

import os
from typing import Any


def _key() -> str:
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY") or ""
    if not key:
        raise RuntimeError("VOICE_*_PROVIDER=openai requires OPENAI_API_KEY or LLM_API_KEY")
    return key


def _base_url() -> str | None:
    return os.environ.get("OPENAI_BASE_URL") or os.environ.get("LLM_BASE_URL") or None


def make_openai_stt(cfg: Any):
    from livekit.plugins import openai
    kw: dict[str, Any] = {
        "model": os.environ.get("OPENAI_STT_MODEL", "whisper-1"),
        "api_key": _key(),
    }
    base = _base_url()
    if base:
        kw["base_url"] = base
    return openai.STT(**kw)


def make_openai_tts(cfg: Any, voice):
    from livekit.plugins import openai
    kw: dict[str, Any] = {
        "model": os.environ.get("OPENAI_TTS_MODEL", "tts-1"),
        "voice": getattr(voice, "openai_voice", None) or "alloy",
        "api_key": _key(),
    }
    base = _base_url()
    if base:
        kw["base_url"] = base
    return openai.TTS(**kw)
