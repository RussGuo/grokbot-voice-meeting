"""Context bundle: build, write (chmod 600), scene reference, load with hash check, and in-meeting bot add. Offline."""
import asyncio
import hashlib
import json
import logging
import stat

import pytest

from meet.context_bundle import build_bundle, scene_ref, sha256_hex
from meet.live_controls import build_bot
from orchestrator.retrieval import ContextIndex
from orchestrator.scene import build_scene, load_scene_metadata
from scripts.mklink import write_context_file

NOTES = {
    "pm": "# Roadmap\n\nThe Q3 roadmap has three bets: offline sync, AI summary, and the new onboarding flow.\n\n"
          "# Risks\n\nThe biggest risk is the sync engine rewrite slipping past August.",
    "eng": "# 离线同步\n\n冲突率改造后降到 0.4%，用的是 CRDT 合并。\n\n# 人手\n\n两位工程师在做同步，第三位等预算。",
    "ops": "   \n",
}


def test_build_bundle_format_and_empty_bots_skipped():
    b = build_bundle(NOTES)
    assert set(b.bots) == {"pm", "eng"} and not b.warnings and b
    data = json.loads(b.to_json())
    assert data["v"] == 1 and set(data["bots"]) == {"pm", "eng"}
    assert data["bots"]["pm"]["chunks"][0] == {"id": "pm#1", "text": data["bots"]["pm"]["chunks"][0]["text"]}
    assert [c["id"] for c in data["bots"]["eng"]["chunks"]] == ["eng#1", "eng#2"]
    assert "冲突率" in b.to_json().decode("utf-8")                       # 不转义成 \uXXXX，文件小一点
    assert not build_bundle({"a": "", "b": "  "})


def test_build_bundle_caps_warn_and_truncate_tail():
    big = "\n\n".join(f"Paragraph {i}: " + "word " * 60 for i in range(100))
    b = build_bundle({"a": big, "b": big}, per_bot_bytes=5_000, total_bytes=8_000)
    used = {k: sum(len(c["text"].encode()) for c in v) for k, v in b.bots.items()}
    assert 0 < used["a"] <= 5_000 and sum(used.values()) <= 8_000
    assert any(w.startswith("a:") for w in b.warnings) and any(w.startswith("b:") for w in b.warnings)
    assert b.bots["a"][0]["id"] == "a#1"                                  # 丢的是尾巴
    b2 = build_bundle({"a": big, "b": big}, per_bot_bytes=50_000, total_bytes=6_000)
    assert "b" not in b2.bots and any("dropped" in w for w in b2.warnings)


def test_write_context_file_is_private_and_absolute(tmp_path):
    data = build_bundle(NOTES).to_json()
    path = write_context_file("room-1", data, tmp_path / "links")
    assert path.is_absolute() and path.name == "room-1.context.json" and path.read_bytes() == data
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(ValueError):
        write_context_file("../evil", data, tmp_path)


def test_scene_carries_reference_and_load_scene_metadata_keeps_tuple_shape(tmp_path):
    data = build_bundle(NOTES).to_json()
    path = write_context_file("room-1", data, tmp_path)
    ref = scene_ref(str(path), data)
    assert ref == {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()} == {"path": str(path), "sha256": sha256_hex(data)}
    scene = build_scene(who=["pm"], topic="Roadmap", agenda=["Scope"], briefs={}, voices={},
                        registry={"pm": {"display_name": "Product"}}, language="en", context_bundle=ref)
    assert scene["context_bundle"] == ref
    assert "context_bundle" not in build_scene(who=["pm"], topic="t", agenda=None, briefs={}, voices={}, registry={})
    blob = json.dumps(scene)
    assert "Q3 roadmap" not in blob and len(blob.encode()) < 60_000       # 场景里只有路径和哈希，没有 context
    loaded = load_scene_metadata(blob)
    meeting, briefs, voices = loaded                                       # 仍可三元组解包
    assert loaded.context_bundle == ref and meeting.title == "Roadmap"
    bad = dict(scene, context_bundle={"path": str(path), "sha256": "abc"})
    assert load_scene_metadata(json.dumps(bad)).context_bundle is None
    assert load_scene_metadata(json.dumps({**scene, "context_bundle": "x"})).context_bundle is None
    no_ref = {k: v for k, v in scene.items() if k != "context_bundle"}
    assert load_scene_metadata(json.dumps(no_ref)).context_bundle is None


