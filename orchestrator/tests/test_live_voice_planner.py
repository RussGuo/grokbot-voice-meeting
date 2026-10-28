"""MeetingVoiceLayer.speak 用脚本化 chat 与假 session 的离线测试：事件契约、令牌失效、字段来源、统计。"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_VOICE = str(Path(__file__).resolve().parents[2] / "voice")
if _VOICE not in sys.path:
    sys.path.insert(0, _VOICE)
pytest.importorskip("livekit.agents")

from voice_layer.config import VoiceConfig  # noqa: E402
from voice_layer.livekit_layer import LiveKitVoiceLayer  # noqa: E402

from orchestrator.fake_llm import FakeLLM  # noqa: E402
from orchestrator.live_voice import MeetingVoiceLayer  # noqa: E402
from orchestrator.planner import LLMPlanner  # noqa: E402

from .planner_helpers import ScriptedChat, make_ctx, make_meeting  # noqa: E402
from .test_planner import hdr  # noqa: E402


class _Vad:
    """永不触发事件的 VAD 流（HumanVad 的循环只需要它能 async for）。"""

    def stream(self):
        return self

    def push_frame(self, _f):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Event().wait()

    async def aclose(self):
        pass


class _Handle:
    def __init__(self, gen, hold: asyncio.Event | None):
        self.interrupted = False
        self._cbs = []
        self.spoken: list[str] = []
        self.task = asyncio.ensure_future(self._run(gen, hold))

    async def _run(self, gen, hold):
        async for t in gen:
            self.spoken.append(t)
            if hold is not None:
                await hold.wait()
        for cb in self._cbs:
            cb(self)

    def add_done_callback(self, cb):
        self._cbs.append(cb)


class _Session:
    def __init__(self, hold=None):
        self.agent_state = "listening"
        self.hold = hold
        self.handles: list[_Handle] = []

    def on(self, *_a):
        pass

    def say(self, gen, add_to_chat_ctx=False):
        h = _Handle(gen, self.hold)
        self.handles.append(h)
        return h

    def interrupt(self, force=False):
        pass


def make_layer(planner, session=None, lang="en", pid="alice"):
    cfg = VoiceConfig()
    cfg.tts_provider = "local"
    layer = MeetingVoiceLayer(cfg, session or _Session(), vad=_Vad(), planner=planner,
                              moderator_id="moderator", language=lang)
    layer.current = SimpleNamespace(bot_id=pid)
    events = []
    layer.bus.subscribe(events.append)
    return layer, events


async def finish(layer, session):
    for h in session.handles:
        await h.task
    await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _local_voice(monkeypatch):
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "local")
    monkeypatch.setenv("VOICE_ASR_PROVIDER", "local")


async def test_speak_events_order_fields_and_stats():
    m, b = make_meeting("en")
    chat = ScriptedChat([hdr(act="commit", commit="send the Q3 report", provenance="brief", confidence=0.9) + "\n",
                         "Yes, I will send the Q3 report. ", "It goes out Friday."])
    sess = _Session()
    layer, events = make_layer(LLMPlanner(m, b, chat), sess)
    await layer.speak("tok-1", "alice", make_ctx(last_text="Can you send the Q3 report?"))
    await finish(layer, sess)
    types = [e["type"] for e in events]
    assert types.count("tts.segment") >= 2
    assert types[-2:] == ["bot.utterance", "tts.done"]               # utterance 先于 done
    utt = events[-2]
    assert utt["text"] == "Yes, I will send the Q3 report. It goes out Friday."
    assert "act" not in utt["text"] and "{" not in utt["text"]
    assert utt["provenance"] == "brief" and utt["confidence"] == 0.9
    assert utt["commitments"] == ["send the Q3 report"] and utt["addressees"] == []
    assert "ask_principal" not in utt and utt["interrupted"] is False
    assert sess.handles[0].spoken == [e["text"] for e in events if e["type"] == "tts.segment"]
    st = layer.llm_stats[0]
    assert st["pid"] == "alice" and st["llm"] is True and st["ok"] is True
    assert st["header_ms"] >= 0 and st["first_seg_ms"] >= st["header_ms"] and st["total_ms"] >= st["first_seg_ms"]
    assert not layer._bg                                           # producer 任务引用已释放


async def test_invalid_commit_utterance_has_ask_principal_and_no_promise():
    m, b = make_meeting("en")
    chat = ScriptedChat([hdr(act="commit", commit="give a refund") + "\n", "I promise a refund."])
    sess = _Session()
    layer, events = make_layer(LLMPlanner(m, b, chat), sess)
    await layer.speak("tok-1", "alice", make_ctx())
    await finish(layer, sess)
    utt = next(e for e in events if e["type"] == "bot.utterance")
    assert utt["text"] == "I need to check that with my principal and get back to you."
    assert utt["commitments"] == [] and "give a refund" in utt["ask_principal"]
    assert layer.llm_stats[0]["guard"] == "invalid_commit"


async def test_fallback_recorded_in_stats_and_fields_from_fake():
    m, b = make_meeting("zh")
    sess = _Session()
    layer, events = make_layer(LLMPlanner(m, b, ScriptedChat(["没有头的话"])), sess, lang="zh")
    ctx = make_ctx(last_text="预算怎么样", system="你是 Alice")
    await layer.speak("tok-1", "alice", ctx)
    await finish(layer, sess)
    utt = next(e for e in events if e["type"] == "bot.utterance")
    fake = FakeLLM(m, b)("alice", ctx)
    assert utt["text"] == fake["text"] and utt["provenance"] == fake["provenance"]
    st = layer.llm_stats[0]
    assert st["fallback"] == "no_header" and st["ok"] is False


async def test_legacy_sync_callable_planner_still_works():
    m, b = make_meeting("zh")
    fake = FakeLLM(m, b)
    sess = _Session()
    layer, events = make_layer(fake, sess, lang="zh")
    ctx = make_ctx(last_text="budget.q3 呢", system="x")
    await layer.speak("tok-1", "alice", ctx)
    await finish(layer, sess)
    utt = next(e for e in events if e["type"] == "bot.utterance")
    assert utt["text"] == fake("alice", ctx)["text"]
    assert [e["type"] for e in events][-2:] == ["bot.utterance", "tts.done"]
    assert layer.llm_stats[0]["llm"] is False


async def test_token_revoked_stops_and_closes_llm_stream():
    m, b = make_meeting("en")
    pieces = [hdr() + "\n"] + ["Sentence number %d. " % i for i in range(20)]
    chat = ScriptedChat(pieces, delay=0.01)
    sess = _Session()
    layer, events = make_layer(LLMPlanner(m, b, chat), sess)
    valid = {"tok-1": True}
    layer.token_valid = lambda t: valid.get(t, False)
    await layer.speak("tok-1", "alice", make_ctx())
    await asyncio.sleep(0.08)
    valid["tok-1"] = False                       # 令牌被收回
    await finish(layer, sess)
    await asyncio.sleep(0.05)
    segs = [e for e in events if e["type"] == "tts.segment"]
    assert 0 < len(segs) < 10
    assert chat.closed == 1                      # 上游 LLM 流已关闭
    assert not layer._bg


async def test_interrupted_speech_cancels_producer_and_closes_stream():
    m, b = make_meeting("en")
    chat = ScriptedChat([hdr() + "\n"] + ["Sentence number %d. " % i for i in range(50)], delay=0.01)
    hold = asyncio.Event()                       # TTS 卡住不消费
    sess = _Session(hold)
    layer, events = make_layer(LLMPlanner(m, b, chat), sess)
    await layer.speak("tok-1", "alice", make_ctx())
    await asyncio.sleep(0.05)
    h = sess.handles[0]
    h.interrupted = True
    h.task.cancel()
    for cb in h._cbs:                            # 模拟 SpeechHandle 被打断后触发回调
        cb(h)
    await asyncio.sleep(0.05)
    assert [e["type"] for e in events][-2:] == ["bot.utterance", "tts.done"]
    assert events[-2]["interrupted"] is True
    assert chat.closed == 1 and not layer._bg


async def test_must_ask_no_llm_call_through_speak():
    m, b = make_meeting("en")
    chat = ScriptedChat([hdr() + "\nnever"])
    sess = _Session()
    layer, events = make_layer(LLMPlanner(m, b, chat), sess)
    await layer.speak("tok-1", "alice", make_ctx(last_text="what is the pricing?"))
    await finish(layer, sess)
    utt = next(e for e in events if e["type"] == "bot.utterance")
    assert chat.calls == [] and utt["ask_principal"] == "what is the pricing?"
    assert layer.llm_stats[0]["guard"] == "must_ask"


def test_meeting_voice_layer_is_a_livekit_layer():
    assert issubclass(MeetingVoiceLayer, LiveKitVoiceLayer)
