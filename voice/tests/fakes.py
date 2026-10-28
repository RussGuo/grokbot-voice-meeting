"""测试替身：FakeVad（可手动触发 VAD 事件）、FakeSession（记录 update_agent / interrupt）。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from livekit.agents import vad as lk_vad


class FakeVadStream:
    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()

    def push_frame(self, frame) -> None:
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.q.get()

    async def aclose(self) -> None:
        pass

    def fire(self, kind: str, speech_duration: float = 0.0) -> None:
        t = {"start": lk_vad.VADEventType.START_OF_SPEECH, "inf": lk_vad.VADEventType.INFERENCE_DONE,
             "end": lk_vad.VADEventType.END_OF_SPEECH}[kind]
        self.q.put_nowait(SimpleNamespace(type=t, speaking=kind != "end", speech_duration=speech_duration))


class FakeVad:
    def __init__(self) -> None:
        self.s = FakeVadStream()

    def stream(self):
        return self.s


class FakeSession:
    def __init__(self) -> None:
        self.agent_state = "listening"
        self.handlers = {}
        self.calls: list = []
        self.enter_delay = 0.05
        self.current = None

    def on(self, name, cb):
        self.handlers[name] = cb

    def interrupt(self, force: bool = False):
        self.calls.append(("interrupt", force))
        f = asyncio.get_event_loop().create_future()
        f.set_result(None)
        return f

    def update_agent(self, agent) -> None:
        self.calls.append(("update_agent", agent.bot_id))

        async def enter():
            await asyncio.sleep(self.enter_delay)   # 模拟切换窗口
            self.current = agent
            await agent.on_enter()

        asyncio.ensure_future(enter())


async def settle(n: int = 5) -> None:
    for _ in range(n):
        await asyncio.sleep(0)
