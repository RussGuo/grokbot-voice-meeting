"""worker 端的网页控制：解析 / 校验 `meeting.control` 消息，以及会中加 bot 的后台构建。

不依赖 livekit，worker.py 只负责接线（发送方校验、起后台任务、连接 LiveKit），这里的逻辑可以离线单测。

页面发来的 JSON（topic=meeting.control，只认用户身份；其他字段一律忽略）：
    {"action": "end"}                                         结束会议
    {"action": "interrupt"}                                   打断当前发言者
    {"action": "agenda_add", "title": "..."}                  在末尾加一项议程（≤80 字）
    {"action": "agenda_next"}                                 现在跳到下一项（最后一项不行）
    {"action": "agenda_rename", "title": "...", "item_id"?}   给当前议程项改名；item_id 与当前项不符则拒绝
    {"action": "bot_add", "bot_id": "pm"}                     把注册表里的 bot 请进会议

`parse_control` 的返回值就是要投进 `orchestrator.inbox` 的事件；字段不合法时返回 `control.rejected`，
由编排器落成 ControlRejected 事件让网页看到原因，而不是悄悄吞掉。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Optional

from orchestrator.orchestrator import BOT_ID_RE, MAX_BOTS, clean_title
from orchestrator.scene import bot_participant, brief_from_blob
from orchestrator.models import Brief, Participant

from .briefs import summarize
from .registry import load_registry, read_context, registry_path

log = logging.getLogger("voice-meeting")

MAX_RAW_BYTES = 2048            # 控制消息很小；超过的直接丢
BUILD_TIMEOUT_S = 60.0          # 摘要 brief（阻塞 LLM）的总等待上限
CONNECT_TIMEOUT_S = 20.0        # bot 作为独立参会者连进房间的上限
MODERATOR_KEY = "__moderator__"  # 注册表里主持人那一项，不是可加入的 bot
_ACTION_SAFE = re.compile(r"[A-Za-z0-9_]{1,32}")
_SIMPLE = {"end", "interrupt", "agenda_next"}


class ControlError(Exception):
    """加 bot 失败。code 是短代码，经 ControlRejected.reason 到网页再翻成中文。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ———————————————————— 解析 ————————————————————

def _reject(action: Any, reason: str) -> dict:
    a = action if isinstance(action, str) and _ACTION_SAFE.fullmatch(action) else "?"
    return {"type": "control.rejected", "action": a, "reason": reason}


def parse_control(raw: bytes | bytearray | memoryview | str) -> Optional[dict]:
    """把一条 meeting.control 消息变成编排器 inbox 事件；不是 JSON 对象 / 过大返回 None（只记日志）。"""
    try:
        if len(raw) > MAX_RAW_BYTES:
            log.warning("忽略过大的控制消息（%d 字节）", len(raw))
            return None
        text = raw if isinstance(raw, str) else bytes(raw).decode("utf-8")
        msg = json.loads(text)
    except (ValueError, TypeError):      # UnicodeDecodeError / JSONDecodeError 都是 ValueError
        return None
    if not isinstance(msg, dict):
        return None
    action = msg.get("action")
    if action in _SIMPLE:
        return {"type": f"control.{action}"}
    if action == "agenda_add":
        title = clean_title(msg.get("title"))
        return {"type": "control.agenda_add", "title": title} if title else _reject(action, "invalid")
    if action == "agenda_rename":
        title = clean_title(msg.get("title"))
        item_id = msg.get("item_id")
        if title is None or not (item_id is None or (isinstance(item_id, str) and BOT_ID_RE.fullmatch(item_id))):
            return _reject(action, "invalid")
        ev = {"type": "control.agenda_rename", "title": title}
        if item_id is not None:
            ev["item_id"] = item_id
        return ev
    if action == "bot_add":
        bid = msg.get("bot_id")
        if not isinstance(bid, str) or not BOT_ID_RE.fullmatch(bid) or bid == MODERATOR_KEY:
            return _reject(action, "invalid")
        return {"type": "control.bot_add", "bot_id": bid}
    return _reject(action, "unknown_action")


