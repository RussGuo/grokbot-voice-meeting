"""举手打分器的可插拔层：规则版（默认）+ 可选的异步 LLM 批量二次打分。

- `Scorer`：编排器 `_fan_out` 需要的最小接口。RuleScorer（scheduler.py）是默认实现。
- `LlmBatchScorer`：规则分照常立即举手（调度 < 50ms 不变）；同时每条发言发**一次** LLM 请求，
  一次覆盖所有成员 bot，返回 {bot_id: {relevance, novelty, gist}}。结果当作 `scorer.update`
  事件投回编排器 inbox（与 principal.answered 同一条路），由编排器丢弃过期结果、重算 S、
  补举手 / 替换 / 撤回。任何 LLM 错误都静默回落到规则分（只打一次 warning）。
- `build_scorer(briefs, aliases, chat=None, mode=None)`：mode 取自环境变量 MEETING_SCORER=rule|llm。
  mode=llm 但没有 chat（没配 key）时退回规则版。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from .models import ID, Brief, HandScore, Millis
from .scheduler import RuleScorer, hand_score

log = logging.getLogger("orchestrator.scoring")

SCORER_UPDATE = "scorer.update"      # 投回编排器的事件类型
LLM_TIMEOUT_S = 1.5                  # 硬超时：超过就放弃，规则分照旧
_MAX_FACTS, _MAX_FACT_CHARS = 8, 120


class Scorer(Protocol):
    """`_fan_out` 与 `_on_scorer_update` 用到的全部接口。"""

    async_updates: bool

    def addressed(self, bot_id: ID, text: str) -> bool: ...

    def score(self, bot_id: ID, text: str, said_in_item: list[str], penalty: float,
              agenda_item_id: ID = "") -> tuple[HandScore, str]: ...

    def bind(self, deliver: Callable[[Millis, dict], None]) -> None:
        """编排器建好后注入「投递事件」函数（principal.deliver_to）。"""

    def submit(self, req: "ScoreRequest") -> None:
        """非阻塞：登记一次异步二次打分，立即返回。"""

    def blend(self, rule: HandScore, rule_gist: str, update: dict, penalty: float) -> tuple[HandScore, str]:
        """把异步结果并进规则分，返回新的 (HandScore, gist)。"""


@dataclass
class BotCtx:
    id: ID
    name: str
    rule: HandScore
    gist: str


@dataclass
class ScoreRequest:
    seq: int                              # 触发本次打分的事件序号（编排器用它判断是否过期）
    agenda_item_id: ID
    text: str                             # 刚刚的发言
    speaker_id: ID
    bots: list[BotCtx]
    said: list[str] = field(default_factory=list)       # 本议程项已有的成员发言
    recent: list[tuple[str, str]] = field(default_factory=list)   # (说话人, 内容) 最近几条，给 LLM 当上下文


def _clip(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else max(0.0, min(1.0, v))


def parse_scores(raw: str, bot_ids: set[ID]) -> dict[ID, dict]:
    """从 LLM 输出里取第一个 JSON 对象；只收已知 bot、数值合法的条目。"""
    i = raw.find("{")
    if i < 0:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(raw[i:])
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[ID, dict] = {}
    for bid, v in data.items():
        if bid not in bot_ids or not isinstance(v, dict):
            continue
        rel, nov = _clip(v.get("relevance")), _clip(v.get("novelty"))
        if rel is None or nov is None:
            continue
        gist = v.get("gist")
        out[bid] = {"relevance": rel, "novelty": nov, "gist": gist.strip()[:200] if isinstance(gist, str) else ""}
    return out


_SYSTEM = (
    "You score, for a live multi-bot meeting, how much each bot stand-in has to add after the latest utterance. "
    "Judge only from each bot's brief below. Reply with ONE JSON object and nothing else: "
    '{"<bot_id>": {"relevance": 0..1, "novelty": 0..1, "gist": "<=20 words"}, ...} covering every listed bot. '
    "relevance = how directly the latest utterance concerns what this bot can speak to (0 = unrelated, 1 = squarely its topic). "
    "novelty = how much NEW information the bot's contribution would add beyond what was already said (0 = repeats it). "
    "gist = the one point the bot would make, in the meeting's language; empty if it has nothing."
)


class LlmBatchScorer(RuleScorer):
    """规则打分 + 异步 LLM 批量二次打分。同一时刻最多一个请求在飞，新发言顶掉旧请求。"""

    async_updates = True

    def __init__(self, briefs: dict[ID, Brief], aliases: dict[ID, list[str]], chat: Any, *,
                 timeout_s: float = LLM_TIMEOUT_S, max_tokens: int = 400):
        super().__init__(briefs, aliases)
        self.chat = chat
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self._deliver: Optional[Callable[[Millis, dict], None]] = None
        self._task: Optional[asyncio.Task] = None
        self._latest = -1
        self._warned = False

    def bind(self, deliver: Callable[[Millis, dict], None]) -> None:
        self._deliver = deliver

    # —— 提交 ——
    def submit(self, req: ScoreRequest) -> None:
        if self._deliver is None or not req.bots:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:                       # 没有事件循环（纯同步调用）：只用规则分
            return
        prev = self._task
        if prev is not None and not prev.done():
            prev.cancel()                          # 新发言顶掉旧请求
        self._latest = req.seq
        self._task = loop.create_task(self._run(req, prev))

    async def wait_idle(self) -> None:
        """测试用：等当前请求结束（含被取消）。"""
        t = self._task
        if t is not None:
            await asyncio.gather(t, return_exceptions=True)

    async def _run(self, req: ScoreRequest, prev: Optional[asyncio.Task]) -> None:
        if prev is not None:                       # 保证旧请求彻底退出后才开新的：任何时刻至多一个在飞
            await asyncio.gather(prev, return_exceptions=True)
        if req.seq != self._latest:
            return
        try:
            raw = await asyncio.wait_for(self._collect(self._messages(req)), self.timeout_s)
            scores = parse_scores(raw, {b.id for b in req.bots})
            if not scores:
                raise ValueError("LLM 打分没有可用条目")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 任何失败都回落到规则分
            self._fail(e)
            return
        if req.seq != self._latest or self._deliver is None:
            return
        self._deliver(0, {"type": SCORER_UPDATE, "based_on_seq": req.seq,
                          "agenda_item_id": req.agenda_item_id, "scores": scores})

    async def _collect(self, messages: list[dict]) -> str:
        parts: list[str] = []
        async with contextlib.aclosing(self.chat.astream_text(
                messages, max_tokens=self.max_tokens, thinking="disabled")) as stream:
            async for piece in stream:
                parts.append(piece)
        return "".join(parts)

    def _fail(self, e: BaseException) -> None:
        if not self._warned:
            self._warned = True
            log.warning("LLM 批量打分不可用，继续用规则分（%s：%s）；后续同类错误不再提示",
                        type(e).__name__, e or "timeout")
        else:
            log.debug("LLM 批量打分失败：%r", e)

    # —— 提示词 ——
    def _messages(self, req: ScoreRequest) -> list[dict]:
        lines = []
        for b in req.bots:
            brief = self.briefs[b.id]
            pos = next((p.stance for p in brief.positions if p.agenda_item_id == req.agenda_item_id), "")
            facts = [f"{f.key}: {f.value}"[:_MAX_FACT_CHARS] for f in brief.memory_snapshot.facts[:_MAX_FACTS]]
            lines.append(f"- {b.id} ({b.name})\n  keywords: {', '.join(brief.keywords)}\n"
                         f"  stance on this agenda item: {pos or '(none)'}\n  facts: {' | '.join(facts) or '(none)'}")
        ctx = "\n".join(f"{who}: {t}" for who, t in req.recent) or "(start of item)"
        said = "\n".join(f"- {s}" for s in req.said[-6:]) or "(nothing yet)"
        user = ("Bots:\n" + "\n".join(lines) + f"\n\nRecent turns:\n{ctx}\n\nAlready said by bots on this item:\n{said}\n\n"
                f"Latest utterance:\n{req.text}\n")
        return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]

    # —— 合并 ——
    def blend(self, rule: HandScore, rule_gist: str, update: dict, penalty: float) -> tuple[HandScore, str]:
        """用 LLM 的相关度 / 新信息量重算 S；置信度（能否引用 brief 事实）和被点名仍取规则结果。

        规则新信息量已经 ≤ 0.1（几乎逐字复读）时不让 LLM 抬高，防复读这条底线保持确定性。"""
        rel = update.get("relevance", rule.relevance)
        nov = update.get("novelty", rule.novelty) if rule.novelty > 0.1 else rule.novelty
        return (hand_score(rel, rule.confidence, nov, rule.addressed, penalty),
                update.get("gist") or rule_gist)


def build_scorer(briefs: dict[ID, Brief], aliases: dict[ID, list[str]], chat: Any = None,
                 mode: Optional[str] = None) -> Scorer:
    """mode 缺省读环境变量 MEETING_SCORER（rule | llm，默认 rule）。llm 没有 chat 时退回 rule。"""
    mode = (mode or os.environ.get("MEETING_SCORER") or "rule").strip().lower()
    if mode == "llm":
        if chat is None:
            log.warning("MEETING_SCORER=llm 但没有可用的 LLM（未配置 key），退回规则打分")
        else:
            return LlmBatchScorer(briefs, aliases, chat)
    elif mode != "rule":
        log.warning("未知的 MEETING_SCORER=%r，按 rule 处理", mode)
    return RuleScorer(briefs, aliases)
