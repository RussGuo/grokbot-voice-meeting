"""按参会者的音量与开口/闭口事件，推到 LiveKit data topic `meeting.level`（给可视化界面用）。

消息（JSON）：
  {"type": "level",    "participant_id": "pm_bot", "dbfs": -23.4, "t_ms": <epoch ms>}   ~10Hz，仅在有声或刚转静音时发
  {"type": "speaking", "participant_id": "host", "speaking": true|false, "t_ms": <epoch ms>}
bot 的电平取自送出去的 TTS 帧（已含 duck 增益，即房间里实际听到的）；人类取自订阅到的人声音轨。
开口/闭口用能量 + 迟滞：dbfs > start_db 记开口，连续 hold_ms 低于 stop_db 记闭口。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

LEVEL_TOPIC = "meeting.level"
SILENT_DB = -100.0


@dataclass
class _Acc:
    sumsq: float = 0.0
    n: int = 0
    speaking: bool = False
    below_since: Optional[float] = None
    reported_silent: bool = True


@dataclass
class LevelMeter:
    start_db: float = -42.0
    stop_db: float = -50.0
    hold_ms: float = 300.0
    clock: Callable[[], float] = field(default=lambda: time.time() * 1000.0)
    _acc: dict = field(default_factory=dict)

    def add(self, pid: str, samples) -> None:
        """samples：int16 的 bytes/memoryview/ndarray（单声道或交错多声道都行）。"""
        a = np.frombuffer(samples, dtype=np.int16) if not isinstance(samples, np.ndarray) else samples
        if a.size == 0:
            return
        x = a.astype(np.float32) / 32768.0
        acc = self._acc.setdefault(pid, _Acc())
        acc.sumsq += float(np.dot(x, x))
        acc.n += int(x.size)

    def tick(self) -> list[dict]:
        """每 ~100ms 调一次；返回本窗口要发的消息。"""
        now = self.clock()
        out: list[dict] = []
        for pid, acc in self._acc.items():
            db = SILENT_DB if acc.n == 0 or acc.sumsq <= 0 else max(SILENT_DB, 10 * math.log10(acc.sumsq / acc.n))
            acc.sumsq, acc.n = 0.0, 0
            if db > SILENT_DB or not acc.reported_silent:
                out.append({"type": "level", "participant_id": pid, "dbfs": round(db, 1), "t_ms": round(now)})
                acc.reported_silent = db <= SILENT_DB
            if not acc.speaking and db > self.start_db:
                acc.speaking, acc.below_since = True, None
                out.append({"type": "speaking", "participant_id": pid, "speaking": True, "t_ms": round(now)})
            elif acc.speaking:
                if db < self.stop_db:
                    acc.below_since = acc.below_since if acc.below_since is not None else now
                    if now - acc.below_since >= self.hold_ms:
                        acc.speaking, acc.below_since = False, None
                        out.append({"type": "speaking", "participant_id": pid, "speaking": False, "t_ms": round(now)})
                else:
                    acc.below_since = None
        return out
