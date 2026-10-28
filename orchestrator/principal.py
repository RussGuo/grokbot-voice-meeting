"""askPrincipal：会中慢路径，向 Grok Bot 本体异步提问。

原型只提供接口与模拟实现，不调用任何 bot 或外部服务。
真实实现（待平台可行性分析师实测往返延迟、并经用户确认后再做）：编排器 POST 到本体的
webhook routine 或经 MCP 发问，本体通过自建 MCP 回写答案 → 编排器收到 principal.answered。
"""
from __future__ import annotations

import abc
from typing import Callable, Optional

from .models import ID, Millis, PrincipalQuery

# 编排器提供的定时投递函数：schedule(delay_ms, event_dict)
ScheduleFn = Callable[[Millis, dict], None]


class PrincipalClient(abc.ABC):
    @abc.abstractmethod
    async def ask(self, q: PrincipalQuery) -> None:
        """异步发问，立即返回；答案经编排器事件 {"type": "principal.answered", "query_id", "text"} 回来。"""


class MockPrincipal(PrincipalClient):
    """模拟本体：延迟可配，答案来自预置字典或模板。绝不联网。"""

    def __init__(self, schedule: ScheduleFn, *, delay_ms: Millis = 20_000,
                 answers: Optional[dict[ID, str]] = None, never_answer: bool = False):
        self.schedule = schedule
        self.delay_ms = delay_ms
        self.answers = answers or {}
        self.never_answer = never_answer
        self.asked: list[PrincipalQuery] = []

    async def ask(self, q: PrincipalQuery) -> None:
        self.asked.append(q)
        if self.never_answer:
            return
        text = self.answers.get(q.principal_bot_id) or f"（模拟本体答复）关于「{q.question}」：按本体记忆，结论待会后书面确认。"
        self.schedule(self.delay_ms, {"type": "principal.answered", "query_id": q.id, "text": text})
