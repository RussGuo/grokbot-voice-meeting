"""同一轮里问本体（inline principal）：编排器、规划器、文本模拟端到端。live 语音层见 test_inline_principal_live.py。"""
import asyncio

import pytest

from orchestrator import minutes
from orchestrator.chunker import FLUSH
from orchestrator.i18n import inline_principal_lines
from orchestrator.orchestrator import PrincipalUnavailable
from orchestrator.planner import (CallablePlanner, InlineAsk, LLMPlanner, build_planner, safe_ask_line)
from orchestrator.principal import DeferPrincipal, FilePrincipal, MockPrincipal, WebhookPrincipal
from orchestrator.state_machine import Floor
from orchestrator.voice_iface import FakeVoiceLayer

from .helpers import started, types
from .planner_helpers import ScriptedChat, collect, make_ctx, make_meeting
from .test_planner import hdr

ANSWER = "两周内能给出评估。"


# ———————————————————— 编排器 ————————————————————
async def playing(**kw):
    """跑到某个成员 bot（非主持）正在说话，返回 (driver, bot id)。"""
    d = await started(principal_delay_ms=kw.pop("delay", 1000), **kw)
    d.say(11_000, "离线同步的冲突率现在怎么样？工程分身说说。")
    t = 11_000
    while t < 60_000:
        t += 100
        await d.run_until(t)
        o = d.orch
        if o.sm.floor == Floor.PLAYING and o.token.holder_id != o.moderator_id:
            return d, o.token.holder_id
    raise AssertionError("没等到成员 bot 发言")


async def step(d, ms=0):
    await d.run_until(d.clock.now + ms)
    await asyncio.sleep(0)             # 让后台 principal.ask 任务跑一轮
    await d.run_until(d.clock.now)


def events(d, *t):
    return [e for e in d.orch.log if e.type in t]


async def test_inline_answer_goes_to_waiter_not_pending_queue():
    d, pid = await playing()
    o = d.orch
    fut = o.request_principal(pid, "能批多少预算？")
    assert not fut.done() and fut.query_id.startswith("pq-")
    await step(d)
    q = o.queries[fut.query_id]
    assert q.status == "pending" and o.sm.pending_count == 1
    assert o.sm.floor == Floor.PLAYING                      # 话还没说完：floor 不动，不进 AskingPrincipal
    asked = events(d, "PrincipalAsked")[-1]
    assert asked.data["inline"] is True and asked.data["query"]["id"] == q.id
    assert any(a.id == q.id for a in d.orch.principal.asked)
    await step(d, 1000)
    assert fut.done() and fut.result().startswith("（模拟本体答复）")
    assert q.status == "deliveredInMeeting" and o.sm.pending_count == 0
    ans = events(d, "PrincipalAnswered")
    assert len(ans) == 1 and ans[0].data["inline"] is True and ans[0].data["token_id"] == o.token.id
    assert o.sched.s.ready_answers == []                    # 没有 pendingAnswer 举手


async def test_answer_after_waiter_cancelled_takes_the_normal_path():
    d, pid = await playing()
    o = d.orch
    fut = o.request_principal(pid, "能批多少预算？")
    await step(d)
    fut.cancel()                                            # 规划器等不及了（超时 / 被打断）
    await step(d, 1000)
    q = o.queries[fut.query_id]
    assert q.status == "answered" and o.sched.s.ready_answers == [q]
    ans = events(d, "PrincipalAnswered")
    assert len(ans) == 1 and "inline" not in ans[0].data


async def test_ttl_expiry_cancels_the_waiter():
    d, pid = await playing(never_answer=True)
    o = d.orch
    fut = o.request_principal(pid, "能批多少预算？")
    await step(d)
    qid = fut.query_id
    await o._on_timer_principalTtl({"query_id": qid})
    assert fut.cancelled() and o.queries[qid].status == "expired" and o.sm.pending_count == 0
    assert qid not in o._inline_waiters


async def test_meeting_close_cancels_pending_waiters():
    d, pid = await playing(never_answer=True)
    o = d.orch
    fut = o.request_principal(pid, "能批多少预算？")
    await step(d)
    await o._revoke("test")
    o.sm.floor = Floor.FLOOR_IDLE                           # close() 只在空闲时调用
    await o.close()
    assert fut.cancelled() and o.queries[fut.query_id].status == "deferredToMinutes"


