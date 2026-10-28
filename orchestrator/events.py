"""事件溯源的会议事件日志。

- MeetingEvent 是唯一写入路径：编排器每处理一个实时事件，先 append 再改内存状态。
- append-only：不提供修改/删除；seq 单调递增。
- 可存为 JSONL、可从 JSONL 读回；project() 只靠事件重放出纪要视图，用于回放一致性校验。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

from .models import ID, Millis, is_model

# 设计页 MeetingEvent 的 type 全集（另加原型需要的几个，见 EXTRA_TYPES）
EVENT_TYPES = {
    "MeetingCreated", "InviteSent", "InviteAnswered", "BriefSubmitted",
    "AgendaItemStarted", "AgendaItemClosed", "ModeratorSwitched", "AgentSwitched",
    "FloorGranted", "FloorReleased", "FloorRevoked",
    "HandRaised", "HandWithdrawn", "HandExpired",
    "Ducked", "DuckRestored", "UtteranceFinal", "Interrupted",
    "PrincipalAsked", "PrincipalAnswered", "PrincipalExpired",
    "CommitmentMade", "VoteOpened", "VoteCast", "DecisionRecorded",
    "MeetingEnded", "ReconcileResult",
}
# 原型补充（设计页未列，待评审）：
EXTRA_TYPES = {
    "HumanVad",
    "BotFirstAudio",       # bot 首帧音频（整轮延迟 = 用户说完 → 首帧）            # 用户 VAD 起止（验收：人声起点 → bot 停声）
    "PhaseChanged",        # 顶层阶段或 floor 子状态变化，便于回放状态机
    "OutputDropped",       # 过期令牌的输出被丢弃
    "ModeratorRecap",      # 本体答案跨议程插回前的主持人回溯
    "PrincipalDeferred",   # 插回配额用完或会议结束 → 进纪要「会后补充」
    "FloorWarned",         # 单次发言剩 10 秒提示
    "SpeakerPublished",    # 发给 web/ 的 meeting.speaker / meeting.roster 数据消息（审计用）
    "EndConfirm",          # 最后一项冷场：主持人询问是否结束 / 用户确认 / 继续 / 超时结束
    "ContextTruncated",    # chat_ctx 里的 brief / 快照超长被截断
    "DecisionSummarized",  # 议程项结论的后台总结落地（覆盖同一项 DecisionRecorded 里的兜底文本）
    "HumanDirective",      # 用户的口头指令（如「先让工程说说成本」）：stage=parsed / executed
}


@dataclass(frozen=True)
class MeetingEvent:
    seq: int
    at: Millis
    meeting_id: ID
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {"seq": self.seq, "at": self.at, "meeting_id": self.meeting_id, "type": self.type, "data": self.data},
            ensure_ascii=False, sort_keys=True,
        )

    @staticmethod
    def from_json(line: str) -> "MeetingEvent":
        d = json.loads(line)
        return MeetingEvent(seq=d["seq"], at=d["at"], meeting_id=d["meeting_id"], type=d["type"], data=d.get("data", {}))


def _jsonable(v: Any) -> Any:
    if is_model(v):
        return v.to_dict()
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


class EventLog:
    def __init__(self, meeting_id: ID, path: Optional[Path] = None):
        self.meeting_id = meeting_id
        self._events: list[MeetingEvent] = []
        self._subs: list[Callable[[MeetingEvent], None]] = []
        self._path = Path(path) if path else None
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, type_: str, at: Millis, **data: Any) -> MeetingEvent:
        if type_ not in EVENT_TYPES and type_ not in EXTRA_TYPES:
            raise ValueError(f"未知事件类型: {type_}")
        ev = MeetingEvent(seq=len(self._events) + 1, at=at, meeting_id=self.meeting_id,
                          type=type_, data=_jsonable(data))
        # 先确保可序列化（JSON 往返），保证回放一致
        ev = MeetingEvent.from_json(ev.to_json())
        self._events.append(ev)
        if self._path:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(ev.to_json() + "\n")
        for cb in list(self._subs):
            cb(ev)
        return ev

    def subscribe(self, cb: Callable[[MeetingEvent], None]) -> Callable[[], None]:
        self._subs.append(cb)
        return lambda: self._subs.remove(cb)

    @property
    def last_seq(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[MeetingEvent]:
        return iter(tuple(self._events))

    def __len__(self) -> int:
        return len(self._events)

    def of_type(self, *types: str) -> list[MeetingEvent]:
        return [e for e in self._events if e.type in types]

    def to_jsonl(self) -> str:
        return "".join(e.to_json() + "\n" for e in self._events)

    def dump(self, path: Path) -> None:
        Path(path).write_text(self.to_jsonl(), encoding="utf-8")

    @staticmethod
    def load(path_or_text: Path | str) -> list[MeetingEvent]:
        text = Path(path_or_text).read_text(encoding="utf-8") if isinstance(path_or_text, Path) else path_or_text
        return [MeetingEvent.from_json(l) for l in text.splitlines() if l.strip()]


def project(events: Iterable[MeetingEvent]) -> dict[str, Any]:
    """纯函数：只靠事件重放出会议视图（纪要、承诺、令牌历史、本体问答状态）。

    纪要、行动项、承诺审计和会后对账都应从这里派生，而不是从编排器内存派生。
    """
    view: dict[str, Any] = {
        "utterances": [], "commitments": {}, "floor_history": [], "decisions": [],
        "queries": {}, "agenda_started": [], "interruptions": 0, "ducks": 0, "restores": 0,
        "dropped_outputs": 0, "recaps": 0, "agent_switches": 0, "last_seq": 0, "_decision_idx": {},
    }
    for e in events:
        if e.seq != view["last_seq"] + 1:
            raise ValueError(f"事件序号不连续: 期望 {view['last_seq'] + 1} 得到 {e.seq}")
        view["last_seq"] = e.seq
        t, d = e.type, e.data
        if t == "UtteranceFinal":
            u = d["utterance"]
            view["utterances"].append((u["speaker_id"], u["agenda_item_id"], u["text"]))
        elif t == "CommitmentMade":
            c = d["commitment"]
            view["commitments"][c["id"]] = c["status"]
        elif t == "ReconcileResult":
            for item in d.get("items", []):
                if item.get("commitment_id"):
                    view["commitments"][item["commitment_id"]] = {
                        "confirm": "confirmed", "correct": "corrected", "retract": "retracted"}[item["verdict"]]
        elif t in ("FloorGranted", "FloorReleased", "FloorRevoked"):
            tok = d["token"]
            view["floor_history"].append((t, tok["id"], tok["holder_id"], tok["source"]))
        elif t == "DecisionRecorded":
            view["decisions"].append(d.get("payload", {}).get("text"))
            view["_decision_idx"][d.get("payload", {}).get("agenda_item_id")] = len(view["decisions"]) - 1
        elif t == "DecisionSummarized":
            i = view["_decision_idx"].get(d["payload"].get("agenda_item_id"))
            if i is not None:
                view["decisions"][i] = d["payload"]["text"]
        elif t in ("PrincipalAsked", "PrincipalAnswered", "PrincipalExpired", "PrincipalDeferred"):
            q = d["query"]
            view["queries"][q["id"]] = q["status"]
        elif t == "AgendaItemStarted":
            view["agenda_started"].append(d["agenda_item_id"])
        elif t == "Interrupted":
            view["interruptions"] += 1
        elif t == "Ducked":
            view["ducks"] += 1
        elif t == "DuckRestored":
            view["restores"] += 1
        elif t == "OutputDropped":
            view["dropped_outputs"] += 1
        elif t == "ModeratorRecap":
            view["recaps"] += 1
        elif t == "AgentSwitched":
            view["agent_switches"] += 1
    view.pop("_decision_idx")
    return view
