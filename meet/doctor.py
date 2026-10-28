"""Check that a live meeting can start. Never prints secret values."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _present(name: str) -> bool:
    return bool(os.environ.get(name))


def _tcp(host: str, port: int, timeout: float = 5.0) -> str | None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as e:
        return type(e).__name__


def run_checks(*, connect: bool) -> list[Check]:
    from orchestrator.config import llm_config
    from providers.registry import selected_names

    checks: list[Check] = []
    live = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
    missing = [k for k in live if not _present(k)]
    checks.append(Check("livekit", not missing, "ready" if not missing else "missing " + ", ".join(missing)))
    if connect and not missing:
        url = os.environ.get("LIVEKIT_URL") or ""
        parsed = urlparse(url if "://" in url else "wss://" + url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme != "ws" else 80)
        err = _tcp(host, port) if host else "no host"
        checks.append(Check("livekit-tcp", err is None, f"{host}:{port}" if err is None else f"{host}:{port} {err}"))
    names = selected_names()
    lc = llm_config()
    checks.append(Check("llm", lc.provider != "mock", f"{names['llm']} model={lc.model}" if lc.provider != "mock"
                         else "mock (no DEEPSEEK_API_KEY or LLM_API_KEY) — fine for `meet demo`"))
    checks.append(Check("asr", True, names["asr"]))
    checks.append(Check("tts", True, names["tts"]))
    checks.append(Check("transport", names["transport"] == "livekit", names["transport"]))
    human = os.environ.get("MEETING_HUMAN_IDENTITY") or "host"
    name = os.environ.get("MEETING_HUMAN_NAME") or "(default)"
    lang = os.environ.get("MEETING_LANG") or "zh"
    checks.append(Check("human", True, f"identity={human} name={name} lang={lang}"))
    mod = os.environ.get("MEETING_MODERATOR_ID") or "moderator"
    checks.append(Check("moderator", True, mod))
    if connect and lc.provider != "mock" and lc.base_url:
        parsed = urlparse(lc.base_url if "://" in lc.base_url else "https://" + lc.base_url)
        host = parsed.hostname or ""
        port = parsed.port or 443
        err = _tcp(host, port) if host else "no host"
        # The key stays on this machine. This is only a TCP handshake.
        checks.append(Check("llm-tcp", err is None, f"{host}:{port}" if err is None else f"{host}:{port} {err}"))
    return checks


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="meet doctor")
    ap.add_argument("--offline", action="store_true", help="do not open network connections")
    ap.add_argument("--strict", action="store_true", help="exit 1 unless LiveKit and an LLM key are both set")
    args = ap.parse_args(argv)
    checks = run_checks(connect=not args.offline)
    for c in checks:
        mark = "ok" if c.ok else "MISSING"
        print(f"[{mark}] {c.name}: {c.detail}")
    live_ok = next(c.ok for c in checks if c.name == "livekit")
    llm_ok = next(c.ok for c in checks if c.name == "llm")
    if args.strict and not (live_ok and llm_ok):
        return 1
    if not args.offline and not live_ok:
        return 1
    failed_net = [c for c in checks if c.name.endswith("-tcp") and not c.ok]
    if failed_net:
        return 1
    return 0
