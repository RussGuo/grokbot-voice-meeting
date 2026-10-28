import json

from orchestrator.config import human_identity, llm_config
from orchestrator.events import EventLog, project
from orchestrator.state_machine import Floor, Phase
from orchestrator.voice_iface import ROSTER_TOPIC, SPEAKER_TOPIC

from .helpers import run, started, types, until_floor


def speaker_msgs(d):
    return [p for t, p in d.voice.published if t == SPEAKER_TOPIC]


# —— 人类优先 / 打断 ——

def test_human_priority_takes_free_floor():
    async def go():
        d = await started()
        await until_floor(d, Floor.FLOOR_IDLE, 30_000)          # 主持人开场结束
        t = d.clock.now + 10
        d.voice.inject({"type": "vad.start", "participant_id": "host"}, at=t)
        await d.run_until(t)
        assert d.orch.sm.floor == Floor.HUMAN_SPEAKING
        assert d.orch.token.holder_id == "host" and d.orch.token.source == "human"
        assert speaker_msgs(d)[-1] == {"speaker_id": "host", "display_name": "用户", "state": "speaking"}
    run(go())


def test_duck_then_false_trigger_restores():
    async def go():
        d = await started()
        await d.run_until(1_000)                                 # 主持人开场播放中
        assert d.orch.sm.floor == Floor.PLAYING
        tok = d.orch.token.id
        d.blip(1_100, dur_ms=120)                                # < min_duration 250ms
        await d.run_until(1_150)
        assert d.orch.sm.floor == Floor.DUCKING and d.voice.gain_db == -18.0
        assert speaker_msgs(d)[-1]["state"] == "ducking"
        await d.run_until(1_400)
        assert d.orch.sm.floor == Floor.PLAYING and d.voice.gain_db == 0.0
        assert d.orch.token.id == tok                             # 令牌没变，继续播
        assert ("interrupt",) not in d.voice.calls
        assert speaker_msgs(d)[-1] == {"speaker_id": "chief_of_staff", "display_name": "Chief of Staff", "state": "speaking"}
        assert "DuckRestored" in types(d) and "Interrupted" not in types(d)
    run(go())


def test_duck_then_confirmed_bargein():
    async def go():
        d = await started()
        await d.run_until(2_000)
        d.say(2_100, "等一下", dur_ms=900)
        await d.run_until(2_349)
        assert d.orch.sm.floor == Floor.DUCKING
        await d.run_until(2_350)                                  # 恰好 min_duration
        assert d.orch.sm.floor == Floor.HUMAN_SPEAKING
        assert ("interrupt",) in d.voice.calls
        ev = d.orch.log.of_type("Interrupted")[-1]
        assert ev.data["by"] == "host" and ev.data["atChar"] > 0
        states = [(m["speaker_id"], m["state"]) for m in speaker_msgs(d)][-3:]
        assert states == [("chief_of_staff", "ducking"), ("chief_of_staff", "idle"), ("host", "speaking")]
        assert d.orch.metrics["bargein_stop_ms"][-1] == 250
    run(go())


def test_bargein_can_also_be_confirmed_by_voice_layer_event():
    async def go():
        d = await started()
        await d.run_until(1_000)
        tok = d.orch.token.id
        d.voice.inject({"type": "vad.start", "participant_id": "host"}, at=1_010)
        d.voice.inject({"type": "bargein.confirmed", "token_id": tok}, at=1_100)
        await d.run_until(1_100)
        assert d.orch.sm.floor == Floor.HUMAN_SPEAKING
    run(go())


def test_non_linked_participant_vad_ignored():
    async def go():
        d = await started()
        await d.run_until(1_000)
        d.voice.inject({"type": "vad.start", "participant_id": "someone_else"}, at=1_010)
        await d.run_until(1_500)
        assert d.orch.sm.floor == Floor.PLAYING
    run(go())


# —— 过期令牌 ——