async def test_pending_limit_fails_the_future_immediately():
    d, pid = await playing(rules_override={"max_pending_per_bot": 1}, never_answer=True)
    o = d.orch
    first = o.request_principal(pid, "问题一")
    await step(d)
    second = o.request_principal(pid, "问题二")
    assert second.done()                                    # 没经过事件循环：规划器不用垫那句话
    with pytest.raises(PrincipalUnavailable, match="pending_limit"):
        second.result()
    assert len(o.queries) == 1 and not first.done()


async def test_not_the_speaker_or_no_floor_is_refused():
    d, pid = await playing()
    other = next(p.id for p in d.orch.m.participants if p.kind == "bot" and p.id not in (pid, d.orch.moderator_id))
    f = d.orch.request_principal(other, "x")
    assert f.done() and "no_floor" in str(f.exception())
    await d.run_until(300_000)                              # 会议结束后
    f = d.orch.request_principal(pid, "x")
    assert f.done() and isinstance(f.exception(), PrincipalUnavailable)


async def test_state_changed_between_request_and_handling_still_refuses():
    d, pid = await playing()
    o = d.orch
    fut = o.request_principal(pid, "x")
    await o._revoke("test")                                 # 话轮在事件处理前被收回
    await step(d)
    assert fut.done() and isinstance(fut.exception(), PrincipalUnavailable) and not o.queries


async def test_utterance_with_query_id_does_not_ask_again():
    d, pid = await playing(never_answer=True)
    o = d.orch
    fut = o.request_principal(pid, "能批多少预算？")
    await step(d)
    tok = o.token
    await o.handle({"type": "bot.utterance", "token_id": tok.id, "participant_id": pid, "text": "稍等。本体还没回。",
                    "principal_query_id": fut.query_id, "principal_pending": True})
    assert len(o.queries) == 1 and len(events(d, "PrincipalAsked")) == 1
    assert o.sm.floor == Floor.PLAYING                      # 没有被拉进 AskingPrincipal


async def test_utterance_with_ask_principal_alone_still_uses_the_slow_path():
    d, pid = await playing(never_answer=True)
    o = d.orch
    await o.handle({"type": "bot.utterance", "token_id": o.token.id, "participant_id": pid, "text": "我问一下本体。",
                    "ask_principal": "能批多少预算？"})
    assert len(o.queries) == 1 and o.sm.floor == Floor.ASKING_PRINCIPAL
    assert "inline" not in events(d, "PrincipalAsked")[-1].data


async def test_interrupted_turn_requeues_an_answer_that_may_not_have_been_spoken():
    d, pid = await playing()
    o = d.orch
    fut = o.request_principal(pid, "能批多少预算？")
    await step(d)
    await step(d, 1000)
    q = o.queries[fut.query_id]
    assert q.status == "deliveredInMeeting"
    await o._revoke("human_priority")                       # 答复刚交给规划器，话轮就被收回
    assert q.status == "answered" and o.sched.s.ready_answers == [q]
    last = events(d, "PrincipalAnswered")[-1].data
    assert last["inline"] is False and last["requeued"] is True


async def test_completed_turn_keeps_the_inline_delivery():
    d, pid = await playing()
    o = d.orch
    fut = o.request_principal(pid, "能批多少预算？")
    await step(d)
    await step(d, 1000)
    tok = o.token
    await o.handle({"type": "bot.utterance", "token_id": tok.id, "participant_id": pid, "text": "本体说：…",
                    "principal_query_id": fut.query_id, "provenance": "principal", "confidence": 0.95})
    await o._revoke("whatever")                             # 说完之后再收回不影响
    assert o.queries[fut.query_id].status == "deliveredInMeeting" and o.sched.s.ready_answers == []


# ———————————————————— 客户端能力位 ————————————————————
def test_supports_inline_flags(tmp_path):
    noop = lambda *_: None  # noqa: E731
    assert DeferPrincipal().supports_inline is False
    assert MockPrincipal(noop).supports_inline is False
    assert MockPrincipal(noop, inline=True).supports_inline is True
    assert FilePrincipal(noop, tmp_path).supports_inline is True
    assert WebhookPrincipal(noop, "http://127.0.0.1:9/x", directory=tmp_path).supports_inline is True


def test_mock_inline_env(monkeypatch):
    from orchestrator.principal import principal_from_env

    class FakeOrch:
        schedule_timer = staticmethod(lambda *_: None)
        m = None
    monkeypatch.delenv("MEETING_PRINCIPAL", raising=False)
    assert principal_from_env(FakeOrch()).supports_inline is False
    monkeypatch.setenv("MEETING_PRINCIPAL_MOCK_INLINE", "1")
    assert principal_from_env(FakeOrch()).supports_inline is True


