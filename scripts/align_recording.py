#!/usr/bin/env python3
"""把 sim/recorder.py 的输出（runs/<id>/audio/<identity>__<track>.wav + meta.json，墙钟对齐）
转成 acceptance.py 的约定：runs/<id>/audio/manifest.json = {"tracks": {"<participant_id>": {"file", "start_ms"}}}。

- start_ms = 录音 t0（墙钟）- 编排器 t0_epoch_ms（clock.json），即 wav 第 0 帧对应的会议时间
- 单 AgentSession：所有 bot 共用 agent 参与者的一条音轨 → 每个 bot participant_id 都指向它
- 人类：identity = linked_human_id（MEETING_HUMAN_IDENTITY）
用法：python scripts/align_recording.py runs/<id>
"""
from __future__ import annotations

import json
import sys
import wave
from pathlib import Path


def frames(p: Path) -> int:
    try:
        with wave.open(str(p)) as w:
            return w.getnframes()
    except Exception:  # noqa: BLE001
        return 0


def align(run: Path) -> dict:
    audio = run / "audio"
    meta = json.loads((audio / "meta.json").read_text("utf-8"))
    clock = json.loads((run / "clock.json").read_text("utf-8"))
    created = next(json.loads(l) for l in (run / "events.jsonl").read_text("utf-8").splitlines()
                   if '"MeetingCreated"' in l)["data"]["payload"]
    human = created["linked_human_id"]
    bots = [p["id"] for p in created["participants"] if p["kind"] == "bot"]
    start = int(round(meta["t0_ms"] - clock["t0_epoch_ms"]))
    by_id: dict[str, Path] = {}
    for t in meta["tracks"].values():
        f = audio / t["file"]
        cur = by_id.get(t["identity"])
        if cur is None or frames(f) > frames(cur):      # 同一参与者多条轨：取最长的
            by_id[t["identity"]] = f
    agents = [i for i in by_id if i != human and i != "recorder"]
    tracks = {}
    if human in by_id:
        tracks[human] = {"file": by_id[human].name, "start_ms": start}
    for b in bots:                       # 多身份：bot 自己的参会者轨（identity = bot id）
        if b in by_id:
            tracks[b] = {"file": by_id[b].name, "start_ms": start}
    shared = [i for i in agents if i not in bots]
    if shared:                           # 单身份兜底：没有自己轨的 bot 指向共用的 agent 轨
        agent = max(shared, key=lambda i: frames(by_id[i]))
        for b in bots:
            tracks.setdefault(b, {"file": by_id[agent].name, "start_ms": start, "shared_identity": agent})
    man = {"tracks": tracks, "recorder_t0_ms": meta["t0_ms"], "meeting_t0_epoch_ms": clock["t0_epoch_ms"]}
    (audio / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), "utf-8")
    return man


if __name__ == "__main__":
    print(json.dumps(align(Path(sys.argv[1])), ensure_ascii=False, indent=1))
