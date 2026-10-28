"""LLM 首 token 延迟探针（DeepSeek，OpenAI 兼容，流式）。

用法：
  .venv/bin/python scripts/llm_ttft_probe.py -n 10 --model deepseek-flash --thinking disabled --context short
参数：
  --model     覆盖 DEEPSEEK_MODEL（不改 .env）
  --thinking  default | enabled | disabled（官方：extra_body={"thinking":{"type":...}}，默认开启）
  --context   plain（短 system + 一句话）| full（全量 JSON brief + 快照）| short（压缩 brief ≤800 字）
指标：首 token（含思考）、首正文 token（TTFT）、首句就绪（SentenceChunker 切出第一段）、总耗时；
报 min / 中位数 / p90。绝不打印 key，只打印变量是否存在。max_tokens ≤ 64。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

from _env import load_env

PROMPTS = [
    "离线同步的冲突率现在怎么样？",
    "工程这边下周能出技术评估吗？",
    "AI 摘要先灰度多少比例合适？",
    "这个需求的优先级你怎么看？",
    "推理成本能降多少？",
]
SYSTEM = "你是多 bot 语音会议里的工程分身。口语化，一两句话回答，不超过 40 字。"


def pct(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def ctx_messages(mode: str, question: str) -> list[dict]:
    if mode == "plain":
        return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
    from orchestrator.driver import SimDriver
    d = SimDriver()
    asyncio.run(d.start())
    d.orch.minutes.append(f"用户：工程分身，{question}")
    ctx = d.orch.assemble_chat_ctx("eng_bot", "answer", {"addressed": True})
    return ctx.to_messages(compact=(mode == "short"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument("--max-tokens", type=int, default=48)
    ap.add_argument("--model", default=None)
    ap.add_argument("--thinking", choices=["default", "enabled", "disabled"], default="default")
    ap.add_argument("--context", choices=["plain", "full", "short"], default="plain")
    ap.add_argument("--json", action="store_true", help="最后输出一行 JSON 汇总")
    a = ap.parse_args()
    assert a.max_tokens <= 64, "max_tokens 不超过 64"
    load_env()
    from orchestrator.config import llm_config
    from orchestrator.llm import DeepSeekChat
    cfg = llm_config()
    if cfg.provider != "deepseek":
        print("没有 DEEPSEEK_API_KEY，跳过真实调用。")
        return
    model = a.model or cfg.model
    msgs_by_q = {q: ctx_messages(a.context, q) for q in PROMPTS}
    in_chars = sum(len(m["content"]) for m in msgs_by_q[PROMPTS[0]])
    print(f"model={model} thinking={a.thinking} context={a.context}（输入约 {in_chars} 字）max_tokens={a.max_tokens} n={a.n}")
    chat = DeepSeekChat(cfg)
    rows = {"first_any": [], "ttft": [], "first_seg": [], "total": []}
    empty = errors = 0
    for i in range(a.n):
        q = PROMPTS[i % len(PROMPTS)]
        try:
            r = chat.stream(msgs_by_q[q], max_tokens=a.max_tokens, model=model,
                            thinking=None if a.thinking == "default" else a.thinking)
        except Exception as e:  # 只打异常类型与截断消息，不含请求头
            errors += 1
            print(f"#{i + 1} 失败：{type(e).__name__}: {str(e)[:160]}")
            continue
        if r.ttft_ms is None:
            empty += 1
        for k, v in (("first_any", r.first_any_ms), ("ttft", r.ttft_ms), ("first_seg", r.first_segment_ms), ("total", r.total_ms)):
            if v is not None:
                rows[k].append(v)
        f = lambda v: f"{v:6.0f}" if v is not None else "   无 "
        print(f"#{i + 1:02d} 首token={f(r.first_any_ms)} 首正文={f(r.ttft_ms)} 首句={f(r.first_segment_ms)} "
              f"总={f(r.total_ms)}ms 思考{r.reasoning_chars}字 「{r.text[:26]}」")
        time.sleep(0.3)
    summary = {"model": model, "thinking": a.thinking, "context": a.context, "in_chars": in_chars,
               "n_ok": a.n - errors, "no_content": empty, "errors": errors}
    for k, xs in rows.items():
        if xs:
            summary[k] = {"min": round(min(xs)), "p50": round(statistics.median(xs)), "p90": round(pct(xs, 0.9))}
            print(f"{k:9s} min={min(xs):.0f}ms 中位数={statistics.median(xs):.0f}ms p90={pct(xs, 0.9):.0f}ms（n={len(xs)}）")
    if empty:
        print(f"注意：{empty} 次在 max_tokens 内没有产出正文（思考占满了 token）")
    if a.json:
        print("SUMMARY " + json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
