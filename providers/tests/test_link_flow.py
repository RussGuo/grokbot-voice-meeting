"""make_link / transport / dispatch.py / token_server, all offline. LiveKit's API client is faked."""
import asyncio
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from providers.transport import LiveKitTransport, _join_urls
from scripts import dispatch as dispatch_script
from scripts import mklink

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "aaa.bbb-ccc_ddd.eee"
SECRET_BRIEF = "RAW-EXPORTED-CONTEXT-DO-NOT-LEAK"

SCENE = {
    "version": 1, "topic": "Q3 roadmap", "who": ["pm", "eng"],
    "agenda": [{"id": "ag-1", "title": "Scope", "owner_id": "pm"}],
    "participants": [{"id": "pm", "kind": "bot"}],
    "briefs": {"pm": {"memory_snapshot": {"facts": [{"value": SECRET_BRIEF}]}}},
    "voices": {"pm": "v1"},
}


class FakeTransport:
    def __init__(self):
        self.calls = []

    async def ensure_room(self, room, metadata, empty_timeout_s):
        self.calls.append(("ensure_room", room, metadata))

    async def dispatch(self, room, metadata, agent_name):
        self.calls.append(("dispatch", room, metadata, agent_name))

    def access_token(self, **kw):
        return TOKEN

    def join_urls(self, *, url, token, join_page=None):
        return _join_urls(url, token, join_page)


@pytest.fixture
def fake(monkeypatch, tmp_path):
    t = FakeTransport()
    monkeypatch.setattr("providers.transport.get_transport", lambda name=None: t)
    monkeypatch.setenv("LIVEKIT_URL", "wss://demo.livekit.cloud")
    monkeypatch.setenv("MEETING_JOIN_PAGE", "https://me.github.io/repo/web/index.html")
    return t


def test_static_link_keeps_token_in_fragment_not_query():
    urls = _join_urls("wss://demo.livekit.cloud", TOKEN, "https://me.github.io/repo/index.html/")
    parts = urlsplit(urls["static"])
    assert parts.query == ""
    frag = parse_qs(parts.fragment)
    assert frag == {"liveKitUrl": ["wss://demo.livekit.cloud"], "token": [TOKEN]}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://me.github.io/repo/index.html"
    # The third-party fallback still needs the query form.
    assert parse_qs(urlsplit(urls["livekit"]).query)["token"] == [TOKEN]


def test_static_link_with_existing_query_and_fragment():
    urls = _join_urls("wss://x", TOKEN, "https://h/p?room=r1#old")
    parts = urlsplit(urls["static"])
    assert parts.query == "room=r1"
    assert parse_qs(parts.fragment)["token"] == [TOKEN]
    assert "#old" not in urls["static"]


def test_no_static_link_without_join_page(monkeypatch):
    monkeypatch.delenv("MEETING_JOIN_PAGE", raising=False)
    assert "static" not in _join_urls("wss://x", TOKEN, None)


def test_public_room_metadata_has_no_briefs_and_is_not_a_scene():
    meta = mklink.public_room_metadata(SCENE)
    assert SECRET_BRIEF not in meta and "briefs" not in meta and "voices" not in meta
    data = json.loads(meta)
    assert data == {"v": 1, "room": {"topic": "Q3 roadmap", "who": ["pm", "eng"]}}
    # The worker's room-metadata fallback must find no scene in it.
    from orchestrator.scene import load_scene_metadata
    assert load_scene_metadata(meta) is None


def test_make_link_room_metadata_small_dispatch_metadata_full(fake, tmp_path):
    out = asyncio.run(mklink.make_link("room-1", SCENE, out=tmp_path / "room-1.txt"))
    kinds = {c[0]: c for c in fake.calls}
    room_meta = kinds["ensure_room"][2]
    assert SECRET_BRIEF not in room_meta
    assert json.loads(room_meta)["room"]["who"] == ["pm", "eng"]
    disp = kinds["dispatch"]
    assert disp[1] == "room-1" and disp[3] == os.environ.get("LIVEKIT_AGENT_NAME", "voice-meeting")
    assert json.loads(disp[2]) == SCENE
    lines = out.read_text().splitlines()
    assert lines[0].startswith("https://me.github.io/repo/web/index.html#liveKitUrl=")
    assert "?" not in lines[0] and "token=" in lines[0]
    assert lines[1].startswith("https://meet.livekit.io/custom?")
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    scene_file = out.with_suffix(".scene.json")
    assert json.loads(scene_file.read_text()) == SCENE
    assert stat.S_IMODE(scene_file.stat().st_mode) == 0o600


def test_make_link_no_dispatch_still_saves_scene(fake, tmp_path):
    out = asyncio.run(mklink.make_link("room-2", SCENE, out=tmp_path / "room-2.txt", dispatch=False))
    assert [c[0] for c in fake.calls] == ["ensure_room"]
    assert json.loads(out.with_suffix(".scene.json").read_text()) == SCENE


@pytest.mark.parametrize("room", ["bad room", "x/../y", "ok\n", ""])
def test_make_link_rejects_bad_room(fake, tmp_path, room):
    with pytest.raises(ValueError):
        asyncio.run(mklink.make_link(room, SCENE, out=tmp_path / "x.txt"))


