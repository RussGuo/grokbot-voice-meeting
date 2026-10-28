"""用 bots/ 里的 brief 拼一次 chat_ctx，调真实 LLM 发言一次，确认上下文组装可用。

用法：.venv/bin/python scripts/ctx_smoke.py [--bot eng_bot]
没有 DEEPSEEK_API_KEY 时只打印组装结果，不调用。
"""
from __future__ import annotations

import argparse
import asyncio

from _env import load_env


async def build(bot: str):
    from orchestrator.driver import SimDriver
    d = SimDriver()
    await d.start()
    d.say(11_000, f"{d.orch.name(bot)}，离线同步的冲突率和工期怎么样？")
    await d.run_until(16_000)
    ctx = d.orch.assemble_chat_ctx(bot, "answer", {"addressed": True})
    return ctx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default="eng_bot")
    a = ap.parse_args()
    load_env()
    ctx = asyncio.run(build(a.bot))
    msgs = ctx.to_messages()
    print(f"chat_ctx：{len(msgs)} 条消息，约 {sum(len(m['content']) for m in msgs)} 字")
    for m in msgs:
        print(f"  [{m['role']}] {m['content'][:90].replace(chr(10), ' ')}…")
    from orchestrator.llm import make_chat
    chat = make_chat()
    r = chat.stream(msgs, max_tokens=64)
    print(f"LLM 发言（TTFT={r.ttft_ms and round(r.ttft_ms)}ms，总={round(r.total_ms)}ms）：{r.text[:120]}")


if __name__ == "__main__":
    main()
