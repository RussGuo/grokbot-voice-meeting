"""Keep the suite offline: drop credential env vars before each test."""
import os

import pytest

_PREFIXES = ("LIVEKIT_", "DEEPSEEK_", "VOLC_", "ARK_", "OPENAI_", "LLM_", "DASHSCOPE_")
_EXACT = {
    "MEETING_HUMAN_IDENTITY", "MEETING_HUMAN_NAME", "MEETING_LANG",
    "MEETING_MODERATOR_ID", "MEETING_MODERATOR_MODE", "MEETING_MODERATOR_NAME",
    "MEETING_PRINCIPAL", "MEETING_PRINCIPAL_DIR", "MEETING_PRINCIPAL_WEBHOOK",
    "MEETING_BRIEF_CACHE", "VOICE_ASR_PROVIDER", "VOICE_TTS_PROVIDER", "VOICE_CATALOG",
    "VOICE_CONFIG", "MEETING_TRANSPORT", "MEETING_JOIN_PAGE", "MEETING_BOTS",
}


@pytest.fixture(autouse=True)
def _no_secrets(monkeypatch):
    for k in list(os.environ):
        if k.startswith(_PREFIXES) or k in _EXACT:
            monkeypatch.delenv(k, raising=False)
