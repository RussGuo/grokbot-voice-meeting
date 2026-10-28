import asyncio

import numpy as np
import pytest
from livekit import rtc

from voice_layer.config import DuckConfig
from voice_layer.duck import BargeInTracker, GainController, Pacer, db_to_gain, rms_dbfs


def tone(n=480, sr=24000, amp=10000):
    a = (np.sin(np.arange(n) / sr * 2 * np.pi * 440) * amp).astype(np.int16)
    return rtc.AudioFrame(a.tobytes(), sr, 1, n)


def test_duck_restore_stop_gain():
    g = GainController(DuckConfig(gain_db=-18, ramp_ms=1))
    f = tone()
    base = rms_dbfs(g.apply(f))
    g.duck()
    g.apply(f)  # ramp 帧
    ducked = rms_dbfs(g.apply(f))
    assert abs((base - ducked) - 18) < 0.5
    g.restore()
    g.apply(f)
    assert abs(rms_dbfs(g.apply(f)) - base) < 0.5
    g.stop()
    assert rms_dbfs(g.apply(f)) < -100
    assert g.stopped and g.generation == 1


def test_stop_ignores_later_duck_until_new_turn():
    g = GainController(DuckConfig())
    g.stop()
    g.duck()
    g.restore()
    assert g.target == 0.0
    g.begin_turn()
    assert g.target == 1.0 and not g.stopped


def test_new_turn_starts_ducked_if_human_still_talking():
    g = GainController(DuckConfig(gain_db=-18))
    g.human_active = True
    g.begin_turn()
    assert g.target == pytest.approx(db_to_gain(-18))


def test_tracker_confirm_and_reject():
    g = GainController(DuckConfig())
    ev = []
    t = BargeInTracker(0.25, g, on_confirmed=lambda: ev.append("c"), on_rejected=lambda: ev.append("r"))
    t.on_start()
    assert g.ducked
    t.on_speech_duration(0.1)
    t.on_end()
    assert ev == ["r"] and not g.ducked          # 误触发 → 恢复
    t.on_start()
    t.on_speech_duration(0.26)
    t.on_speech_duration(0.40)
    assert ev == ["r", "c"]                       # 只确认一次
    t.on_end()
    assert ev == ["r", "c"]


async def _frames(n, dur_n=1200, sr=24000):
    for _ in range(n):
        yield tone(dur_n, sr)


async def test_pacer_limits_lead_and_stop_drops_rest():
    g = GainController(DuckConfig())
    p = Pacer(lead_s=0.1)
    out = []
    t0 = asyncio.get_event_loop().time()

    async def consume():
        async for f in p.run(_frames(20), g):     # 20 × 50ms = 1s 音频
            out.append(asyncio.get_event_loop().time() - t0)
            if len(out) == 8:
                g.stop()

    await asyncio.wait_for(consume(), 3)
    assert len(out) == 8                          # stop 之后剩余帧全部丢弃
    # 第 8 帧在音频时间 0.35s 处，最多领先 0.1s → 不早于 ~0.25s 放出
    assert out[-1] >= 0.22


async def test_pacer_hold_pauses_clock():
    g = GainController(DuckConfig())
    p = Pacer(lead_s=0.05)
    got = []

    async def consume():
        async for f in p.run(_frames(6), g):
            got.append(1)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.02)
    p.hold()
    n = len(got)
    await asyncio.sleep(0.2)
    assert len(got) == n                          # hold 期间不放帧
    p.release()
    await asyncio.wait_for(task, 2)
    assert len(got) == 6
