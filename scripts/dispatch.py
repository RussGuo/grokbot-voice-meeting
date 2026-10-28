#!/usr/bin/env python3
"""显式派发编排器 agent 到房间（AgentDispatch API）。房间由 recorder 先建时，token 里的 room_config 派发不会触发。
用法：python scripts/dispatch.py --room sim-meeting-01 [--agent voice-meeting]；从 .env 读 LIVEKIT_*，不打印任何值。"""
import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from livekit import api

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


async def main(room: str, agent: str) -> None:
    lk = api.LiveKitAPI(os.environ["LIVEKIT_URL"].replace("wss://", "https://"),
                        os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
    try:
        d = await lk.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(agent_name=agent, room=room))
        print("dispatched", d.id, "agent", agent, "room", room)
    finally:
        await lk.aclose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", required=True)
    ap.add_argument("--agent", default=os.environ.get("LIVEKIT_AGENT_NAME", "voice-meeting"))
    a = ap.parse_args()
    asyncio.run(main(a.room, a.agent))
