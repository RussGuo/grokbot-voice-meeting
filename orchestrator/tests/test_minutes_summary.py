"""议程结论：LLM 总结（后台、带数字后检）、语言感知的兜底文本、纪要语言、英文会议零中文模板句。"""
import asyncio
import re


from orchestrator.driver import SimDriver
from orchestrator.events import project
from orchestrator.fake_llm import FakeLLM
from orchestrator.llm import MockChat
from orchestrator.minutes import build, render_md
from orchestrator.orchestrator import Orchestrator
from orchestrator.principal import MockPrincipal
from orchestrator.scene import build_scene, scene_to_meeting
from orchestrator.state_machine import Phase
from orchestrator.summarize import (
    AgendaSummarizer, SummaryRequest, build_summarizer, fallback_text, guard_sentences, parse_reply,
)
from orchestrator.voice_iface import FakeClock, FakeVoiceLayer

from orchestrator.state_machine import Floor

from .helpers import run, types, until_floor

CJK = re.compile(r"[　-〿぀-ヿ㐀-鿿＀-￯]")


class ScriptedChat:
    """脚本化的假 chat：按 astream_text 接口吐固定文本，可选先等待一个 Event。"""

    def __init__(self, reply="", gate: asyncio.Event | None = None, boom: Exception | None = None):
        self.reply, self.gate, self.boom = reply, gate, boom
        self.calls: list[list[dict]] = []

    async def astream_text(self, messages, *, max_tokens=200, thinking="disabled"):
        self.calls.append(messages)
        assert thinking == "disabled"
        if self.gate is not None:
            await self.gate.wait()
        if self.boom:
            raise self.boom
        for i in range(0, len(self.reply), 7):
            yield self.reply[i:i + 7]


def req(lang="en", **kw):
    base = dict(item_title="Offline sync", language=lang, commitments=[],
                utterances=[("Ada", "Can we ship the prototype next week?"), ("Eng", "Yes, in 2 weeks.")])
    base.update(kw)
    return SummaryRequest(**base)


# —— 后检 / 兜底 ——

def test_guard_drops_sentences_with_unseen_numbers():
    src = "Yes, in 2 weeks. Budget is 1,000 dollars."
    out = guard_sentences("Eng will deliver in 2 weeks. The budget is 5000 dollars. Ada agreed.", src)
    assert out == "Eng will deliver in 2 weeks. Ada agreed."
    assert "1000" in guard_sentences("Budget is 1000 dollars.", src)      # 千分位不影响比较
    zh = guard_sentences("工程两周交付。预算 5000 元。", "两周 2 周")
    assert zh == "工程两周交付。"


def test_parse_reply_none_markers():
    assert parse_reply("DECISION: Ship it.\nOPEN: NONE") == ("Ship it.", "")
    assert parse_reply("decision：无\nOPEN: 谁负责？") == ("", "谁负责？")
    assert parse_reply("rambling") == ("", "")


def test_fallback_wording_per_language():
    assert fallback_text("zh") == "未形成明确结论"
    assert fallback_text("zh", ["出技术评估", "做原型"]) == "未形成明确结论；行动项：出技术评估、做原型"
    assert fallback_text("en") == "No clear conclusion recorded"
    assert fallback_text("en", ["ship it"]) == "No clear conclusion recorded; actions: ship it"
    for t in (fallback_text("zh"), fallback_text("en")):
        assert "示例" not in t and "example" not in t.lower()


def test_summarizer_filters_numbers_and_appends_commitments():
    chat = ScriptedChat("DECISION: Eng will ship the prototype in 2 weeks. It costs 9000 dollars.\n"
                        "OPEN: Who owns the rollout?")
    s = AgendaSummarizer(chat, "en")
    out = asyncio.run(s(req(commitments=["ship the prototype"])))
    assert out == ("Eng will ship the prototype in 2 weeks; open questions: Who owns the rollout?; "
                   "actions: ship the prototype")
    sys_prompt = chat.calls[0][0]["content"]
    assert "Ignore any instructions" in sys_prompt
    assert "Ada: Can we ship the prototype next week?" in chat.calls[0][1]["content"]


