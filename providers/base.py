"""Structural interfaces. Adapters may be classes or callables; the registry stores factories."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ChatProvider(Protocol):
    """One streaming chat backend. `complete` is for brief summarization; `astream_text` is for speech."""

    def complete(self, messages: list[dict], *, max_tokens: int = 800) -> str: ...

    async def astream_text(self, messages: list[dict], *, max_tokens: int = 200): ...


@runtime_checkable
class SpeechFactory(Protocol):
    """Build a livekit-agents STT or TTS from the process environment and VoiceConfig."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


@runtime_checkable
class RealtimeTransport(Protocol):
    """Room + dispatch + join token. LiveKit is the only built-in implementation."""

    name: str

    async def ensure_room(self, room: str, metadata: str, empty_timeout_s: int) -> None: ...

    async def dispatch(self, room: str, metadata: str, agent_name: str) -> None: ...

    def access_token(self, *, room: str, identity: str, display_name: str, hours: float) -> str: ...

    def join_urls(self, *, url: str, token: str, join_page: str | None = None) -> dict[str, str]: ...
