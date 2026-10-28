import asyncio
import re

import pytest

from orchestrator.fake_llm import FakeLLM
from orchestrator.i18n import moderator_lines
from orchestrator.planner import (CallablePlanner, HeaderError, HeaderParser, LLMPlanner, as_planner, build_planner,
                                  match_commit, safe_ask_line)

from .planner_helpers import ScriptedChat, collect, make_ctx, make_meeting

CJK = re.compile(r"[一-鿿]")


def hdr(**kw):
    import json
    h = {"act": "speak", "question": None, "commit": None, "confidence": 0.9, "provenance": "brief",
         "addressees": []}
    h.update(kw)
    return json.dumps(h)


def mk(chat, lang="en", **kw):
    m, b = make_meeting(lang)
    return LLMPlanner(m, b, chat, **kw), m, b


async def run(planner, pid, ctx):
    ps = await planner.plan(pid, ctx)
    text = await collect(ps)
    return text, ps


async def test_header_split_across_chunks_and_never_spoken():
    h = hdr(addressees=["host"], provenance="snapshot", confidence=0.85)
    cut = [h[:7], h[7:20], h[20:-5], h[-5:] + "\n", "Budget is ", "300k, ", "on track."]
    chat = ScriptedChat(cut)
    p, _, _ = mk(chat)
    text, ps = await run(p, "alice", make_ctx())
    assert text == "Budget is 300k, on track."
    assert "{" not in text and "act" not in text
    assert ps.fields["provenance"] == "snapshot" and ps.fields["confidence"] == 0.85
    assert ps.fields["addressees"] == ["host"]
    assert ps.fields["commitments"] == [] and "ask_principal" not in ps.fields
    assert ps.stats["header_ms"] >= 0 and "fallback" not in ps.stats
    assert chat.calls[0][1]["thinking"] == "disabled" and chat.calls[0][1]["max_tokens"] <= 200


async def test_header_and_text_in_one_chunk_without_newline_and_fenced():
    p, _, _ = mk(ScriptedChat([hdr() + "Fine, thanks."]))
    text, _ = await run(p, "alice", make_ctx())
    assert text == "Fine, thanks."
    p, _, _ = mk(ScriptedChat(["```json\n", hdr(), "\n```\n", "All good."]))
    text, ps = await run(p, "alice", make_ctx())
    assert text == "All good." and "fallback" not in ps.stats


async def test_ready_returns_fields_before_first_speech():
    p, _, _ = mk(ScriptedChat([hdr(confidence=0.77) + "\n", "Hello."]))
    ps = await p.plan("alice", make_ctx())
    fields = await ps.ready()
    assert fields["confidence"] == 0.77
    assert await collect(ps) == "Hello."


async def test_unknown_addressee_dropped_and_values_clamped():
    p, _, _ = mk(ScriptedChat([hdr(addressees=["host", "ghost", "alice"], confidence=7, provenance="??") + "\nOk."]))
    _, ps = await run(p, "alice", make_ctx())
    assert ps.fields["addressees"] == ["host"]
    assert ps.fields["confidence"] == 1.0 and ps.fields["provenance"] == "model"


@pytest.mark.parametrize("pieces,reason", [
    (["I think the budget is fine."], "no_header"),
    (['{"act": "speak", "x": "' + "y" * 500], "header_too_long"),
    ([hdr(act="dance") + "\nhi"], "bad_act"),
    ([hdr()], "empty_speech"),
    ([], "no_header"),
])
async def test_malformed_header_or_empty_falls_back_to_fake(pieces, reason):
    chat = ScriptedChat(pieces)
    p, m, b = mk(chat)
    ctx = make_ctx()
    text, ps = await run(p, "alice", ctx)
    assert ps.stats["fallback"] == reason
    assert text == FakeLLM(m, b)("alice", ctx)["text"] or CJK.search(text) is None   # en 会议用英文回退句
    assert ps.fields["provenance"] in ("brief", "snapshot", "model")
    assert chat.closed == 1


async def test_fallback_text_matches_fake_plan_zh():
    m, b = make_meeting("zh")
    p = LLMPlanner(m, b, ScriptedChat(["no header here"]))
    ctx = make_ctx()
    text, ps = await run(p, "alice", ctx)
    fake = FakeLLM(m, b)("alice", ctx)
    assert text == fake["text"]
    assert ps.fields["confidence"] == fake["confidence"]


async def test_invalid_commit_switches_to_safe_ask_and_promise_not_spoken():
    chat = ScriptedChat([hdr(act="commit", commit="refund everything") + "\n", "Sure, I promise a full refund.",
                         " Done."])
    p, _, _ = mk(chat, "en")
    text, ps = await run(p, "alice", make_ctx())
    assert text == safe_ask_line("en")
    assert "refund" not in text
    assert ps.fields["commitments"] == []
    assert "refund everything" in ps.fields["ask_principal"]
    assert ps.stats["guard"] == "invalid_commit"
    assert chat.closed == 1                       # 中止了上游流


