"""模拟人类参会者（默认 identity = MEETING_HUMAN_IDENTITY / host）。
进房（token 带 agent dispatch，触发编排器入会），持续发布一条麦克风音轨（空闲时发静音），
按脚本播放预合成 wav。
脚本 JSON：{"cues":[{"file":"lines/a.wav","at_ms":15000},
                    {"file":"lines/b.wav","on_speaking":"any"|"<speaker_id>","delay_ms":1500,"after_ms":30000}]}
- at_ms：进房后固定时刻开口
- on_speaking：收到 meeting.speaker state=speaking（且 speaker 不是自己）后延迟 delay_ms 开口（=插话测试）；
  after_ms 表示进房多久后才开始等待该触发
开口/说完时间写进 <out>/sim_human.jsonl。"""
import argparse, asyncio, json, wave, time
from pathlib import Path
import numpy as np
from livekit import rtc
from common import url, token, now_ms, HUMAN_ID, ROOT

SR = 48000; FR = 480  # 10ms

def load_wav(p):
    with wave.open(str(p)) as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16 if sw == 2 else np.int32)
    if sw != 2: a = (a >> 16).astype(np.int16)
    if ch > 1: a = a.reshape(-1, ch).mean(axis=1).astype(np.int16)
    if sr != SR:
        x = np.linspace(0, len(a) - 1, int(len(a) * SR / sr)); a = np.interp(x, np.arange(len(a)), a).astype(np.int16)
    return a

async def main(a):
    if a.lines:  # 架构师的 runs/script/human_lines.json -> cues
        hl = json.loads(Path(a.lines).read_text()); cues = []
        for L in hl["lines"]:
            f = str(Path(a.wav_dir) / f"{L['id']}.wav")
            if L.get("barge_in"):  # 在 t_ms*0.75 之后第一个 bot 开口，再等 3s 插话（落在发言中途）
                cues.append({"file": f, "on_speaking": L.get("barge_speaker", "any"), "after_ms": int(L["t_ms"] * 0.75),
                             "delay_ms": L.get("barge_delay_ms", 3000), "from_meeting_start": True, "id": L["id"]})
            else:  # 到 t_ms 后，等 floor 空闲（最多再等 20s）再开口
                cues.append({"file": f, "at_ms": L["t_ms"], "wait_idle_ms": 20000, "from_meeting_start": True, "id": L["id"]})
        script = {"cues": cues}; base = Path(".")
    else:
        script = json.loads(Path(a.script).read_text()) if a.script else {"cues": []}
        base = Path(a.script).parent if a.script else Path(".")
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    lf = open(out / "sim_human.jsonl", "a", encoding="utf-8")
    def log(kind, **kw): lf.write(json.dumps({"t_ms": round(now_ms(), 1), "kind": kind, **kw}, ensure_ascii=False) + "\n"); lf.flush()

    queue: asyncio.Queue = asyncio.Queue()
    cur_speaker = {"id": None, "state": None}
    pending = [dict(c, file=str((base / c["file"]).resolve()) if not Path(c["file"]).is_absolute() else c["file"]) for c in script["cues"]]
    room = rtc.Room(); t_join = None; t_meet = {"t": None}; fns = {}
    def t_ref(c): return t_meet["t"] if c.get("from_meeting_start") else t_join

    @room.on("data_received")
    def _dr(pkt):
        if pkt.topic in ("meeting.roster", "meeting.speaker") and t_meet["t"] is None:
            t_meet["t"] = now_ms(); log("meeting_start_seen", topic=pkt.topic)
            if "schedule_timed" in fns: fns["schedule_timed"]()
        if pkt.topic != "meeting.speaker": return
        try: m = json.loads(pkt.data.decode())
        except Exception: return
        cur_speaker.update(id=m.get("speaker_id"), state=m.get("state"))
        if m.get("state") == "speaking" and m.get("speaker_id") != HUMAN_ID:
            for c in list(pending):
                if "on_speaking" in c and (c["on_speaking"] in ("any", m.get("speaker_id"))) \
                        and t_ref(c) is not None and now_ms() - t_ref(c) >= c.get("after_ms", 0):
                    pending.remove(c)
                    asyncio.get_running_loop().call_later(c.get("delay_ms", 1000) / 1000, queue.put_nowait, (c, m.get("speaker_id")))
                    break

    await room.connect(url(), token(a.room, HUMAN_ID, a.name, dispatch=not a.no_dispatch))
    t_join = now_ms(); log("joined", room=a.room, identity=HUMAN_ID)
    src = rtc.AudioSource(SR, 1)
    track = rtc.LocalAudioTrack.create_audio_track("sim-mic", src)
    await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    log("published")
    loop = asyncio.get_running_loop()
    async def timed(c):
        await asyncio.sleep(max(0, (t_ref(c) + c["at_ms"] - now_ms()) / 1000))
        dl = now_ms() + c.get("wait_idle_ms", 0)
        while cur_speaker["state"] == "speaking" and cur_speaker["id"] != HUMAN_ID and now_ms() < dl:
            await asyncio.sleep(0.1)
        queue.put_nowait((c, None))
    def schedule_timed():
        for c in [c for c in pending if "at_ms" in c and t_ref(c) is not None]:
            pending.remove(c); loop.create_task(timed(c))
    fns["schedule_timed"] = schedule_timed; schedule_timed()

    silence = rtc.AudioFrame(bytes(FR * 2), SR, 1, FR)
    end_at = t_join + a.seconds * 1000
    while now_ms() < end_at:
        if not queue.empty():
            c, interrupted = queue.get_nowait(); pcm = load_wav(c["file"])
            if interrupted is None and c.get("wait_idle_ms"):  # 出队时再确认一次 floor 空闲
                dl = now_ms() + c["wait_idle_ms"]
                while cur_speaker["state"] == "speaking" and cur_speaker["id"] != HUMAN_ID and now_ms() < dl:
                    await src.capture_frame(silence)
            log("speak_start", file=Path(c["file"]).name, line_id=c.get("id"), interrupting=interrupted,
                bot_state=cur_speaker["state"], dur_ms=round(len(pcm) / SR * 1000))
            for i in range(0, len(pcm), FR):
                chunk = pcm[i:i + FR]
                if len(chunk) < FR: chunk = np.pad(chunk, (0, FR - len(chunk)))
                await src.capture_frame(rtc.AudioFrame(chunk.tobytes(), SR, 1, FR))
            log("speak_end", file=Path(c["file"]).name)
        else:
            await src.capture_frame(silence)
    log("leaving"); await room.disconnect()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", required=True); ap.add_argument("--script"); ap.add_argument("--out", required=True)
    ap.add_argument("--name", default="用户（模拟）"); ap.add_argument("--seconds", type=float, default=600)
    ap.add_argument("--no-dispatch", action="store_true")
    ap.add_argument("--lines", help="runs/script/human_lines.json（与 --script 二选一）")
    ap.add_argument("--wav-dir", default=str(ROOT / "sim" / "lines"))
    asyncio.run(main(ap.parse_args()))
