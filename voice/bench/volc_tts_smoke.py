"""豆包 TTS 2.0 双向流式冒烟：测 TTFB（StartSession 发出后首个音频包 / 首个 TaskRequest 后首包）。
用法：python bench/volc_tts_smoke.py [--speaker ID] [--n 5] [--save samples/x.wav]
不打印任何 key；出错时打印错误码与 X-Tt-Logid。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from pathlib import Path

import aiohttp
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from voice_layer.envfile import load_env  # noqa: E402
from voice_layer.volc import protocol as P  # noqa: E402
from voice_layer.volc.auth import tts_headers  # noqa: E402

URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"


async def synth_once(session: aiohttp.ClientSession, text: str, speaker: str, resource_id: str,
                     sample_rate: int = 24000, char_stream: bool = True) -> dict:
    h = tts_headers(resource_id)
    t_conn = time.perf_counter()
    async with session.ws_connect(URL, headers=h, max_msg_size=0) as ws:
        logid = ws._response.headers.get("X-Tt-Logid", "") if ws._response is not None else ""
        t_open = time.perf_counter()
        await ws.send_bytes(P.tts_event(P.Ev.StartConnection, {}))
        f = P.parse((await ws.receive()).data)
        if f.event != P.Ev.ConnectionStarted:
            return {"ok": False, "stage": "connection", "event": f.event, "error_code": f.error_code,
                    "msg": f.payload.decode("utf-8", "ignore")[:300], "logid": logid}
        sid = str(uuid.uuid4())
        req = {"user": {"uid": "voice-meeting-smoke"}, "event": P.Ev.StartSession, "namespace": "BidirectionalTTS",
               "req_params": {"speaker": speaker, "audio_params": {"format": "pcm", "sample_rate": sample_rate}}}
        await ws.send_bytes(P.tts_event(P.Ev.StartSession, req, sid))
        f = P.parse((await ws.receive()).data)
        if f.event != P.Ev.SessionStarted:
            return {"ok": False, "stage": "session", "event": f.event, "error_code": f.error_code,
                    "msg": f.payload.decode("utf-8", "ignore")[:300], "logid": logid}
        t_text = time.perf_counter()
        chunks = list(text) if char_stream else [text]
        for c in chunks:
            await ws.send_bytes(P.tts_event(P.Ev.TaskRequest, {"event": P.Ev.TaskRequest, "namespace": "BidirectionalTTS",
                                                               "req_params": {"text": c, "speaker": speaker}}, sid))
        t_last_text = time.perf_counter()
        await ws.send_bytes(P.tts_event(P.Ev.FinishSession, {}, sid))
        audio = bytearray()
        t_first = None
        err = None
        while True:
            m = await ws.receive()
            if m.type != aiohttp.WSMsgType.BINARY:
                err = f"ws {m.type}"
                break
            f = P.parse(m.data)
            if f.msg_type == P.AUDIO_ONLY_SERVER or f.event == P.Ev.TTSResponse:
                if t_first is None and f.payload:
                    t_first = time.perf_counter()
                audio += f.payload
            elif f.msg_type == P.ERROR or f.event in (P.Ev.SessionFailed, P.Ev.SessionCanceled):
                err = {"error_code": f.error_code, "event": f.event, "msg": f.payload.decode("utf-8", "ignore")[:300]}
                break
            elif f.event == P.Ev.SessionFinished:
                break
        await ws.send_bytes(P.tts_event(P.Ev.FinishConnection, {}))
        t_end = time.perf_counter()
    if err or t_first is None:
        return {"ok": False, "stage": "synth", "err": err, "logid": logid}
    return {"ok": True, "logid": logid, "connect_ms": (t_open - t_conn) * 1000,
            "ttfb_from_first_text_ms": (t_first - t_text) * 1000,
            "ttfb_from_last_text_ms": (t_first - t_last_text) * 1000,
            "total_ms": (t_end - t_conn) * 1000,
            "audio_s": len(audio) / 2 / sample_rate, "pcm": bytes(audio), "sr": sample_rate}


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--speaker", default="zh_female_vv_uranus_bigtts")
    ap.add_argument("--resource", default=None)
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--text", default="大家好，我是会议里的语音分身，今天先同步一下原型进度。")
    ap.add_argument("--save", default=None)
    ap.add_argument("--whole", action="store_true", help="整句一次发送（默认逐字流式发送）")
    a = ap.parse_args()
    load_env()
    import os
    rid = a.resource or os.environ.get("VOLC_TTS_RESOURCE_ID") or "seed-tts-2.0"
    res = []
    async with aiohttp.ClientSession() as s:
        for i in range(a.n):
            r = await synth_once(s, a.text, a.speaker, rid, char_stream=not a.whole)
            if not r["ok"]:
                print(json.dumps({k: v for k, v in r.items()}, ensure_ascii=False))
                return
            res.append(r)
            print(json.dumps({k: (round(v, 1) if isinstance(v, float) else v) for k, v in r.items() if k not in ("pcm",)}, ensure_ascii=False))
    if a.save and res:
        import soundfile as sf
        Path(a.save).parent.mkdir(parents=True, exist_ok=True)
        sf.write(a.save, np.frombuffer(res[0]["pcm"], dtype=np.int16), res[0]["sr"])
    for key in ("connect_ms", "ttfb_from_first_text_ms", "ttfb_from_last_text_ms", "total_ms"):
        xs = [r[key] for r in res]
        print(f"{key}: n={len(xs)} P50={pct(xs, .5):.0f} P90={pct(xs, .9):.0f}")


if __name__ == "__main__":
    asyncio.run(main())
