"""v0.2 设计页 TYPES_CODE 的 Python dataclass 版本。

约定：时间单位一律毫秒（Millis = int），由注入的时钟提供，便于测试与回放。
所有 dataclass 都可用 to_dict() 序列化进事件日志（JSON）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
from typing import Any, Literal, Optional

ID = str
Millis = int

ParticipantKind = Literal["human", "bot"]
FloorSource = Literal["human", "pendingAnswer", "moderatorCue", "handRaise", "free"]

# 调度基础优先级（设计页「发言调度算法」）
PRIORITY: dict[str, int] = {
    "human": 100,
    "pendingAnswer": 70,
    "moderatorCue": 60,
    "handRaise": 40,
    "free": 10,
}


class Serializable:
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)  # type: ignore[arg-type]

    @classmethod
    def from_dict(cls, d: dict[str, Any]):
        """浅层反序列化：只处理本模块已知的嵌套 dataclass 字段。"""
        kwargs = {}
        hints = {f.name: f for f in fields(cls)}  # type: ignore[arg-type]
        for k, v in d.items():
            if k not in hints:
                continue
            kwargs[k] = _NESTED.get((cls.__name__, k), lambda x: x)(v)
        return cls(**kwargs)


@dataclass
class AgentProfile(Serializable):
    tts_voice: str            # 音色占位名，由 voice/ 映射到火山（豆包）voice_type
    llm: str = "ark"          # LLM 占位：ark 表示火山方舟 OpenAI 兼容接口；fake 表示测试用
    instructions: str = ""


@dataclass
class PrincipalRef(Serializable):
    bot_id: ID
    channel: Literal["mcp", "message"] = "mcp"


@dataclass
class Participant(Serializable):
    id: ID
    kind: ParticipantKind
    display_name: str
    role: Literal["moderator", "member", "observer"] = "member"
    principal_ref: Optional[PrincipalRef] = None
    agent_profile: Optional[AgentProfile] = None
    avatar_url: Optional[str] = None
    invite: Literal["pending", "accepted", "declined"] = "accepted"
    speaking_budget_ms: Millis = 45_000
    aliases: list[str] = field(default_factory=list)  # 被点名检测用的称呼


@dataclass
class Decision(Serializable):
    text: str
    method: Literal["consensus", "vote", "ownerCall"] = "consensus"
    votes: Optional[dict[ID, str]] = None


@dataclass
class AgendaItem(Serializable):
    id: ID
    title: str
    owner_id: ID
    timebox_ms: Millis
    goal: Literal["inform", "discuss", "decide"] = "discuss"
    status: Literal["todo", "active", "decided", "parked"] = "todo"
    decision: Optional[Decision] = None
    keywords: list[str] = field(default_factory=list)  # 跑题/相关度规则打分用


@dataclass
class ModeratorConfig(Serializable):
    mode: Literal["neutral", "chiefOfStaff"] = "neutral"
    participant_id: ID = "moderator"
    switched_at: Optional[Millis] = None


@dataclass
class MeetingRules(Serializable):
    floor_ttl_ms: Millis = 45_000          # bot 单次发言上限
    floor_warn_ms: Millis = 35_000         # 剩 10 秒时主持人提示
    max_end_asks: int = 2                  # 「是否结束」最多问 2 次
    silence_ms: Millis = 4_000             # 冷场 4s 主持人小结或 cue
    silence_advance_ms: Millis = 8_000     # 冷场 8s 推进议程
    max_bot_chain: int = 4                 # 连续 4 轮只有 bot 发言 → 交还人类
    cooldown_ms: Millis = 15_000           # 刚说完的 bot 冷却
    hand_ttl_ms: Millis = 20_000           # 举手 20s 过期
    hand_threshold: float = 0.6            # 举手阈值
    novelty_floor: float = 0.3             # 新信息量低于此不举手
    bargein_min_duration_ms: Millis = 250  # 真打断判定（min_duration 0.2-0.3s）
    duck_gain_db: float = -18.0            # 软打断压低增益
    cue_timeout_ms: Millis = 5_000         # 被 cue 者 5s 未响应跳过
    max_reinsert_per_item: int = 2         # 每议程项最多插回 2 次本体答案
    max_pending_per_bot: int = 2           # 每个分身最多 2 个 pending_answer
    minutes_window: int = 8                # chat_ctx 带最近 8 条共享纪要
    total_ms: Millis = 30 * 60_000
    end_confirm_timeout_ms: Millis = 15_000
    opening_round: bool = False            # 开场后主持人依次 cue 每个成员 bot 一句话立场  # 最后一项冷场：主持人问「是否结束」后再冷场 15s 才结束


@dataclass
class VoiceConfig(Serializable):
    provider: Literal["livekitCloud", "livekitSelfHosted"] = "livekitCloud"
    room: str = "meeting-dev"
    linked_human_id: ID = "host"         # 必须等于用户的 LiveKit identity


@dataclass
class Meeting(Serializable):
    id: ID
    title: str
    initiator: ID
    participants: list[Participant]
    agenda: list[AgendaItem]
    language: str = "zh"          # zh | en；提示词与主持稿的语言
    status: Literal["inviting", "preparing", "live", "closing", "post", "archived"] = "inviting"
    current_agenda_idx: int = 0
    moderator: ModeratorConfig = field(default_factory=ModeratorConfig)
    rules: MeetingRules = field(default_factory=MeetingRules)
    voice: VoiceConfig = field(default_factory=VoiceConfig)

    def participant(self, pid: ID) -> Participant:
        for p in self.participants:
            if p.id == pid:
                return p
        raise KeyError(pid)

    @property
    def current_agenda(self) -> Optional[AgendaItem]:
        if 0 <= self.current_agenda_idx < len(self.agenda):
            return self.agenda[self.current_agenda_idx]
        return None


@dataclass
class FloorToken(Serializable):
    id: ID
    holder_id: ID
    source: FloorSource
    granted_at: Millis
    ttl_ms: Millis
    interruptible: bool = True
    switch_ms: Optional[float] = None      # update_agent 实际耗时，待实测
    intent: Optional[str] = None           # 主持人发言意图：recap / fill_silence / ask_human / cue


@dataclass
class HandScore(Serializable):
    relevance: float
    confidence: float
    novelty: float
    addressed: bool
    total: float


@dataclass
class HandRaise(Serializable):
    id: ID
    participant_id: ID
    agenda_item_id: ID
    kind: Literal["normal", "pendingAnswer"]
    score: HandScore
    gist: str
    based_on_event_seq: int
    created_at: Millis
    expires_at: Millis
    query_id: Optional[ID] = None


@dataclass
class Commitment(Serializable):
    """决策 1：分身承诺当场生效（binding），会后本体可确认或更正。"""
    id: ID
    text: str
    owner_id: ID
    made_by: ID
    utterance_id: ID
    status: Literal["binding", "confirmed", "corrected", "retracted"] = "binding"
    correction: Optional[str] = None


@dataclass
class Utterance(Serializable):
    id: ID
    speaker_id: ID
    agenda_item_id: ID
    text: str
    started_at: Millis
    ended_at: Millis
    token_id: Optional[ID] = None
    planned_text: Optional[str] = None
    interrupted: Optional[dict[str, Any]] = None   # {"by": ID, "at_char": int}
    ducked: list[dict[str, Any]] = field(default_factory=list)  # [{"at": ms, "restored": bool}]
    addressees: list[ID] = field(default_factory=list)
    provenance: Optional[Literal["brief", "snapshot", "principal", "model"]] = None
    confidence: Optional[float] = None
    commitments: list[Commitment] = field(default_factory=list)


@dataclass
class PrincipalAnswer(Serializable):
    text: str
    received_at: Millis


@dataclass
class PrincipalQuery(Serializable):
    """pending_answer：会中慢路径向本体异步提问。"""
    id: ID
    asked_by: ID
    principal_bot_id: ID
    question: str
    agenda_item_id: ID
    context_event_seq: int
    asked_at: Millis
    ttl_ms: Millis
    status: Literal["pending", "answered", "expired", "deliveredInMeeting", "deferredToMinutes"] = "pending"
    answer: Optional[PrincipalAnswer] = None
    round_trip_ms: Optional[int] = None    # 往返延迟，待实测


@dataclass
class SnapshotFact(Serializable):
    key: str
    value: str
    source: str
    as_of: str


@dataclass
class MemorySnapshot(Serializable):
    """决策 2：按会议主题导出，参会各方互相可见。"""
    topic: str
    facts: list[SnapshotFact]
    open_todos: list[dict[str, Any]] = field(default_factory=list)
    recent_decisions: list[str] = field(default_factory=list)
    red_lines: list[str] = field(default_factory=list)
    exported_at: str = ""
    token_estimate: int = 0
    visibility: Literal["sharedWithParticipants"] = "sharedWithParticipants"


@dataclass
class Position(Serializable):
    agenda_item_id: ID
    stance: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class Mandate(Serializable):
    can_commit: list[str] = field(default_factory=list)
    must_ask: list[str] = field(default_factory=list)


@dataclass
class Brief(Serializable):
    participant_id: ID
    agenda_item_ids: list[ID]
    positions: list[Position]
    memory_snapshot: MemorySnapshot
    mandate: Mandate
    questions_for_others: list[dict[str, str]] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)  # 相关度规则打分用的领域词


@dataclass
class ChatCtxRecipe(Serializable):
    """编排器组装给 Agent 的上下文（update_agent 切换后新 Agent 默认不带历史）。"""
    participant_id: ID
    system: str                       # 角色 instructions + 会议规则
    brief: Optional[dict[str, Any]]   # 本 bot 的 brief（主持人为 None）
    topic_snapshots: dict[ID, dict[str, Any]]  # 各方主题快照（共享可见）
    shared_minutes: list[str]         # 最近 N 条共享纪要
    max_turns: int = 8
    turn_instruction: str = ""        # 本轮要做什么（回答、回溯、转述本体答案等）
    turn_meta: dict[str, Any] = field(default_factory=dict)  # 结构化意图，给 fake LLM / 工具用，不进 prompt

    brief_text: str = ""              # 压缩后的 brief（≤800 字，见 brief_compact.py）
    snapshots_text: dict[ID, str] = field(default_factory=dict)  # 其他参会方快照的关键数字

    def to_messages(self, compact: bool = True) -> list[dict[str, str]]:
        """转成通用 role/content 列表；main.py 再转成 livekit ChatContext。

        compact=True（默认）用压缩文本；False 为全量 JSON（只用于对比测试）。
        """
        import json
        msgs = [{"role": "system", "content": self.system}]
        if compact:
            if self.brief_text:
                msgs.append({"role": "system", "content": "【你的会前 brief】\n" + self.brief_text})
            if self.snapshots_text:
                msgs.append({"role": "system", "content": "【其他参会方共享快照（关键数字）】\n" + "\n".join(
                    f"{pid}：\n{t}" for pid, t in self.snapshots_text.items() if t)})
        else:
            if self.brief is not None:
                msgs.append({"role": "system", "content": "【你的会前 brief】\n" + json.dumps(self.brief, ensure_ascii=False)})
            if self.topic_snapshots:
                msgs.append({"role": "system", "content": "【参会各方主题记忆快照（共享可见）】\n" + json.dumps(self.topic_snapshots, ensure_ascii=False)})
        if self.shared_minutes:
            msgs.append({"role": "user", "content": "【最近会议纪要】\n" + "\n".join(self.shared_minutes[-self.max_turns:])})
        if self.turn_instruction:
            msgs.append({"role": "user", "content": "【本轮任务】" + self.turn_instruction})
        return msgs


def _list_of(cls):
    return lambda v: [cls.from_dict(x) if isinstance(x, dict) else x for x in (v or [])]


def _opt(cls):
    return lambda v: cls.from_dict(v) if isinstance(v, dict) else v


_NESTED = {
    ("FloorToken", ""): None,
    ("HandRaise", "score"): _opt(HandScore),
    ("Utterance", "commitments"): _list_of(Commitment),
    ("PrincipalQuery", "answer"): _opt(PrincipalAnswer),
    ("MemorySnapshot", "facts"): _list_of(SnapshotFact),
    ("Brief", "positions"): _list_of(Position),
    ("Brief", "memory_snapshot"): _opt(MemorySnapshot),
    ("Brief", "mandate"): _opt(Mandate),
    ("Participant", "principal_ref"): _opt(PrincipalRef),
    ("Participant", "agent_profile"): _opt(AgentProfile),
    ("AgendaItem", "decision"): _opt(Decision),
    ("Meeting", "participants"): _list_of(Participant),
    ("Meeting", "agenda"): _list_of(AgendaItem),
    ("Meeting", "moderator"): _opt(ModeratorConfig),
    ("Meeting", "rules"): _opt(MeetingRules),
    ("Meeting", "voice"): _opt(VoiceConfig),
}
_NESTED.pop(("FloorToken", ""))


def is_model(obj: Any) -> bool:
    return is_dataclass(obj) and not isinstance(obj, type)
