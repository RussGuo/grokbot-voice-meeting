"""环境变量读取。只读不打印值；report() 只报告“是否存在”。

单测与 sim.py 不调用 load_dotenv，也不依赖任何 key。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_HUMAN_IDENTITY = "host"
DEEPSEEK_BASE_URL_DEFAULT = "https://api.deepseek.com"   # 官方文档：https://api-docs.deepseek.com/
DEEPSEEK_MODEL_DEFAULT = "deepseek-flash"                # 同上，2026-10-27 查证

# README 环境变量清单（只列变量名）
ENV_VARS = [
    "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
    "VOLC_API_KEY", "VOLC_APP_ID", "VOLC_ACCESS_TOKEN",
    "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
    "LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "OPENAI_API_KEY",
    "MEETING_HUMAN_IDENTITY", "MEETING_HUMAN_NAME", "MEETING_LANG",
    "MEETING_MODERATOR_ID", "VOICE_ASR_PROVIDER", "VOICE_TTS_PROVIDER", "MEETING_TRANSPORT",
]


def human_identity(env: dict[str, str] | None = None) -> str:
    """linkedHumanId 必须等于用户的 LiveKit identity，默认 host。"""
    env = os.environ if env is None else env
    return (env.get("MEETING_HUMAN_IDENTITY") or DEFAULT_HUMAN_IDENTITY).strip()


@dataclass
class LLMConfig:
    provider: str          # "deepseek" | "mock"
    base_url: str
    model: str
    api_key: str | None


def llm_config(env: dict[str, str] | None = None) -> LLMConfig:
    """Select an OpenAI-compatible chat backend.

    Precedence when LLM_PROVIDER / MEETING_LLM_PROVIDER is unset (auto):
      DEEPSEEK_API_KEY → deepseek, else LLM_API_KEY or OPENAI_API_KEY → openai, else mock.
    DeepSeek is the default named provider. Any other OpenAI-compatible endpoint is
    `openai` plus LLM_BASE_URL / LLM_API_KEY / LLM_MODEL.
    """
    env = os.environ if env is None else env
    requested = (env.get("LLM_PROVIDER") or env.get("MEETING_LLM_PROVIDER") or "auto").strip().lower()
    deepseek_key = env.get("DEEPSEEK_API_KEY") or None
    generic_key = env.get("LLM_API_KEY") or env.get("OPENAI_API_KEY") or None
    if requested in ("", "auto"):
        requested = "deepseek" if deepseek_key else ("openai" if generic_key else "mock")
    if requested == "deepseek":
        return LLMConfig(
            provider="deepseek" if deepseek_key else "mock",
            base_url=env.get("DEEPSEEK_BASE_URL") or env.get("LLM_BASE_URL") or DEEPSEEK_BASE_URL_DEFAULT,
            model=env.get("DEEPSEEK_MODEL") or env.get("LLM_MODEL") or DEEPSEEK_MODEL_DEFAULT,
            api_key=deepseek_key,
        )
    if requested in ("openai", "openai-compatible", "compatible"):
        return LLMConfig(
            provider="openai" if generic_key else "mock",
            base_url=env.get("LLM_BASE_URL") or env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1",
            model=env.get("LLM_MODEL") or env.get("OPENAI_MODEL") or "gpt-4o-mini",
            api_key=generic_key,
        )
    if requested == "mock":
        return LLMConfig("mock", DEEPSEEK_BASE_URL_DEFAULT, DEEPSEEK_MODEL_DEFAULT, None)
    # Unknown name: still an OpenAI-compatible endpoint if a key and base URL are set.
    if generic_key or deepseek_key:
        return LLMConfig(
            provider=requested,
            base_url=env.get("LLM_BASE_URL") or env.get("DEEPSEEK_BASE_URL") or DEEPSEEK_BASE_URL_DEFAULT,
            model=env.get("LLM_MODEL") or env.get("DEEPSEEK_MODEL") or DEEPSEEK_MODEL_DEFAULT,
            api_key=generic_key or deepseek_key,
        )
    return LLMConfig("mock", DEEPSEEK_BASE_URL_DEFAULT, DEEPSEEK_MODEL_DEFAULT, None)


def report(env: dict[str, str] | None = None) -> dict[str, bool]:
    env = os.environ if env is None else env
    return {k: bool(env.get(k)) for k in ENV_VARS}
