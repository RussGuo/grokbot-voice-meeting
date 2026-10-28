"""隐藏参会者：录每条音轨 + 数据消息事件日志，写到 runs/<ts>/。
用法: python sim/recorder.py --room ROOM [--out DIR] [--seconds N]
不使用 LiveKit Egress（避免付费）。每条音轨按到达的墙钟时间对齐（缺口补静音）。
另写 levels.jsonl：每 20ms 每条音轨的 RMS dBFS，用于计算插话延迟。"""
import argparse
import asyncio
import json
import math
import signal
import wave
import time
from pathlib import Path
import numpy as np
from livekit import rtc
from common import url, token, now_ms, ROOT

SR = 48000

class TrackWriter:
    def __init__(self, path, t0_ms):
        self.w = wave.open(str(path), "wb"); self.w.setnchannels(1); self.w.setsampwidth(2); self.w.setframerate(SR)
        self.t0 = t0_ms; self.written = 0  # samples
    def write(self, pcm: np.ndarray, arrival_ms: float):
        # 对齐：如果到达时间比已写入位置晚 >60ms，补静音
        expect = int((arrival_ms - self.t0) / 1000 * SR) - len(pcm)
        gap = expect - self.written
        if gap > int(0.06 * SR):
            self.w.writeframes(np.zeros(gap, np.int16).tobytes()); self.written += gap
        self.w.writeframes(pcm.tobytes()); self.written += len(pcm)
    def close(self): self.w.close()

async def main(a):
    out = Path(a.out or ROOT / "runs" / time.strftime("%Y%m%d-%H%M%S")); out.mkdir(parents=True, exist_ok=True)
    t0 = now_ms()
    ev = open(out / "events.jsonl", "a", encoding="utf-8")
    lv = open(out / "levels.jsonl", "a", encoding="utf-8")
    meta = {"room": a.room, "t0_ms": t0, "tracks": {}}
    def log(kind, **kw):
        ev.write(json.dumps({"t_ms": round(now_ms(), 1), "kind": kind, **kw}, ensure_ascii=False) + "\n"); ev.flush()
    room = rtc.Room(); tasks = []

    async def pump(track, pid):
        name = f"{pid}__{track.sid}"
        tw = TrackWriter(out / f"{name}.wav", t0); meta["tracks"][name] = {"identity": pid, "file": f"{name}.wav"}
        log("track_subscribed", identity=pid, track=track.sid)
        stream = rtc.AudioStream(track, sample_rate=SR, num_channels=1)
        try:
            async for e in stream:
                f = e.frame; arr = np.frombuffer(f.data, dtype=np.int16).copy(); t = now_ms()
                tw.write(arr, t)
                rms = math.sqrt(float(np.mean(arr.astype(np.float64) ** 2)) + 1e-9)
                lv.write(json.dumps({"t_ms": round(t, 1), "id": pid, "db": round(20 * math.log10(rms / 32768 + 1e-12), 1)}) + "\n")
        finally:
            tw.close(); log("track_ended", identity=pid, track=track.sid)

    @room.on("track_subscribed")
    def _ts(track, pub, p):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            tasks.append(asyncio.create_task(pump(track, p.identity)))
    @room.on("data_received")
    def _dr(pkt: rtc.DataPacket):
        try: payload = json.loads(pkt.data.decode("utf-8"))
        except Exception: payload = {"raw_len": len(pkt.data)}
        log("data", topic=pkt.topic, frm=pkt.participant.identity if pkt.participant else None, payload=payload)
    @room.on("participant_connected")
    def _pc(p): log("participant_connected", identity=p.identity, pkind=int(p.kind))
    @room.on("participant_disconnected")
    def _pd(p): log("participant_disconnected", identity=p.identity)
    @room.on("transcription_received")
    def _tr(segs, p, pub):
        for s in segs: log("transcription", identity=p.identity if p else None, text=s.text, final=s.final)

    await room.connect(url(), token(a.room, "recorder", hidden=True), rtc.RoomOptions(auto_subscribe=True))
    log("recorder_joined", room=a.room)
    for p in room.remote_participants.values(): log("participant_present", identity=p.identity)
    # 文本流（lk.transcription）
    def _txt(reader, pid):
        async def rd():
            txt = await reader.read_all(); log("text_stream", topic=reader.info.topic, identity=pid, text=txt,
                                              attrs=dict(reader.info.attributes or {}))
        asyncio.create_task(rd())
    try: room.register_text_stream_handler("lk.transcription", _txt)
    except Exception as e: log("warn", msg=f"text stream handler: {e}")

    stop = asyncio.Event()
    for s in (signal.SIGINT, signal.SIGTERM): asyncio.get_running_loop().add_signal_handler(s, stop.set)
    try: await asyncio.wait_for(stop.wait(), timeout=a.seconds)
    except asyncio.TimeoutError: pass
    log("recorder_stopping"); await room.disconnect()
    for t in tasks: t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    ev.close(); lv.close()
    wavs = sorted(out.glob("*__*.wav"))
    if wavs:  # 所有音轨都从 t0 对齐，直接混音
        import subprocess
        args = sum([["-i", str(w)] for w in wavs], [])
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args, "-filter_complex",
                        f"amix=inputs={len(wavs)}:duration=longest:normalize=0", str(out / "mix.wav")], check=False)
    print(out)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--room", required=True); ap.add_argument("--out"); ap.add_argument("--seconds", type=float, default=900)
    asyncio.run(main(ap.parse_args()))
