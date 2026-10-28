import asyncio
import json
import threading

import pytest

from meet import live_controls as lc
from meet.live_controls import ControlError, addable_bots, build_bot, parse_control, pick_voice, run_bot_add
from orchestrator.i18n import is_unknown
from orchestrator.scene import build_scene, scene_to_meeting

POOL = None


def _pool():
    global POOL
    if POOL is None:
        import voice  # noqa: F401
        from voice_layer.config import VERIFIED_VOICE_POOL
        POOL = [s for s, _ in VERIFIED_VOICE_POOL]
    return POOL


def _meeting(lang="en"):
    voices = {"moderator": _pool()[0], "pm": _pool()[1]}
    scene = build_scene(who=["pm"], topic="Roadmap", agenda=["Scope", "Risks"], briefs={}, voices=voices,
                        registry={"pm": {"display_name": "Product"}}, language=lang)
    meeting, _ = scene_to_meeting(scene)
    return meeting, voices


REGISTRY = {
    "__moderator__": {"id": "moderator"},
    "pm": {"display_name": "Product", "context": "pm ctx"},
    "eng": {"display_name": "Engineering", "aliases": ["Eng"], "principal": "eng-real",
            "context": "Engineering owns the sync engine.\nCan commit:\n- write the spec\n"},
    "ops": {"display_name": "Ops", "context_file": "missing.md"},
}


def enc(obj) -> bytes:
    return json.dumps(obj).encode()


# —— 解析 / 校验 ——

def test_parse_valid_actions():
    assert parse_control(enc({"action": "end"})) == {"type": "control.end"}
    assert parse_control(enc({"action": "interrupt"})) == {"type": "control.interrupt"}
    assert parse_control(enc({"action": "agenda_next"})) == {"type": "control.agenda_next"}
    assert parse_control(enc({"action": "agenda_add", "title": "  Budget \n review "})) == \
        {"type": "control.agenda_add", "title": "Budget review"}
    assert parse_control(enc({"action": "agenda_rename", "title": "New", "item_id": "ag-2"})) == \
        {"type": "control.agenda_rename", "title": "New", "item_id": "ag-2"}
    assert parse_control(enc({"action": "agenda_rename", "title": "New"})) == {"type": "control.agenda_rename", "title": "New"}
    assert parse_control(enc({"action": "bot_add", "bot_id": "eng_2.x"})) == {"type": "control.bot_add", "bot_id": "eng_2.x"}
    assert parse_control(json.dumps({"action": "end"})) == {"type": "control.end"}      # str 也收


def test_parse_drops_unknown_fields_and_garbage():
    ev = parse_control(enc({"action": "agenda_add", "title": "x", "type": "control.bot_ready", "evil": {"a": 1}}))
    assert ev == {"type": "control.agenda_add", "title": "x"}                      # 不能借字段伪造别的事件
    assert parse_control(b"not json") is None
    assert parse_control(b"\xff\xfe") is None
    assert parse_control(enc(["end"])) is None
    assert parse_control(enc("end")) is None
    assert parse_control(b"{" + b" " * 3000 + b"}") is None                         # 过大


@pytest.mark.parametrize("msg", [
    {"action": "agenda_add"}, {"action": "agenda_add", "title": ""}, {"action": "agenda_add", "title": 3},
    {"action": "agenda_add", "title": "x" * 81}, {"action": "agenda_add", "title": "‮\n"},
    {"action": "agenda_rename", "title": "ok", "item_id": 5}, {"action": "agenda_rename", "title": "ok", "item_id": "a b"},
    {"action": "agenda_rename", "item_id": "ag-1"},
    {"action": "bot_add"}, {"action": "bot_add", "bot_id": ""}, {"action": "bot_add", "bot_id": "../x"},
    {"action": "bot_add", "bot_id": "a" * 65}, {"action": "bot_add", "bot_id": ["pm"]},
    {"action": "bot_add", "bot_id": "__moderator__"},
])
def test_parse_rejects_invalid_fields(msg):
    ev = parse_control(enc(msg))
    assert ev["type"] == "control.rejected" and ev["reason"] == "invalid" and ev["action"] == msg["action"]


def test_parse_unknown_or_malformed_action():
    assert parse_control(enc({"action": "rm -rf"})) == {"type": "control.rejected", "action": "?", "reason": "unknown_action"}
    assert parse_control(enc({"action": "shutdown"}))["reason"] == "unknown_action"
    assert parse_control(enc({"action": 5}))["action"] == "?"
    assert parse_control(enc({}))["reason"] == "unknown_action"