def test_stale_token_output_dropped():
    async def go():
        d = await started()
        await d.run_until(1_000)
        old = d.orch.token.id
        d.say(1_100, "停", dur_ms=600)
        await d.run_until(1_400)                                  # 已真打断，令牌换成用户的
        assert d.orch.token.id != old
        n_utt = len(d.orch.utterances)
        d.voice.inject({"type": "tts.segment", "token_id": old, "participant_id": "chief_of_staff", "seq": 9, "text": "迟到的段"}, at=1_500)
        d.voice.inject({"type": "bot.utterance", "token_id": old, "participant_id": "chief_of_staff", "text": "迟到的旧输出"}, at=1_500)
        d.voice.inject({"type": "tts.done", "token_id": old, "participant_id": "chief_of_staff"}, at=1_500)
        await d.run_until(1_500)
        assert len(d.orch.utterances) == n_utt
        assert len(d.orch.log.of_type("OutputDropped")) == 3
        assert not d.orch.token_valid(old)
        assert d.orch.sm.floor == Floor.HUMAN_SPEAKING
    run(go())


# —— 举手 / bot 链 ——

def test_addressed_bot_gets_floor_after_eot_with_fresh_chat_ctx():
    async def go():
        d = await started()
        await until_floor(d, Floor.FLOOR_IDLE, 30_000)
        t = d.clock.now + 100
        d.say(t, "工程分身，离线同步的冲突率怎么样？")
        await until_floor(d, Floor.PLAYING, t + 20_000)
        assert d.orch.token.holder_id == "eng_bot" and d.orch.token.source == "handRaise"
        ctx = d.voice.last_ctx
        assert ctx.brief["participant_id"] == "eng_bot"
        assert set(ctx.topic_snapshots) == {"eng_bot", "pm_bot", "chief_of_staff"}   # 主题快照共享可见
        assert set(ctx.snapshots_text) == {"pm_bot", "chief_of_staff"}               # 压缩文本只放他人快照
        assert len(ctx.brief_text) <= 800
        assert ctx.shared_minutes[-1].startswith("用户：")
        assert len(ctx.shared_minutes) <= 8
        assert ("switch_agent", "eng_bot", len(ctx.shared_minutes)) in d.voice.calls
    run(go())


def test_bot_chain_limit_hands_back_to_human():
    async def go():
        d = await started(rules_override={"cooldown_ms": 0, "hand_threshold": 0.0, "novelty_floor": 0.0,
                                          "silence_ms": 60_000, "hand_ttl_ms": 600_000})
        d.say(11_000, "离线同步大家怎么看？")
        await d.run_until(150_000)
        grants = [e.data["token"] for e in d.orch.log.of_type("FloorGranted")]
        holders = [g["holder_id"] for g in grants]
        intents = [g.get("intent") for g in grants]
        assert "ask_human" in intents
        i = intents.index("ask_human")
        last_human = max(j for j in range(i) if holders[j] == "host")
        bots_between = [h for h in holders[last_human + 1:i] if h in ("pm_bot", "eng_bot")]
        assert len(bots_between) == 4                                   # 连续 4 轮 bot → 交还用户
        assert all(h not in ("pm_bot", "eng_bot") for h in holders[i + 1:])   # 用户开口前 bot 不再接话
    run(go())


# —— 慢路径：跨议程插回 + 主持人回溯 ——

def test_pending_answer_reinserted_across_agenda_with_recap():
    async def go():
        d = await started(principal_delay_ms=30_000, rules_override={"silence_ms": 20_000},
                          principal_answers={"principal-pm": "示例答复：可批 2 人月"})
        d.say(11_000, "产品分身，离线同步的预算能批多少？")
        await d.run_until(25_000)
        q = next(iter(d.orch.queries.values()))
        assert q.status == "pending" and d.orch.sm.pending.value == "PendingAnswer"
        assert q.agenda_item_id == "ag-1"
        await until_floor(d, Floor.FLOOR_IDLE, 40_000)
        await d.orch.advance_agenda()                            # 议程推进到 ag-2，答案还没回
        assert d.orch.agenda_id == "ag-2"
        await d.run_until(90_000)
        assert q.status == "deliveredInMeeting" and q.round_trip_ms == 30_000
        tl = types(d)
        i_recap = tl.index("ModeratorRecap")
        grants = [(e.seq, e.data["token"]) for e in d.orch.log.of_type("FloorGranted") if e.seq > d.orch.log.of_type("ModeratorRecap")[0].seq]
        assert grants[0][1]["intent"] == "recap" and grants[0][1]["holder_id"] == "chief_of_staff"
        assert grants[1][1]["source"] == "pendingAnswer" and grants[1][1]["holder_id"] == "pm_bot"
        relay = [u for u in d.orch.utterances if u.provenance == "principal"]
        assert relay and relay[0].text.startswith("本体回复了") and relay[0].agenda_item_id == "ag-2"
        assert i_recap > 0
    run(go())


