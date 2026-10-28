"""硬规则：人声优先。vad.start 在空闲 / bot 在说 / 切换中都立即上报；任何在播 TTS（含切换中）都能 duck/stop。"""
import asyncio

import pytest
import pytest_asyncio

from voice_layer.config import VoiceConfig
from voice_layer.livekit_layer import LiveKitVoiceLayer
from voice_layer.mock_llm import ScriptedLLM

from fakes import FakeSession, FakeVad, settle


@pytest_asyncio.fixture
async def layer(monkeypatch):
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "local")
    monkeypatch.setenv("VOICE_ASR_PROVIDER", "local")
    cfg = VoiceConfig()
    cfg.tts_provider = "local"
    vad = FakeVad()
    sess = FakeSession()
    lay = LiveKitVoiceLayer(cfg, sess, vad=vad, llm_factory=lambda pid: ScriptedLLM())
    lay._vad = vad
    lay._sess = sess
    return lay


def types(lay):
    return [e["type"] for e in lay.bus.history]


@pytest.mark.parametrize("state", ["idle", "speaking", "thinking", "listening"])
async def test_vad_start_reported_in_every_state(layer, state):
    layer._sess.agent_state = state
    if state == "speaking":
        layer._speaking_pid = "pm_bot"
    layer._vad.s.fire("start")
    await settle()
    assert types(layer) == ["vad.start"]
    assert layer.bus.history[0]["participant_id"] == layer.human_id


async def test_vad_start_reported_and_ducks_during_switch(layer):
    layer._sess.enter_delay = 0.2
    sw = asyncio.create_task(layer.switch_agent("eng_bot", None))
    await asyncio.sleep(0.02)                  # 切换窗口中（on_enter 未到）
    assert not sw.done()
    layer._vad.s.fire("start")
    await settle()
    assert "vad.start" in types(layer)
    assert layer.gain.ducked                   # 全局增益，与哪个 Agent 无关
    r = await sw
    assert r["switch_ms"] >= 150


async def test_bargein_confirmed_stops_tts_even_mid_switch(layer):
    layer._sess.enter_delay = 0.2
    layer._speaking_pid = "pm_bot"
    layer._sess.agent_state = "speaking"
    sw = asyncio.create_task(layer.switch_agent("eng_bot", None))
    await asyncio.sleep(0.01)
    layer._vad.s.fire("start")
    layer._vad.s.fire("inf", 0.30)             # ≥ min_duration 0.25
    await settle()
    assert "bargein.confirmed" in types(layer)
    assert layer.gain.stopped
    assert ("interrupt", True) in layer._sess.calls
    await sw


async def test_false_trigger_restores(layer):
    layer._vad.s.fire("start")
    layer._vad.s.fire("inf", 0.1)
    layer._vad.s.fire("end")
    await settle()
    assert types(layer) == ["vad.start", "bargein.rejected", "vad.end"]
    assert not layer.gain.ducked and not layer.gain.stopped


async def test_interrupt_is_safe_with_no_activity(layer):
    def boom(force=False):
        raise RuntimeError("AgentSession isn't running")
    layer._sess.interrupt = boom
    r = layer.interrupt()
    assert r == {"played_chars": 0}
    assert layer.gain.stopped


async def test_orchestrator_duck_restore_passthrough(layer):
    layer.duck(-12)
    assert layer.gain.ducked
    layer.restore()
    assert not layer.gain.ducked


async def test_subscriber_error_does_not_break_layer(layer):
    layer.subscribe(lambda e: 1 / 0)
    got = []
    layer.subscribe(got.append)
    layer._vad.s.fire("start")
    await settle()
    assert got and got[0]["type"] == "vad.start"
