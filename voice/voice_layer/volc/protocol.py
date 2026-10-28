"""豆包语音 V3 WebSocket 二进制帧（ASR sauc 与 TTS bidirection 共用同一套 header 布局）。

header 4 字节：
  byte0 = version(4bit)=1 | header_size(4bit)=1（×4 字节）
  byte1 = message_type(4bit) | flags(4bit)
  byte2 = serialization(4bit: 0 无 / 1 JSON) | compression(4bit: 0 无 / 1 gzip)
  byte3 = reserved
flags：0 无序号 / 1 正序号 / 2 末包无序号 / 3 负序号(末包) / 4 带 event 号（TTS 用）
ASR 文档：https://docs.volcengine.com/docs/6561/1354869
TTS 文档：https://docs.volcengine.com/docs/doubao-voice/bidirectional-streaming-text-to-speech-websocket
TTS 帧：header | event(int32) | [session_id len+bytes]（会话级事件）| payload len + payload
"""
from __future__ import annotations

import gzip
import json
import struct
from dataclasses import dataclass
from typing import Optional

# message types
FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_REQUEST = 0b0010
FULL_SERVER_RESPONSE = 0b1001
AUDIO_ONLY_SERVER = 0b1011
FRONTEND_RESULT_SERVER = 0b1100
ERROR = 0b1111

# flags
NO_SEQ = 0b0000
POS_SEQ = 0b0001
LAST_NO_SEQ = 0b0010
NEG_SEQ = 0b0011
WITH_EVENT = 0b0100

JSON_SER = 0b0001
NO_SER = 0b0000
NO_COMP = 0b0000
GZIP = 0b0001

# TTS events
class Ev:
    StartConnection = 1
    FinishConnection = 2
    ConnectionStarted = 50
    ConnectionFailed = 51
    ConnectionFinished = 52
    StartSession = 100
    CancelSession = 101
    FinishSession = 102
    SessionStarted = 150
    SessionCanceled = 151
    SessionFinished = 152
    SessionFailed = 153
    TaskRequest = 200
    TTSSentenceStart = 350
    TTSSentenceEnd = 351
    TTSResponse = 352

CONNECTION_EVENTS = {Ev.StartConnection, Ev.FinishConnection, Ev.ConnectionStarted,
                     Ev.ConnectionFailed, Ev.ConnectionFinished}


def header(msg_type: int, flags: int, ser: int = JSON_SER, comp: int = NO_COMP) -> bytes:
    return bytes([(1 << 4) | 1, (msg_type << 4) | flags, (ser << 4) | comp, 0])


# —— ASR ——
def asr_full_request(payload: dict, seq: int = 1, use_gzip: bool = True) -> bytes:
    body = json.dumps(payload).encode()
    if use_gzip:
        body = gzip.compress(body)
    return header(FULL_CLIENT_REQUEST, POS_SEQ, JSON_SER, GZIP if use_gzip else NO_COMP) + \
        struct.pack(">i", seq) + struct.pack(">I", len(body)) + body


def asr_audio(pcm: bytes, seq: int, last: bool, use_gzip: bool = True) -> bytes:
    body = gzip.compress(pcm) if use_gzip else pcm
    flags = NEG_SEQ if last else POS_SEQ
    s = -abs(seq) if last else seq
    return header(AUDIO_ONLY_REQUEST, flags, NO_SER, GZIP if use_gzip else NO_COMP) + \
        struct.pack(">i", s) + struct.pack(">I", len(body)) + body


# —— TTS ——
def tts_event(event: int, payload: dict | bytes = b"{}", session_id: Optional[str] = None) -> bytes:
    body = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode()
    out = header(FULL_CLIENT_REQUEST, WITH_EVENT, JSON_SER, NO_COMP) + struct.pack(">i", event)
    if event not in CONNECTION_EVENTS:
        sid = (session_id or "").encode()
        out += struct.pack(">I", len(sid)) + sid
    out += struct.pack(">I", len(body)) + bytes(body)
    return out


@dataclass
class Frame:
    msg_type: int
    flags: int
    serialization: int
    compression: int
    sequence: Optional[int] = None
    event: Optional[int] = None
    session_id: Optional[str] = None
    error_code: Optional[int] = None
    payload: bytes = b""

    def json(self) -> dict:
        try:
            return json.loads(self.payload.decode("utf-8")) if self.payload else {}
        except Exception:
            return {}


def parse(data: bytes) -> Frame:
    hs = (data[0] & 0x0F) * 4
    mt, fl = data[1] >> 4, data[1] & 0x0F
    ser, comp = data[2] >> 4, data[2] & 0x0F
    f = Frame(mt, fl, ser, comp)
    p = hs
    if fl in (POS_SEQ, NEG_SEQ):
        f.sequence = struct.unpack(">i", data[p:p + 4])[0]
        p += 4
    if fl == WITH_EVENT:
        f.event = struct.unpack(">i", data[p:p + 4])[0]
        p += 4
    if mt == ERROR:
        f.error_code = struct.unpack(">I", data[p:p + 4])[0]
        p += 4
    if f.event is not None and f.event != Ev.StartConnection and f.event != Ev.FinishConnection:
        # 服务端会话级/连接级事件都带一个 id（session_id 或 connect_id）
        n = struct.unpack(">I", data[p:p + 4])[0]
        p += 4
        f.session_id = data[p:p + n].decode("utf-8", "ignore")
        p += n
    if p + 4 <= len(data):
        n = struct.unpack(">I", data[p:p + 4])[0]
        p += 4
        body = data[p:p + n]
        if comp == GZIP and body:
            body = gzip.decompress(body)
        f.payload = body
    return f
