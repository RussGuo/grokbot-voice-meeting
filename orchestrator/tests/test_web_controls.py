"""网页控制（meeting.control → inbox 的 control.* 事件）：会中加议程 / 跳下一项 / 改名 / 加 bot。"""
from meet.briefs import heuristic_brief
from orchestrator.minutes import build, render_md
from orchestrator.models import Participant
from orchestrator.scene import bot_participant, brief_from_blob
from orchestrator.state_machine import Floor, Phase
from orchestrator.voice_iface import ROSTER_TOPIC

from .helpers import run, started, types, until_floor

NEW_TYPES = {"AgendaItemAdded", "AgendaItemRenamed", "AddableBots", "BotAdding", "BotJoined", "ControlRejected"}


def events(d, *kinds):
    return [e for e in d.orch.log if e.type in kinds]


def rejected(d):
    return [(e.data["action"], e.data["reason"]) for e in events(d, "ControlRejected")]


async def send(d, ev, wait=50):
    """把控制事件投进 inbox（FakeVoiceLayer 的事件队列与 worker 的 orch.inbox 同一条路），跑到它被处理。"""
    t = d.clock.now + 10
    d.voice.inject(ev, at=t)
    await d.run_until(t + wait)


def growth_bot():
    meta = {"display_name": "增长分身", "aliases": ["增长"]}
    p = Participant.from_dict(bot_participant("growth_bot", meta, "zh_female_vv_uranus_bigtts", "zh"))
    ctx = "增长团队负责灰度实验。转化率实验需要先做小流量灰度，再放量。"
    blob = heuristic_brief(bot_id="growth_bot", context=ctx, topic="t", agenda=[{"id": "ag-1", "title": "x"}], lang="zh")
    blob["keywords"] = ["灰度", "转化率"]
    return p, brief_from_blob("growth_bot", blob, "t", ["ag-1"])


def ready(p, brief):
    return {"type": "control.bot_ready", "participant": p, "brief": brief, "voice": "x"}


# —— 没有控制时什么都不变 ——

def test_no_controls_means_no_new_events():
    async def go():
        d = await started()
        await d.run_until(150_000)
        assert not (set(types(d)) & NEW_TYPES)
    run(go())


# —— 议程 ——

def test_agenda_add_appends_item_and_is_in_minutes():
    async def go():
        d = await started()
        await send(d, {"type": "control.agenda_add", "title": "  风险\n  复盘\t<b>x</b> "})
        o = d.orch
        assert [a.title for a in o.m.agenda][-1] == "风险 复盘 <b>x</b>"       # 空白折叠；标签原样保留，渲染端转义
        ev = events(d, "AgendaItemAdded")[0].data
        assert ev["agenda_item"]["id"] == "ag-3" and ev["index"] == 2
        assert o.m.agenda[-1].owner_id == o.moderator_id and o.m.agenda[-1].timebox_ms > 0
        await send(d, {"type": "control.end"}, wait=500)
        assert o.sm.phase == Phase.POST_MEETING
        b = build(o.log)
        assert b["meta"]["agenda"][-1]["title"] == "风险 复盘 <b>x</b>"
        assert "- 风险 复盘" in render_md(b)
    run(go())


def test_agenda_add_rejects_bad_titles():
    async def go():
        d = await started()
        n = len(d.orch.m.agenda)
        for bad in ("", "   ", None, 5, ["a"], "x" * 81, "\n\t"):
            await send(d, {"type": "control.agenda_add", "title": bad})
        assert len(d.orch.m.agenda) == n and not events(d, "AgendaItemAdded")
        assert rejected(d) == [("agenda_add", "invalid")] * 7
        await send(d, {"type": "control.agenda_add", "title": "x" * 80})
        assert len(d.orch.m.agenda) == n + 1
    run(go())


