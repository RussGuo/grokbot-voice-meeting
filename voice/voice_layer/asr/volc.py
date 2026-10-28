"""豆包流式语音识别 2.0 适配器（双向流式优化版 bigmodel_async）。

endpoint  wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async
文档      https://docs.volcengine.com/docs/6561/1354869
鉴权      X-Api-Key=VOLC_API_KEY（或旧版 X-Api-App-Key/X-Api-Access-Key），
          X-Api-Resource-Id = VOLC_ASR_RESOURCE_ID | volc.seedasr.sauc.duration（1.0：volc.bigasr.sauc.duration）
音频      16k / 16bit / mono PCM，200ms 一包（文档推荐值）
结果      result_type=single（增量，不回传历史分句），utterances[].definite=true 为定稿分句 => FINAL
"""
from __future__ import annotations

import asyncio
import logging
import json
import uuid

import aiohttp
from livekit import rtc
from livekit.agents import stt as lk_stt
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions, NotGivenOr

from ..volc import protocol as P
from ..volc.auth import asr_headers

URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
CHUNK_MS = 200
log = logging.getLogger("voice.asr.volc")


def request_payload(end_window_ms: int = 600, hotwords: list[str] | None = None) -> dict:
    req = {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True,
           "show_utterances": True, "result_type": "single", "end_window_size": end_window_ms}
    if hotwords:
        # 热词直传：request.corpus.context = 序列化 JSON；双向流式上限约 100 tokens（文档 6561/1354869）
        req["corpus"] = {"context": json.dumps({"hotwords": [{"word": w} for w in hotwords]}, ensure_ascii=False)}
    return {"user": {"uid": "voice-meeting"},
            "audio": {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1},
            "request": req}


class VolcSTT(lk_stt.STT):
    def __init__(self, *, resource_id: str = "volc.seedasr.sauc.duration", end_window_ms: int = 600,
                 hotwords: list[str] | None = None) -> None:
        super().__init__(capabilities=lk_stt.STTCapabilities(streaming=True, interim_results=True,
                                                             offline_recognize=False))
        self.resource_id = resource_id
        self.end_window_ms = end_window_ms
        self.hotwords = list(hotwords or [])

    @property
    def model(self) -> str:
        return self.resource_id

    @property
    def provider(self) -> str:
        return "volcengine-doubao"

    async def _recognize_impl(self, buffer, *, language=NOT_GIVEN, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        raise NotImplementedError("只支持流式")

    def stream(self, *, language: NotGivenOr[str] = NOT_GIVEN,
               conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS) -> "VolcRecognizeStream":
        return VolcRecognizeStream(stt=self, conn_options=conn_options)


class VolcRecognizeStream(lk_stt.RecognizeStream):
    def __init__(self, *, stt: VolcSTT, conn_options: APIConnectOptions) -> None:
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=16000)
        self._v = stt
        self.logid = ""

    async def _run(self) -> None:
        req_id = str(uuid.uuid4())
        async with aiohttp.ClientSession() as http:
            ws = await http.ws_connect(URL, headers=asr_headers(self._v.resource_id), max_msg_size=0)
            self.logid = ws._response.headers.get("X-Tt-Logid", "") if ws._response is not None else ""
            payload = request_payload(self._v.end_window_ms, self._v.hotwords)
            log.info("volc asr stream start resource=%s hotwords=%d X-Tt-Logid=%s", self._v.resource_id,
                     len(self._v.hotwords), self.logid)
            await ws.send_bytes(P.asr_full_request(payload, seq=1))
            f = P.parse((await ws.receive()).data)
            if f.msg_type == P.ERROR:
                raise RuntimeError(f"volc asr init error_code={f.error_code} X-Tt-Logid={self.logid}")

            async def sender() -> None:
                seq = 2
                buf = bytearray()
                step = 16000 * 2 * CHUNK_MS // 1000
                async for item in self._input_ch:
                    if isinstance(item, rtc.AudioFrame):
                        buf += bytes(item.data)
                        while len(buf) >= step:
                            await ws.send_bytes(P.asr_audio(bytes(buf[:step]), seq=seq, last=False))
                            del buf[:step]
                            seq += 1
                await ws.send_bytes(P.asr_audio(bytes(buf), seq=seq, last=True))

            st = asyncio.create_task(sender())
            emitted: set[tuple] = set()
            last_interim = ""
            try:
                while True:
                    m = await ws.receive()
                    if m.type != aiohttp.WSMsgType.BINARY:
                        break
                    f = P.parse(m.data)
                    if f.msg_type == P.ERROR:
                        raise RuntimeError(f"volc asr error_code={f.error_code} X-Tt-Logid={self.logid}")
                    r = (f.json() or {}).get("result") or {}
                    utts = r.get("utterances") or [] if isinstance(r, dict) else []
                    for u in utts:
                        text = (u.get("text") or "").strip()
                        if not text:
                            continue
                        key = (u.get("start_time"), u.get("end_time"), text)
                        if u.get("definite"):
                            if key in emitted:
                                continue
                            emitted.add(key)
                            last_interim = ""
                            self._event_ch.send_nowait(lk_stt.SpeechEvent(
                                type=lk_stt.SpeechEventType.FINAL_TRANSCRIPT, request_id=req_id,
                                alternatives=[lk_stt.SpeechData(language="zh", text=text)]))
                        elif text != last_interim:
                            last_interim = text
                            self._event_ch.send_nowait(lk_stt.SpeechEvent(
                                type=lk_stt.SpeechEventType.INTERIM_TRANSCRIPT, request_id=req_id,
                                alternatives=[lk_stt.SpeechData(language="zh", text=text)]))
                    if f.flags == P.NEG_SEQ or (f.sequence is not None and f.sequence < 0):
                        break
            finally:
                st.cancel()
                await ws.close()