def test_unanswered_query_deferred_to_minutes_at_close():
    async def go():
        d = await started(never_answer=True)
        await until_floor(d, Floor.FLOOR_IDLE, 30_000)
        d.say(d.clock.now + 100, "产品分身，离线同步的预算能批多少？")
        await d.run_until(400_000)
        assert d.orch.sm.phase == Phase.POST_MEETING
        assert all(q.status == "deferredToMinutes" for q in d.orch.queries.values())
    run(go())


# —— 承诺 ——

def test_commitment_binding_then_reconcile():
    async def go():
        d = await started()
        await until_floor(d, Floor.FLOOR_IDLE, 30_000)
        d.say(d.clock.now + 100, "工程分身，能不能下周出技术评估？")
        await d.run_until(d.clock.now + 15_000)
        (cid, c), = d.orch.commitments.items()
        assert c.status == "binding" and c.owner_id == "eng_bot"
        await d.orch.reconcile("eng_bot", [{"commitment_id": cid, "verdict": "correct", "correction": "改为两周"}])
        assert project(d.orch.log)["commitments"][cid] == "corrected"
    run(go())


# —— 事件日志回放 ——

def test_event_log_replay_consistent(tmp_path):
    async def go():
        from orchestrator import sim
        d = sim.SimDriver(principal_delay_ms=45_000)
        await sim.scripted(d)
        return d
    d = run(go())
    p = tmp_path / "log.jsonl"
    d.orch.log.dump(p)
    reloaded = EventLog.load(p)
    assert [e.to_json() for e in reloaded] == [e.to_json() for e in d.orch.log]
    v1, v2 = project(d.orch.log), project(reloaded)
    assert v1 == v2
    assert len(v1["utterances"]) == len(d.orch.utterances)
    assert v1["commitments"] == {k: c.status for k, c in d.orch.commitments.items()}
    assert v1["queries"] == {k: q.status for k, q in d.orch.queries.items()}
    assert [e.seq for e in reloaded] == list(range(1, len(reloaded) + 1))
    # 篡改序号应被发现
    bad = p.read_text().splitlines()
    bad.pop(3)
    import pytest
    with pytest.raises(ValueError):
        project(EventLog.load("\n".join(bad)))


def test_event_log_is_append_only_and_rejects_unknown_type():
    import pytest
    log = EventLog("m")
    log.append("MeetingCreated", 0, payload={})
    with pytest.raises(ValueError):
        log.append("Whatever", 0)
    assert not hasattr(log, "remove") and not hasattr(log, "__setitem__")


# —— 前端数据约定 ——

def test_roster_published_on_start_and_on_participant_change():
    async def go():
        from orchestrator.models import Participant
        d = await started()
        rosters = [p for t, p in d.voice.published if t == ROSTER_TOPIC]
        assert len(rosters) == 1
        assert {"speaker_id": "host", "display_name": "用户"} in rosters[0]["participants"]
        await d.orch.add_participant(Participant(id="obs", kind="human", display_name="旁听", role="observer"))
        rosters = [p for t, p in d.voice.published if t == ROSTER_TOPIC]
        assert len(rosters) == 2 and any(p["speaker_id"] == "obs" for p in rosters[1]["participants"])
    run(go())


def test_speaker_message_schema_and_release_idle():
    async def go():
        d = await started()
        await until_floor(d, Floor.FLOOR_IDLE, 30_000)
        for m in speaker_msgs(d):
            assert set(m) == {"speaker_id", "display_name", "state"}
            assert m["state"] in ("speaking", "idle", "ducking")
            json.dumps(m)
        msgs = speaker_msgs(d)
        cos = {"speaker_id": "chief_of_staff", "display_name": "Chief of Staff"}
        i = msgs.index({**cos, "state": "speaking"})
        assert {**cos, "state": "idle"} in msgs[i + 1:]  # 释放令牌 → idle
        assert msgs[-1]["state"] == "idle"
    run(go())


# —— 用户 identity ——

