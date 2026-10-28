"""Provider registry.

Core meeting logic talks to ASR, TTS, LLM, and the realtime transport through this package.
Select an implementation with environment variables. Defaults:

- LLM: DeepSeek (OpenAI-compatible). Any other endpoint is LLM_PROVIDER=openai plus LLM_BASE_URL.
- ASR / TTS: Volcengine Doubao when credentials exist, otherwise the local sherpa fallback.
  Set VOICE_ASR_PROVIDER / VOICE_TTS_PROVIDER to volc, aliyun, openai, or local.
- Transport: LiveKit (MEETING_TRANSPORT=livekit).

See docs/PROVIDERS.md for how to add an adapter.
"""
from __future__ import annotations

from .registry import registry

__all__ = ["registry"]
