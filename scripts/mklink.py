#!/usr/bin/env python3
"""Create a room, dispatch the worker, and write a join link.

The link file is chmod 600 because the URL contains a join token.
Nothing here prints API keys. The token is inside the URL; treat the file as a secret.

Library use:
    from scripts.mklink import make_link
    path = asyncio.run(make_link(room, scene))

Scene is the dict produced by orchestrator.scene.build_scene
({who, topic, agenda, briefs, voices}). The worker reads it from dispatch (job) metadata only.
Room metadata is visible to every participant holding a token, so it carries just a small public
summary (see public_room_metadata), never briefs. The full scene is also saved beside the link
(<room>.scene.json, chmod 600) so `scripts/dispatch.py` can dispatch it later.
"""
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
load_dotenv(Path.cwd() / ".env")
load_dotenv(ROOT / ".env")
SAFE = re.compile(r"[A-Za-z0-9_-]{1,64}")  # use fullmatch: `$` would accept a trailing newline
MAX_META = 60_000  # LiveKit metadata is about 64KB. Large briefs belong in files.


def public_room_metadata(scene: dict) -> str:
    """Room metadata: readable by every participant, so only non-sensitive facts, never briefs or voices.

    The summary is nested under "room" on purpose. orchestrator.scene.load_scene_metadata treats any
    top-level object with topic/who as a scene, and this one must never be mistaken for one.
    """
    who = [str(x) for x in (scene.get("who") or []) if isinstance(x, (str, int))]
    topic = str(scene.get("topic") or scene.get("title") or "")[:120]
    return json.dumps({"v": 1, "room": {"topic": topic, "who": who}}, ensure_ascii=False)


def _human_name() -> str:
    return os.environ.get("MEETING_HUMAN_NAME") or "用户"


def write_context_file(room: str, data: bytes, out_dir: Path | None = None) -> Path:
    """Save the context bundle as <links>/<room>.context.json (chmod 600) and return its absolute path.

    It holds every stand-in's full exported context, so it is as private as the link file. The scene carries
    only its path and sha256 (see meet/context_bundle.py).
    """
    if not SAFE.fullmatch(room):
        raise ValueError("room must match [A-Za-z0-9_-]{1,64}")
    path = (Path(out_dir) if out_dir else ROOT / "links") / f"{room}.context.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(path, 0o600)
    return path.resolve()


async def make_link(room: str, scene: dict, out: Path | None = None, hours: float = 2.0,
                    dispatch: bool = True, empty_timeout_s: int = 30 * 60) -> Path:
    if not SAFE.fullmatch(room):
        raise ValueError("room must match [A-Za-z0-9_-]{1,64}")
    meta = json.dumps(scene, ensure_ascii=False)
    if len(meta.encode()) > MAX_META:
        raise ValueError("scene too large for metadata; put briefs in files and pass briefs_path")
    from providers.transport import get_transport
    transport = get_transport()
    url = os.environ["LIVEKIT_URL"]
    agent = os.environ.get("LIVEKIT_AGENT_NAME", "voice-meeting")
    human = os.environ.get("MEETING_HUMAN_IDENTITY", "host")
    await transport.ensure_room(room, public_room_metadata(scene), empty_timeout_s)
    if dispatch:
        await transport.dispatch(room, meta, agent)
    token = transport.access_token(room=room, identity=human, display_name=_human_name(), hours=hours)
    urls = transport.join_urls(url=url, token=token, join_page=os.environ.get("MEETING_JOIN_PAGE"))
    link = urls.get("static") or urls["livekit"]
    out = Path(out or ROOT / f"links/{room}.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    body = link + "\n"
    if urls.get("static") and urls.get("livekit"):
        body = urls["static"] + "\n" + urls["livekit"] + "\n"
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body)
    os.chmod(out, 0o600)
    # Stash the full scene beside the link: it is what `scripts/dispatch.py` sends after `--no-dispatch`.
    # chmod 600: it may contain briefs.
    scene_path = out.with_suffix(".scene.json")
    fd = os.open(scene_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(meta)
    os.chmod(scene_path, 0o600)
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
