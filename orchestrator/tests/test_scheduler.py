from orchestrator.models import HandScore, MeetingRules, PrincipalQuery
from orchestrator.scheduler import Cue, Scheduler, cooldown_penalty, hand_score, should_raise

R = MeetingRules()


def sched():
    return Scheduler(R, ["host"], "moderator")


def test_hand_score_formula_and_threshold():
    s = hand_score(1.0, 1.0, 1.0, False)
    assert abs(s.total - 0.90) < 1e-9          # 0.35+0.25+0.30
    s2 = hand_score(0.6, 0.6, 0.6, False)
    assert abs(s2.total - 0.54) < 1e-9 and not should_raise(s2, R)
    s3 = hand_score(0.8, 0.6, 0.7, False)      # 0.28+0.15+0.21=0.64
    assert should_raise(s3, R)
    assert hand_score(0, 0, 0, True).total == 1.0 and should_raise(hand_score(0, 0, 0, True), R)


def test_low_novelty_never_raises():
    s = hand_score(1.0, 1.0, 0.2, False)       # 0.35+0.25+0.06=0.66 但新信息量 < 0.3
    assert not should_raise(s, R)


def test_cooldown_penalty_decays():
    assert cooldown_penalty(None, 0, R) == 0
    assert cooldown_penalty(0, 0, R) > cooldown_penalty(0, 7_500, R) > 0
    assert cooldown_penalty(0, 15_000, R) == 0


def test_dedup_same_participant_keeps_one_hand():
    s = sched()
    hi = hand_score(0.9, 0.9, 0.9, False)
    h1, _ = s.raise_hand("pm_bot", "ag-1", hi, "要点A", 0, 1)
    assert h1
    dup, _ = s.raise_hand("pm_bot", "ag-1", hi, "要点A", 100, 2)
    assert dup is None
    h2, replaced = s.raise_hand("pm_bot", "ag-1", hand_score(1, 1, 1, False), "完全不同的要点", 200, 3)
    assert replaced is h1 and len(s.s.hands) == 1


def test_priority_human_vad_blocks_everything():
    s = sched()
    s.cue(Cue("moderator", "cue", 0))
    c, _ = s.pick(0, "ag-1", floor_free=True, human_vad_active=True)
    assert c is None


def test_priority_order_pending_over_cue_over_hand():
    s = sched()
    s.raise_hand("eng_bot", "ag-1", hand_score(1, 1, 1, False), "x", 0, 1)
    s.cue(Cue("pm_bot", "cue", 0))
    q = PrincipalQuery("pq-1", "pm_bot", "p", "q?", "ag-1", 1, 0, 10_000, status="answered")
    s.answer_ready(q)
    c, _ = s.pick(10, "ag-1", floor_free=True, human_vad_active=False)
    assert c.source == "pendingAnswer"
    s.consume(c, "ag-1")
    c, _ = s.pick(10, "ag-1", floor_free=True, human_vad_active=False)
    assert c.source == "moderatorCue"
    s.consume(c, "ag-1")
    c, _ = s.pick(10, "ag-1", floor_free=True, human_vad_active=False)
    assert c.source == "handRaise"


def test_cooldown_filters_hand_but_not_when_addressed():
    s = sched()
    s.on_turn_end("eng_bot", "ag-1", 0, 1_000)
    s.raise_hand("eng_bot", "ag-1", hand_score(1, 1, 1, False), "x", 2_000, 1)
    c, _ = s.pick(5_000, "ag-1", floor_free=True, human_vad_active=False)
    assert c is None                               # 15s 冷却内
    c, _ = s.pick(16_001, "ag-1", floor_free=True, human_vad_active=False)
    assert c is not None and c.participant_id == "eng_bot"
    s2 = sched()
    s2.on_turn_end("eng_bot", "ag-1", 0, 1_000)
    s2.raise_hand("eng_bot", "ag-1", hand_score(0, 0, 0, True), "被点名", 2_000, 1)
    c, _ = s2.pick(2_000, "ag-1", floor_free=True, human_vad_active=False)
    assert c is not None                           # 被点名不受冷却限制


def test_hand_expires_after_20s_and_on_agenda_change():
    s = sched()
    s.raise_hand("eng_bot", "ag-1", hand_score(1, 1, 1, False), "x", 0, 1)
    assert s.expire_hands(19_999, "ag-1") == []
    assert len(s.expire_hands(20_000, "ag-1")) == 1
    s.raise_hand("eng_bot", "ag-1", hand_score(1, 1, 1, False), "x", 0, 1)
    assert len(s.expire_hands(1, "ag-2")) == 1


def test_bot_chain_hands_floor_back_to_human():
    s = sched()
    for i in range(4):
        s.on_turn_end("pm_bot" if i % 2 else "eng_bot", "ag-1", i * 100, i * 100 + 50)
    s.raise_hand("pm_bot", "ag-1", hand_score(0, 0, 0, True), "x", 1_000, 1)
    c, actions = s.pick(1_000, "ag-1", floor_free=True, human_vad_active=False)
    assert "ask_human" in actions and c.participant_id == "moderator" and c.ref.intent == "ask_human"
    c, _ = s.pick(1_001, "ag-1", floor_free=True, human_vad_active=False)
    assert c is None                               # 等用户开口前，bot 举手不被授予
    s.on_human_spoke()
    c, _ = s.pick(1_002, "ag-1", floor_free=True, human_vad_active=False)
    assert c is not None and c.source == "handRaise"


def test_reinsert_cap_two_per_item():
    s = sched()
    for i in range(3):
        s.answer_ready(PrincipalQuery(f"pq-{i}", "pm_bot", "p", "q", "ag-1", 1, 0, 1, status="answered"))
    got = []
    for _ in range(3):
        c, _ = s.pick(0, "ag-2", floor_free=True, human_vad_active=False)
        if c is None:
            break
        s.consume(c, "ag-2")
        got.append(c.ref.id)
    assert len(got) == 2 and len(s.s.ready_answers) == 1
    s.on_agenda_changed()                          # 下一议程项配额重置
    c, _ = s.pick(0, "ag-3", floor_free=True, human_vad_active=False)
    assert c is not None