def test_index_load_roundtrip_and_search(tmp_path):
    data = build_bundle(NOTES).to_json()
    path = write_context_file("r", data, tmp_path)
    ix = ContextIndex.load(path, sha256_hex(data))
    assert ix is not None and sorted(ix.bots()) == ["eng", "pm"] and ix.chunk_count("pm") == 2
    assert ix.search("pm", "what is the biggest risk with the sync engine rewrite")[0].id == "pm#2"
    assert ix.search("eng", "冲突率现在多少")[0].id == "eng#1"
    assert ix.search("pm", "冲突率现在多少") == []                          # 不跨 bot


def test_index_load_rejects_hash_mismatch_missing_and_garbage(tmp_path, caplog):
    data = build_bundle(NOTES).to_json()
    path = write_context_file("r", data, tmp_path)
    with caplog.at_level(logging.WARNING, logger="retrieval"):
        assert ContextIndex.load(path, "0" * 64) is None
        assert "sha256" in caplog.text
        path.write_bytes(data + b" ")                                      # 内容被改过
        assert ContextIndex.load(path, sha256_hex(data)) is None
        assert ContextIndex.load(tmp_path / "gone.json", sha256_hex(data)) is None
        for junk in (b"not json", b"[]", b'{"v":2,"bots":{}}', b'{"v":1,"bots":{"a":{"chunks":"x"}}}'):
            path.write_bytes(junk)
            assert ContextIndex.load(path, sha256_hex(junk)) is None
        assert ContextIndex.load(None, "0" * 64) is None


def test_index_load_skips_malformed_chunks_but_keeps_good_ones(tmp_path):
    raw = json.dumps({"v": 1, "bots": {"a": {"chunks": [{"id": "a#1", "text": "alpha beta gamma delta"}, {"id": "a#2"}, 5]}}}).encode()
    path = tmp_path / "c.json"
    path.write_bytes(raw)
    ix = ContextIndex.load(path, sha256_hex(raw))
    assert ix is not None and ix.chunk_count("a") == 1


# —— 会中加 bot：完整 context 也进索引 ——

REGISTRY = {"eng": {"display_name": "Engineering", "context": NOTES["eng"] + "\n\n# 缓存\n\n编译缓存命中率约 87%。"}}


def _meeting():
    import voice  # noqa: F401
    from voice_layer.config import VERIFIED_VOICE_POOL
    from orchestrator.scene import scene_to_meeting
    pool = [s for s, _ in VERIFIED_VOICE_POOL]
    voices = {"moderator": pool[0], "pm": pool[1]}
    scene = build_scene(who=["pm"], topic="Roadmap", agenda=["Scope"], briefs={}, voices=voices,
                        registry={"pm": {"display_name": "Product"}}, language="zh")
    return scene_to_meeting(scene)[0], voices


def test_build_bot_adds_context_to_index(monkeypatch, tmp_path):
    monkeypatch.setenv("MEETING_BRIEF_CACHE", str(tmp_path / "cache"))
    meeting, voices = _meeting()
    ix = ContextIndex()
    asyncio.run(build_bot("eng", meeting=meeting, registry=REGISTRY, voices=voices, context_index=ix))
    assert ix.bots() == ["eng"]
    assert ix.search("eng", "编译缓存命中率是多少")[0].text.startswith("缓存")
    # 没有 context_index 时行为不变
    meeting, voices = _meeting()
    built = asyncio.run(build_bot("eng", meeting=meeting, registry=REGISTRY, voices=voices))
    assert built.participant.id == "eng"


