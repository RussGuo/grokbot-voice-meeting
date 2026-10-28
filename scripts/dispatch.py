#!/usr/bin/env python3
"""显式派发编排器 agent 到房间（AgentDispatch API）。房间由 recorder 先建时，token 里的 room_config 派发不会触发。

用法：python scripts/dispatch.py --room sim-meeting-01 [--agent voice-meeting] [--scene links/<room>.scene.json | --no-scene]

场景（scene）只通过派发 metadata 交给 worker，房间 metadata 里不再放 brief。
`meet --no-dispatch` 会把完整场景写到 links/<room>.scene.json；缺省时若该文件存在就用它，
不存在则不带 metadata（worker 回退到 MEETING_SCENARIO 样例，run_sim.sh 走这条）。
从 .env 读 LIVEKIT_*，不打印任何值（包括场景内容）。"""
import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
MAX_META = 60_000  # LiveKit metadata is about 64KB


def default_scene_path(room: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", room):
        raise ValueError("room must match [A-Za-z0-9_-]{1,64}")
    return ROOT / "links" / f"{room}.scene.json"


def load_scene_metadata_file(path: Path) -> str:
    """Read a scene JSON file and return it as a metadata string. Raises ValueError if unusable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise ValueError(f"cannot read scene file {path}: {e.strerror}") from e
    except json.JSONDecodeError as e:
        raise ValueError(f"scene file {path} is not valid JSON") from e
    if not isinstance(data, dict):
        raise ValueError(f"scene file {path} must hold a JSON object")
    meta = json.dumps(data, ensure_ascii=False)
    if len(meta.encode()) > MAX_META:
        raise ValueError("scene too large for metadata; put briefs in files and pass briefs_path")
    return meta


def resolve_metadata(room: str, scene: str | None, no_scene: bool = False) -> tuple[str, Path | None]:
    """Explicit --scene must exist. Otherwise links/<room>.scene.json when present. Returns (metadata, path)."""
    if no_scene:
        return "", None
    path = Path(scene) if scene else default_scene_path(room)
    if not scene and not path.is_file():
        return "", None
    return load_scene_metadata_file(path), path


async def main(room: str, agent: str, metadata: str = "") -> str:
    from providers.transport import get_transport
    transport = get_transport()
    await transport.dispatch(room, metadata, agent)
    return metadata


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", required=True)
    ap.add_argument("--agent", default=os.environ.get("LIVEKIT_AGENT_NAME", "voice-meeting"))
    ap.add_argument("--scene", help="scene JSON 文件（缺省 links/<room>.scene.json，存在才用）")
    ap.add_argument("--no-scene", action="store_true", help="不带场景 metadata（worker 用 MEETING_SCENARIO 样例）")
    a = ap.parse_args()
    try:
        meta, path = resolve_metadata(a.room, a.scene, a.no_scene)
    except ValueError as e:
        sys.exit(f"dispatch: {e}")
    asyncio.run(main(a.room, a.agent, meta))
    print("dispatched agent", a.agent, "room", a.room, f"scene={path}" if path else "scene=none")
