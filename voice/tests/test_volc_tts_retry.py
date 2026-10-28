"""池里的旧连接被服务端关掉时，VolcTTS 换新连接重试一次，不把错误抛给框架。全离线（假 ws）。"""
import asyncio
import struct

import aiohttp
import pytest

from voice_layer.volc import protocol as P
from voice_layer.tts import volc as V


def srv(event, payload=b"{}", sid="s", audio=False):
    mt = P.AUDIO_ONLY_SERVER if audio else P.FULL_SERVER_RESPONSE
    sid_b = sid.encode()
    return (P.header(mt, P.WITH_EVENT) + struct.pack(">i", event) + struct.pack(">I", len(sid_b)) + sid_b
            + struct.pack(">I", len(payload)) + payload)


class Msg:
    def __init__(self, data):
        self.type, self.data = aiohttp.WSMsgType.BINARY, data


class DeadWS:
    closed = False

    async def send_bytes(self, b):
        raise aiohttp.ClientConnectionResetError("Cannot write to closing transport")

    async def receive(self):
        raise AssertionError("unreachable")

    async def close(self):
        pass


class GoodWS:
    closed = False

    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()
        self.sent = []

    async def send_bytes(self, b):
        f = P.parse(b)
        self.sent.append(f.event)
        if f.event == P.Ev.StartSession:
            await self.q.put(srv(P.Ev.SessionStarted))
        elif f.event == P.Ev.FinishSession:
            await self.q.put(srv(P.Ev.TTSResponse, b"\x00\x00" * 2400, audio=True))
            await self.q.put(srv(P.Ev.SessionFinished))

    async def receive(self):
        return Msg(await self.q.get())

    async def close(self):
        pass


class FakeHttp:
    async def close(self):
        pass


@pytest.mark.asyncio
async def test_retry_on_dead_pooled_conn(monkeypatch):
    monkeypatch.setenv("VOLC_API_KEY", "dummy")
    t = V.VolcTTS(speaker="x", resource_id="seed-tts-2.0", sample_rate=24000)
    t._conn = V._Conn(DeadWS(), FakeHttp(), "")
    good = GoodWS()

    async def fake_connect():
        return V._Conn(good, FakeHttp(), "")

    monkeypatch.setattr(t, "_connect", fake_connect)
    st = t.stream()
    st.push_text("你好")
    st.end_input()
    n = 0
    async for ev in st:
        n += ev.frame.samples_per_channel
    await st.aclose()
    assert n == 2400
    assert good.sent[0] == P.Ev.StartSession
    assert t._conn is not None and t._conn.ws is good      # 新连接回池复用