def test_agenda_next_closes_current_item_through_advance_agenda():
    async def go():
        d = await started()
        await d.run_until(3_000)                                   # 主持人开场正在播
        assert d.orch.sm.bot_speaking
        await send(d, {"type": "control.agenda_next"})
        o = d.orch
        assert o.m.current_agenda_idx == 1 and o.agenda_id == "ag-2"
        assert o.m.agenda[0].status == "decided"
        dec = events(d, "DecisionRecorded")[0].data["payload"]
        assert dec["agenda_item_id"] == "ag-1"
        assert any("「" + o.m.agenda[0].title + "」" in line for line in o.minutes)   # 进了共享纪要
        seq = [(e.type, e.data.get("agenda_item_id")) for e in o.log
               if e.type in ("AgendaItemClosed", "AgendaItemStarted")]
        assert seq == [("AgendaItemStarted", "ag-1"), ("AgendaItemClosed", "ag-1"), ("AgendaItemStarted", "ag-2")]
        assert any(e.type == "FloorRevoked" and e.data["reason"] == "agenda_skip" for e in o.log)   # 说话的 bot 被收回
        await d.run_until(d.clock.now + 5_000)                      # 新一项由主持人开场，会议继续
        assert o.sm.phase == Phase.IN_AGENDA and any(
            e.type == "FloorGranted" and e.data["token"]["intent"] == "open_item" for e in o.log)
    run(go())


def test_agenda_next_on_last_item_is_rejected_and_added_item_can_be_reached():
    async def go():
        d = await started()
        await send(d, {"type": "control.agenda_next"})
        assert d.orch.agenda_id == "ag-2"
        await send(d, {"type": "control.agenda_next"})
        assert d.orch.agenda_id == "ag-2" and rejected(d) == [("agenda_next", "last_item")]
        await send(d, {"type": "control.agenda_add", "title": "补充事项"})
        await send(d, {"type": "control.agenda_next"})
        assert d.orch.agenda_id == "ag-3" and d.orch.sm.phase == Phase.IN_AGENDA
        assert [e.data["agenda_item_id"] for e in events(d, "AgendaItemClosed")] == ["ag-1", "ag-2"]
    run(go())


def test_agenda_next_while_human_holds_the_floor():
    async def go():
        d = await started()
        await until_floor(d, Floor.FLOOR_IDLE, 30_000)             # 主持人开场结束，话轮空闲
        await send(d, {"type": "vad.start", "participant_id": "host"})
        assert d.orch.token and d.orch.token.holder_id == "host"
        await send(d, {"type": "control.agenda_next"})
        assert d.orch.agenda_id == "ag-2" and d.orch.sm.phase == Phase.IN_AGENDA
        assert not (d.orch.token and d.orch.token.holder_id == "host")
    run(go())


def test_agenda_rename_current_item():
    async def go():
        d = await started()
        old = d.orch.m.current_agenda.title
        await send(d, {"type": "control.agenda_rename", "title": " 新标题 ", "item_id": "ag-1"})
        assert d.orch.m.current_agenda.title == "新标题"
        e = events(d, "AgendaItemRenamed")[0].data
        assert (e["agenda_item_id"], e["title"], e["old_title"]) == ("ag-1", "新标题", old)
        await send(d, {"type": "control.agenda_rename", "title": "另一个", "item_id": "ag-2"})   # 页面上看到的已不是当前项
        await send(d, {"type": "control.agenda_rename", "title": "x" * 99})
        assert d.orch.m.current_agenda.title == "新标题"
        assert rejected(d) == [("agenda_rename", "stale_item"), ("agenda_rename", "invalid")]
        await send(d, {"type": "control.agenda_next"})
        assert events(d, "DecisionRecorded")[0].data["payload"]["title"] == "新标题"     # 决策用改后的名字
    run(go())


def test_adding_an_item_cancels_the_pending_end_question():
    async def go():
        d = await started()
        await send(d, {"type": "control.agenda_next"})              # 到最后一项
        d.orch._end_confirm = "waiting"                              # 主持人刚问过「是否结束」
        await send(d, {"type": "control.agenda_add", "title": "再议一项"})
        assert d.orch._end_confirm is None
        assert any(e.type == "EndConfirm" and e.data.get("reason") == "agenda_added" for e in d.orch.log)
    run(go())


