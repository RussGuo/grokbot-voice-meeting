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
    "MEETING_HUMAN_IDENTITY",
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
    env = os.environ if env is None else env
    key = env.get("DEEPSEEK_API_KEY") or None
    return LLMConfig(
        provider="deepseek" if key else "mock",   # key 到位前自动用 mock
        base_url=env.get("DEEPSEEK_BASE_URL") or DEEPSEEK_BASE_URL_DEFAULT,
        model=env.get("DEEPSEEK_MODEL") or DEEPSEEK_MODEL_DEFAULT,
        api_key=key,
    )


def report(env: dict[str, str] | None = None) -> dict[str, bool]:
    env = os.environ if env is None else env
    return {k: bool(env.get(k)) for k in ENV_VARS}
