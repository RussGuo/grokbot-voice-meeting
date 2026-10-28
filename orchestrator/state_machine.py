"""会议状态机（对应设计页 STATE_MMD）。

三层：
1. 顶层阶段 Phase：Inviting → Preparing → OpeningSync → InAgenda ⇄ Deciding → Closing → PostMeeting → Done
2. InAgenda 内的发言子状态 Floor（BotSpeaking 再分 Playing / Ducking）
3. InAgenda 内的并行区 pending_answer：NoPending / PendingAnswer（按未回答数计数）

任何不在转移表里的转移都抛 IllegalTransition。
"""
from __future__ import annotations

from enum import Enum
from typing import Callable, Optional


class IllegalTransition(RuntimeError):
    pass


class Phase(str, Enum):
    INVITING = "Inviting"
    PREPARING = "Preparing"
    OPENING_SYNC = "OpeningSync"
    IN_AGENDA = "InAgenda"
    DECIDING = "Deciding"
    CLOSING = "Closing"
    POST_MEETING = "PostMeeting"
    DONE = "Done"


class Floor(str, Enum):
    FLOOR_IDLE = "FloorIdle"
    HUMAN_SPEAKING = "HumanSpeaking"
    CUEING = "Cueing"
    GRANTING = "Granting"
    SWITCHING = "Switching"
    PLAYING = "BotSpeaking.Playing"
    DUCKING = "BotSpeaking.Ducking"
    INTERRUPTED = "Interrupted"
    OVERTIME = "Overtime"
    ASKING_PRINCIPAL = "AskingPrincipal"
    SILENCE = "Silence"
    OFF_TOPIC = "OffTopic"


class Pending(str, Enum):
    NO_PENDING = "NoPending"
    PENDING_ANSWER = "PendingAnswer"


BOT_SPEAKING = {Floor.PLAYING, Floor.DUCKING}

PHASE_TRANSITIONS: dict[Phase, set[Phase]] = {
    Phase.INVITING: {Phase.PREPARING},
    Phase.PREPARING: {Phase.OPENING_SYNC},
    Phase.OPENING_SYNC: {Phase.IN_AGENDA},
    Phase.IN_AGENDA: {Phase.DECIDING},
    Phase.DECIDING: {Phase.IN_AGENDA, Phase.CLOSING},
    Phase.CLOSING: {Phase.POST_MEETING},
    Phase.POST_MEETING: {Phase.DONE},
    Phase.DONE: set(),
}

F = Floor
# 设计图中的转移
_DESIGN: dict[Floor, set[Floor]] = {
    F.FLOOR_IDLE: {F.HUMAN_SPEAKING, F.CUEING, F.GRANTING, F.SILENCE, F.OFF_TOPIC},
    F.CUEING: {F.SWITCHING, F.HUMAN_SPEAKING, F.FLOOR_IDLE},
    F.GRANTING: {F.SWITCHING},
    F.SWITCHING: {F.PLAYING},
    F.PLAYING: {F.DUCKING, F.FLOOR_IDLE, F.INTERRUPTED, F.OVERTIME, F.ASKING_PRINCIPAL},
    F.DUCKING: {F.PLAYING, F.FLOOR_IDLE, F.INTERRUPTED, F.OVERTIME, F.ASKING_PRINCIPAL},
    F.ASKING_PRINCIPAL: {F.FLOOR_IDLE},
    F.INTERRUPTED: {F.HUMAN_SPEAKING},
    F.OVERTIME: {F.FLOOR_IDLE},
    F.HUMAN_SPEAKING: {F.FLOOR_IDLE},
    F.SILENCE: {F.CUEING},
    F.OFF_TOPIC: {F.CUEING},
}
# 原型补充转移（设计图未画，待评审）：人类优先——在令牌尚未落到 bot 的过渡态，用户开口直接夺回发言权；
# 冷场/跑题态下用户开口；Switching 失败或令牌过期回到 FloorIdle。
_EXTRA: dict[Floor, set[Floor]] = {
    F.GRANTING: {F.HUMAN_SPEAKING, F.FLOOR_IDLE},
    F.SWITCHING: {F.HUMAN_SPEAKING, F.FLOOR_IDLE},
    F.SILENCE: {F.HUMAN_SPEAKING, F.FLOOR_IDLE},
    F.OFF_TOPIC: {F.HUMAN_SPEAKING, F.FLOOR_IDLE},
}
FLOOR_TRANSITIONS: dict[Floor, set[Floor]] = {k: set(v) | _EXTRA.get(k, set()) for k, v in _DESIGN.items()}

# 离开 InAgenda 进入 Deciding 时，发言子状态必须是空闲类
FLOOR_EXIT_OK = {F.FLOOR_IDLE, F.SILENCE}


class MeetingStateMachine:
    def __init__(self, on_change: Optional[Callable[[str, str, str], None]] = None):
        self.phase = Phase.INVITING
        self.floor: Optional[Floor] = None
        self.pending_count = 0
        self._on_change = on_change or (lambda layer, a, b: None)

    # —— 顶层 ——
    def to_phase(self, nxt: Phase) -> None:
        if nxt not in PHASE_TRANSITIONS[self.phase]:
            raise IllegalTransition(f"阶段 {self.phase.value} → {nxt.value} 不合法")
        if self.phase == Phase.IN_AGENDA and self.floor not in FLOOR_EXIT_OK:
            raise IllegalTransition(f"发言子状态 {self.floor.value if self.floor else None} 下不能离开 InAgenda")
        prev = self.phase
        self.phase = nxt
        if nxt == Phase.IN_AGENDA:
            self.floor = Floor.FLOOR_IDLE   # 进入复合状态的初始子状态
        elif prev == Phase.IN_AGENDA:
            self.floor = None
        self._on_change("phase", prev.value, nxt.value)

    # —— 发言子状态 ——
    def to_floor(self, nxt: Floor) -> None:
        if self.phase != Phase.IN_AGENDA or self.floor is None:
            raise IllegalTransition(f"不在 InAgenda（当前 {self.phase.value}），不能切到 {nxt.value}")
        if nxt not in FLOOR_TRANSITIONS.get(self.floor, set()):
            raise IllegalTransition(f"发言子状态 {self.floor.value} → {nxt.value} 不合法")
        prev = self.floor
        self.floor = nxt
        self._on_change("floor", prev.value, nxt.value)

    def can_floor(self, nxt: Floor) -> bool:
        return self.phase == Phase.IN_AGENDA and self.floor is not None and nxt in FLOOR_TRANSITIONS.get(self.floor, set())

    # —— 并行区 pending_answer ——
    @property
    def pending(self) -> Pending:
        return Pending.PENDING_ANSWER if self.pending_count > 0 else Pending.NO_PENDING

    def pending_asked(self) -> None:
        if self.phase not in (Phase.IN_AGENDA, Phase.DECIDING):
            raise IllegalTransition("只能在会中发出 askPrincipal")
        prev = self.pending
        self.pending_count += 1
        if prev != self.pending:
            self._on_change("pending", prev.value, self.pending.value)

    def pending_resolved(self) -> None:
        """答案到达或 TTL 过期。"""
        if self.pending_count <= 0:
            raise IllegalTransition("没有待回答的 pending_answer")
        prev = self.pending
        self.pending_count -= 1
        if prev != self.pending:
            self._on_change("pending", prev.value, self.pending.value)

    @property
    def bot_speaking(self) -> bool:
        return self.floor in BOT_SPEAKING
