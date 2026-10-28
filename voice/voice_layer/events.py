"""语音层事件：dict，字段与 orchestrator/voice_iface.py 约定一致。

- type ∈ VOICE_EVENT_TYPES；participant_id；at = time.monotonic()*1000（毫秒）
- bot 侧事件（tts.first_audio / bot.utterance / tts.done）带 token_id
- 硬规则：语音层不按会议状态过滤任何人声事件（vad.start 在空闲、bot 在说、切换中都立刻上报）
"""
from __future__ import annotations

import time
from typing import Any, Callable

VOICE_EVENT_TYPES = {
    "vad.start", "vad.end", "asr.partial", "asr.final", "eot",
    "bargein.confirmed", "bargein.rejected",
    "tts.first_audio", "tts.segment", "bot.utterance", "tts.done",
}

VoiceEvent = dict[str, Any]


def mono_ms() -> float:
    return time.monotonic() * 1000.0


def make_event(type_: str, participant_id: str, **kw: Any) -> VoiceEvent:
    if type_ not in VOICE_EVENT_TYPES:
        raise ValueError(f"unknown voice event type: {type_}")
    ev: VoiceEvent = {"type": type_, "participant_id": participant_id, "at": kw.pop("at", None) or mono_ms()}
    ev.update({k: v for k, v in kw.items() if v is not None})
    return ev


class EventBus:
    """同步扇出；subscribe 返回 unsubscribe。单个回调抛错不影响其他回调，也不影响语音层。"""

    def __init__(self) -> None:
        self._subs: list[Callable[[VoiceEvent], None]] = []
        self.errors: list[BaseException] = []
        self.history: list[VoiceEvent] = []
        self.keep_history = True

    def subscribe(self, cb: Callable[[VoiceEvent], None]) -> Callable[[], None]:
        self._subs.append(cb)

        def _unsub() -> None:
            if cb in self._subs:
                self._subs.remove(cb)

        return _unsub

    def emit(self, ev: VoiceEvent) -> None:
        if self.keep_history:
            self.history.append(ev)
        for cb in list(self._subs):
            try:
                cb(ev)
            except BaseException as e:  # noqa: BLE001
                self.errors.append(e)