# ———————————————————— 规划器（假 hook）————————————————————
class FakeHook:
    """request_principal 的替身：可延迟作答、可立刻拒绝。"""

    def __init__(self, answer=None, delay=0.0, reject=None):
        self.answer, self.delay, self.reject = answer, delay, reject
        self.calls: list[tuple[str, str]] = []
        self.futs: list[asyncio.Future] = []

    def __call__(self, pid, question):
        self.calls.append((pid, question))
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        fut.query_id = f"pq-{len(self.calls)}"
        self.futs.append(fut)
        if self.reject:
            fut.set_exception(PrincipalUnavailable(self.reject))
        elif self.answer is not None:
            loop.call_later(self.delay, lambda: fut.done() or fut.set_result(self.answer))
        return fut


def planner(lang="en", hook=None, inline_ms=2000, chat=None):
    m, b = make_meeting(lang)
    return LLMPlanner(m, b, chat or ScriptedChat([hdr() + "\nnever spoken"]), ask_hook=hook, inline_ms=inline_ms)


async def pieces(ps):
    return [p async for p in ps]


@pytest.mark.parametrize("lang", ["zh", "en"])
async def test_must_ask_answered_in_time_filler_then_verbatim_answer(lang):
    hook = FakeHook(answer=ANSWER, delay=0.03)
    p = planner(lang, hook)
    term = "pricing" if lang == "en" else "定价"
    ps = await p.plan("alice", make_ctx(last_text=f"what about {term}?"))
    out = await pieces(ps)
    L = inline_principal_lines(lang)
    assert out == [L["filler"], FLUSH, L["answer"].format(answer=ANSWER)]
    assert hook.calls == [("alice", f"what about {term}?")]
    assert ps.fields["provenance"] == "principal" and ps.fields["confidence"] == 0.95
    assert ps.fields["principal_query_id"] == "pq-1"
    assert "ask_principal" not in ps.fields and "principal_pending" not in ps.fields
    assert ps.stats["inline"] == "answered" and ps.stats["guard"] == "must_ask"


async def test_fields_are_final_before_the_first_piece():
    hook = FakeHook(answer=ANSWER, delay=0.03)
    ps = await planner("zh", hook).plan("alice", make_ctx(last_text="定价呢"))
    fields = await ps.ready()
    assert fields["principal_query_id"] == "pq-1" and "ask_principal" not in fields


async def test_timeout_line_keeps_query_id_pending_and_never_asks_twice():
    hook = FakeHook(answer=None)
    ps = await planner("zh", hook, inline_ms=60).plan("alice", make_ctx(last_text="定价呢"))
    text = await collect(ps)
    L = inline_principal_lines("zh")
    assert text == L["filler"] + L["timeout"]
    assert ps.fields["principal_pending"] is True and ps.fields["principal_query_id"] == "pq-1"
    assert "ask_principal" not in ps.fields
    assert hook.futs[0].cancelled()                           # 之后到的答复走 pendingAnswer
    assert ps.stats["inline"] == "timeout" and ps.stats["inline_wait_ms"] >= 50


async def test_answer_landing_at_the_instant_of_timeout_is_used(monkeypatch):
    hook = FakeHook(answer=None)
    real_wait = asyncio.wait

    async def racy_wait(fs, timeout=None, **kw):
        await real_wait(fs, timeout=0.01)                    # 超时返回的同一刻，答复落地
        hook.futs[0].set_result(ANSWER)
        return set(), set(fs)

    monkeypatch.setattr(asyncio, "wait", racy_wait)
    ps = await planner("zh", hook).plan("alice", make_ctx(last_text="定价呢"))
    text = await collect(ps)
    assert text.endswith(inline_principal_lines("zh")["answer"].format(answer=ANSWER))
    assert ps.fields["provenance"] == "principal"


async def test_interrupted_while_waiting_cancels_the_future():
    hook = FakeHook(answer=None)
    ps = await planner("zh", hook, inline_ms=5000).plan("alice", make_ctx(last_text="定价呢"))
    got = []

    async def consume():
        async for x in ps:
            got.append(x)

    t = asyncio.ensure_future(consume())
    await asyncio.sleep(0.05)
    assert got[0] == inline_principal_lines("zh")["filler"] and not hook.futs[0].done()
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert hook.futs[0].cancelled()


