"""planner / live_voice 测试共用的会议、brief 与脚本化 chat（全部离线）。"""
import asyncio

from orchestrator.models import (AgendaItem, Brief, ChatCtxRecipe, Mandate, Meeting, MemorySnapshot, Participant,
                                 Position, SnapshotFact)


def make_meeting(lang: str = "zh"):
    parts = [
        Participant(id="host", kind="human", display_name="User"),
        Participant(id="moderator", kind="bot", display_name="Host", role="moderator"),
        Participant(id="alice", kind="bot", display_name="Alice"),
        Participant(id="bob", kind="bot", display_name="Bob"),
    ]
    agenda = [AgendaItem(id="ag-1", title="Q3 budget", owner_id="alice", timebox_ms=60_000)]
    m = Meeting(id="m1", title="t", initiator="host", participants=parts, agenda=agenda, language=lang)
    brief = Brief(
        participant_id="alice", agenda_item_ids=["ag-1"],
        positions=[Position(agenda_item_id="ag-1", stance="budget is 300k")],
        memory_snapshot=MemorySnapshot(topic="t", facts=[SnapshotFact("budget.q3", "300k", "doc", "2026-01-01")]),
        mandate=Mandate(can_commit=["send the Q3 report"], must_ask=["pricing"]) if lang == "en" else
        Mandate(can_commit=["send the Q3 report", "发送季度报告"], must_ask=["pricing", "定价"]))
    return m, {"alice": brief, "bob": Brief("bob", ["ag-1"], [], MemorySnapshot("t", []), Mandate())}


def make_ctx(pid="alice", intent="answer", system="You are Alice.", **meta) -> ChatCtxRecipe:
    base = {"intent": intent, "agenda_title": "Q3 budget", "agenda_item_id": "ag-1",
            "last_text": "How is the rollout going?"}
    base.update(meta)
    return ChatCtxRecipe(participant_id=pid, system=system, brief=None, topic_snapshots={},
                         shared_minutes=["Bob: hello"], turn_instruction="Respond.", turn_meta=base,
                         brief_text="stance: x", snapshots_text={})


class ScriptedChat:
    """按给定文本块依次产出；可设每块延迟、中途报错。记录调用与是否被关闭。"""

    def __init__(self, pieces=(), *, delay=0.0, error_after=None, first_delay=0.0):
        self.pieces = list(pieces)
        self.delay = delay
        self.first_delay = first_delay
        self.error_after = error_after
        self.calls: list[tuple[list, dict]] = []
        self.closed = 0

    async def astream_text(self, messages, **kw):
        self.calls.append((messages, kw))
        try:
            if self.first_delay:
                await asyncio.sleep(self.first_delay)
            for i, p in enumerate(self.pieces):
                if self.error_after is not None and i >= self.error_after:
                    raise RuntimeError("boom")
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield p
        finally:
            self.closed += 1


async def collect(ps):
    out = []
    async for p in ps:
        out.append(p)
    return "".join(out)