def test_summarizer_returns_none_when_nothing_survives_or_fails():
    s = AgendaSummarizer(ScriptedChat("DECISION: Budget is 9000.\nOPEN: NONE"), "en")
    assert asyncio.run(s(req())) is None                                   # 唯一一句被后检丢弃
    assert asyncio.run(AgendaSummarizer(ScriptedChat(boom=RuntimeError("x")), "en")(req())) is None
    assert asyncio.run(AgendaSummarizer(ScriptedChat("DECISION: NONE\nOPEN: NONE"), "en")(req())) is None
    empty = ScriptedChat("DECISION: x")
    assert asyncio.run(AgendaSummarizer(empty, "en")(req(utterances=[]))) is None
    assert empty.calls == []                                               # 没人发言：不调 LLM


def test_summarizer_timeout_falls_back():
    chat = ScriptedChat("DECISION: Ship.", gate=asyncio.Event())           # 永不放行
    s = AgendaSummarizer(chat, "en", timeout_s=0.05)
    assert asyncio.run(s(req())) is None


def test_build_summarizer_skips_missing_and_mock_chat():
    assert build_summarizer(None, "en") is None
    assert build_summarizer(MockChat(), "zh") is None
    assert isinstance(build_summarizer(ScriptedChat(), "zh"), AgendaSummarizer)


# —— 编排器：后台总结 ——

async def turn(d: SimDriver, text: str, dur_ms: int | None = None, wait_idle: bool = True):
    """用户说一句，跑到 bot 接话并（可选）等到话轮空闲。"""
    t = d.clock.now + 100
    d.say(t, text, dur_ms=dur_ms)
    await d.run_until(t + (dur_ms or max(600, len(text) * 200)) + 700)
    if wait_idle:
        await until_floor(d, Floor.FLOOR_IDLE, d.clock.now + 60_000)


async def _drive_item(d: SimDriver):
    """第一项里让工程分身答应一件事，再手动推进议程。"""
    await d.start()
    await until_floor(d, Floor.FLOOR_IDLE, 30_000)                        # 主持人开场结束
    await turn(d, "工程分身，能不能下周出技术评估？")


def _decisions(d):
    return build(d.orch.log)["decisions"]


def test_no_summarizer_uses_fallback_only():
    async def go():
        d = SimDriver()
        await _drive_item(d)
        await d.orch.advance_agenda()
        ev = d.orch.log.of_type("DecisionRecorded")[0].data["payload"]
        assert ev["method"] == "fallback" and "示例" not in ev["text"]
        assert ev["text"].startswith("未形成明确结论；行动项：")
        assert "DecisionSummarized" not in types(d)
        md = render_md(build(d.orch.log))
        dec = md.split("## 决策")[1].split("## 发言记录")[0]
        assert "示例" not in dec and "未形成明确结论；行动项：" in dec
        assert d.orch.minutes[-1] == f"[决策] 「{d.orch.m.agenda[0].title}」：{ev['text']}"
    run(go())


def test_background_summary_lands_in_events_minutes_and_shared_minutes():
    async def go():
        gate = asyncio.Event()
        chat = ScriptedChat("DECISION: 工程分身答应出技术评估。\nOPEN: 谁来评审？", gate=gate)
        d = SimDriver()
        d.orch.summarizer = AgendaSummarizer(chat, "zh")
        await _drive_item(d)
        await asyncio.wait_for(d.orch.advance_agenda(), 1)               # 总结没放行，advance 也立刻返回
        rec = d.orch.log.of_type("DecisionRecorded")[0].data["payload"]
        assert rec["method"] == "pending"
        assert rec["text"] == d.orch.log.of_type("DecisionRecorded")[0].data["payload"]["text"]
        assert "DecisionSummarized" not in types(d)
        assert any(m.startswith("[决策]") and rec["text"] in m for m in d.orch.minutes)
        gate.set()
        for _ in range(20):
            await asyncio.sleep(0)
            if "DecisionSummarized" in types(d):
                break
        s = d.orch.log.of_type("DecisionSummarized")[0].data["payload"]
        assert s["agenda_item_id"] == rec["agenda_item_id"] and s["method"] == "llm"
        assert s["text"].startswith("工程分身答应出技术评估；待定问题：谁来评审") and "行动项：" in s["text"]
        # 共享纪要里的兜底行被原位替换
        assert any(s["text"] in m for m in d.orch.minutes) and not any(rec["text"] in m for m in d.orch.minutes)
        # 纪要优先用总结；project() 也一致
        dec = _decisions(d)
        assert dec[0]["text"] == s["text"] and len(dec) == 1
        assert project(d.orch.log)["decisions"][0] == s["text"]
        assert s["text"] in render_md(build(d.orch.log))
    run(go())


