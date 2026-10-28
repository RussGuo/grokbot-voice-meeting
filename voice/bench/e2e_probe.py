"""端到端延迟探针（真实 LiveKit 房间 + 真实火山 ASR/TTS + mock 或 DeepSeek LLM）。

同一进程里起两个 rtc 参与者：
  host（模拟用户：按实时节奏推一段合成人声，同时订阅 bot 音轨并做能量检测）
  agent （AgentSession + LiveKitVoiceLayer，RoomIO 只链接 host）
两边用同一个 time.monotonic 时钟，所以跨参与者的时间差可直接相减（含两次 LiveKit Cloud 往返）。

测项：
  s2s       一轮语音到语音：用户语音结束 → 收到 bot 首个有声音频；分段 asr.final / eot / LLM 首 token / TTS 首帧
  bargein   bot 正在说 → 用户开口：开口 → 收到的 bot 音频 duck（降 ≥10dB）/ 停（< -50dBFS）
  switch    update_agent 切换耗时（layer.switch_agent 返回值）
  trunc     bot A 说话中调用 switch_agent(B)：调用 → A 音频静音的时间；B 开口前是否还收到 A 音频（干净截断）
用法：python bench/e2e_probe.py --llm mock|deepseek --n 5 [--skip s2s,bargein,...]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from livekit import api, rtc  # noqa: E402
from livekit.plugins import silero  # noqa: E402

from voice_layer.config import VoiceConfig  # noqa: E402
from voice_layer.envfile import load_env  # noqa: E402
from voice_layer.livekit_layer import LiveKitVoiceLayer, build_session, room_options  # noqa: E402
from voice_layer.llm_factory import make_llm  # noqa: E402

SR = 48000
FRAME = 480  # 10ms
SPEECH_DB = -45.0


def now_ms() -> float:
    return time.monotonic() * 1000


def pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summary(name, xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"metric": name, "n": 0}
    return {"metric": name, "n": len(xs), "p50_ms": round(pct(xs, .5)), "p90_ms": round(pct(xs, .9)),
            "min_ms": round(min(xs)), "max_ms": round(max(xs))}


def load_48k(path: str) -> np.ndarray:
    a, sr = sf.read(path, dtype="float32", always_2d=False)
    if a.ndim > 1:
        a = a.mean(axis=1)
    if sr != SR:
        n = int(len(a) * SR / sr)
        a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)
    # 去掉首尾静音，得到准确的「语音起止」
    db = 20 * np.log10(np.maximum(np.abs(a), 1e-6))
    idx = np.where(db > -40)[0]
    a = a[max(0, idx[0] - 240): idx[-1] + 240]
    return (np.clip(a, -1, 1) * 32767).astype(np.int16)


def frame_db(fr: rtc.AudioFrame) -> float:
    x = np.frombuffer(fr.data, dtype=np.int16).astype(np.float32)
    if x.size == 0:
        return -120.0
    return 20 * math.log10(max(math.sqrt(float(np.mean(x * x))) / 32768, 1e-6))


class Human:
    def __init__(self, room: rtc.Room) -> None:
        self.room = room
        self.src = rtc.AudioSource(SR, 1, queue_size_ms=50)
        self.rx: list[tuple[float, float]] = []   # (t_ms, dBFS) 收到的 bot 音频
        self.rx_by: dict[str, list[tuple[float, float]]] = {}   # identity -> 同上
        self._rx_task = None

    async def publish(self) -> None:
        tr = rtc.LocalAudioTrack.create_audio_track("mic", self.src)
        await self.room.local_participant.publish_track(tr, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        # 持续推静音，保持轨道活跃
        self._silence = asyncio.create_task(self._silence_loop())

    async def _silence_loop(self) -> None:
        """唯一的推流循环（AudioSource 不允许并发 capture）：有人声就推人声，否则推静音。"""
        self._pending: list = []
        z = rtc.AudioFrame(b"\x00" * FRAME * 2, SR, 1, FRAME)
        while True:
            if self._pending:
                job = self._pending[0]
                i = job["i"]
                chunk = job["pcm"][i:i + FRAME]
                if len(chunk) < FRAME:
                    chunk = np.pad(chunk, (0, FRAME - len(chunk)))
                await self.src.capture_frame(rtc.AudioFrame(chunk.tobytes(), SR, 1, FRAME))
                if job["t_start"] is None:
                    job["t_start"] = now_ms()
                job["i"] = i + FRAME
                if job["i"] >= len(job["pcm"]):
                    job["t_end"] = now_ms()
                    self._pending.pop(0)
                    job["done"].set()
            else:
                await self.src.capture_frame(z)

    def on_track(self, track, pub, participant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO and participant.identity != "host":
            ident = participant.identity
            async def pump():
                async for ev in rtc.AudioStream(track, sample_rate=SR, num_channels=1):
                    x = (now_ms(), frame_db(ev.frame))
                    self.rx.append(x)
                    self.rx_by.setdefault(ident, []).append(x)
            self._rx_task = asyncio.create_task(pump())

    async def speak(self, pcm: np.ndarray) -> tuple[float, float]:
        """按实时节奏推人声，返回 (语音开始, 语音结束) 的 monotonic ms（帧进入 AudioSource 的时刻）。"""
        job = {"pcm": pcm, "i": 0, "t_start": None, "t_end": None, "done": asyncio.Event()}
        self._pending.append(job)
        await job["done"].wait()
        return job["t_start"], job["t_end"]

    def first_voice_after(self, t: float, until: float | None = None) -> float | None:
        for ts, db in self.rx:
            if ts >= t and db > SPEECH_DB and (until is None or ts <= until):
                return ts
        return None

    def level_before(self, t: float, win_ms: float = 300) -> float:
        xs = [db for ts, db in self.rx if t - win_ms <= ts < t and db > -90]
        return float(np.median(xs)) if xs else -120.0

    def first_below(self, t: float, thresh_db: float, hold_ms: float = 30) -> float | None:
        """t 之后第一次连续 hold_ms 都低于阈值的起点。发送端不推帧时接收端也收不到帧，
        所以两帧之间的空档 ≥ hold_ms 也算静音（起点取上一帧时刻）。"""
        start = None
        prev = None
        for ts, db in self.rx:
            if ts < t:
                prev = ts
                continue
            if prev is not None and ts - prev >= hold_ms and start is None:
                return max(prev, t)
            prev = ts
            if db < thresh_db:
                if start is None:
                    start = ts
                if ts - start >= hold_ms:
                    return start
            else:
                start = None
        if prev is not None and now_ms() - prev >= hold_ms and start is None:
            return max(prev, t)
        return start


def d(mk, a, b):
    return round(mk[b] - mk[a]) if (a in mk and b in mk) else None


def token(identity: str, room: str) -> str:
    return (api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
            .with_identity(identity).with_name(identity)
            .with_grants(api.VideoGrants(room_join=True, room=room)).to_jwt())


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", default="mock", choices=["mock", "deepseek"])
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--skip", default="")
    ap.add_argument("--human-wav", default=str(ROOT / "samples" / "zh_female_vv_uranus_bigtts.wav"))
    ap.add_argument("--out", default=str(ROOT / "bench" / "out"))
    ap.add_argument("--framework-interrupt", default="on", choices=["on", "off"])
    ap.add_argument("--discard", default="off", choices=["on", "off"], help="discard_audio_if_uninterruptible")
    ap.add_argument("--auto-stop", default="on", choices=["on", "off"])
    ap.add_argument("--multi", action="store_true", help="每个 bot 独立参会者身份发布音轨")
    a = ap.parse_args()
    load_env()
    skip = set(filter(None, a.skip.split(",")))
    cfg = VoiceConfig()
    cfg.human_identity = "host"
    cfg.interruption.framework_enabled = a.framework_interrupt == "on"
    cfg.interruption.discard_audio_if_uninterruptible = a.discard == "on"
    cfg.interruption.auto_stop = a.auto_stop == "on"
    room_name = f"voice-probe-{uuid.uuid4().hex[:8]}"
    url = os.environ["LIVEKIT_URL"]
    env = {"asr": cfg.resolve_asr_provider(), "tts": cfg.resolve_tts_provider(), "llm": a.llm,
           "framework_interrupt": a.framework_interrupt, "discard": a.discard, "auto_stop": a.auto_stop,
           "livekit": "cloud" if "livekit.cloud" in url else "self-hosted", "room": room_name}
    print(json.dumps({"env": env}, ensure_ascii=False), flush=True)

    human_room, agent_room = rtc.Room(), rtc.Room()
    human = Human(human_room)
    human_room.on("track_subscribed", human.on_track)
    await human_room.connect(url, token("host", room_name))
    await human.publish()
    await agent_room.connect(url, token("agent-voice", room_name))

    vad_sess = silero.VAD.load(min_silence_duration=cfg.vad.min_silence_duration)
    vad_human = silero.VAD.load(min_silence_duration=cfg.vad.min_silence_duration)
    session = build_session(cfg, vad_sess)
    llm_kind = a.llm
    replies = {"pm_bot": "我先补充一下产品侧的看法：离线同步用户呼声很高，建议进入二点零版本。",
               "eng_bot": "工程这边评估过，冲突合并大概需要两周，风险可控。",
               "moderator": "好的，我们进入下一个议题。"}
    llm_factory = (lambda pid: make_llm("deepseek")) if llm_kind == "deepseek" else (lambda pid: make_llm("mock", reply=replies.get(pid, "收到。")))
    layer = LiveKitVoiceLayer(cfg, session, vad=vad_human, llm_factory=llm_factory, room=agent_room,
                              instructions={p: "你是会议中的语音分身，用一两句简短口语中文回答，不要列表。" for p in replies})
    first_agent = layer.agent_for("moderator")
    await session.start(first_agent, room=agent_room, room_options=room_options(cfg))
    speakers = None
    if a.multi:   # 与 main.py 相同的接法（架构师的 BotSpeakers + RoutingAudioOutput）
        from voice_layer.multi_identity import BotSpeakers, RoutingAudioOutput
        speakers = BotSpeakers(room_name)
        await speakers.connect([("moderator", "主持人"), ("pm_bot", "产品分身"), ("eng_bot", "工程分身")])
        session.output.audio = RoutingAudioOutput(session.output.audio, speakers,
                                                  lambda: layer.current.bot_id if layer.current else None)
    await asyncio.sleep(1.0)
    for pid in ("pm_bot", "eng_bot", "moderator"):
        layer.agent_for(pid)  # 预热 TTS 连接
    await asyncio.sleep(2.0)

    pcm = load_48k(a.human_wav)
    results: dict[str, list] = {k: [] for k in ("s2s", "asr_final", "eot", "llm_ttft", "tts_first_frame", "rx_after_tts_frame",
                                                 "duck", "stop", "switch", "trunc_stop", "trunc_clean")}
    raw: list[dict] = []
    evq: list[dict] = []
    layer.subscribe(evq.append)
    fw_user: list[tuple[float, str]] = []
    session.on("user_state_changed", lambda ev: fw_user.append((now_ms(), ev.new_state)))

    async def wait_event(kind: str, after: float, timeout: float = 15.0):
        t_end = time.monotonic() + timeout
        while time.monotonic() < t_end:
            for e in evq:
                if e["type"] == kind and e["at"] >= after:
                    return e
            await asyncio.sleep(0.01)
        return None

    # —— s2s ——
    if "s2s" not in skip:
        for i in range(a.n):
            await layer.switch_agent("pm_bot", None)
            evq.clear()
            layer.marks.clear()
            t0, t1 = await human.speak(pcm)
            eot = await wait_event("eot", t0)
            if eot is None:
                raw.append({"s2s": i, "err": "no eot"})
                continue
            fin = next((e for e in evq if e["type"] == "asr.final" and e["at"] >= t0), None)
            tok = f"tok-s2s-{i}"
            await layer.speak(tok, "pm_bot", None)
            done = await wait_event("tts.done", t1, timeout=30)
            await asyncio.sleep(0.3)
            rx = human.first_voice_after(eot["at"])
            mk = {m["name"]: m["at"] for m in layer.marks if m["token_id"] == tok}
            r = {"s2s": i, "speech_ms": round(t1 - t0),
                 "asr_final_after_speech_end": fin and round(fin["at"] - t1),
                 "eot_after_speech_end": round(eot["at"] - t1),
                 "llm_ttft": d(mk, "llm_start", "llm_first_token"),
                 "tts_first_frame_after_llm_first": d(mk, "llm_first_token", "tts_first_frame"),
                 "rx_after_tts_frame": round(rx - mk["tts_first_frame"]) if (rx and "tts_first_frame" in mk) else None,
                 "s2s_speech_end_to_rx": rx and round(rx - t1), "done": bool(done)}
            raw.append(r)
            print(json.dumps(r, ensure_ascii=False), flush=True)
            results["s2s"].append(r["s2s_speech_end_to_rx"])
            results["asr_final"].append(r["asr_final_after_speech_end"])
            results["eot"].append(r["eot_after_speech_end"])
            results["llm_ttft"].append(r["llm_ttft"])
            results["tts_first_frame"].append(r["tts_first_frame_after_llm_first"])
            results["rx_after_tts_frame"].append(r["rx_after_tts_frame"])
            await asyncio.sleep(1.0)

    long_text = "我把这个议题的背景完整说一遍：离线同步涉及本地数据库、冲突合并、增量上传和多端一致性，" \
                "我们评估了三种方案，各自的成本和风险都不一样，下面我一个一个展开讲，请大家耐心听完。"
    # —— bargein ——
    if "bargein" not in skip:
        for i in range(a.n):
            await layer.switch_agent("eng_bot", None)
            tok = f"tok-barge-{i}"
            layer.say(tok, "eng_bot", long_text)
            fa = await wait_event("tts.first_audio", now_ms() - 10, timeout=10)
            await asyncio.sleep(1.5)
            t_on, _ = await human.speak(pcm[: SR * 2])   # 2 秒插话
            await asyncio.sleep(0.5)
            base = human.level_before(t_on)
            t_duck = human.first_below(t_on, base - 10)
            t_stop = human.first_below(t_on, -50, hold_ms=100)
            vs = next((e for e in evq if e["type"] == "vad.start" and e["at"] >= t_on), None)
            bc = next((e for e in evq if e["type"] == "bargein.confirmed" and e["at"] >= t_on), None)
            asr_in = [e for e in evq if e["type"] in ("asr.partial", "asr.final") and e["at"] >= t_on]
            fw_sp = next((t for t, st in fw_user if t >= t_on and st == "speaking"), None)
            r = {"bargein": i, "base_db": round(base, 1),
                 "asr_events_during_bot_speech": len(asr_in),
                 "asr_first_text": asr_in[0].get("text") if asr_in else None,
                 "framework_vad_speaking_after_onset": fw_sp and round(fw_sp - t_on),
                 "vad_start_after_onset": vs and round(vs["at"] - t_on),
                 "confirmed_after_onset": bc and round(bc["at"] - t_on),
                 "rx_duck_after_onset": t_duck and round(t_duck - t_on),
                 "rx_stop_after_onset": t_stop and round(t_stop - t_on)}
            raw.append(r)
            print(json.dumps(r, ensure_ascii=False), flush=True)
            results["duck"].append(r["rx_duck_after_onset"])
            results["stop"].append(r["rx_stop_after_onset"])
            if a.auto_stop == "off":
                layer.interrupt()
            await wait_event("tts.done", t_on, timeout=5)
            await asyncio.sleep(2.0)   # 等人声 VAD 结束

    # —— switch + trunc ——
    if "switch" not in skip:
        for i in range(a.n):
            await layer.switch_agent("pm_bot", None)
            tok = f"tok-trunc-{i}"
            layer.say(tok, "pm_bot", long_text)
            await wait_event("tts.first_audio", now_ms() - 10, timeout=10)
            await asyncio.sleep(1.2)
            t_call = now_ms()
            r_sw = await layer.switch_agent("eng_bot", None)
            t_sw_done = now_ms()
            await asyncio.sleep(0.6)
            t_silent = human.first_below(t_call, -50, hold_ms=100)
            tok2 = f"tok-trunc-b-{i}"
            t_b = now_ms()
            layer.say(tok2, "eng_bot", "收到，我来回答。")
            await wait_event("tts.done", t_b, timeout=10)
            await asyncio.sleep(0.3)
            t_b_rx = human.first_voice_after(t_b)
            leak = [ts for ts, db in human.rx if t_silent and t_silent + 100 < ts < (t_b_rx or t_b) and db > SPEECH_DB]
            r = {"switch": i, "switch_ms": round(r_sw["switch_ms"]), "a_silent_after_call": t_silent and round(t_silent - t_call),
                 "leak_frames_before_b": len(leak), "b_rx_after_say": t_b_rx and round(t_b_rx - t_b)}
            raw.append(r)
            print(json.dumps(r, ensure_ascii=False), flush=True)
            results["switch"].append(r["switch_ms"])
            results["trunc_stop"].append(r["a_silent_after_call"])
            results["trunc_clean"].append(1 if (t_silent is not None and not leak) else 0)
            await asyncio.sleep(0.5)

    summ = [summary("s2s 语音结束→收到bot首音", results["s2s"]),
            summary("asr.final 距语音结束", results["asr_final"]),
            summary("eot 距语音结束", results["eot"]),
            summary("LLM 首 token", results["llm_ttft"]),
            summary("LLM 首 token→TTS 首帧", results["tts_first_frame"]),
            summary("TTS 首帧→对端收到(含限速+网络)", results["rx_after_tts_frame"]),
            summary("打断：开口→对端听到 duck(-10dB)", results["duck"]),
            summary("打断：开口→对端静音", results["stop"]),
            summary("update_agent 切换", results["switch"]),
            summary("切换调用→旧 TTS 在对端静音", results["trunc_stop"])]
    clean = results["trunc_clean"]
    summ.append({"metric": "旧 TTS 干净截断", "n": len(clean), "clean": sum(clean)})
    for s in summ:
        print(json.dumps(s, ensure_ascii=False), flush=True)
    Path(a.out).mkdir(parents=True, exist_ok=True)
    if speakers is not None:
        session.output.audio.clear_buffer()
        await speakers.aclose()
    by = {k: {"frames": len(v), "voiced_frames": sum(1 for _, db in v if db > SPEECH_DB)} for k, v in human.rx_by.items()}
    print(json.dumps({"rx_by_identity": by,
                      "participants": {p.identity: p.name for p in human_room.remote_participants.values()}},
                     ensure_ascii=False), flush=True)
    Path(a.out, f"e2e_{a.llm}_{int(time.time())}.json").write_text(json.dumps({"env": env, "raw": raw, "summary": summ}, ensure_ascii=False, indent=1))
    try:
        async with api.LiveKitAPI(url.replace("wss://", "https://"), os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"]) as lk:
            await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))
        print(json.dumps({"room_deleted": room_name}), flush=True)
    except Exception as e:
        print(json.dumps({"room_delete_error": str(e)[:200]}), flush=True)
    await session.aclose()
    await layer.aclose()
    await agent_room.disconnect()
    await human_room.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
