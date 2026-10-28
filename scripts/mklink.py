#!/usr/bin/env python3
"""一步签发会议链接：建房（场景写入 room metadata）→ 派发 worker（同一场景放 dispatch metadata）→
签 host token → 写 meet.livekit.io 链接文件（chmod 600），返回/打印文件路径。不打印任何 key / token。

用法（CLI）：python scripts/mklink.py --room demo-room --scene scene.json [--out TRY_LINK.txt] [--hours 2] [--no-dispatch]
用法（库）：from scripts.mklink import make_link; path = asyncio.run(make_link(room, scene))
scene 结构由编排器定义，约定至少有 {who, topic, agenda, briefs_path, voices}；worker 优先读 ctx.job.metadata，其次 room.metadata。
"""
import argparse
import asyncio
import json
import os
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from livekit import api

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
SAFE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_META = 60_000  # LiveKit metadata 上限约 64KB（待验证），大 brief 请放文件、这里只传路径


async def make_link(room: str, scene: dict, out: Path | None = None, hours: float = 2.0,
                    dispatch: bool = True, empty_timeout_s: int = 30 * 60) -> Path:
    if not SAFE.match(room):
        raise ValueError("room must match [A-Za-z0-9_-]{1,64}")
    meta = json.dumps(scene, ensure_ascii=False)
    if len(meta.encode()) > MAX_META:
        raise ValueError("scene too large for metadata; put briefs in files and pass briefs_path")
    url = os.environ["LIVEKIT_URL"]
    agent = os.environ.get("LIVEKIT_AGENT_NAME", "voice-meeting")
    human = os.environ.get("MEETING_HUMAN_IDENTITY", "host")
    lk = api.LiveKitAPI(url.replace("wss://", "https://"), os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
    try:
        await lk.room.create_room(api.CreateRoomRequest(name=room, metadata=meta, empty_timeout=empty_timeout_s,
                                                        departure_timeout=empty_timeout_s))
        if dispatch:
            await lk.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(agent_name=agent, room=room, metadata=meta))
    finally:
        await lk.aclose()
    tok = (api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
           .with_identity(human).with_name(os.environ.get("MEETING_HUMAN_NAME", "用户"))
           .with_ttl(timedelta(hours=hours))
           .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True,
                                        can_publish_data=True)).to_jwt())
    link = f"https://meet.livekit.io/custom?liveKitUrl={quote(url, safe='')}&token={tok}"
    out = Path(out or ROOT / f"links/{room}.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(link + "\n")
    os.chmod(out, 0o600)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", required=True)
    ap.add_argument("--scene", help="scene JSON 文件；缺省为空场景 {}")
    ap.add_argument("--out")
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--no-dispatch", action="store_true")
    a = ap.parse_args()
    scene = json.loads(Path(a.scene).read_text()) if a.scene else {}
    print(asyncio.run(make_link(a.room, scene, Path(a.out) if a.out else None, a.hours, not a.no_dispatch)))
