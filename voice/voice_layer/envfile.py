"""读取仓库根目录的 .env（KEY=VALUE），只设置进 os.environ，不打印值。"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV = Path(__file__).resolve().parent.parent.parent / ".env"
WATCHED = ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "VOLC_API_KEY", "VOLC_APP_ID",
           "VOLC_ACCESS_TOKEN", "VOLC_ASR_RESOURCE_ID", "VOLC_TTS_RESOURCE_ID", "ARK_API_KEY", "DASHSCOPE_API_KEY"]


def load_env(path: Path | str = DEFAULT_ENV, override: bool = False) -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if override or not os.environ.get(k):
            os.environ[k] = v


def status() -> dict[str, str]:
    return {k: ("已设置" if os.environ.get(k) else "未设置") for k in WATCHED}
