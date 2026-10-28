"""会后删房：python sim/room_cleanup.py --room sim-meeting-01"""
import argparse
import asyncio
import os
from livekit import api
import common  # noqa: F401  读 .env（副作用导入）
async def main(room):
    lk = api.LiveKitAPI(os.environ["LIVEKIT_URL"].replace("wss://", "https://"), os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
    try:
        await lk.room.delete_room(api.DeleteRoomRequest(room=room)); print("deleted", room)
    except Exception as e: print("delete:", type(e).__name__)
    rooms = await lk.room.list_rooms(api.ListRoomsRequest()); print("remaining:", [r.name for r in rooms.rooms])
    await lk.aclose()
ap = argparse.ArgumentParser(); ap.add_argument("--room", default="sim-meeting-01"); asyncio.run(main(ap.parse_args().room))