# —— 加 bot ——

def test_bot_add_joins_roster_scorer_and_is_announced():
    async def go():
        d = await started()
        spawned = []
        d.orch.bot_spawner = spawned.append
        d.orch.addable = {"growth_bot": "增长分身", "ops_bot": "运营分身"}
        await send(d, {"type": "control.bot_add", "bot_id": "growth_bot"})
        assert spawned == ["growth_bot"] and events(d, "BotAdding")[0].data["bot_id"] == "growth_bot"
        assert not any(p.id == "growth_bot" for p in d.orch.m.participants)        # 还没建好，不进名册
        p, brief = growth_bot()
        await send(d, ready(p, brief))
        o = d.orch
        assert "growth_bot" in {x.id for x in o.m.participants} and o.briefs["growth_bot"] is brief
        assert events(d, "BotJoined")[0].data["participant"]["display_name"] == "增长分身"
        roster = [x for t, x in d.voice.published if t == ROSTER_TOPIC][-1]["participants"]
        assert "growth_bot" in {x["speaker_id"] for x in roster}
        added = events(d, "AddableBots")[-1].data["bots"]
        assert added == [{"id": "ops_bot", "display_name": "运营分身"}]               # 只有 id 和名字
        assert o.scorer.briefs["growth_bot"] is brief and "增长" in o.scorer.aliases["growth_bot"]
        # 主持人用模板句播报
        for _ in range(40):
            await d.run_until(d.clock.now + 1_000)
            if any(u.speaker_id == o.moderator_id and "增长分身加入了会议" in u.text for u in o.utterances):
                break
        else:
            raise AssertionError([u.text for u in o.utterances])
        # 被点名 + 话题相关 → 能举手
        t = d.clock.now + 2_000
        d.say(t, "增长，灰度实验的转化率你怎么看？")
        await d.run_until(t + 8_000)
        said = [u for u in o.utterances if u.speaker_id == "host"][-1]
        assert "growth_bot" in said.addressees
        raised = [e.data["hand"] for e in events(d, "HandRaised") if e.data["hand"]["participant_id"] == "growth_bot"]
        assert raised and raised[0]["score"]["addressed"]
    run(go())


def test_bot_add_english_announcement():
    async def go():
        d = await started()
        d.orch.m.language = "en"
        d.orch.bot_spawner = lambda _b: None
        d.orch.addable = {"growth_bot": "Growth"}
        await send(d, {"type": "control.bot_add", "bot_id": "growth_bot"})
        p, brief = growth_bot()
        p.display_name = "Growth"
        await send(d, ready(p, brief))
        for _ in range(40):
            await d.run_until(d.clock.now + 1_000)
            if any("Growth has joined the meeting." in u.text for u in d.orch.utterances):
                break
        else:
            raise AssertionError([u.text for u in d.orch.utterances])
    run(go())


def test_bot_add_rejections():
    async def go():
        d = await started()
        spawned = []
        d.orch.addable = {"growth_bot": "增长分身"}
        await send(d, {"type": "control.bot_add", "bot_id": "growth_bot"})          # 还没有 spawner
        d.orch.bot_spawner = spawned.append
        for bid in ("nope", "pm_bot", "../etc", "", None, 7):
            await send(d, {"type": "control.bot_add", "bot_id": bid})
        await send(d, {"type": "control.bot_add", "bot_id": "growth_bot"})          # 正常
        await send(d, {"type": "control.bot_add", "bot_id": "growth_bot"})          # 重复点击：还在准备
        assert spawned == ["growth_bot"]
        assert rejected(d) == [
            ("bot_add", "unsupported"), ("bot_add", "unknown_bot"), ("bot_add", "already_in_meeting"),
            ("bot_add", "invalid"), ("bot_add", "invalid"), ("bot_add", "invalid"), ("bot_add", "invalid"),
            ("bot_add", "already_adding")]
        assert [p.id for p in d.orch.m.participants].count("growth_bot") == 0
        # 建好之前它已经在名册里 → 拒绝（重复入会）
        p, brief = growth_bot()
        await send(d, ready(p, brief))
        await send(d, ready(p, brief))
        assert [x.id for x in d.orch.m.participants].count("growth_bot") == 1
        assert rejected(d)[-1] == ("bot_add", "already_in_meeting")
    run(go())


