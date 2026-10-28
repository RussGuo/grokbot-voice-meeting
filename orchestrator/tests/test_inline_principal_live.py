"""MeetingVoiceLayer.speak 里的当场问本体：事件顺序（tts.segment… → bot.utterance → tts.done）与字段。"""
import asyncio

from orchestrator.i18n import inline_principal_lines
from orchestrator.planner import LLMPlanner

from .planner_helpers import ScriptedChat, make_ctx, make_meeting
from .test_inline_principal import FakeHook
from .test_live_voice_planner import _Session, finish, make_layer  # importorskip livekit 在被导入的模块里
from .test_planner import hdr


def make(hook, lang="en", inline_ms=2000, sess=None, chat=None):
    m, b = make_meeting(lang)
    planner = LLMPlanner(m, b, chat or ScriptedChat([hdr() + "\nnever"]), ask_hook=hook, inline_ms=inline_ms)
    sess = sess or _Session()
    layer, events = make_layer(planner, sess, lang=lang)
    return layer, events, sess


async def test_answered_in_time_event_order_and_fields():
    hook = FakeHook(answer="It is 12 dollars per seat.", delay=0.05)
    layer, events, sess = make(hook)
    await layer.speak("tok-1", "alice", make_ctx(last_text="what is the pricing?"))
    await finish(layer, sess)
    L = inline_principal_lines("en")
    kinds = [e["type"] for e in events]
    assert kinds[-2:] == ["bot.utterance", "tts.done"] and set(kinds[:-2]) == {"tts.segment"}
    segs = [e["text"] for e in events if e["type"] == "tts.segment"]
    k = next(i for i in range(1, len(segs) + 1) if "".join(segs[:i]) == L["filler"])   # 先垫的那句整句先出去
    assert k < len(segs)
    assert "".join(segs) == L["filler"] + L["answer"].format(answer="It is 12 dollars per seat.")
    utt = events[-2]
    assert utt["text"] == "".join(segs) and utt["interrupted"] is False
    assert utt["provenance"] == "principal" and utt["confidence"] == 0.95
    assert utt["principal_query_id"] == "pq-1" and "ask_principal" not in utt and "principal_pending" not in utt
    assert sess.handles[0].spoken == segs
    st = layer.llm_stats[0]
    assert st["inline"] == "answered" and st["inline_wait_ms"] >= 40 and st["guard"] == "must_ask"
    assert not layer._bg and not hook.futs[0].cancelled()


async def test_timeout_utterance_is_pending_without_ask_principal():
    hook = FakeHook(answer=None)
    layer, events, sess = make(hook, "zh", inline_ms=60)
    await layer.speak("tok-1", "alice", make_ctx(last_text="定价是多少"))
    await finish(layer, sess)
    L = inline_principal_lines("zh")
    utt = events[-2]
    assert [e["type"] for e in events][-2:] == ["bot.utterance", "tts.done"]
    assert utt["text"] == L["filler"] + L["timeout"]
    assert utt["principal_query_id"] == "pq-1" and utt["principal_pending"] is True
    assert "ask_principal" not in utt and utt["provenance"] == "model"
    assert hook.futs[0].cancelled() and layer.llm_stats[0]["inline"] == "timeout"


async def test_interrupted_while_waiting_cancels_the_future_and_keeps_the_query_id():
    hook = FakeHook(answer=None)
    layer, events, sess = make(hook, inline_ms=5000)
    await layer.speak("tok-1", "alice", make_ctx(last_text="what is the pricing?"))
    await asyncio.sleep(0.05)
    assert not hook.futs[0].done()
    said = "".join(e["text"] for e in events if e["type"] == "tts.segment")
    assert said == inline_principal_lines("en")["filler"]      # 等答复期间，垫的那句已整句送出（英文句号不在 SENT_END，靠 FLUSH）
    h = sess.handles[0]
    h.interrupted = True
    h.task.cancel()
    for cb in h._cbs:                                          # 模拟 SpeechHandle 被打断后触发回调
        cb(h)
    await asyncio.sleep(0.05)
    assert hook.futs[0].cancelled()                            # 迟到的答复由编排器走 pendingAnswer
    assert [e["type"] for e in events][-2:] == ["bot.utterance", "tts.done"]
    utt = events[-2]
    assert utt["interrupted"] is True and utt["principal_query_id"] == "pq-1" and "ask_principal" not in utt
    assert layer.llm_stats[0]["inline"] == "cancelled" and not layer._bg


async def test_without_hook_the_utterance_is_the_old_safe_line_with_ask_principal():
    layer, events, sess = make(None)
    await layer.speak("tok-1", "alice", make_ctx(last_text="what is the pricing?"))
    await finish(layer, sess)
    utt = events[-2]
    assert utt["ask_principal"] == "what is the pricing?" and "principal_query_id" not in utt
    assert "My principal" not in utt["text"] and "inline" not in layer.llm_stats[0]


async def test_rejected_hook_gives_the_old_line_with_no_filler():
    hook = FakeHook(reject="pending_limit")
    layer, events, sess = make(hook)
    await layer.speak("tok-1", "alice", make_ctx(last_text="what is the pricing?"))
    await finish(layer, sess)
    utt = events[-2]
    assert utt["ask_principal"] == "what is the pricing?" and not utt["text"].startswith("One moment")


async def test_non_ask_turns_are_untouched():
    hook = FakeHook(answer="x")
    layer, events, sess = make(hook, chat=ScriptedChat([hdr() + "\n", "All on track."]))
    await layer.speak("tok-1", "alice", make_ctx())
    await finish(layer, sess)
    assert hook.calls == [] and events[-2]["text"] == "All on track." and "principal_query_id" not in events[-2]