# ———————————————————— 注册表 ————————————————————

def load_worker_registry(explicit: str | None = None) -> tuple[dict[str, dict], Optional[str]]:
    """worker 所在机器上的 bots.yaml。返回 (registry, 失败原因)；没有 / 读不了就是空表，会议照常，只是不能加 bot。"""
    try:
        path = registry_path(explicit)
        if path is None:
            return {}, "no_registry"
        return load_registry(path), None
    except (OSError, ValueError, RuntimeError) as e:
        log.warning("读取 bots 注册表失败（%s），本场不提供会中加 bot", type(e).__name__)
        return {}, "registry_unreadable"


def addable_bots(registry: dict[str, dict], in_meeting: Iterable[str]) -> dict[str, str]:
    """可加入的 bot：注册表里还没入会的。只给 id 和显示名（context 不出 worker）。"""
    here = set(in_meeting)
    out: dict[str, str] = {}
    for bid, meta in registry.items():
        if bid == MODERATOR_KEY or bid in here or not BOT_ID_RE.fullmatch(bid) or not isinstance(meta, dict):
            continue
        out[bid] = str(meta.get("display_name") or bid)[:60]
    return out


# ———————————————————— 构建一个要加入的 bot ————————————————————

@dataclass
class BuiltBot:
    participant: Participant
    brief: Brief
    voice: str


def pick_voice(bot_id: str, voices: dict[str, str], preferred: str | None = None) -> str:
    """分配一个没人用的音色。voices = 已入会各方 {id: 火山 speaker}；池子不够抛 ControlError("no_voice")。"""
    import voice as _voice_pkg  # noqa: F401  # 把 voice/ 加进 sys.path，使 voice_layer 可导入
    from voice_layer.config import VERIFIED_VOICE_POOL, assign_voices  # type: ignore
    pool = {s for s, _ in VERIFIED_VOICE_POOL}
    held = {b: v for b, v in voices.items() if v in pool and b != bot_id}    # 池外的音色与池内不会冲突，不占位
    try:
        out = assign_voices([*held, bot_id], preferred={**held, **({bot_id: preferred} if preferred else {})})
    except ValueError as e:
        raise ControlError("no_voice") from e
    return out[bot_id]


def _brief_sync(bot_id: str, meta: dict, topic: str, agenda: list[dict], lang: str, chat: Any, model: str,
                context_index: Any = None) -> dict:
    """阻塞：读 context + 摘要 brief（可能是 LLM 调用）。只在线程里跑。LLM 失败退回启发式 brief。
    有 context_index 时顺手把完整 context 分段建索引（ContextIndex.add_bot 是整体换入，线程里调用安全）。"""
    try:
        context = read_context({**meta, "id": bot_id})
    except (OSError, ValueError) as e:
        raise ControlError("context_unreadable") from e
    if context_index is not None and context:
        try:
            context_index.add_bot(bot_id, context)
        except Exception as e:  # noqa: BLE001 — 没有笔记检索不该让 bot 加不进来
            log.warning("bot %s 的笔记建索引失败（%s），本场它没有笔记检索", bot_id, type(e).__name__)
    try:
        return summarize(bot_id=bot_id, context=context, topic=topic, agenda=agenda, lang=lang, chat=chat, model=model)
    except Exception as e:  # noqa: BLE001 — LLM / 网络 / 缓存任何失败都不该让 bot 加不进来
        log.warning("bot %s 的 brief 摘要失败（%s），改用启发式 brief", bot_id, type(e).__name__)
    try:
        return summarize(bot_id=bot_id, context=context, topic=topic, agenda=agenda, lang=lang, chat=None)
    except Exception as e:  # noqa: BLE001
        raise ControlError("brief_failed") from e


