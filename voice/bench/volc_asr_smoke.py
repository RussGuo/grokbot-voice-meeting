"""豆包流式 ASR 2.0 冒烟（bigmodel_async）。合成音频 → 16k PCM → 按 200ms 实时推流。
测：首个非空 partial 延迟（相对推流开始）、首个 definite 分句、末包发出 → 最终包（负序号）延迟。
用法：python bench/volc_asr_smoke.py --wav samples/x.wav [--resource volc.bigasr.sauc.duration] [--n 3]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

import aiohttp
import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from voice_layer.envfile import load_env  # noqa: E402
from voice_layer.volc import protocol as P  # noqa: E402
from voice_layer.volc.auth import asr_headers  # noqa: E402

URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"


def to_16k_pcm(path: str) -> bytes:
    a, sr = sf.read(path, dtype="float32", always_2d=False)
    if a.ndim > 1:
        a = a.mean(axis=1)
    if sr != 16000:
        n = int(len(a) * 16000 / sr)
        a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)
    return (np.clip(a, -1, 1) * 32767).astype(np.int16).tobytes()


def request_payload() -> dict:
    return {"user": {"uid": "voice-meeting-smoke"},
            "audio": {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1},
            "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True,
                        "show_utterances": True, "result_type": "full", "end_window_size": 600}}


async def run_once(pcm: bytes, resource_id: str, chunk_ms: int = 200, tail_silence_ms: int = 0) -> dict:
    h = asr_headers(resource_id)
    step = 16000 * 2 * chunk_ms // 1000
    speech_ms = len(pcm) / 32
    pcm = pcm + b"\x00" * (32 * tail_silence_ms)
    chunks = [pcm[i:i + step] for i in range(0, len(pcm), step)]
    out: dict = {"resource_id": resource_id}
    async with aiohttp.ClientSession() as s:
        t0 = time.perf_counter()
        try:
            ws = await s.ws_connect(URL, headers=h, max_msg_size=0)
        except aiohttp.WSServerHandshakeError as e:
            return {"ok": False, "stage": "handshake", "status": e.status, "msg": str(e.message)[:200],
                    "logid": (e.headers or {}).get("X-Tt-Logid", "")}
        logid = ws._response.headers.get("X-Tt-Logid", "") if ws._response is not None else ""
        out["logid"] = logid
        out["connect_ms"] = (time.perf_counter() - t0) * 1000
        await ws.send_bytes(P.asr_full_request(request_payload(), seq=1))
        m = await ws.receive()
        f = P.parse(m.data)
        if f.msg_type == P.ERROR:
            await ws.close()
            return {"ok": False, "stage": "init", "error_code": f.error_code, "msg": f.payload.decode("utf-8", "ignore")[:300], "logid": logid}
        res = {"partial_first_ms": None, "definite_first_ms": None, "final_after_last_ms": None, "text": ""}
        t_stream = time.perf_counter()
        t_last = [None]

        async def sender():
            for i, c in enumerate(chunks):
                last = i == len(chunks) - 1
                await ws.send_bytes(P.asr_audio(c, seq=i + 2, last=last))
                if last:
                    t_last[0] = time.perf_counter()
                await asyncio.sleep(chunk_ms / 1000)

        st = asyncio.create_task(sender())
        err = None
        while True:
            m = await ws.receive()
            if m.type != aiohttp.WSMsgType.BINARY:
                break
            f = P.parse(m.data)
            if f.msg_type == P.ERROR:
                err = {"error_code": f.error_code, "msg": f.payload.decode("utf-8", "ignore")[:300]}
                break
            j = f.json()
            r = j.get("result") or {}
            text = r.get("text", "") if isinstance(r, dict) else ""
            now = time.perf_counter()
            if text and res["partial_first_ms"] is None:
                res["partial_first_ms"] = (now - t_stream) * 1000
            utts = r.get("utterances") or [] if isinstance(r, dict) else []
            if any(u.get("definite") for u in utts) and res["definite_first_ms"] is None:
                res["definite_first_ms"] = (now - t_stream) * 1000
            if text:
                res["text"] = text
            if f.flags in (P.NEG_SEQ, P.LAST_NO_SEQ) or (f.sequence is not None and f.sequence < 0):
                if t_last[0] is not None:
                    res["final_after_last_ms"] = (now - t_last[0]) * 1000
                break
        st.cancel()
        await ws.close()
    out.update(res)
    if res["definite_first_ms"] is not None:
        # 推流是实时节奏，所以 definite 时刻 − 语音音频时长 ≈ 「语音结束 → 分句定稿」延迟（含 end_window_size 判停）
        out["definite_after_speech_end_ms"] = res["definite_first_ms"] - speech_ms
    out["audio_s"] = len(pcm) / 32000
    out["ok"] = err is None and res["final_after_last_ms"] is not None
    if err:
        out["err"] = err
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--resource", default=None)
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--tail-silence-ms", type=int, default=0)
    a = ap.parse_args()
    load_env()
    rid = a.resource or os.environ.get("VOLC_ASR_RESOURCE_ID") or "volc.seedasr.sauc.duration"
    pcm = to_16k_pcm(a.wav)
    for _ in range(a.n):
        r = await run_once(pcm, rid, tail_silence_ms=a.tail_silence_ms)
        print(json.dumps({k: (round(v, 1) if isinstance(v, float) else v) for k, v in r.items()}, ensure_ascii=False), flush=True)
        if not r.get("ok"):
            break


if __name__ == "__main__":
    asyncio.run(main())
