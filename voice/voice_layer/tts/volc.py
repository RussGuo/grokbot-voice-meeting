"""豆包语音合成 2.0（seed-tts-2.0）双向流式 WebSocket 适配器（首选）。

endpoint  wss://openspeech.bytedance.com/api/v3/tts/bidirection
文档      https://docs.volcengine.com/docs/doubao-voice/bidirectional-streaming-text-to-speech-websocket?lang=zh
鉴权      X-Api-Key=VOLC_API_KEY（或旧版 VOLC_APP_ID/VOLC_ACCESS_TOKEN），X-Api-Resource-Id=VOLC_TTS_RESOURCE_ID|seed-tts-2.0
流程      StartConnection → ConnectionStarted → [StartSession → SessionStarted → TaskRequest×N → FinishSession
          → TTSResponse(音频)… → SessionFinished]×M → FinishConnection
实测（本机 → 火山，2026-10-27）：TLS+WS 建连 P50≈1.2s，所以连接常驻复用（prewarm），每段话只开一个 session。
中断：流被 aclose/取消时丢弃该连接（避免残留音频串到下一段），后台立刻预建新连接。
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Optional

import aiohttp
from livekit.agents import tts as lk_tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

from ..volc import protocol as P
from ..volc.auth import tts_headers

URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
NS = "BidirectionalTTS"


class VolcTTSError(RuntimeError):
    def __init__(self, msg: str, *, error_code=None, event=None, logid: str = "") -> None:
        super().__init__(f"{msg} (error_code={error_code}, event={event}, X-Tt-Logid={logid})")
        self.error_code, self.event, self.logid = error_code, event, logid


class _Conn:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse, http: aiohttp.ClientSession, logid: str) -> None:
        self.ws, self.http, self.logid = ws, http, logid

    async def close(self) -> None:
        try:
            if not self.ws.closed:
                await self.ws.send_bytes(P.tts_event(P.Ev.FinishConnection, {}))
                await self.ws.close()
        except Exception:
            pass
        await self.http.close()


class VolcTTS(lk_tts.TTS):
    def __init__(self, *, speaker: str, resource_id: str = "seed-tts-2.0", sample_rate: int = 24000,
                 speed: float = 1.0) -> None:
        super().__init__(capabilities=lk_tts.TTSCapabilities(streaming=True), sample_rate=sample_rate, num_channels=1)
        self.speaker = speaker
        self.resource_id = resource_id
        self.speed = speed
        self._conn: Optional[_Conn] = None
        self._conn_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._bg: set[asyncio.Task] = set()   # 后台关闭任务的强引用（asyncio 只弱引用 task，不持有会被 GC，session 就泄漏）

    def _spawn_close(self, c: "_Conn") -> None:
        t = asyncio.ensure_future(c.close())
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    def _adopt_done_task(self) -> None:
        """预建任务已完成但还没被取走：把结果收进 _conn（或关掉多余的），避免覆盖 task 时丢掉连接。"""
        t = self._conn_task
        if t is None or not t.done():
            return
        self._conn_task = None
        if t.cancelled() or t.exception() is not None:
            return
        c = t.result()
        if self._conn is None and not c.ws.closed:
            self._conn = c
        else:
            self._spawn_close(c)

    @property
    def model(self) -> str:
        return self.resource_id

    @property
    def provider(self) -> str:
        return "volcengine-doubao"

    async def _connect(self) -> _Conn:
        http = aiohttp.ClientSession()
        try:
            try:
                ws = await http.ws_connect(URL, headers=tts_headers(self.resource_id), max_msg_size=0)
            except aiohttp.WSServerHandshakeError as e:
                raise VolcTTSError(f"handshake {e.status}", logid=(e.headers or {}).get("X-Tt-Logid", "")) from e
            logid = ws._response.headers.get("X-Tt-Logid", "") if ws._response is not None else ""
            await ws.send_bytes(P.tts_event(P.Ev.StartConnection, {}))
            f = P.parse((await ws.receive()).data)
            if f.event != P.Ev.ConnectionStarted:
                raise VolcTTSError("StartConnection failed", error_code=f.error_code, event=f.event, logid=logid)
            return _Conn(ws, http, logid)
        except BaseException:
            # 任何失败（含超时、取消）都关掉 session
            await asyncio.shield(http.close())
            raise

    def prewarm(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._adopt_done_task()
        if self._conn is None and self._conn_task is None:
            self._conn_task = loop.create_task(self._connect())

    async def _acquire(self) -> _Conn:
        if self._conn is not None and not self._conn.ws.closed:
            c, self._conn = self._conn, None
            return c
        if self._conn_task is not None:
            t, self._conn_task = self._conn_task, None
            try:
                c = await t
                if not c.ws.closed:
                    return c
            except Exception:
                pass
        return await self._connect()

    def _release(self, c: _Conn, healthy: bool) -> None:
        if healthy and not c.ws.closed and self._conn is None:
            self._conn = c
        else:
            self._spawn_close(c)
            self.prewarm()

    def stream(self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS) -> "VolcSynthesizeStream":
        return VolcSynthesizeStream(tts=self, conn_options=conn_options)

    def synthesize(self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS):
        return self._synthesize_with_stream(text, conn_options=conn_options)

    async def aclose(self) -> None:
        t, self._conn_task = self._conn_task, None
        if t is not None:
            if t.done():
                if not t.cancelled() and t.exception() is None:
                    await t.result().close()
            else:
                t.cancel()   # _connect 被取消时会自己关 session
                await asyncio.gather(t, return_exceptions=True)
        if self._conn:
            await self._conn.close()
            self._conn = None
        if self._bg:
            await asyncio.gather(*self._bg, return_exceptions=True)
        await super().aclose()


class VolcSynthesizeStream(lk_tts.SynthesizeStream):
    def __init__(self, *, tts: VolcTTS, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, conn_options=conn_options)
        self._v = tts

    async def _run(self, output_emitter: lk_tts.AudioEmitter) -> None:
        v = self._v
        sid = str(uuid.uuid4())
        output_emitter.initialize(request_id=sid, sample_rate=v.sample_rate, num_channels=1,
                                  mime_type="audio/pcm", stream=True, frame_size_ms=50)
        async with v._lock:
            conn = await v._acquire()
            healthy = False
            ws = conn.ws
            try:
                req = {"user": {"uid": "voice-meeting"}, "event": P.Ev.StartSession, "namespace": NS,
                       "req_params": {"speaker": v.speaker,
                                      "audio_params": {"format": "pcm", "sample_rate": v.sample_rate,
                                                       "speech_rate": int(round((v.speed - 1.0) * 100))}}}
                f = None
                for attempt in range(2):
                    # 池里的连接可能已被服务端空闲关闭：开 session 失败（尚未出音频）就换新连接重试一次
                    try:
                        await ws.send_bytes(P.tts_event(P.Ev.StartSession, req, sid))
                        m0 = await ws.receive()
                        if m0.type != aiohttp.WSMsgType.BINARY:
                            raise ConnectionResetError(f"ws {m0.type}")
                        f = P.parse(m0.data)
                        break
                    except (aiohttp.ClientError, ConnectionError) as e:
                        if attempt:
                            raise VolcTTSError(f"StartSession transport: {type(e).__name__}", logid=conn.logid) from e
                        v._spawn_close(conn)
                        conn = await v._connect()
                        ws = conn.ws
                if f.event != P.Ev.SessionStarted:
                    raise VolcTTSError("StartSession failed", error_code=f.error_code, event=f.event, logid=conn.logid)
                output_emitter.start_segment(segment_id=sid)

                async def send_text() -> None:
                    async for tok in self._input_ch:
                        if isinstance(tok, self._FlushSentinel):
                            continue
                        if tok:
                            await ws.send_bytes(P.tts_event(P.Ev.TaskRequest, {
                                "event": P.Ev.TaskRequest, "namespace": NS,
                                "req_params": {"text": tok, "speaker": v.speaker}}, sid))
                    await ws.send_bytes(P.tts_event(P.Ev.FinishSession, {}, sid))

                sender = asyncio.create_task(send_text())
                try:
                    while True:
                        m = await ws.receive()
                        if m.type != aiohttp.WSMsgType.BINARY:
                            raise VolcTTSError(f"ws closed: {m.type}", logid=conn.logid)
                        f = P.parse(m.data)
                        if f.msg_type == P.AUDIO_ONLY_SERVER or f.event == P.Ev.TTSResponse:
                            if f.payload:
                                output_emitter.push(f.payload)
                        elif f.msg_type == P.ERROR or f.event in (P.Ev.SessionFailed, P.Ev.SessionCanceled):
                            raise VolcTTSError("session failed", error_code=f.error_code, event=f.event, logid=conn.logid)
                        elif f.event == P.Ev.SessionFinished:
                            break
                finally:
                    sender.cancel()
                output_emitter.end_segment()
                healthy = True
            finally:
                # 正常结束 => 连接复用；被打断/取消/出错 => 丢弃连接并后台预建新连接
                v._release(conn, healthy)
