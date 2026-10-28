"""sim/ 共用：读 .env、签 token（不打印任何密钥）。"""
import os
import time
from pathlib import Path
from dotenv import load_dotenv
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
from livekit import api  # noqa: E402  load_dotenv 之后再导入

AGENT_NAME = os.environ.get("LIVEKIT_AGENT_NAME", "voice-meeting")
HUMAN_ID = os.environ.get("MEETING_HUMAN_IDENTITY", "host")

def url():
    return os.environ["LIVEKIT_URL"]

def token(room, identity, name=None, hidden=False, dispatch=False):
    g = api.VideoGrants(room_join=True, room=room, can_subscribe=True,
                        can_publish=not hidden, can_publish_data=not hidden, hidden=hidden)
    t = (api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
         .with_identity(identity).with_name(name or identity).with_grants(g))
    if dispatch:
        t = t.with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)]))
    return t.to_jwt()

def now_ms():
    return time.time() * 1000.0