def test_livekit_transport_passes_metadata_through(monkeypatch):
    """ensure_room / dispatch hand exactly the given metadata to the (faked) LiveKit API."""
    import livekit.api as lk_api
    seen = {}

    class FakeRoomService:
        async def create_room(self, req):
            seen["room"] = req

    class FakeDispatchService:
        async def create_dispatch(self, req):
            seen["dispatch"] = req

    class FakeAPI:
        def __init__(self, *a, **k):
            self.room, self.agent_dispatch = FakeRoomService(), FakeDispatchService()

        async def aclose(self):
            seen["closed"] = seen.get("closed", 0) + 1

    monkeypatch.setattr(lk_api, "LiveKitAPI", FakeAPI)
    t = LiveKitTransport("wss://demo.livekit.cloud", "key", "secret")
    asyncio.run(t.ensure_room("r", '{"v": 1}', 60))
    asyncio.run(t.dispatch("r", json.dumps(SCENE), "voice-meeting"))
    assert seen["room"].metadata == '{"v": 1}'
    assert json.loads(seen["dispatch"].metadata) == SCENE
    assert seen["dispatch"].agent_name == "voice-meeting" and seen["closed"] == 2


# ---- scripts/dispatch.py ----

def test_dispatch_scene_loading_explicit_and_default(tmp_path, monkeypatch):
    f = tmp_path / "s.scene.json"
    f.write_text(json.dumps(SCENE))
    meta, path = dispatch_script.resolve_metadata("any", str(f))
    assert json.loads(meta) == SCENE and path == f
    # default path used when present
    monkeypatch.setattr(dispatch_script, "ROOT", tmp_path)
    (tmp_path / "links").mkdir()
    (tmp_path / "links" / "r1.scene.json").write_text(json.dumps(SCENE))
    meta, path = dispatch_script.resolve_metadata("r1", None)
    assert json.loads(meta) == SCENE and path.name == "r1.scene.json"
    # absent default -> no metadata (sample-scenario flow, e.g. run_sim.sh)
    assert dispatch_script.resolve_metadata("r2", None) == ("", None)
    # --no-scene wins over an existing default
    assert dispatch_script.resolve_metadata("r1", None, no_scene=True) == ("", None)


def test_dispatch_scene_errors(tmp_path):
    with pytest.raises(ValueError, match="cannot read"):
        dispatch_script.resolve_metadata("r", str(tmp_path / "missing.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    with pytest.raises(ValueError, match="not valid JSON"):
        dispatch_script.load_scene_metadata_file(bad)
    arr = tmp_path / "arr.json"
    arr.write_text("[1]")
    with pytest.raises(ValueError, match="object"):
        dispatch_script.load_scene_metadata_file(arr)
    big = tmp_path / "big.json"
    big.write_text(json.dumps({"x": "a" * 70_000}))
    with pytest.raises(ValueError, match="too large"):
        dispatch_script.load_scene_metadata_file(big)
    with pytest.raises(ValueError):
        dispatch_script.default_scene_path("../etc/passwd")


def test_dispatch_main_sends_metadata(fake):
    asyncio.run(dispatch_script.main("r1", "voice-meeting", json.dumps(SCENE)))
    asyncio.run(dispatch_script.main("r1", "voice-meeting"))
    assert json.loads(fake.calls[0][2]) == SCENE and fake.calls[1][2] == ""


# ---- web/token_server.py ----

def _load_token_server():
    spec = importlib.util.spec_from_file_location("token_server_under_test", ROOT / "web" / "token_server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def test_replay_run_id_validation_and_listing(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    ts = _load_token_server()
    runs = tmp_path / "runs"
    (runs / "good-run").mkdir(parents=True)
    (runs / "good-run" / "events.jsonl").write_text('{"type":"MeetingCreated","at":0,"seq":1,"data":{}}\n')
    (runs / "no-events").mkdir()
    (runs / "bad name").mkdir()
    (runs / "bad name" / "events.jsonl").write_text("{}\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "events.jsonl").write_text("{}\n")
    try:
        (runs / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pass
    monkeypatch.setattr(ts, "RUNS", runs)
    monkeypatch.setattr(ts, "ACCESS_KEY", "")
    async with TestClient(TestServer(ts.make_app())) as c:
        listed = (await (await c.get("/api/replay")).json())["runs"]
        assert listed == ["good-run"]  # no events.jsonl, unsafe name and symlink escape are all hidden
        r = await c.get("/api/replay/good-run/events")
        assert r.status == 200 and len((await r.json())["events"]) == 1
        assert (await c.get("/api/replay/no-events/events")).status == 404
        assert (await c.get("/api/replay/good-run/audio")).status == 404
        assert (await c.get("/api/replay/..%2Foutside/events")).status == 404
        assert (await c.get("/api/replay/good-run%0A/events")).status == 404
        assert (await c.get("/api/replay/bad%20name/events")).status == 404
        if (runs / "link").exists():
            assert (await c.get("/api/replay/link/events")).status == 404
        assert (await c.get("/api/token?room=ok%0A")).status == 400