async def test_commit_act_without_commit_text_is_invalid():
    p, _, _ = mk(ScriptedChat([hdr(act="commit") + "\nI promise."]))
    text, ps = await run(p, "alice", make_ctx(last_text="Can you do it?"))
    assert text == safe_ask_line("en") and ps.fields["commitments"] == []
    assert ps.fields["ask_principal"]


async def test_valid_commit_sets_commitments_with_canonical_text():
    chat = ScriptedChat([hdr(act="commit", commit="Send the Q3 Report!") + "\n", "Yes, I'll send ", "the Q3 report."])
    p, _, _ = mk(chat)
    text, ps = await run(p, "alice", make_ctx())
    assert text == "Yes, I'll send the Q3 report."
    assert ps.fields["commitments"] == ["send the Q3 report"]
    assert "ask_principal" not in ps.fields


def test_match_commit_normalizes_and_substring_both_ways():
    can = ["send the Q3 report", "发送季度报告"]
    assert match_commit("  SEND the q3 report. ", can) == "send the Q3 report"
    assert match_commit("I will send the Q3 report by Friday", can) == "send the Q3 report"
    assert match_commit("发送 季度报告", can) == "发送季度报告"
    assert match_commit("approve budget", can) is None
    assert match_commit("a", ["a"]) is None
    assert match_commit("", can) is None


async def test_must_ask_precheck_skips_llm():
    chat = ScriptedChat([hdr() + "\nWe charge 5 dollars."])
    p, _, _ = mk(chat)
    text, ps = await run(p, "alice", make_ctx(last_text="What about our Pricing model?"))
    assert chat.calls == []
    assert ps.fields["ask_principal"] == "What about our Pricing model?"
    assert ps.fields["confidence"] == 0.3 and ps.stats["guard"] == "must_ask"
    assert "pricing" in text.lower()


async def test_must_ask_uses_directive_text_zh():
    chat = ScriptedChat([hdr() + "\nx"])
    p, _, _ = mk(chat, "zh")
    text, ps = await run(p, "alice", make_ctx(last_text="别的", directive_text="说说定价"))
    assert chat.calls == [] and "本体" in text and ps.fields["ask_principal"] == "说说定价"


async def test_relay_skips_llm_and_speaks_answer_verbatim():
    chat = ScriptedChat([hdr() + "\nignored"])
    p, _, _ = mk(chat)
    text, ps = await run(p, "alice", make_ctx(intent="relay", answer="We can go to 350k."))
    assert chat.calls == []
    assert text == "My principal replied: We can go to 350k."
    assert ps.fields["provenance"] == "principal"
    m, b = make_meeting("zh")
    p = LLMPlanner(m, b, chat)
    text, _ = await run(p, "alice", make_ctx(intent="relay", answer="可以到35万"))
    assert text == "本体回复了：可以到35万" and chat.calls == []


async def test_header_timeout_falls_back():
    chat = ScriptedChat([hdr() + "\nhi"], first_delay=0.5)
    p, m, b = mk(chat, header_timeout=0.05)
    ctx = make_ctx()
    text, ps = await run(p, "alice", ctx)
    assert ps.stats["fallback"] == "header_timeout"
    assert text
    assert "header_ms" not in ps.stats


async def test_llm_error_before_header_falls_back_and_midstream_error_truncates():
    p, _, _ = mk(ScriptedChat(["x"], error_after=0))
    _, ps = await run(p, "alice", make_ctx())
    assert ps.stats["fallback"] == "error:RuntimeError"
    chat = ScriptedChat([hdr() + "\n", "Part one. ", "Part two."], error_after=2)
    p, _, _ = mk(chat)
    text, ps = await run(p, "alice", make_ctx())
    assert text == "Part one. " and "fallback" not in ps.stats and ps.stats["truncated"] == "error:RuntimeError"


async def test_stall_after_speech_started_truncates_not_falls_back():
    chat = ScriptedChat([hdr() + "\nFirst.", "Second."], delay=0.0)

    async def slow(messages, **kw):
        yield hdr() + "\nFirst."
        await asyncio.sleep(1)
        yield "Second."

    chat.astream_text = slow
    p, _, _ = mk(chat, stall_timeout=0.05)
    text, ps = await run(p, "alice", make_ctx())
    assert text == "First." and ps.stats["truncated"] == "stall_timeout"


async def test_low_confidence_when_addressed_asks_principal():
    chat = ScriptedChat([hdr(confidence=0.4) + "\n", "Probably around 200k."])
    p, _, _ = mk(chat)
    text, ps = await run(p, "alice", make_ctx(addressed=True))
    assert text == safe_ask_line("en") and "200k" not in text
    assert ps.stats["guard"] == "low_confidence" and ps.fields["ask_principal"]
    # 没被点名的低把握：照常说，不强制问本体
    chat = ScriptedChat([hdr(confidence=0.4) + "\n", "Probably around 200k."])
    p, _, _ = mk(chat)
    text, ps = await run(p, "alice", make_ctx(addressed=False))
    assert text == "Probably around 200k." and "ask_principal" not in ps.fields