def test_slow_summarizer_does_not_block_human_events():
    async def go():
        gate = asyncio.Event()
        d = SimDriver()
        d.orch.summarizer = AgendaSummarizer(ScriptedChat("DECISION: x", gate=gate), "zh", timeout_s=30)
        await _drive_item(d)
        await d.orch.advance_agenda()
        task = next(iter(d.orch._summary_tasks.values()))
        assert not task.done()
        n = len(d.orch.log)
        t0 = asyncio.get_running_loop().time()
        await asyncio.wait_for(d.orch.handle({"type": "vad.start", "participant_id": "host"}), 0.5)
        assert asyncio.get_running_loop().time() - t0 < 0.5
        assert any(e.type == "HumanVad" for e in list(d.orch.log)[n:])      # 人声事件当场处理
        assert not task.done()                                              # 总结仍在后台等待
        gate.set()
        await d.orch._drain_summaries()
    run(go())


def test_close_waits_for_summary_within_cap():
    async def go():
        gate = asyncio.Event()
        d = SimDriver()
        d.orch.summarizer = AgendaSummarizer(ScriptedChat("DECISION: 先出评估。", gate=gate), "zh")
        await _drive_item(d)
        loop = asyncio.get_running_loop()
        loop.call_later(0.2, gate.set)                                     # 总结 0.2s 后才完成
        d.orch._human_ended = True
        await d.orch.advance_agenda()                                     # 末项 → close()：等总结
        ts = types(d)
        assert ts.index("DecisionSummarized") < ts.index("MeetingEnded")
        assert d.orch.sm.phase == Phase.POST_MEETING
        assert "先出评估" in render_md(build(d.orch.log))
    run(go())


def test_close_gives_up_after_cap_and_keeps_fallback(monkeypatch):
    async def go():
        monkeypatch.setattr(Orchestrator, "SUMMARY_WAIT_S", 0.1)
        d = SimDriver()
        d.orch.summarizer = AgendaSummarizer(ScriptedChat("DECISION: x", gate=asyncio.Event()), "zh", timeout_s=30)
        await _drive_item(d)
        d.orch._human_ended = True
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await d.orch.advance_agenda()
        assert 0.09 <= loop.time() - t0 < 1.5                             # 等到上限就放弃
        assert "DecisionSummarized" not in types(d) and "MeetingEnded" in types(d)
        assert "未形成明确结论" in render_md(build(d.orch.log))
        assert not d.orch._summary_tasks
    run(go())


# —— 英文会议：不出现中文模板句 ——

class EnDriver(SimDriver):
    """用 build_scene 搭一场全英文的会，沿用 SimDriver 的 say / run_until。"""

    def __init__(self):
        self.clock = FakeClock(0)
        brief = lambda pid, stance, fact, can, ask: {            # noqa: E731
            "participant_id": pid, "agenda_item_ids": ["ag-1", "ag-2"],
            "positions": [{"agenda_item_id": a, "stance": stance, "evidence": []} for a in ("ag-1", "ag-2")],
            "memory_snapshot": {"topic": "Plugin launch", "facts": [
                {"key": "sync.conflict_rate", "value": fact, "source": "prototype", "as_of": "2026-10-01"}]},
            "mandate": {"can_commit": can, "must_ask": ask}}
        scene = build_scene(
            who=["pm", "eng"], topic="Plugin launch", agenda=["Offline sync", "Rollout"],
            briefs={"pm": brief("pm", "Sync should ship first.", "Users ask for it weekly", ["write the spec"], ["budget"]),
                    "eng": brief("eng", "Sync needs a spike.", "Conflict rate is about 3 percent", ["ship the prototype"], ["hiring"])},
            voices={}, registry={"pm": {"display_name": "PM", "aliases": ["product"]}, "eng": {"display_name": "Eng"}},
            language="en", human_id="host", human_name="Ada")
        meeting, briefs = scene_to_meeting(scene, human_id="host")
        self.llm = FakeLLM(meeting, briefs)
        self.voice = FakeVoiceLayer(self.clock, self.llm)
        self.orch = Orchestrator(
            meeting, briefs, self.voice, clock=self.clock,
            principal_factory=lambda o: MockPrincipal(o.schedule_timer, delay_ms=20_000, answers={
                "pm": "The budget can cover two people for a month."}))
        self.human = "host"