# —— 注册表 ——

def test_addable_bots_lists_only_ids_and_names():
    out = addable_bots(REGISTRY, ["pm", "moderator", "host"])
    assert out == {"eng": "Engineering", "ops": "Ops"}
    assert all("context" not in k for k in out) and "Engineering owns" not in json.dumps(out)
    assert addable_bots({"bad id!": {"display_name": "x"}, "ok": {}}, []) == {"ok": "ok"}


def test_load_worker_registry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    assert lc.load_worker_registry() == ({}, "no_registry")
    assert lc.load_worker_registry(str(tmp_path / "nope.yaml")) == ({}, "registry_unreadable")
    (tmp_path / "bots.json").write_text(json.dumps({"bots": {"pm": {"display_name": "P"}}}), "utf-8")
    reg, why = lc.load_worker_registry()
    assert why is None and reg["pm"]["display_name"] == "P"
    (tmp_path / "bad.json").write_text("{", "utf-8")
    assert lc.load_worker_registry(str(tmp_path / "bad.json"))[1] == "registry_unreadable"


# —— 音色 ——

def test_pick_voice_avoids_used_voices_and_honors_preference():
    pool = _pool()
    used = {"moderator": pool[0], "pm": pool[1]}
    v = pick_voice("eng", used)
    assert v in pool and v not in used.values()
    assert pick_voice("eng", used, preferred=pool[5]) == pool[5]
    assert pick_voice("eng", used, preferred=pool[1]) not in used.values()          # 偏好被占 → 另选
    assert pick_voice("eng", {**used, "eng": pool[2]}) in pool                       # 自己的旧条目不占位（不抛错）
    full = {f"b{i}": s for i, s in enumerate(pool)}
    with pytest.raises(ControlError) as e:
        pick_voice("eng", full)
    assert e.value.code == "no_voice"


# —— 构建 ——

@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_BRIEF_CACHE", str(tmp_path / "cache"))


def test_build_bot_heuristic_without_llm():
    meeting, voices = _meeting()
    meeting.agenda.append(type(meeting.agenda[0])(id="ag-3", title="Added later", owner_id="moderator", timebox_ms=1000))
    built = asyncio.run(build_bot("eng", meeting=meeting, registry=REGISTRY, voices=voices))
    p, brief = built.participant, built.brief
    assert p.id == "eng" and p.kind == "bot" and p.display_name == "Engineering"
    assert p.aliases[:2] == ["Engineering", "eng"] and "Eng" in p.aliases
    assert p.principal_ref.bot_id == "eng-real" and p.agent_profile.tts_voice == built.voice
    assert built.voice not in {_pool()[0], _pool()[1]} and voices["eng"] == built.voice   # 立刻占位
    assert [x.agenda_item_id for x in brief.positions] == ["ag-1", "ag-2", "ag-3"]      # 含新增议程项
    assert all(is_unknown(x.stance) for x in brief.positions)                           # 没有 LLM：立场标未知，不编
    assert brief.mandate.can_commit == ["write the spec"]
    assert brief.memory_snapshot.facts[0].key == "context.excerpt"


def test_build_bot_runs_summarize_in_a_worker_thread(monkeypatch):
    meeting, voices = _meeting()
    seen = {}
    real = lc.summarize

    def spy(**kw):
        seen["thread"] = threading.current_thread()
        seen["chat"] = kw["chat"]
        return real(**kw)
    monkeypatch.setattr(lc, "summarize", spy)
    class Chat:
        def complete(self, messages, **_):
            seen["complete_thread"] = threading.current_thread()
            return json.dumps({"positions": [{"agenda_item_id": "ag-1", "stance": "ship it", "evidence": []}]})

    chat = Chat()
    built = asyncio.run(build_bot("eng", meeting=meeting, registry=REGISTRY, voices=voices, chat=chat, model="m"))
    assert seen["thread"] is not threading.main_thread() and seen["chat"] is chat
    assert seen["complete_thread"] is not threading.main_thread()                  # 阻塞的 LLM 调用不在事件循环线程
    assert built.brief.positions[0].stance == "ship it"