def test_build_bot_survives_index_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("MEETING_BRIEF_CACHE", str(tmp_path / "cache"))

    class Boom:
        def add_bot(self, *a):
            raise RuntimeError("index")
    meeting, voices = _meeting()
    built = asyncio.run(build_bot("eng", meeting=meeting, registry=REGISTRY, voices=voices, context_index=Boom()))
    assert built.participant.id == "eng"


# —— `meet` 端到端（假传输层）：bundle 写在 link 旁边，场景只带路径 + 哈希 ——

def test_cmd_start_writes_bundle_beside_link_and_scene_has_only_reference(monkeypatch, tmp_path, capsys):
    from meet import cli
    from providers.transport import _join_urls
    from scripts import mklink

    calls = []

    class FakeTransport:
        async def ensure_room(self, room, metadata, empty_timeout_s):
            pass

        async def dispatch(self, room, metadata, agent_name):
            calls.append(json.loads(metadata))

        def access_token(self, **kw):
            return "aaa.bbb.ccc"

        def join_urls(self, *, url, token, join_page=None):
            return _join_urls(url, token, join_page)

    monkeypatch.setattr("providers.transport.get_transport", lambda name=None: FakeTransport())
    monkeypatch.setattr(mklink, "ROOT", tmp_path)
    monkeypatch.setenv("LIVEKIT_URL", "wss://demo.livekit.cloud")
    monkeypatch.setenv("MEETING_BRIEF_CACHE", str(tmp_path / "cache"))
    monkeypatch.chdir(tmp_path)
    ctx_dir = tmp_path / "ctx"
    ctx_dir.mkdir()
    (ctx_dir / "pm.md").write_text(NOTES["pm"], encoding="utf-8")
    (ctx_dir / "eng.md").write_text(NOTES["eng"], encoding="utf-8")
    rc = cli.main(["--who", "pm,eng", "--topic", "Roadmap", "--context-dir", str(ctx_dir), "--no-llm",
                   "--room", "r-ctx", "--lang", "en", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    bundle_path = tmp_path / "links" / "r-ctx.context.json"
    assert out["context_file"] == str(bundle_path.resolve()) and bundle_path.is_file()
    assert stat.S_IMODE(bundle_path.stat().st_mode) == 0o600
    scene = calls[0]
    raw = bundle_path.read_bytes()
    assert scene["context_bundle"] == {"path": str(bundle_path.resolve()), "sha256": hashlib.sha256(raw).hexdigest()}
    assert "new onboarding flow" not in json.dumps(scene["context_bundle"])      # 场景里只有路径和哈希
    ix = ContextIndex.load(scene["context_bundle"]["path"], scene["context_bundle"]["sha256"])
    assert ix is not None and sorted(ix.bots()) == ["eng", "pm"]
    saved = json.loads((tmp_path / "links" / "r-ctx.scene.json").read_text())      # 之后 dispatch.py 发的也是同一份
    assert saved["context_bundle"] == scene["context_bundle"]


def test_cmd_start_without_any_context_writes_no_bundle(monkeypatch, tmp_path, capsys):
    from meet import cli
    from providers.transport import _join_urls
    from scripts import mklink

    class FakeTransport:
        async def ensure_room(self, *a):
            pass

        async def dispatch(self, room, metadata, agent_name):
            self.meta = json.loads(metadata)

        def access_token(self, **kw):
            return "aaa.bbb.ccc"

        def join_urls(self, *, url, token, join_page=None):
            return _join_urls(url, token, join_page)

    t = FakeTransport()
    monkeypatch.setattr("providers.transport.get_transport", lambda name=None: t)
    monkeypatch.setattr(mklink, "ROOT", tmp_path)
    monkeypatch.setenv("LIVEKIT_URL", "wss://demo.livekit.cloud")
    monkeypatch.setenv("MEETING_BRIEF_CACHE", str(tmp_path / "cache"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bots.json").write_text(json.dumps({"bots": {"pm": {"display_name": "Product"}}}), encoding="utf-8")
    assert cli.main(["--who", "pm", "--topic", "Roadmap", "--no-llm", "--room", "r-none", "--lang", "en", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["context_file"] == ""
    assert "context_bundle" not in t.meta and not (tmp_path / "links" / "r-none.context.json").exists()
