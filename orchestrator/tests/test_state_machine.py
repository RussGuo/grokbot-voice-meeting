import pytest

from orchestrator.state_machine import (
    FLOOR_TRANSITIONS, Floor, IllegalTransition, MeetingStateMachine, Phase,
)


def live():
    sm = MeetingStateMachine()
    for p in (Phase.PREPARING, Phase.OPENING_SYNC, Phase.IN_AGENDA):
        sm.to_phase(p)
    return sm


def test_phase_happy_path():
    sm = live()
    assert sm.floor == Floor.FLOOR_IDLE
    sm.to_phase(Phase.DECIDING)
    sm.to_phase(Phase.IN_AGENDA)
    sm.to_phase(Phase.DECIDING)
    sm.to_phase(Phase.CLOSING)
    sm.to_phase(Phase.POST_MEETING)
    sm.to_phase(Phase.DONE)


@pytest.mark.parametrize("bad", [Phase.IN_AGENDA, Phase.CLOSING, Phase.POST_MEETING])
def test_phase_illegal_from_inviting(bad):
    with pytest.raises(IllegalTransition):
        MeetingStateMachine().to_phase(bad)


def test_floor_bot_turn_with_ducking():
    sm = live()
    for f in (Floor.GRANTING, Floor.SWITCHING, Floor.PLAYING, Floor.DUCKING, Floor.PLAYING,
              Floor.DUCKING, Floor.INTERRUPTED, Floor.HUMAN_SPEAKING, Floor.FLOOR_IDLE):
        sm.to_floor(f)


@pytest.mark.parametrize("a,b", [
    (Floor.FLOOR_IDLE, Floor.PLAYING),          # 必须先 Switching（update_agent）
    (Floor.FLOOR_IDLE, Floor.INTERRUPTED),
    (Floor.FLOOR_IDLE, Floor.DUCKING),
])
def test_floor_illegal(a, b):
    sm = live()
    assert sm.floor == a
    with pytest.raises(IllegalTransition):
        sm.to_floor(b)


def test_human_cannot_jump_to_bot_speaking_and_interrupted_only_to_human():
    assert FLOOR_TRANSITIONS[Floor.INTERRUPTED] == {Floor.HUMAN_SPEAKING}
    assert Floor.PLAYING not in FLOOR_TRANSITIONS[Floor.HUMAN_SPEAKING]


def test_cannot_leave_agenda_while_bot_speaking():
    sm = live()
    for f in (Floor.GRANTING, Floor.SWITCHING, Floor.PLAYING):
        sm.to_floor(f)
    with pytest.raises(IllegalTransition):
        sm.to_phase(Phase.DECIDING)


def test_floor_outside_agenda_raises():
    sm = MeetingStateMachine()
    with pytest.raises(IllegalTransition):
        sm.to_floor(Floor.HUMAN_SPEAKING)


def test_pending_region_parallel():
    sm = live()
    sm.pending_asked()
    sm.to_floor(Floor.HUMAN_SPEAKING)            # 并行区不影响发言子状态
    assert sm.pending.value == "PendingAnswer"
    sm.pending_resolved()
    assert sm.pending.value == "NoPending"
    with pytest.raises(IllegalTransition):
        sm.pending_resolved()