def test_human_identity_default_and_env_override(monkeypatch):
    assert human_identity({}) == "host"
    assert human_identity({"MEETING_HUMAN_IDENTITY": "gh-mac"}) == "gh-mac"

    async def go():
        d = await started(human_id="gh-mac")
        assert d.orch.m.voice.linked_human_id == "gh-mac"
        await until_floor(d, Floor.FLOOR_IDLE, 30_000)
        d.voice.inject({"type": "vad.start", "participant_id": "gh-mac"}, at=d.clock.now + 10)
        await d.run_until(d.clock.now + 10)
        assert speaker_msgs(d)[-1]["speaker_id"] == "gh-mac"
        assert d.orch.token.holder_id == "gh-mac"
    run(go())
    monkeypatch.setenv("MEETING_HUMAN_IDENTITY", "from-env")
    from orchestrator.bots import load_example
    m, _ = load_example()
    assert m.voice.linked_human_id == "from-env" and m.participant("from-env").kind == "human"


def test_llm_config_mock_without_key():
    c = llm_config({})
    assert c.provider == "mock" and c.base_url == "https://api.deepseek.com"
    c = llm_config({"DEEPSEEK_API_KEY": "x", "DEEPSEEK_MODEL": "deepseek-chat"})
    assert c.provider == "deepseek" and c.model == "deepseek-chat"


# —— 主持模式 ——

def test_default_moderator_is_chief_of_staff_and_neutral_optional():
    async def go():
        d = await started()
        assert d.orch.m.moderator.mode == "chiefOfStaff" and d.orch.moderator_id == "chief_of_staff"
        assert "chief_of_staff" in d.orch.briefs
        assert d.voice.calls[0][:2] == ("switch_agent", "chief_of_staff")
        await d.run_until(30_000)
        opening = d.orch.log.of_type("UtteranceFinal")[0].data["utterance"]
        assert opening["speaker_id"] == "chief_of_staff"
        n = await started(moderator_mode="neutral")
        assert n.orch.moderator_id == "moderator" and n.orch.m.moderator.mode == "neutral"
        assert n.orch.m.participant("chief_of_staff").role == "member"
        await n.run_until(30_000)
        assert n.orch.log.of_type("UtteranceFinal")[0].data["utterance"]["speaker_id"] == "moderator"
    run(go())


def test_chief_of_staff_opening_declares_stance():
    from orchestrator.fake_llm import FakeLLM
    from orchestrator.bots import load_example
    m, b = load_example()
    out = FakeLLM(m, b)("chief_of_staff", __import__("orchestrator.models", fromlist=["ChatCtxRecipe"]).ChatCtxRecipe(
        participant_id="chief_of_staff", system="", brief=None, topic_snapshots={}, shared_minutes=[],
        turn_meta={"intent": "open", "agenda_title": "x"}))
    assert "兼任" in out["text"] and "立场" in out["text"]


# —— 最后一项冷场：先问是否结束 ——

async def _to_last_item_idle(**kw):
    d = await started(rules_override={"total_ms": 10 ** 9}, **kw)
    await until_floor(d, Floor.FLOOR_IDLE, 40_000)
    await d.orch.advance_agenda()                       # 直接进入最后一项 ag-2
    assert d.orch.agenda_id == "ag-2"
    return d


def _asked(d):
    return [e for e in d.orch.log.of_type("EndConfirm") if e.data["stage"] == "asked"]


def test_last_item_silence_asks_before_ending_then_timeout_15s():
    async def go():
        d = await _to_last_item_idle()
        await d.run_until(d.clock.now + 30_000)
        assert _asked(d), "应先询问是否结束"
        assert d.orch.sm.phase == Phase.IN_AGENDA               # 问完还没结束
        ask_done = [e for e in d.orch.log.of_type("FloorReleased") if e.data["token"].get("intent") == "confirm_end"][0].at
        await d.run_until(ask_done + 14_999)
        assert d.orch.sm.phase == Phase.IN_AGENDA
        await d.run_until(ask_done + 15_000)
        assert d.orch.sm.phase == Phase.POST_MEETING
        assert [e.data["stage"] for e in d.orch.log.of_type("EndConfirm")][-1] == "timeout"
    run(go())


