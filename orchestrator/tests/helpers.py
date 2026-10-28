import asyncio

from orchestrator.driver import SimDriver
from orchestrator.state_machine import Floor


def run(coro):
    return asyncio.run(coro)


async def started(**kw) -> SimDriver:
    d = SimDriver(**kw)
    await d.start()
    return d


async def until_floor(d: SimDriver, floor: Floor, t_max: int, step: int = 50):
    t = d.clock.now
    while t <= t_max:
        await d.run_until(t)
        if d.orch.sm.floor == floor:
            return t
        t += step
    raise AssertionError(f"{t_max}ms 内没有进入 {floor}（当前 {d.orch.sm.floor}）")


def types(d: SimDriver):
    return [e.type for e in d.orch.log]