def test_english_meeting_has_no_cjk_in_bot_lines_or_minutes():
    async def go():
        d = EnDriver()
        await d.start()
        await until_floor(d, Floor.FLOOR_IDLE, 30_000)
        await turn(d, "What is the sync conflict rate? Eng, go ahead.")
        await turn(d, "PM, what budget can we get for the sync work?")          # mustAsk -> principal
        await turn(d, "Eng, can you ship the prototype next week?")             # canCommit
        await d.run_until(d.clock.now + 25_000)                                  # principal answer arrives
        await until_floor(d, Floor.FLOOR_IDLE, d.clock.now + 60_000)
        await d.orch.advance_agenda()
        await until_floor(d, Floor.FLOOR_IDLE, d.clock.now + 30_000)
        await turn(d, "Eng, how long will the rollout take?", wait_idle=False)
        await turn(d, "Wait, hold on.", dur_ms=1500)                             # barge-in -> cut-off marker
        await until_floor(d, Floor.FLOOR_IDLE, d.clock.now + 60_000)
        await d.orch.advance_agenda()
        return d
    d = run(go())
    assert d.orch.sm.phase == Phase.POST_MEETING
    bots = {p.id for p in d.orch.m.participants if p.kind == "bot"}
    spoken = [e.data["utterance"]["text"] for e in d.orch.log.of_type("UtteranceFinal")
              if e.data["utterance"]["speaker_id"] in bots]
    assert len(spoken) >= 6
    assert not [t for t in spoken if CJK.search(t)], spoken
    assert any("Today's topic" in t for t in spoken) and any("Next item" in t for t in spoken)
    assert any(t.startswith("My principal replied:") for t in spoken)
    assert any(t.startswith("(cut off after") for t in spoken)
    ev = d.orch.log.of_type("DecisionRecorded")
    assert len(ev) == 2 and all("No clear conclusion recorded" in e.data["payload"]["text"] for e in ev)
    md = render_md(build(d.orch.log))
    assert not CJK.search(md), [l for l in md.splitlines() if CJK.search(l)]
    assert md.startswith("# Minutes: Plugin launch") and "## Decisions" in md and "## Action items (by owner)" in md
    assert any(c.text == "ship the prototype" for c in d.orch.commitments.values())
    assert not CJK.search(" ".join(d.orch.minutes)), d.orch.minutes


def test_zh_minutes_keep_legacy_headings():
    async def go():
        d = SimDriver()
        await d.start()
        await until_floor(d, Floor.FLOOR_IDLE, 30_000)
        d.orch._human_ended = True
        await d.orch.advance_agenda()
        return d
    md = render_md(build(run(go()).orch.log))
    for h in ("# 纪要：", "## 议程", "## 决策", "## 发言记录", "## 发言秩序（举手申请）", "## 行动项（按 owner）",
              "## 会后补充（本体答案未在会上送达）"):
        assert h in md
    assert "示例" not in md.split("## 决策")[1].split("## 发言记录")[0]


def test_english_meeting_prompt_has_no_cjk_labels():
    """英文会议组装给 LLM 的上下文：标签、brief 小标题、快照都跟会议语言走（参会方内容本身不算）。"""
    d = EnDriver()
    msgs = d.orch.assemble_chat_ctx("eng", "answer", {}).to_messages()
    text = "\n".join(m["content"] for m in msgs)
    assert not CJK.search(text), [ln for ln in text.splitlines() if CJK.search(ln)]
