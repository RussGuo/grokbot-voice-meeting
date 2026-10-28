"""LLM 工厂（探针 / 自测用；正式由 orchestrator 决定每个 bot 的 llm）。
deepseek：OpenAI 兼容，读 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL（不打印）。
mock：ScriptedLLM，无 key。
"""
from __future__ import annotations

import os

from .mock_llm import ScriptedLLM


def deepseek_available() -> bool:
    return bool(os.environ.get("DEEPSEEK_API_KEY"))


def make_llm(kind: str = "mock", **kw):
    if kind == "deepseek":
        from livekit.plugins import openai as lk_openai
        return lk_openai.LLM(model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
                             base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                             api_key=os.environ["DEEPSEEK_API_KEY"], temperature=0.3)
    return ScriptedLLM(**kw)