async def test_ask_principal_act_carries_question_and_speech():
    chat = ScriptedChat([hdr(act="ask_principal", question="What is the Q4 target?", confidence=0.3) + "\n",
                         "Let me check on that."])
    p, _, _ = mk(chat)
    text, ps = await run(p, "alice", make_ctx())
    assert text == "Let me check on that."
    assert ps.fields["ask_principal"] == "What is the Q4 target?"


async def test_english_meeting_prompt_and_lines_are_english():
    chat = ScriptedChat([hdr() + "\nOk."])
    p, _, _ = mk(chat, "en")
    ctx = make_ctx(directive_text="Tell us the status")
    await run(p, "alice", ctx)
    turn = chat.calls[0][0][-1]["content"]
    assert "Output format" in turn and "Speaking rules" in turn
    assert not CJK.search(turn)
    assert "send the Q3 report" in turn and "pricing" in turn           # mandate 列表在 prompt 里
    assert "bob=Bob" in turn and "alice=" not in turn
    # 回退句也是英文
    p, _, _ = mk(ScriptedChat(["no header"]), "en")
    text, ps = await run(p, "alice", make_ctx(last_text="budget.q3 status?"))
    assert not CJK.search(text), text
    assert ps.stats["fallback"] == "no_header"
    p, _, _ = mk(ScriptedChat(), "en")
    text, _ = await run(p, "alice", make_ctx(last_text="our pricing?"))
    assert not CJK.search(text)


async def test_zh_meeting_prompt_is_chinese_and_mentions_mandate():
    chat = ScriptedChat([hdr() + "\n好的。"])
    p, _, _ = mk(chat, "zh")
    await run(p, "alice", make_ctx())
    turn = chat.calls[0][0][-1]["content"]
    assert "输出格式" in turn and "发送季度报告" in turn and "定价" in turn


@pytest.mark.parametrize("intent", ["open", "open_item", "confirm_end", "recap", "ask_human", "cue", "warn",
                                    "directive", "something_else"])
@pytest.mark.parametrize("lang", ["zh", "en"])
async def test_moderator_procedural_intents_use_templates(intent, lang):
    chat = ScriptedChat(["should not be used"])
    p, m, b = mk(chat, lang)
    ctx = make_ctx("moderator", intent, agenda_title="Q3 budget", agenda_item_id="ag-1", recap_item="R",
                   target_name="Bob")
    text, ps = await run(p, "moderator", ctx)
    assert chat.calls == []
    assert text == FakeLLM(m, b)("moderator", ctx)["text"]
    if lang == "en" and intent in moderator_lines("en", chief=False):
        assert not CJK.search(text)


async def test_moderator_fill_silence_uses_llm_with_template_fallback():
    chat = ScriptedChat(["  We covered the budget. ", "Anyone to add?"])
    p, m, b = mk(chat, "en")
    ctx = make_ctx("moderator", "fill_silence")
    text, ps = await run(p, "moderator", ctx)
    assert text == "We covered the budget. Anyone to add?" and len(chat.calls) == 1
    assert "recap" in chat.calls[0][0][-1]["content"].lower()
    # 错误 → 模板
    p, m, b = mk(ScriptedChat([], error_after=0), "en")
    p.chat = ScriptedChat(["x"], error_after=0)
    text, ps = await run(p, "moderator", ctx)
    assert text == FakeLLM(m, b)("moderator", ctx)["text"] and ps.stats["fallback"] == "error:RuntimeError"
    # 超时 → 模板
    p, m, b = mk(ScriptedChat(["late"], first_delay=0.5), "en", header_timeout=0.05)
    text, ps = await run(p, "moderator", ctx)
    assert text == FakeLLM(m, b)("moderator", ctx)["text"] and ps.stats["fallback"] == "timeout"
    # 开关关闭 → 不调 LLM
    chat = ScriptedChat(["x"])
    p, m, b = mk(chat, "en", moderator_llm=False)
    text, _ = await run(p, "moderator", ctx)
    assert chat.calls == [] and text == FakeLLM(m, b)("moderator", ctx)["text"]


async def test_build_planner_and_as_planner():
    m, b = make_meeting()
    assert isinstance(build_planner(m, b, None), CallablePlanner)
    assert isinstance(build_planner(m, b, ScriptedChat()), LLMPlanner)
    wrapped = as_planner(FakeLLM(m, b))
    ctx = make_ctx(last_text="budget.q3?")
    ps = await wrapped.plan("alice", ctx)
    assert await collect(ps) == FakeLLM(m, b)("alice", ctx)["text"]
    assert as_planner(wrapped) is wrapped
    with pytest.raises(TypeError):
        as_planner(42)


def test_header_parser_edge_cases():
    hp = HeaderParser(50)
    assert hp.feed('{"a":') is None
    obj, rest = hp.feed('1}\nhi')
    assert obj == {"a": 1} and rest == "\nhi"
    with pytest.raises(HeaderError):
        HeaderParser().feed("hello")
    with pytest.raises(HeaderError):
        HeaderParser().feed("[1,2]")
    with pytest.raises(HeaderError):
        HeaderParser(10).feed('{"a": "' + "x" * 20)
