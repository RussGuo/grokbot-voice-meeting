"""最简 token 服务 + 静态网页。

默认只监听 127.0.0.1，不对公网暴露。密钥只从环境变量读取：
  LIVEKIT_URL         wss://<project>.livekit.cloud
  LIVEKIT_API_KEY
  LIVEKIT_API_SECRET
可选：
  LIVEKIT_AGENT_NAME  若 agent 用显式 dispatch（agent_name），在 token 里带上，进房即派发
  WEB_HOST / WEB_PORT 默认 127.0.0.1 / 8787
"""
import os
import re
from pathlib import Path

from aiohttp import web
from livekit import api

HERE = Path(__file__).parent
SAFE = re.compile(r"[A-Za-z0-9_\-]{1,64}")  # use fullmatch: `$` would accept a trailing newline


def _env(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        raise web.HTTPServiceUnavailable(text=f"missing env {name}")
    return v


async def token(request: web.Request) -> web.Response:
    room = request.query.get("room", "meeting-dev")
    identity = request.query.get("identity") or os.environ.get("MEETING_HUMAN_IDENTITY", "host")
    if not SAFE.fullmatch(room) or not SAFE.fullmatch(identity):
        raise web.HTTPBadRequest(text="room/identity must match [A-Za-z0-9_-]{1,64}")

    at = (
        api.AccessToken(_env("LIVEKIT_API_KEY"), _env("LIVEKIT_API_SECRET"))
        .with_identity(identity)
        .with_name(request.query.get("name", identity)[:64])
        .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True,
                                     can_subscribe=True, can_publish_data=True))
    )
    agent_name = os.environ.get("LIVEKIT_AGENT_NAME")
    if agent_name:
        at = at.with_room_config(api.RoomConfiguration(
            agents=[api.RoomAgentDispatch(agent_name=agent_name)]))
    return web.json_response({"url": _env("LIVEKIT_URL"), "token": at.to_jwt(),
                              "room": room, "identity": identity})


RUNS = HERE.parent / "runs"


def _safe_run(run: str) -> Path | None:
    """runs/<run> if the id is plain and the directory really lives under runs/ (no symlink escape)."""
    if not SAFE.fullmatch(run):
        return None
    d = RUNS / run
    if not d.is_dir() or RUNS.resolve() not in d.resolve().parents:
        return None
    return d


def _run_dir(request: web.Request) -> Path:
    d = _safe_run(request.match_info["run"])
    if d is None:
        raise web.HTTPNotFound(text="no such run")
    return d


async def replay_events(request: web.Request) -> web.Response:
    """回放：返回 runs/<run>/events.jsonl（编排器领域事件）+ 音频对齐偏移。只读、只限 runs/ 下。"""
    import json
    d = _run_dir(request)
    if not (d / "events.jsonl").is_file():
        raise web.HTTPNotFound(text="no events for this run")
    evs = [json.loads(ln) for ln in (d / "events.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    offset_ms = 0.0
    man = d / "audio" / "manifest.json"
    if man.exists():
        m = json.loads(man.read_text())
        offset_ms = m.get("meeting_t0_epoch_ms", 0) - m.get("recorder_t0_ms", 0)
    return web.json_response({"events": evs, "audio_offset_ms": offset_ms,
                              "has_audio": (d / "audio" / "mix.wav").exists()})


async def replay_levels(request: web.Request) -> web.Response:
    """回放波形：audio/levels.jsonl 降采样到 50ms 一格，时间换算成会议时间（ms）。"""
    import json
    d = _run_dir(request)
    lv, man = d / "audio" / "levels.jsonl", d / "audio" / "manifest.json"
    if not lv.exists() or not man.exists():
        return web.json_response({"step_ms": 50, "tracks": {}})
    t0 = json.loads(man.read_text()).get("meeting_t0_epoch_ms", 0)
    out: dict = {}
    for line in lv.read_text().splitlines():
        x = json.loads(line)
        k = int((x["t_ms"] - t0) // 50)
        if k < 0:
            continue
        arr = out.setdefault(x["id"], {})
        arr[k] = max(arr.get(k, -120.0), x["db"])
    tracks = {}
    for ident, m in out.items():
        n = max(m) + 1
        tracks[ident] = [round(m.get(i, -120.0), 1) for i in range(n)]
    return web.json_response({"step_ms": 50, "tracks": tracks})


async def replay_minutes(request: web.Request) -> web.Response:
    import json
    d = _run_dir(request)
    md = (d / "minutes.md").read_text(encoding="utf-8") if (d / "minutes.md").exists() else ""
    actions = {}
    for f in sorted((d / "actions").glob("*.json")) if (d / "actions").is_dir() else []:
        actions[f.stem] = json.loads(f.read_text(encoding="utf-8"))
    return web.json_response({"markdown": md, "actions": actions})


async def replay_audio(request: web.Request) -> web.FileResponse:
    f = _run_dir(request) / "audio" / "mix.wav"
    if not f.is_file():
        raise web.HTTPNotFound(text="no audio for this run")
    return web.FileResponse(f)


async def replay_list(_: web.Request) -> web.Response:
    runs = sorted(p.name for p in RUNS.iterdir()
                  if _safe_run(p.name) and (p / "events.jsonl").exists()) if RUNS.is_dir() else []
    return web.json_response({"runs": runs})


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(HERE / "index.html")


async def health(_: web.Request) -> web.Response:
    missing = [k for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
               if not os.environ.get(k)]
    return web.json_response({"ok": not missing, "missing_env": missing})


ACCESS_KEY = os.environ.get("MEETING_ACCESS_KEY", "")  # 设了即「公网模式」


@web.middleware
async def access_guard(request: web.Request, handler):
    """公网模式（经隧道对外）：除 /healthz 外都要口令；口令可走 ?k= 或首访后下发的 HttpOnly cookie。
    公网模式下关闭 /api/replay，token 只签给 MEETING_HUMAN_IDENTITY，房间可锁定为 MEETING_ROOM。"""
    import hmac
    if not ACCESS_KEY or request.path == "/healthz":
        return await handler(request)
    if request.path.startswith("/api/replay"):
        raise web.HTTPNotFound()
    got = request.query.get("k") or request.cookies.get("mk", "")
    if not hmac.compare_digest(got.encode(), ACCESS_KEY.encode()):
        raise web.HTTPForbidden(text="forbidden")
    if request.path == "/api/token":
        human = os.environ.get("MEETING_HUMAN_IDENTITY", "host")
        if request.query.get("identity", human) != human:
            raise web.HTTPForbidden(text="identity locked")
        locked = os.environ.get("MEETING_ROOM")
        if locked and request.query.get("room") != locked:
            raise web.HTTPForbidden(text="room locked")
    resp = await handler(request)
    if request.query.get("k"):
        resp.set_cookie("mk", ACCESS_KEY, httponly=True, secure=True, samesite="Strict", max_age=6 * 3600)
    return resp


def make_app() -> web.Application:
    app = web.Application(middlewares=[access_guard])
    app.add_routes([web.get("/", index), web.get("/api/token", token),
                    web.get("/healthz", health), web.get("/api/replay", replay_list),
                    web.get("/api/replay/{run}/events", replay_events),
                    web.get("/api/replay/{run}/minutes", replay_minutes),
                    web.get("/api/replay/{run}/levels", replay_levels),
                    web.get("/api/replay/{run}/audio", replay_audio)])
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host=os.environ.get("WEB_HOST", "127.0.0.1"),
                port=int(os.environ.get("WEB_PORT", "8787")))
