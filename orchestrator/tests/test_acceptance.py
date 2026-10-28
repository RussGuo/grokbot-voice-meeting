"""scripts/acceptance.py 的单测：全部用假数据（fake 语音层跑出的事件 + 合成 wav），不调任何真实 API。"""
import asyncio
import importlib.util
import json
import math
import struct
import wave
from pathlib import Path

import pytest

from orchestrator.driver import SimDriver
from orchestrator.minutes import export
from orchestrator.sim import run_lines

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("acceptance", ROOT / "scripts" / "acceptance.py")
acc = importlib.util.module_from_spec(spec)
import sys; sys.modules["acceptance"] = acc  # noqa: E402  须在 importlib 装载之间注册
spec.loader.exec_module(acc)

SR = 8000


def write_wav(path: Path, total_ms: int, spans: list[tuple[int, int]], freq=440):
    n = total_ms * SR // 1000
    buf = bytearray(n * 2)
    for a, b in spans:
        for i in range(max(0, a * SR // 1000), min(n, b * SR // 1000)):
            struct.pack_into("<h", buf, i * 2, int(8000 * math.sin(2 * math.pi * freq * i / SR)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(bytes(buf))


def synth_audio(run: Path, events: list[dict], bot_tail_ms: int = 0):
    """按事件时间合成每人一条音轨。bot_tail_ms>0 模拟「日志说停了，音频还在响」。"""
    human = next(e for e in events if e["type"] == "MeetingCreated")["data"]["payload"]["linked_human_id"]
    end = max(e["at"] for e in events) + 1000
    spans: dict[str, list] = {}
    start = None
    for e in events:
        if e["type"] == "HumanVad":
            if e["data"]["stage"] == "start":
                start = e["at"]
            elif start is not None:
                spans.setdefault(human, []).append((start, e["at"])); start = None
        if e["type"] == "UtteranceFinal":
            u = e["data"]["utterance"]
            if u["speaker_id"] != human:
                tail = bot_tail_ms if u.get("interrupted") else 0
                spans.setdefault(u["speaker_id"], []).append((u["started_at"], e["at"] + tail))
    (run / "audio").mkdir(parents=True, exist_ok=True)
    for pid, sp in spans.items():
        write_wav(run / "audio" / f"{pid}.wav", end, sp)


@pytest.fixture(scope="module")
def fake_run(tmp_path_factory):
    run = tmp_path_factory.mktemp("run_ok")
    d = SimDriver(scenario="sim_v1", principal_delay_ms=20_000,
                  principal_answers={"principal-growth": "示例：本月推广预算 3000 元（虚构）。"})
    asyncio.run(run_lines(d, ROOT / "runs" / "script" / "human_lines.json"))
    export(d.orch.log, run)
    events = acc.load_events(run)
    synth_audio(run, events)
    return run, events


def by_no(checks):
    return {c.no: c for c in checks}


def test_all_six_pass_on_fake_run(fake_run):
    run, _ = fake_run
    checks = acc.run_checks(run)
    assert [c.ok for c in checks] == [True] * 6, "\n".join(c.line() for c in checks)
    c3 = by_no(checks)[3]
    assert c3.metrics["log_ms"] is not None and c3.metrics["audio_ms"] is not None
    assert abs(c3.metrics["log_ms"] - c3.metrics["audio_ms"]) <= 100   # 两种方法大致一致


def test_human_lines_script_shape():
    j = json.loads((ROOT / "runs" / "script" / "human_lines.json").read_text("utf-8"))
    lines = j["lines"]
    assert len(lines) == 4
    assert sum(1 for l in lines if l.get("barge_in")) == 1
    assert any(l["kind"] == "ask_principal" for l in lines)
    assert [l["t_ms"] for l in lines] == sorted(l["t_ms"] for l in lines)
    assert max(l["t_ms"] for l in lines) < j["meeting_max_ms"] <= 300_000


def test_audio_tail_fails_criterion_3(fake_run, tmp_path):
    """日志说 250ms 停了，但音轨还响了 3 秒 → 音频法 FAIL。"""
    run, events = fake_run
    bad = tmp_path / "tail"
    bad.mkdir()
    for f in ["events.jsonl", "minutes.md"]:
        (bad / f).write_bytes((run / f).read_bytes())
    (bad / "actions").mkdir()
    for f in (run / "actions").glob("*.json"):
        (bad / "actions" / f.name).write_bytes(f.read_bytes())
    synth_audio(bad, events, bot_tail_ms=3000)
    c = by_no(acc.run_checks(bad))
    assert not c[3].ok and c[3].metrics["audio_ms"] > 1500
    assert c[1].ok and c[2].ok and c[4].ok and c[5].ok and c[6].ok


def _write(run: Path, events: list[dict]):
    run.mkdir(parents=True, exist_ok=True)
    (run / "events.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events), "utf-8")


def test_pending_answer_not_first_fails_4(fake_run, tmp_path):
    _, events = fake_run
    ev = json.loads(json.dumps(events))
    ans = next(e for e in ev if e["type"] == "PrincipalAnswered" and e["data"]["query"]["status"] == "answered")
    g = next(e for e in ev if e["seq"] > ans["seq"] and e["type"] == "FloorGranted")
    g["data"]["token"]["source"] = "handRaise"          # 被普通举手抢先
    _write(tmp_path / "r", ev)
    assert not by_no(acc.run_checks(tmp_path / "r"))[4].ok


def test_duplicate_voice_and_missing_outputs(fake_run, tmp_path):
    _, events = fake_run
    ev = json.loads(json.dumps(events))
    for e in ev:
        if e["type"] == "AgentSwitched" and e["data"]["to"] == "eng_bot":
            e["data"]["tts_voice"] = "voice_pm_female_warm"
        if e["type"] == "MeetingCreated":
            for p in e["data"]["payload"]["participants"]:
                if p["id"] == "eng_bot":
                    p["tts_voice"] = "voice_pm_female_warm"
    _write(tmp_path / "r", ev)
    c = by_no(acc.run_checks(tmp_path / "r"))
    assert not c[1].ok                 # 音色重复
    assert not c[5].ok and not c[6].ok  # 没纪要/行动项、没录音


def test_no_human_interrupt_fails_3_and_no_grants_fail_2(tmp_path):
    ev = [{"seq": 1, "at": 0, "type": "MeetingCreated", "meeting_id": "m", "data": {"payload": {
        "linked_human_id": "host", "moderator": {"participant_id": "mod"},
        "participants": [{"id": "host", "kind": "human"}, {"id": "mod", "kind": "bot", "tts_voice": "a"}]}}}]
    _write(tmp_path / "r", ev)
    c = by_no(acc.run_checks(tmp_path / "r"))
    assert [c[i].ok for i in range(1, 7)] == [False] * 6


def test_missing_events_file(tmp_path):
    checks = acc.run_checks(tmp_path)
    assert len(checks) == 6 and not any(c.ok for c in checks)