def test_build_bot_falls_back_to_heuristic_when_llm_fails():
    class Boom:
        def complete(self, *a, **k):
            raise RuntimeError("network")
    meeting, voices = _meeting()
    built = asyncio.run(build_bot("eng", meeting=meeting, registry=REGISTRY, voices=voices, chat=Boom(), model="m"))
    assert all(is_unknown(x.stance) for x in built.brief.positions)


def test_build_bot_validation_errors_leave_voices_untouched():
    meeting, voices = _meeting()
    before = dict(voices)
    for bid, code in (("nope", "unknown_bot"), ("__moderator__", "unknown_bot"), ("pm", "already_in_meeting"),
                      ("ops", "context_unreadable"), ("../x", "unknown_bot")):
        with pytest.raises(ControlError) as e:
            asyncio.run(build_bot(bid, meeting=meeting, registry=REGISTRY, voices=voices))
        assert e.value.code == code
    assert voices == before                                                              # 失败的占位都撤掉了


def test_build_bot_timeout_releases_voice(monkeypatch):
    meeting, voices = _meeting()
    monkeypatch.setattr(lc, "BUILD_TIMEOUT_S", 0.05)
    release = threading.Event()
    monkeypatch.setattr(lc, "summarize", lambda **kw: release.wait(2) or {})
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(build_bot("eng", meeting=meeting, registry=REGISTRY, voices=voices))
    release.set()
    assert "eng" not in voices


# —— 后台任务 ——

class Inbox:
    def __init__(self):
        self.items = []

    def put_nowait(self, x):
        self.items.append(x)


def test_run_bot_add_delivers_ready_after_connect_and_warm():
    meeting, voices = _meeting()
    inbox, calls = Inbox(), []

    async def connect(bid, name):
        calls.append(("connect", bid, name))

    asyncio.run(run_bot_add("eng", meeting=meeting, registry=REGISTRY, voices=voices, chat=None, model="heuristic",
                            inbox=inbox, apply_voice=lambda b, v: calls.append(("voice", b, v)), connect=connect,
                            warm=lambda b: calls.append(("warm", b))))
    assert [c[0] for c in calls] == ["voice", "connect", "warm"] and calls[1][2] == "Engineering"
    (ev,) = inbox.items
    assert ev["type"] == "control.bot_ready" and ev["participant"].id == "eng" and ev["brief"].participant_id == "eng"
    assert voices["eng"] == ev["voice"] == calls[0][2]


def test_run_bot_add_reports_failures_and_never_raises():
    meeting, voices = _meeting()
    inbox = Inbox()

    async def bad_connect(bid, name):
        raise OSError("room gone")

    def run(bid, **kw):
        asyncio.run(run_bot_add(bid, meeting=meeting, registry=REGISTRY, voices=voices, chat=None, model="heuristic",
                                inbox=inbox, apply_voice=lambda b, v: None, **kw))

    run("nope")
    run("pm")
    run("eng", connect=bad_connect)
    run("eng", warm=lambda b: (_ for _ in ()).throw(RuntimeError("tts")))
    assert [(e["type"], e["bot_id"], e["reason"]) for e in inbox.items] == [
        ("control.bot_failed", "nope", "unknown_bot"), ("control.bot_failed", "pm", "already_in_meeting"),
        ("control.bot_failed", "eng", "connect_failed"), ("control.bot_failed", "eng", "connect_failed")]
    assert "eng" not in voices                                                           # 失败后音色放回去


def test_run_bot_add_connect_timeout(monkeypatch):
    meeting, voices = _meeting()
    inbox = Inbox()
    monkeypatch.setattr(lc, "CONNECT_TIMEOUT_S", 0.05)

    async def hang(bid, name):
        await asyncio.sleep(5)

    asyncio.run(run_bot_add("eng", meeting=meeting, registry=REGISTRY, voices=voices, chat=None, model="heuristic",
                            inbox=inbox, apply_voice=lambda b, v: None, connect=hang))
    assert inbox.items[0]["reason"] == "connect_failed"


def test_two_concurrent_adds_get_different_voices():
    meeting, voices = _meeting()
    registry = {**REGISTRY, "growth": {"display_name": "Growth", "context": "g"}}
    inbox = Inbox()

    async def both():
        await asyncio.gather(*(run_bot_add(b, meeting=meeting, registry=registry, voices=voices, chat=None,
                                           model="heuristic", inbox=inbox, apply_voice=lambda b, v: None)
                               for b in ("eng", "growth")))
    asyncio.run(both())
    assert len({e["voice"] for e in inbox.items}) == 2