async def test_generator_closed_while_waiting_cancels_the_future():
    hook = FakeHook(answer=None)
    ps = await planner("zh", hook, inline_ms=5000).plan("alice", make_ctx(last_text="定价呢"))
    it = ps.__aiter__()
    await it.__anext__()
    await ps.aclose()
    await it.aclose()
    assert hook.futs[0].cancelled()


async def test_orchestrator_cancelling_the_waiter_counts_as_no_answer():
    hook = FakeHook(answer=None)
    ps = await planner("zh", hook, inline_ms=2000).plan("alice", make_ctx(last_text="定价呢"))
    asyncio.get_running_loop().call_later(0.03, lambda: hook.futs[0].cancel())   # TTL 先到
    text = await collect(ps)
    assert text.endswith(inline_principal_lines("zh")["timeout"])


async def test_rejected_immediately_falls_back_to_the_old_behaviour_without_filler():
    hook = FakeHook(reject="pending_limit")
    ps = await planner("zh", hook).plan("alice", make_ctx(last_text="定价呢"))
    text = await collect(ps)
    assert text == "「定价」这个我需要问一下本体，稍后补充。"
    assert ps.fields["ask_principal"] == "定价呢" and "principal_query_id" not in ps.fields
    assert ps.stats["inline"] == "unavailable"


async def test_late_refusal_after_the_filler_hands_the_question_to_the_slow_path():
    hook = FakeHook(answer=None)
    ps = await planner("en", hook).plan("alice", make_ctx(last_text="what is the pricing?"))
    asyncio.get_running_loop().call_later(
        0.03, lambda: hook.futs[0].set_exception(PrincipalUnavailable("no_floor")))
    text = await collect(ps)
    assert text.startswith(inline_principal_lines("en")["filler"])
    assert text.endswith("I need to ask my principal about \"pricing\" and will follow up.")
    assert ps.fields["ask_principal"] == "what is the pricing?" and "principal_query_id" not in ps.fields


async def test_disabled_paths_keep_todays_behaviour():
    for hook, ms in ((None, 5000), (FakeHook(answer=ANSWER), 0)):
        ps = await planner("zh", hook, inline_ms=ms).plan("alice", make_ctx(last_text="定价呢"))
        text = await collect(ps)
        assert text == "「定价」这个我需要问一下本体，稍后补充。"
        assert ps.fields["ask_principal"] == "定价呢" and "principal_query_id" not in ps.fields
        assert "inline" not in ps.stats
        if hook is not None:
            assert hook.calls == []


async def test_inactive_hook_is_treated_as_disabled():
    class Late(FakeHook):
        active = False

    hook = Late(answer=ANSWER)
    ps = await planner("zh", hook).plan("alice", make_ctx(last_text="定价呢"))
    assert (await collect(ps)).startswith("「定价」") and hook.calls == []


async def test_header_ask_principal_closes_the_stream_and_asks_inline():
    chat = ScriptedChat([hdr(act="ask_principal", question="What is the unit price?", confidence=0.4) + "\n",
                         "I will check and follow up.", " more"], delay=0.01)
    hook = FakeHook(answer="It is 12 dollars.", delay=0.03)
    ps = await planner("en", hook, chat=chat).plan("alice", make_ctx(last_text="How much does it cost?"))
    text = await collect(ps)
    L = inline_principal_lines("en")
    assert text == L["filler"] + L["answer"].format(answer="It is 12 dollars.")
    assert hook.calls == [("alice", "What is the unit price?")]
    assert ps.fields["principal_query_id"] == "pq-1" and "ask_principal" not in ps.fields
    assert chat.closed == 1


async def test_invalid_commit_guard_asks_inline_and_promises_nothing():
    chat = ScriptedChat([hdr(act="commit", commit="give a refund") + "\n", "I promise a refund."])
    hook = FakeHook(answer="No refunds.", delay=0.02)
    ps = await planner("en", hook, chat=chat).plan("alice", make_ctx())
    text = await collect(ps)
    assert "refund." not in text.replace("No refunds.", "") and text.endswith("My principal says: No refunds.")
    assert ps.fields["commitments"] == [] and ps.stats["guard"] == "invalid_commit"
    assert hook.calls[0][1].startswith("May I commit to: give a refund")


async def test_low_confidence_guard_asks_inline():
    chat = ScriptedChat([hdr(confidence=0.3) + "\n", "Probably around 12."])
    hook = FakeHook(answer="12 dollars.", delay=0.02)
    ps = await planner("en", hook, chat=chat).plan("alice", make_ctx(addressed=True))
    text = await collect(ps)
    assert text.endswith("My principal says: 12 dollars.") and "Probably" not in text
    assert ps.stats["guard"] == "low_confidence"


