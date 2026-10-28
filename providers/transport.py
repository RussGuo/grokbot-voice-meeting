"""LiveKit transport. Other realtime backends implement RealtimeTransport and register themselves."""
from __future__ import annotations

import os
from datetime import timedelta
from urllib.parse import quote

from providers.base import RealtimeTransport


def _join_urls(url: str, token: str, join_page: str | None) -> dict[str, str]:
    live = f"https://meet.livekit.io/custom?liveKitUrl={quote(url, safe='')}&token={token}"
    out = {"livekit": live}
    page = (join_page or os.environ.get("MEETING_JOIN_PAGE") or "").strip().rstrip("/")
    if page:
        # Static UI (GitHub Pages or any host). Same query the page reads; no token server.
        sep = "&" if "?" in page else "?"
        out["static"] = f"{page}{sep}liveKitUrl={quote(url, safe='')}&token={token}"
    return out


class LiveKitTransport:
    name = "livekit"

    def __init__(self, url: str | None = None, api_key: str | None = None, api_secret: str | None = None):
        self.url = url or os.environ["LIVEKIT_URL"]
        self.api_key = api_key or os.environ["LIVEKIT_API_KEY"]
        self.api_secret = api_secret or os.environ["LIVEKIT_API_SECRET"]

    def _http(self) -> str:
        return self.url.replace("wss://", "https://").replace("ws://", "http://")

    async def ensure_room(self, room: str, metadata: str, empty_timeout_s: int) -> None:
        from livekit import api
        lk = api.LiveKitAPI(self._http(), self.api_key, self.api_secret)
        try:
            await lk.room.create_room(api.CreateRoomRequest(
                name=room, metadata=metadata, empty_timeout=empty_timeout_s, departure_timeout=empty_timeout_s))
        finally:
            await lk.aclose()

    async def dispatch(self, room: str, metadata: str, agent_name: str) -> None:
        from livekit import api
        lk = api.LiveKitAPI(self._http(), self.api_key, self.api_secret)
        try:
            await lk.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(agent_name=agent_name, room=room, metadata=metadata))
        finally:
            await lk.aclose()

    def access_token(self, *, room: str, identity: str, display_name: str, hours: float) -> str:
        from livekit import api
        return (api.AccessToken(self.api_key, self.api_secret)
                .with_identity(identity)
                .with_name(display_name[:64] or identity)
                .with_ttl(timedelta(hours=hours))
                .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True,
                                             can_subscribe=True, can_publish_data=True))
                .to_jwt())

    def join_urls(self, *, url: str, token: str, join_page: str | None = None) -> dict[str, str]:
        return _join_urls(url or self.url, token, join_page)


def get_transport(name: str | None = None) -> RealtimeTransport:
    from providers.registry import registry
    name = (name or os.environ.get("MEETING_TRANSPORT") or "livekit").strip().lower()
    return registry.get("transport", name)()
