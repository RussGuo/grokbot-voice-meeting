"""Regressions: late directives, spoken commitments, English cues."""
from orchestrator.commitments import commitments_in_speech
from orchestrator.orchestrator import parse_directive
from orchestrator.state_machine import Floor

from .helpers import run, started, until_floor


def test_parse_directive_zh_and_en():
    aliases = {"eng_bot": ["工程", "Eng"], "pm_bot": ["产品", "PM"]}
    assert parse_directive("停一下，先让工程说说成本", aliases) == "eng_bot"
    assert parse_directive("let Eng speak about the cost", aliases) == "eng_bot"
    assert parse_directive("ask PM to talk through the risks", aliases) == "pm_bot"
    assert parse_directive("hello there", aliases) is None


def test_directive_in_eot_text_is_executed_after_barge_in():
    """The final transcript often rides on eot, after the floor was already released."""
    async def go():
        d = await started()
        await d.run_until(1_000)  # moderator is speaking
        tok = d.orch.token.id
        t = 1_100
        d.voice.inject({"type": "vad.start", "participant_id": "host"}, at=t)
        d.voice.inject({"type": "bargein.confirmed", "token_id": tok}, at=t + 200)
        d.voice.inject({"type": "vad.end", "participant_id": "host"}, at=t + 700)
        # eot carries the words; asr.final is late and must not double-execute.
        d.voice.inject({"type": "eot", "participant_id": "host", "text": "先让工程说说成本"}, at=t + 800)
        d.voice.inject({"type": "asr.final", "participant_id": "host", "text": "先让工程说说成本"}, at=t + 1_400)
        await d.run_until(t + 20_000)
        dirs = d.orch.log.of_type("HumanDirective")
        assert any(e.data.get("stage") == "parsed" and e.data.get("target") == "eng_bot" for e in dirs)
        assert any(e.data.get("stage") == "executed" and e.data.get("target") == "eng_bot" for e in dirs)
        assert sum(1 for e in dirs if e.data.get("stage") == "parsed") == 1
    run(go())


def test_directive_arriving_after_eot_preempts_the_wrong_speaker():
    async def go():
        d = await started()
        await until_floor(d, Floor.FLOOR_IDLE, 30_000)
        # Human speaks with no text on eot; the scheduler may hand the floor to someone else.
        t = d.clock.now + 100
        d.voice.inject({"type": "vad.start", "participant_id": "host"}, at=t)
        d.voice.inject({"type": "vad.end", "participant_id": "host"}, at=t + 400)
        d.voice.inject({"type": "eot", "participant_id": "host"}, at=t + 500)
        d.voice.inject({"type": "asr.final", "participant_id": "host", "text": "请工程讲一下工期"}, at=t + 1_200)
        await d.run_until(t + 30_000)
        executed = [e for e in d.orch.log.of_type("HumanDirective") if e.data.get("stage") == "executed"]
        assert executed and executed[0].data["target"] == "eng_bot"
    run(go())


def test_spoken_commitment_becomes_an_action_item(tmp_path):
    async def go():
        d = await started()
        await until_floor(d, Floor.FLOOR_IDLE, 30_000)
        # Planner only flags can_commit when the phrase is in the human line.
        # Here the bot says the promise anyway; the utterance text is what counts.
        text = "工程分身，你怎么看？"
        t = d.clock.now + 100
        d.say(t, text)
        eot = t + max(600, len(text) * 200) + 400
        await d.run_until(eot + 80)  # floor just granted; the planned utterance is still in the future
        tok = d.orch.token
        assert tok is not None and tok.holder_id == "eng_bot"
        d.voice.inject({
            "type": "bot.utterance", "token_id": tok.id, "participant_id": tok.holder_id,
            "text": "可以，我答应出技术评估。", "provenance": "model", "confidence": 0.9, "commitments": [],
        }, at=d.clock.now + 10)
        await d.run_until(d.clock.now + 20)
        made = [c for c in d.orch.commitments.values() if "出技术评估" in c.text]
        assert made and made[0].owner_id == tok.holder_id
        from orchestrator.minutes import export
        built = export(d.orch.log, tmp_path)
        assert any("出技术评估" in a["title"] for items in built["actions"].values() for a in items)
    run(go())


def test_commitment_phrase_without_promise_language_is_ignored():
    assert commitments_in_speech("按快照，出技术评估要四周。", ["出技术评估"]) == []
    assert commitments_in_speech("我答应出技术评估。", ["出技术评估"]) == ["出技术评估"]
    assert commitments_in_speech("I commit to ship the prototype.", ["ship the prototype"]) == ["ship the prototype"]
