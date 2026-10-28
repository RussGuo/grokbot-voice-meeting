"""LLM 适配：DeepSeek（OpenAI 兼容接口）+ mock。

- 真实：openai SDK，base_url / model / key 读 DEEPSEEK_BASE_URL / DEEPSEEK_MODEL / DEEPSEEK_API_KEY。
- mock：没有 key 时自动使用（单测与 sim 永远用 FakeLLM，不经过这里）。
- 日志只报告变量是否存在，不打印任何值。

main.py 里各 Agent 的 llm 用 livekit-plugins-openai 的 openai.LLM(base_url=..., api_key=..., model=...)，
本文件的 DeepSeekChat 供探针脚本和离线验证 chat_ctx 组装用。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .config import LLMConfig, llm_config


@dataclass
class StreamResult:
    text: str
    ttft_ms: Optional[float]                 # 首个正文 token
    total_ms: float
    usage: Optional[dict] = None
    first_any_ms: Optional[float] = None     # 首个 token（含思考 reasoning_content）
    first_segment_ms: Optional[float] = None # 首句就绪（SentenceChunker 切出第一段）
    reasoning_chars: int = 0


def thinking_body(thinking: Optional[str]) -> Optional[dict]:
    """DeepSeek 官方文档：OpenAI 格式用 extra_body={"thinking": {"type": "enabled"|"disabled"}}，默认开启。"""
    if thinking in ("enabled", "disabled"):
        return {"thinking": {"type": thinking}}
    return None


class DeepSeekChat:
    def __init__(self, cfg: Optional[LLMConfig] = None):
        self.cfg = cfg or llm_config()
        if self.cfg.provider != "deepseek":
            raise RuntimeError("未配置 DEEPSEEK_API_KEY，不能使用真实 LLM（单测请用 FakeLLM）")
        from openai import OpenAI
        self.client = OpenAI(base_url=self.cfg.base_url, api_key=self.cfg.api_key)

    def stream(self, messages: list[dict], *, max_tokens: int = 64, temperature: float = 0.7,
               model: Optional[str] = None, thinking: Optional[str] = None) -> StreamResult:
        from .chunker import SentenceChunker
        t0 = time.perf_counter()
        ttft = first_any = first_seg = None
        parts: list[str] = []
        usage = None
        rchars = 0
        ch = SentenceChunker()
        kw = {}
        body = thinking_body(thinking)
        if body:
            kw["extra_body"] = body
        resp = self.client.chat.completions.create(
            model=model or self.cfg.model, messages=messages, max_tokens=max_tokens,
            temperature=temperature, stream=True, stream_options={"include_usage": True}, **kw,
        )
        for chunk in resp:
            now = (time.perf_counter() - t0) * 1000
            if chunk.choices:
                delta = chunk.choices[0].delta
                rc = getattr(delta, "reasoning_content", None) or ""
                piece = getattr(delta, "content", None) or ""
                if (rc or piece) and first_any is None:
                    first_any = now
                rchars += len(rc)
                if piece:
                    if ttft is None:
                        ttft = now
                    parts.append(piece)
                    if first_seg is None and ch.feed(piece):
                        first_seg = now
            if getattr(chunk, "usage", None):
                usage = chunk.usage.model_dump() if hasattr(chunk.usage, "model_dump") else None
        total = (time.perf_counter() - t0) * 1000
        if first_seg is None and parts:
            first_seg = total                      # 整段不足一句：流结束时 flush
        return StreamResult("".join(parts), ttft, total, usage, first_any, first_seg, rchars)

    async def astream_text(self, messages: list[dict], *, max_tokens: int = 200,
                           thinking: Optional[str] = "disabled"):
        """异步逐段产出正文（main.py 送 TTS 用）。语音场景默认关思考以降首 token。"""
        from openai import AsyncOpenAI
        client = AsyncOpenAI(base_url=self.cfg.base_url, api_key=self.cfg.api_key)
        kw = {}
        body = thinking_body(thinking)
        if body:
            kw["extra_body"] = body
        resp = await client.chat.completions.create(model=self.cfg.model, messages=messages,
                                                    max_tokens=max_tokens, stream=True, **kw)
        async for chunk in resp:
            if chunk.choices:
                piece = getattr(chunk.choices[0].delta, "content", None)
                if piece:
                    yield piece


class MockChat:
    """key 到位前的占位：返回固定文本，延迟为 0。"""

    def stream(self, messages: list[dict], *, max_tokens: int = 64, temperature: float = 0.7, **_) -> StreamResult:
        return StreamResult("（mock）收到，我补充一点。", 0.0, 0.0, None, 0.0, 0.0)

    async def astream_text(self, messages, **_):
        for p in ("（mock）收到，", "我补充一点。"):
            yield p


def make_chat(cfg: Optional[LLMConfig] = None):
    cfg = cfg or llm_config()
    return DeepSeekChat(cfg) if cfg.provider == "deepseek" else MockChat()
