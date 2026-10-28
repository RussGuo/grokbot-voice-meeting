"""单测公共夹具：确保不读 .env、不依赖任何 key。"""
import os

import pytest


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("LIVEKIT_", "DEEPSEEK_", "VOLC_", "ARK_")) or k == "MEETING_HUMAN_IDENTITY":
            monkeypatch.delenv(k, raising=False)