def test_last_item_human_confirms_end():
    async def go():
        d = await _to_last_item_idle()
        await d.run_until(d.clock.now + 30_000)
        assert _asked(d) and d.orch.sm.phase == Phase.IN_AGENDA
        d.say(d.clock.now + 500, "好，结束吧。")
        await d.run_until(d.clock.now + 4_000)
        assert d.orch.sm.phase == Phase.POST_MEETING
        assert {"confirmed", "human_command"} & {e.data["stage"] for e in d.orch.log.of_type("EndConfirm")}
    run(go())


def test_last_item_human_wants_to_continue():
    async def go():
        d = await _to_last_item_idle()
        await d.run_until(d.clock.now + 30_000)
        assert _asked(d)
        d.say(d.clock.now + 500, "先别结束，工程分身说下推理成本。")
        await d.run_until(d.clock.now + 6_000)
        assert d.orch.sm.phase == Phase.IN_AGENDA
        assert "continue" in [e.data["stage"] for e in d.orch.log.of_type("EndConfirm")]
        assert d.orch.token and d.orch.token.holder_id == "eng_bot"     # 讨论继续
    run(go())


def test_end_affirmation_rules():
    from orchestrator.orchestrator import is_end_affirmation
    assert is_end_affirmation("好，结束吧") and is_end_affirmation("散会")
    assert not is_end_affirmation("先别结束") and not is_end_affirmation("还有一个问题")


def test_non_last_item_silence_still_advances():
    async def go():
        d = await started()
        await d.run_until(60_000)                    # 开场后无人说话：小结 → 再冷场 → 推进到 ag-2
        assert d.orch.agenda_id == "ag-2" and not _asked(d)
    run(go())


def test_ready_answer_inserted_before_agenda_advance():
    """本项的本体答案已到、议程项恰好到时：先 pendingAnswer 插回，再推进（不触发跨议程回溯）。"""
    import asyncio
    from orchestrator.driver import SimDriver
    d = SimDriver(scenario="sim_v1", principal_delay_ms=6_000, rules_override={"silence_ms": 20_000})

    async def go():
        await d.start()
        d.say(60_000, "增长分身，推广预算能给多少？")
        await d.run_until(67_900)
        assert any(e.type == "PrincipalAsked" for e in d.orch.log)
        d.say(68_000, "产品分身，对账的用户反馈怎么样？")   # 让产品分身占着 floor，答案在其发言中到达
        await d.run_until(74_500)
        assert d.orch.token and d.orch.token.holder_id == "pm_bot"
        assert any(e.type == "PrincipalAnswered" for e in d.orch.log)
        d.orch._agenda_due = True                # 答案到达后本项 timebox 到点（e2e-02 的情形）
        await d.run_until(110_000)
    asyncio.run(go())
    ev = list(d.orch.log)
    q = lambda e: e.data["query"]
    st = lambda x: x["status"] if isinstance(x, dict) else x.status
    ans = next(e for e in ev if e.type == "PrincipalAnswered" and st(q(e)) in ("answered", "deliveredInMeeting"))
    src = lambda e: (e.data["token"]["source"] if isinstance(e.data["token"], dict) else e.data["token"].source)
    grants = [e for e in ev if e.seq > ans.seq and e.type == "FloorGranted" and src(e) != "human"]
    assert grants[0].data and src(grants[0]) == "pendingAnswer"
    closes = [e for e in ev if e.type == "AgendaItemClosed"]
    assert closes and closes[0].seq > grants[0].seq
    assert not any(e.type == "ModeratorRecap" for e in ev)


# —— e2e-03 遗留：用户的结束指令立即生效、主持人不重复、答案在主持人填充话时插回 ——

def test_human_end_command_ends_immediately_any_state():
    """硬规则：人的介入优先级最高。第一项讨论中、主持人没问，用户说「结束吧」也立即散会。"""
    async def go():
        d = await started(rules_override={"total_ms": 10 ** 9})
        await until_floor(d, Floor.FLOOR_IDLE, 40_000)
        assert d.orch.m.current_agenda_idx == 0
        t0 = d.clock.now + 500
        d.say(t0, "好，就按这个来，结束吧。")
        await d.run_until(t0 + 5_000)
        assert d.orch.sm.phase == Phase.POST_MEETING
        ev = list(d.orch.log)
        cmd = next(e for e in ev if e.type == "EndConfirm" and e.data["stage"] == "human_command")
        end = next(e for e in ev if e.type == "MeetingEnded")
        assert end.at - cmd.at <= 1_000
        assert not [e for e in ev if e.type == "UtteranceFinal" and e.at > cmd.at]
        assert not _asked(d)
    run(go())


