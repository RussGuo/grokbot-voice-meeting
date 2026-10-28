"""每个 bot 在房间里以独立参会者出现（identity=bot id，name=中文显示名），各自发布一条音轨。

做法：AgentSession 仍是单会话 + update_agent；在 session.output.audio 外面包一层 RoutingAudioOutput。
TTS 帧按「当前 agent」路由到该 bot 自己连接的 AudioSource；同样时长的静音帧继续送进原 RoomIO 输出，
这样框架的 playback_started/finished、wait_for_playout、打断时序全部不变（主 agent 参会者只发静音）。
duck 在 tts_node 里已作用于帧本身，所以跟着轨道走；clear_buffer 同时清掉 bot 轨道队列，插话即停。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, Optional

from livekit import api, rtc
from livekit.agents.voice import io

log = logging.getLogger("voice.multi_identity")


class BotSpeakers:
    def __init__(self, room_name: str, url: Optional[str] = None, *, sample_rate: int = 24000,
                 queue_ms: int = 200) -> None:
        self.room_name = room_name
        self.url = url or os.environ["LIVEKIT_URL"]
        self.sample_rate = sample_rate
        self.queue_ms = queue_ms
        self.rooms: dict[str, rtc.Room] = {}
        self.sources: dict[str, rtc.AudioSource] = {}

    def _token(self, bot_id: str, name: str) -> str:
        g = api.VideoGrants(room_join=True, room=self.room_name, can_publish=True, can_subscribe=False,
                            can_publish_data=False)
        return (api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
                .with_identity(bot_id).with_name(name).with_kind("agent").with_grants(g).to_jwt())

    async def connect(self, bots: list[tuple[str, str]]) -> None:
        async def one(bid: str, name: str) -> None:
            room = rtc.Room()
            await room.connect(self.url, self._token(bid, name), rtc.RoomOptions(auto_subscribe=False))
            src = rtc.AudioSource(self.sample_rate, 1, queue_size_ms=self.queue_ms)
            track = rtc.LocalAudioTrack.create_audio_track(f"{bid}-voice", src)
            await room.local_participant.publish_track(
                track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
            self.rooms[bid], self.sources[bid] = room, src
        await asyncio.gather(*(one(b, n) for b, n in bots))
        log.info("bot 独立参会者已入房：%s", ", ".join(self.rooms))

    def clear(self) -> None:
        for s in self.sources.values():
            try:
                s.clear_queue()
            except Exception:
                pass

    async def aclose(self) -> None:
        for r in self.rooms.values():
            try:
                await r.disconnect()
            except Exception:
                pass
        self.rooms.clear(); self.sources.clear()


class RoutingAudioOutput(io.AudioOutput):
    def __init__(self, nxt: io.AudioOutput, speakers: BotSpeakers, current: Callable[[], Optional[str]]) -> None:
        super().__init__(label="BotRouting", capabilities=io.AudioOutputCapabilities(pause=False),
                         next_in_chain=nxt, sample_rate=speakers.sample_rate)
        self.speakers = speakers
        self.current = current

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)
        src = self.speakers.sources.get(self.current() or "")
        if src is None:   # 没有对应 bot 轨道：退回主 agent 轨道
            await self.next_in_chain.capture_frame(frame)
            return
        await src.capture_frame(frame)
        silent = rtc.AudioFrame(bytes(frame.samples_per_channel * frame.num_channels * 2),
                                frame.sample_rate, frame.num_channels, frame.samples_per_channel)
        await self.next_in_chain.capture_frame(silent)

    def flush(self) -> None:
        super().flush()
        self.next_in_chain.flush()

    def clear_buffer(self) -> None:
        self.speakers.clear()
        self.next_in_chain.clear_buffer()