async def build_bot(bot_id: str, *, meeting, registry: dict[str, dict], voices: dict[str, str], chat: Any = None,
                    model: str = "heuristic", context_index: Any = None) -> BuiltBot:
    """校验 → 分配音色 → 线程里摘要 brief → 组装 Participant + Brief。

    事件循环上只做廉价的快照与校验；阻塞调用 summarize 在 asyncio.to_thread 里。音色在事件循环上分配并立刻记进 voices
    （同时有两个 bot 在加入时不会拿到同一个音色）；失败时调用方负责把它从 voices 里撤掉。"""
    meta = registry.get(bot_id)
    if bot_id == MODERATOR_KEY or not BOT_ID_RE.fullmatch(bot_id) or not isinstance(meta, dict):
        raise ControlError("unknown_bot")
    if any(p.id == bot_id for p in meeting.participants):
        raise ControlError("already_in_meeting")
    if sum(1 for p in meeting.participants if p.kind == "bot") >= MAX_BOTS + 1:   # +1：主持人
        raise ControlError("too_many_bots")
    lang = getattr(meeting, "language", "zh")
    topic = meeting.title
    agenda = [{"id": a.id, "title": a.title} for a in meeting.agenda]            # 含会中新增 / 改名后的议程
    voice = pick_voice(bot_id, voices, meta.get("voice") if isinstance(meta.get("voice"), str) else None)
    voices[bot_id] = voice
    try:
        blob = await asyncio.wait_for(
            asyncio.to_thread(_brief_sync, bot_id, dict(meta), topic, agenda, lang, chat, model, context_index),
            BUILD_TIMEOUT_S)
    except BaseException:
        voices.pop(bot_id, None)
        raise
    participant = Participant.from_dict(bot_participant(bot_id, meta, voice, lang))
    brief = brief_from_blob(bot_id, blob, topic, [a["id"] for a in agenda])
    return BuiltBot(participant, brief, voice)


async def run_bot_add(bot_id: str, *, meeting, registry: dict[str, dict], voices: dict[str, str], chat: Any,
                      model: str, inbox: Any, apply_voice: Callable[[str, str], None],
                      connect: Optional[Callable[[str, str], Awaitable[None]]] = None,
                      warm: Optional[Callable[[str], Any]] = None, context_index: Any = None) -> None:
    """后台任务：build_bot → 写音色 → 连接独立参会者 → 预热 TTS → 把结果投回编排器 inbox。
    成功投 control.bot_ready（带 Participant + Brief 对象），任何失败投 control.bot_failed；绝不向外抛（会议不能因此崩）。"""
    def fail(code: str) -> None:
        inbox.put_nowait({"type": "control.bot_failed", "bot_id": bot_id, "reason": code})

    try:
        built = await build_bot(bot_id, meeting=meeting, registry=registry, voices=voices, chat=chat, model=model,
                                context_index=context_index)
    except asyncio.CancelledError:
        raise
    except ControlError as e:
        return fail(e.code)
    except asyncio.TimeoutError:
        return fail("timeout")
    except Exception as e:  # noqa: BLE001
        log.warning("加入 bot %s：构建失败（%s）", bot_id, type(e).__name__)
        return fail("internal")
    try:
        apply_voice(bot_id, built.voice)
        if connect is not None:
            await asyncio.wait_for(connect(bot_id, built.participant.display_name), CONNECT_TIMEOUT_S)
        if warm is not None:
            warm(bot_id)
    except asyncio.CancelledError:
        voices.pop(bot_id, None)
        raise
    except asyncio.TimeoutError:
        voices.pop(bot_id, None)
        return fail("connect_failed")
    except Exception as e:  # noqa: BLE001
        log.warning("加入 bot %s：连接 / 预热失败（%s）", bot_id, type(e).__name__)
        voices.pop(bot_id, None)
        return fail("connect_failed")
    inbox.put_nowait({"type": "control.bot_ready", "participant": built.participant, "brief": built.brief,
                      "voice": built.voice})
