"""Name → factory maps. Builtins register themselves on import."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable


def _ensure_voice_path() -> None:
    """voice_layer is a top-level package living in voice/. Importing `voice` puts it on sys.path."""
    try:
        import voice_layer  # noqa: F401
        return
    except ImportError:
        pass
    root = Path(__file__).resolve().parents[1]
    voice_dir = root / "voice"
    if voice_dir.is_dir() and str(voice_dir) not in sys.path:
        sys.path.insert(0, str(voice_dir))
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

Factory = Callable[..., Any]


class Registry:
    def __init__(self) -> None:
        self.llm: dict[str, Factory] = {}
        self.asr: dict[str, Factory] = {}
        self.tts: dict[str, Factory] = {}
        self.transport: dict[str, Factory] = {}

    def add(self, kind: str, name: str, factory: Factory) -> None:
        table = getattr(self, kind)
        table[name] = factory

    def get(self, kind: str, name: str) -> Factory:
        table = getattr(self, kind)
        if name not in table:
            known = ", ".join(sorted(table)) or "(none)"
            raise KeyError(f"no {kind} provider {name!r}; known: {known}")
        return table[name]

    def names(self, kind: str) -> list[str]:
        return sorted(getattr(self, kind))


registry = Registry()


def _register_builtins() -> None:
    if registry.llm:
        return

    def llm_factory(cfg=None):
        from orchestrator.config import llm_config as cfg_fn
        from orchestrator.llm import make_chat
        return make_chat(cfg or cfg_fn())

    registry.add("llm", "deepseek", llm_factory)
    registry.add("llm", "openai", llm_factory)
    registry.add("llm", "mock", llm_factory)

    def asr_factory(cfg=None, provider: str | None = None):
        _ensure_voice_path()
        from voice_layer.asr import make_stt
        from voice_layer.config import load_config
        return make_stt(cfg or load_config(), provider)

    def tts_factory(bot_id: str, cfg=None, provider: str | None = None):
        _ensure_voice_path()
        from voice_layer.tts import make_tts
        from voice_layer.config import load_config
        return make_tts(cfg or load_config(), bot_id, provider)

    for name in ("volc", "aliyun", "openai", "local"):
        registry.add("asr", name, asr_factory)
        registry.add("tts", name, tts_factory)

    def livekit_factory():
        from providers.transport import LiveKitTransport
        return LiveKitTransport()

    registry.add("transport", "livekit", livekit_factory)


def selected_names(env: dict[str, str] | None = None) -> dict[str, str]:
    """Resolve which provider each layer would use. Does not open a connection."""
    env = os.environ if env is None else env
    _register_builtins()
    _ensure_voice_path()
    from orchestrator.config import llm_config
    from voice_layer.config import VoiceConfig
    # VoiceConfig reads os.environ at construction. Tests pass a dict, so apply it briefly.
    previous = {k: os.environ.get(k) for k in ("VOICE_ASR_PROVIDER", "VOICE_TTS_PROVIDER", "MEETING_TRANSPORT")}
    try:
        for k in previous:
            if k in env:
                os.environ[k] = env[k]
            elif env is not os.environ:
                os.environ.pop(k, None)
        cfg = VoiceConfig()
        transport = (env.get("MEETING_TRANSPORT") or "livekit").strip().lower() or "livekit"
        return {
            "llm": llm_config(env).provider,
            "asr": cfg.resolve_asr_provider(),
            "tts": cfg.resolve_tts_provider(),
            "transport": transport,
        }
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


_register_builtins()