async def test_llm_fallback_that_asks_the_principal_also_goes_inline():
    hook = FakeHook(answer="12 dollars.", delay=0.02)
    chat = ScriptedChat(["x"], error_after=0)                  # 头读不到 → 回退 FakeLLM；被点名且无依据 → 问本体
    ps = await planner("en", hook, chat=chat).plan("alice", make_ctx(addressed=True, last_text="How is it going?"))
    text = await collect(ps)
    assert text.endswith("My principal says: 12 dollars.") and ps.stats["fallback"].startswith("error")
    assert ps.fields["principal_query_id"] == "pq-1" and "ask_principal" not in ps.fields


async def test_plain_answers_never_touch_the_hook():
    hook = FakeHook(answer=ANSWER)
    chat = ScriptedChat([hdr() + "\n", "All on track."])
    ps = await planner("en", hook, chat=chat).plan("alice", make_ctx())
    assert await collect(ps) == "All on track." and hook.calls == []


async def test_callable_planner_with_fake_llm_goes_inline_too():
    m, b = make_meeting("zh")
    hook = FakeHook(answer=ANSWER, delay=0.02)
    pl = build_planner(m, b, None, "zh", ask_hook=hook)
    assert isinstance(pl, CallablePlanner) and pl.ask is not None
    ps = await pl.plan("alice", make_ctx(last_text="定价呢"))
    text = await collect(ps)
    assert text == "稍等，我问一下本体。本体说：" + ANSWER
    assert build_planner(m, b, None, "zh").ask is None
    assert build_planner(m, b, None, "zh", ask_hook=hook, inline_ms=0).ask is None


def test_inline_ask_enabled_flags():
    assert not InlineAsk(None, 5000, "zh").enabled and not InlineAsk(lambda *_: None, 0, "zh").enabled
    assert InlineAsk(lambda *_: None, 1, "en").enabled


