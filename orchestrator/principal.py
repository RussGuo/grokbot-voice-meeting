"""askPrincipal：会中慢路径，向真实 bot（本体）异步提问。

编排器只依赖 PrincipalClient。三种实现：

- MockPrincipal：测试和离线 demo。延迟后投递一条占位答复，不联网。
- FilePrincipal：把问题写成 JSON，等真实 bot 把答复文件放回来。
  目录布局（MEETING_PRINCIPAL=file，MEETING_PRINCIPAL_DIR）：
    outbox/<query_id>.json   编排器写出的问题
    inbox/<query_id>.json    真实 bot 写回的 {"text": "..."}
- WebhookPrincipal：POST 问题到 MEETING_PRINCIPAL_WEBHOOK。响应体若含 text 就当场采用；
  否则同样落盘到 outbox，并轮询 inbox（方便 Grok Bot 稍后答复）。

会后纪要不是这条慢路径。`meet followup <run>` 读 minutes.md 和 actions/<bot>.json，
生成可以拿去问真实 bot 的提示词。见 skills/voice-meeting/SKILL.md。
"""
from __future__ import annotations

import abc
import asyncio
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from .models import ID, Millis, PrincipalQuery

# 编排器提供的定时投递函数：schedule(delay_ms, event_dict)
ScheduleFn = Callable[[Millis, dict], None]


class PrincipalClient(abc.ABC):
    """In-meeting question channel. ask() returns immediately; the answer is an orchestrator event."""

    @abc.abstractmethod
    async def ask(self, q: PrincipalQuery) -> None:
        """Fire and forget. Deliver {"type": "principal.answered", "query_id", "text"} via the schedule callback."""


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


def _query_payload(q: PrincipalQuery) -> dict:
    return {
        "query_id": q.id,
        "principal_bot_id": q.principal_bot_id,
        "asked_by": q.asked_by,
        "question": q.question,
        "agenda_item_id": q.agenda_item_id,
        "ttl_ms": q.ttl_ms,
    }


class FilePrincipal(PrincipalClient):
    """Drop a question file and poll for the real bot's reply. Never invents an answer."""

    def __init__(self, schedule: ScheduleFn, directory: str | Path, *, poll_s: float = 1.0):
        self.schedule = schedule
        self.directory = Path(directory)
        self.poll_s = poll_s
        self.asked: list[PrincipalQuery] = []

    async def ask(self, q: PrincipalQuery) -> None:
        self.asked.append(q)
        out = self.directory / "outbox"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{q.id}.json").write_text(json.dumps(_query_payload(q), ensure_ascii=False, indent=2), encoding="utf-8")
        asyncio.create_task(self._poll(q))

    async def _poll(self, q: PrincipalQuery) -> None:
        inbox = self.directory / "inbox" / f"{q.id}.json"
        deadline = asyncio.get_event_loop().time() + max(1.0, q.ttl_ms / 1000)
        while asyncio.get_event_loop().time() < deadline:
            if inbox.is_file():
                try:
                    data = json.loads(inbox.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    data = {}
                text = (data.get("text") or "").strip()
                if text:
                    self.schedule(0, {"type": "principal.answered", "query_id": q.id, "text": text})
                    return
            await asyncio.sleep(self.poll_s)


class WebhookPrincipal(PrincipalClient):
    """POST the question. A JSON body with "text" is the answer; otherwise fall back to the file inbox."""

    def __init__(self, schedule: ScheduleFn, url: str, *, directory: str | Path | None = None, timeout_s: float = 10.0):
        self.schedule = schedule
        self.url = url
        self.timeout_s = timeout_s
        self.files = FilePrincipal(schedule, directory or "runs/principal")
        self.asked: list[PrincipalQuery] = []

    async def ask(self, q: PrincipalQuery) -> None:
        self.asked.append(q)
        payload = json.dumps(_query_payload(q), ensure_ascii=False).encode("utf-8")

        def _post() -> str:
            req = urllib.request.Request(self.url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310 — operator-configured URL
                body = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return ""
            return (data.get("text") or "").strip() if isinstance(data, dict) else ""

        text = ""
        try:
            text = await asyncio.to_thread(_post)
        except (urllib.error.URLError, TimeoutError, OSError):
            text = ""
        if text:
            self.schedule(0, {"type": "principal.answered", "query_id": q.id, "text": text})
            return
        await self.files.ask(q)


def deliver_to(orch) -> ScheduleFn:
    """Prefer the live inbox so a late answer is not stuck behind the current timer wait."""
    def deliver(delay_ms: Millis, ev: dict) -> None:
        inbox = getattr(orch, "inbox", None)
        if delay_ms <= 0 and inbox is not None:
            inbox.put_nowait(ev)
            return
        orch.schedule_timer(max(0, delay_ms), ev)
    return deliver


def principal_from_env(orch) -> PrincipalClient:
    """MEETING_PRINCIPAL=mock|file|webhook. Default mock, which is what tests and `meet demo` use."""
    kind = (os.environ.get("MEETING_PRINCIPAL") or "mock").strip().lower()
    deliver = deliver_to(orch)
    if kind == "file":
        return FilePrincipal(deliver, os.environ.get("MEETING_PRINCIPAL_DIR") or "runs/principal")
    if kind == "webhook":
        url = os.environ.get("MEETING_PRINCIPAL_WEBHOOK") or ""
        if not url:
            raise RuntimeError("MEETING_PRINCIPAL=webhook requires MEETING_PRINCIPAL_WEBHOOK")
        return WebhookPrincipal(deliver, url, directory=os.environ.get("MEETING_PRINCIPAL_DIR") or "runs/principal")
    delay = int(os.environ.get("MEETING_PRINCIPAL_DELAY_MS") or "20000")
    return MockPrincipal(orch.schedule_timer, delay_ms=delay)
