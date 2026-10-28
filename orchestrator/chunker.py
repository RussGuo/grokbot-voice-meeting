"""LLM 输出按句切段，流式送 TTS。

规则：遇到 。！？；（及半角 !?;）切一段；或累计超过 max_chars（约 20 字）在下一个逗号/空格处切，
没有逗号就硬切。每段带 token_id 与序号；令牌失效（is_valid(token_id) 为 False）后立刻停止发送后续段，
并停止消费上游（真实实现里即取消 LLM 流）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import AsyncIterable, Awaitable, Callable, Iterable, Optional, Union

SENT_END = set("。！？；!?;")
SOFT_BREAK = set("，,、 ：:")
DEFAULT_MAX_CHARS = 20


@dataclass
class Segment:
    token_id: str
    seq: int
    text: str
    final: bool = False
    t_ms: Optional[float] = None   # 相对流开始的切出时间


class SentenceChunker:
    def __init__(self, max_chars: int = DEFAULT_MAX_CHARS):
        self.max_chars = max_chars
        self.buf = ""

    def feed(self, piece: str) -> list[str]:
        out = []
        for ch in piece:
            self.buf += ch
            if ch in SENT_END:
                out.append(self._take(len(self.buf)))
            elif len(self.buf) > self.max_chars:
                cut = max((i for i, c in enumerate(self.buf) if c in SOFT_BREAK), default=-1)
                out.append(self._take(cut + 1 if cut >= self.max_chars // 2 else len(self.buf)))
        return [s for s in out if s.strip()]

    def flush(self) -> list[str]:
        rest, self.buf = self.buf, ""
        return [rest] if rest.strip() else []

    def _take(self, n: int) -> str:
        seg, self.buf = self.buf[:n], self.buf[n:]
        return seg


SendFn = Callable[[Segment], Union[None, Awaitable[None]]]


async def pump_segments(source: Union[AsyncIterable[str], Iterable[str]], token_id: str,
                        is_valid: Callable[[str], bool], send: SendFn,
                        max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """把文本流切段发给 send。返回统计：{"sent", "stopped", "first_segment_ms"}。"""
    ch = SentenceChunker(max_chars)
    t0 = time.perf_counter()
    seq = 0
    first = None

    async def _emit(text: str, final: bool = False) -> bool:
        nonlocal seq, first
        if not is_valid(token_id):
            return False
        t = (time.perf_counter() - t0) * 1000
        if first is None:
            first = t
        r = send(Segment(token_id, seq, text, final, t))
        if hasattr(r, "__await__"):
            await r
        seq += 1
        return True

    async def _iter():
        if hasattr(source, "__aiter__"):
            async for p in source:  # type: ignore[union-attr]
                yield p
        else:
            for p in source:  # type: ignore[union-attr]
                yield p

    async for piece in _iter():
        if not is_valid(token_id):
            return {"sent": seq, "stopped": True, "first_segment_ms": first}
        for s in ch.feed(piece):
            if not await _emit(s):
                return {"sent": seq, "stopped": True, "first_segment_ms": first}
    rest = ch.flush()
    for s in rest:
        if not await _emit(s, final=True):
            return {"sent": seq, "stopped": True, "first_segment_ms": first}
    return {"sent": seq, "stopped": False, "first_segment_ms": first}