# ———————————————————— 文本模拟端到端 ————————————————————
class InlineSimVoice(FakeVoiceLayer):
    """FakeVoiceLayer + 真实 CallablePlanner/InlineAsk：要问本体的话轮由后台任务驱动（和真实 producer 一样与编排器并发）。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.planner = None
        self.tasks: list[asyncio.Task] = []

    async def speak(self, token_id, participant_id, ctx):
        if self.planner is None or not self.generator(participant_id, ctx).get("ask_principal"):
            return await super().speak(token_id, participant_id, ctx)
        ps = await self.planner.plan(participant_id, ctx)
        self.calls.append(("speak", token_id, participant_id))
        self.inject({"type": "tts.first_audio", "token_id": token_id, "participant_id": participant_id},
                    at=self.clock() + self.first_audio_ms)

        async def drive():
            text = "".join([p async for p in ps])
            at = self.clock() + self.first_audio_ms
            fields = {k: v for k, v in ps.fields.items()}
            self.inject({"type": "bot.utterance", "token_id": token_id, "participant_id": participant_id,
                         "text": text, **fields}, at=at)
            self.inject({"type": "tts.done", "token_id": token_id, "participant_id": participant_id}, at=at)
        self.tasks.append(asyncio.ensure_future(drive()))


async def sim(delay_ms, inline_ms, answer=ANSWER):
    d = await started(principal_delay_ms=delay_ms, principal_answers={"principal-pm": answer})
    voice = InlineSimVoice(d.clock, d.llm)
    voice._subs = d.voice._subs                                 # 沿用编排器已订阅的回调
    d.voice = d.orch.voice = voice
    d.orch.principal.supports_inline = True
    voice.planner = CallablePlanner(d.llm, InlineAsk(d.orch.request_principal, inline_ms, "zh"))
    d.say(11_000, "离线同步的冲突率现在怎么样？工程分身说说。")
    d.say(38_000, "产品分身，离线同步的预算能批多少？")            # mustAsk → 问本体
    return d, voice


async def pump(d, t_end, step=250):
    t = d.clock.now
    while t < t_end:
        t = min(t + step, t_end)
        await d.run_until(t)
        for _ in range(4):
            await asyncio.sleep(0)


async def test_e2e_principal_answers_in_the_same_turn():
    d, voice = await sim(delay_ms=1500, inline_ms=5000)
    await pump(d, 60_000)
    o = d.orch
    asked = events(d, "PrincipalAsked")
    assert len(asked) == 1 and asked[0].data["inline"] is True
    inline = [e for e in events(d, "PrincipalAnswered") if e.data.get("inline")]
    assert len(inline) == 1 and inline[0].data["query"]["status"] == "deliveredInMeeting"
    utts = [e.data["utterance"] for e in events(d, "UtteranceFinal") if e.data["utterance"]["speaker_id"] == "pm_bot"
            and "本体说" in e.data["utterance"]["text"]]
    assert len(utts) == 1
    u = utts[0]
    assert u["text"].startswith("稍等，我问一下本体。本体说：") and ANSWER in u["text"]
    assert u["provenance"] == "principal" and u["confidence"] == 0.95
    assert o.sched.s.ready_answers == []
    assert not any(e.data["token"]["source"] == "pendingAnswer" for e in events(d, "FloorGranted"))
    assert len(o.queries) == 1 and o.sm.pending_count == 0
    md = minutes.render_md(minutes.build(o.log))
    assert any("【当场答复】" in line and ANSWER in line for line in md.splitlines())


async def test_e2e_timeout_then_the_late_answer_is_relayed_in_a_later_turn():
    d, voice = await sim(delay_ms=30_000, inline_ms=80)
    await pump(d, 44_000, step=500)
    await asyncio.sleep(0.2)                                     # 真实时间过去，规划器预算（80ms）到期
    await pump(d, 46_000, step=500)
    o = d.orch
    ours = [e.data["utterance"] for e in events(d, "UtteranceFinal")
            if e.data["utterance"]["speaker_id"] == "pm_bot" and "稍等" in e.data["utterance"]["text"]]
    assert len(ours) == 1 and ours[0]["text"].endswith(inline_principal_lines("zh")["timeout"])
    assert len(events(d, "PrincipalAsked")) == 1 and o.sm.pending_count == 1       # 没有第二次提问
    await pump(d, 120_000, step=500)
    q = o.queries["pq-1"]
    assert q.status == "deliveredInMeeting" and len(o.queries) == 1
    granted = [e for e in events(d, "FloorGranted") if e.data["token"]["source"] == "pendingAnswer"]
    assert granted, types(d)
    relayed = [e.data["utterance"] for e in events(d, "UtteranceFinal") if "本体回复了" in e.data["utterance"]["text"]]
    assert relayed and ANSWER in relayed[0]["text"]
    assert not [e for e in events(d, "PrincipalAnswered") if e.data.get("inline")]
    md = minutes.render_md(minutes.build(o.log))
    assert "当场答复" not in md and "【本体答复插回】" in md


async def test_e2e_without_the_hook_nothing_changes():
    d, voice = await sim(delay_ms=1500, inline_ms=0)
    voice.planner = CallablePlanner(d.llm, None)
    await pump(d, 60_000)
    assert not [e for e in events(d, "PrincipalAsked") if e.data.get("inline")]
    assert not any("稍等" in e.data["utterance"]["text"] for e in events(d, "UtteranceFinal"))


def test_safe_line_is_unchanged():
    assert safe_ask_line("zh") == "这个我需要和本体确认后再答复。"


async def test_flush_marker_sends_the_buffered_half_sentence_without_changing_text():
    from orchestrator.chunker import pump_segments
    got: list[str] = []

    async def src():
        yield "One moment, let me check with my principal."      # 英文句号不是 SENT_END
        yield FLUSH
        yield "Done."
    stats = await pump_segments(src(), "t", lambda _t: True, lambda seg: got.append(seg.text))
    assert "".join(got) == "One moment, let me check with my principal.Done." and stats["sent"] == len(got)
    assert got[-2].endswith("principal.") and got[-1] == "Done."


async def test_worker_late_bound_hook_and_budget(monkeypatch):
    pytest.importorskip("livekit.agents")
    from meet import worker
    hook = worker._LateAskHook()
    assert hook.active is False and hook("alice", "q") is None
    ask = InlineAsk(hook, 5000, "zh")
    assert not ask.enabled                                     # 没绑定编排器（或本体是 defer）：规划器行为不变
    d, pid = await playing()
    hook.orch = d.orch
    assert ask.enabled and hook(pid, "q?").query_id.startswith("pq-")
    monkeypatch.delenv("MEETING_PRINCIPAL_INLINE_MS", raising=False)
    assert worker.inline_budget_ms() == 5000
    for raw, want in (("0", 0), ("1500", 1500), ("-3", 0), ("abc", 5000), ("", 5000)):
        monkeypatch.setenv("MEETING_PRINCIPAL_INLINE_MS", raw)
        assert worker.inline_budget_ms() == want