def test_end_command_rules():
    from orchestrator.orchestrator import is_end_command
    assert is_end_command("好，就按这个来结束吧。") and is_end_command("散会") and is_end_command("今天就到这")
    assert not is_end_command("先别结束") and not is_end_command("还不能散会") and not is_end_command("结束时间定在周五")


def test_moderator_no_repeated_fill_and_end_asks_capped():
    """最后一项长时间冷场：小结最多 1 次、「是否结束」最多 2 次，然后结束；不会死循环。"""
    async def go():
        d = await _to_last_item_idle()
        await d.run_until(d.clock.now + 120_000)
        assert d.orch.sm.phase == Phase.POST_MEETING
        ev = list(d.orch.log)
        intents = [e.data["token"]["intent"] if isinstance(e.data["token"], dict) else e.data["token"].intent
                   for e in ev if e.type == "FloorGranted"]
        ag2 = [e for e in ev if e.type == "UtteranceFinal"]
        assert intents.count("confirm_end") <= 2
        fills_ag2 = [e for e in ag2 if e.data["utterance"].agenda_item_id == "ag-2"
                     and "小结" in e.data["utterance"].text] if ag2 and not isinstance(ag2[0].data["utterance"], dict) else \
                    [e for e in ag2 if e.data["utterance"]["agenda_item_id"] == "ag-2" and "小结" in e.data["utterance"]["text"]]
        assert len(fills_ag2) <= 1
    run(go())


def test_answer_preempts_moderator_filler():
    """本体答案到达时主持人正在说小结/是否结束：立刻让出，答案在这个空档插回。"""
    import asyncio
    from orchestrator.driver import SimDriver
    from orchestrator.state_machine import Floor as Fl
    d = SimDriver(scenario="sim_v1", principal_delay_ms=10 ** 9)

    async def go():
        await d.start()
        d.say(60_000, "增长分身，推广预算能给多少？")
        t = 60_000
        while t < 200_000:
            await d.run_until(t)
            tok = d.orch.token
            if any(e.type == "PrincipalAsked" for e in d.orch.log) and tok and tok.holder_id == d.orch.moderator_id \
                    and tok.intent in ("fill_silence", "confirm_end") and d.orch.sm.floor == Fl.PLAYING:
                break
            t += 50
        else:
            raise AssertionError("没有出现主持人填充话")
        q = next(e for e in d.orch.log if e.type == "PrincipalAsked").data["query"]
        qid = q["id"] if isinstance(q, dict) else q.id
        await d.orch._on_principal_answered({"type": "principal.answered", "query_id": qid, "text": "预算 2000 元（测试）"})
        tok = d.orch.token
        # 同项直接插回；跨议程则先由主持人一句回溯（决策 4），都不再等填充话说完
        assert tok is not None and (tok.source == "pendingAnswer" or tok.intent == "recap"), tok
        assert any(e.type == "FloorRevoked" and (e.data["token"]["intent"] if isinstance(e.data["token"], dict)
                   else e.data["token"].intent) in ("fill_silence", "confirm_end") for e in d.orch.log)
    asyncio.run(go())


def test_control_buttons_interrupt_and_end():
    async def go():
        d = await started(rules_override={"total_ms": 10 ** 9})
        t = d.clock.now
        while not (d.orch.token and d.orch.is_bot(d.orch.token.holder_id) and d.orch.sm.bot_speaking):
            t += 50; await d.run_until(t)
        d.voice.inject({"type": "control.interrupt"}, at=t + 10)
        await d.run_until(t + 100)
        assert d.orch.token and d.orch.token.holder_id == "host"
        await d.run_until(t + 7_000)                       # 不说话 6s 后自动释放
        assert not (d.orch.token and d.orch.token.holder_id == "host")
        d.voice.inject({"type": "control.end"}, at=d.clock.now + 10)
        await d.run_until(d.clock.now + 500)
        assert d.orch.sm.phase == Phase.POST_MEETING
    run(go())