def test_bot_failure_is_reported_and_can_be_retried():
    async def go():
        d = await started()
        spawned = []
        d.orch.bot_spawner = spawned.append
        d.orch.addable = {"growth_bot": "增长分身"}
        await send(d, {"type": "control.bot_add", "bot_id": "growth_bot"})
        await send(d, {"type": "control.bot_failed", "bot_id": "growth_bot", "reason": "no_voice"})
        assert rejected(d) == [("bot_add", "no_voice")] and d.orch.sm.phase == Phase.IN_AGENDA
        assert events(d, "ControlRejected")[0].data["bot_id"] == "growth_bot"
        await send(d, {"type": "control.bot_add", "bot_id": "growth_bot"})
        assert spawned == ["growth_bot", "growth_bot"]
    run(go())


def test_bot_add_limits_concurrent_adds():
    async def go():
        d = await started()
        d.orch.bot_spawner = lambda _b: None
        d.orch.addable = {f"b{i}": f"B{i}" for i in range(4)}
        for i in range(4):
            await send(d, {"type": "control.bot_add", "bot_id": f"b{i}"})
        assert [e.data["bot_id"] for e in events(d, "BotAdding")] == ["b0", "b1"]
        assert rejected(d) == [("bot_add", "busy")] * 2
    run(go())


# —— 阶段限制 ——

def test_controls_are_ignored_outside_in_agenda():
    async def go():
        d = await started()
        spawned = []
        d.orch.bot_spawner = spawned.append
        d.orch.addable = {"growth_bot": "增长分身"}
        await send(d, {"type": "control.end"}, wait=500)
        assert d.orch.sm.phase == Phase.POST_MEETING
        before, titles, idx = len(d.orch.log), [a.title for a in d.orch.m.agenda], d.orch.m.current_agenda_idx
        p, brief = growth_bot()
        for ev in ({"type": "control.agenda_add", "title": "晚了"}, {"type": "control.agenda_next"},
                   {"type": "control.agenda_rename", "title": "晚了"}, {"type": "control.bot_add", "bot_id": "growth_bot"},
                   ready(p, brief), {"type": "control.bot_failed", "bot_id": "growth_bot", "reason": "x"},
                   {"type": "control.rejected", "action": "agenda_add", "reason": "invalid"}):
            await send(d, ev)
        assert len(d.orch.log) == before                      # 收尾后连拒绝事件都不再发
        assert [a.title for a in d.orch.m.agenda] == titles and d.orch.m.current_agenda_idx == idx
        assert spawned == [] and "growth_bot" not in {x.id for x in d.orch.m.participants}
    run(go())


def test_controls_are_rejected_once_the_user_has_confirmed_the_end():
    async def go():
        d = await started()
        await send(d, {"type": "control.agenda_next"})
        d.orch._end_confirmed = True                          # 用户已确认结束，散会流程里
        await send(d, {"type": "control.agenda_add", "title": "来不及了"})
        assert len(d.orch.m.agenda) == 2 and rejected(d) == [("agenda_add", "not_in_agenda")]
    run(go())


def test_rejected_event_from_worker_becomes_an_event():
    async def go():
        d = await started()
        await send(d, {"type": "control.rejected", "action": "agenda_add", "reason": "invalid"})
        assert rejected(d) == [("agenda_add", "invalid")]
    run(go())
