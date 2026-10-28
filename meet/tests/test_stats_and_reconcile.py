import http.server
import json
import shutil
import threading

import pytest

from meet.cli import main as meet_main
from meet.followup import apply_reply, parse_reply
from meet.runs import find_run_by_room, resolve_run
from meet.stats import aggregate, collect_run, percentile

VOICE_STATS = {
    "switch": [{"kind": "switch", "pid": "a", "ms": 40.0}, {"kind": "switch", "pid": "b", "ms": 60.0}],
    "llm": [
        {"pid": "a", "token_id": "t1", "llm": "x", "first_seg_ms": 500, "ttft_ms": 300, "total_ms": 900, "ok": True},
        {"pid": "b", "token_id": "t2", "llm": "x", "first_seg_ms": None, "ttft_ms": None, "total_ms": 5000,
         "ok": False, "error": "TimeoutError: deadline"},
        {"pid": "a", "token_id": "t3", "llm": "x", "first_seg_ms": 700, "ttft_ms": 500, "total_ms": 1200, "ok": True},
    ],
    "marks": [],
    "metrics": {"switch_ms": [], "bargein_stop_ms": []},
}


@pytest.fixture(scope="session")
def demo_run(tmp_path_factory):
    d = tmp_path_factory.mktemp("demo") / "demo"
    assert meet_main(["demo", "--run-dir", str(d)]) == 0
    return d


@pytest.fixture
def run(demo_run, tmp_path):
    d = tmp_path / "20260101-000000"
    shutil.copytree(demo_run, d)
    (d / "voice_stats.json").write_text(json.dumps(VOICE_STATS), encoding="utf-8")
    return d


def write_events(d, events):
    d.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"seq": i, "at": at, "meeting_id": "m", "type": t, "data": data})
             for i, (at, t, data) in enumerate(events, 1)]
    (d / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_percentile_linear_interpolation():
    xs = [10, 20, 30, 40, 50]
    assert percentile(xs, 50) == 30
    assert percentile(xs, 95) == pytest.approx(48)
    assert percentile(xs, 100) == 50
    assert percentile([7], 95) == 7
    assert percentile([], 50) is None


def test_latency_definitions_from_events(tmp_path):
    d = tmp_path / "r"
    write_events(d, [
        (0, "BotFirstAudio", {"speakerId": "a"}),            # before any human turn: ignored
        (1000, "HumanVad", {"stage": "start"}),
        (1300, "Interrupted", {"speakerId": "a", "by": "h"}),  # stop 300
        (3000, "HumanVad", {"stage": "end"}),
        (3900, "BotFirstAudio", {"speakerId": "b"}),            # response 900
        (5000, "HumanVad", {"stage": "end"}),
        (5100, "HumanVad", {"stage": "start"}),                 # human resumes: turn unanswered
        (6000, "HumanVad", {"stage": "end"}),
        (7500, "BotFirstAudio", {"speakerId": "b"}),            # response 1500
        (8000, "Ducked", {}), (8100, "DuckRestored", {}), (8200, "OutputDropped", {}),
        (9000, "AgentSwitched", {"switchMs": 25.0, "to": "b"}),
    ])
    r = collect_run(d)
    assert r["samples"]["response"] == [900, 1500]
    assert r["samples"]["bargein_stop"] == [300]
    assert r["samples"]["switch"] == [25.0]
    assert r["counts"]["unanswered_turns"] == 1
    assert (r["counts"]["ducks"], r["counts"]["restores"], r["counts"]["dropped_outputs"]) == (1, 1, 1)


def test_stats_on_demo_run_with_voice_stats(run):
    rep = aggregate([run])
    lat = rep["latency_ms"]
    assert lat["switch"]["count"] == 2 and lat["switch"]["p50"] == 50
    assert lat["llm_ttft"]["count"] == 2 and lat["llm_ttft"]["max"] == 500
    assert lat["llm_first_seg"]["p50"] == 600
    assert lat["response"]["count"] > 0 and lat["bargein_stop"]["count"] > 0
    assert rep["counts"]["interruptions"] == 6
    assert rep["counts"]["llm_fallbacks"] == 1
    assert rep["llm_errors"] == {"TimeoutError": 1}


def test_missing_files_degrade_to_na(tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    assert meet_main(["stats", str(tmp_path / "empty")]) == 0
    out = capsys.readouterr().out
    assert "n/a" in out and "no events.jsonl" in out


def test_multiple_runs_aggregate(run, tmp_path):
    other = tmp_path / "20260102-000000"
    shutil.copytree(run, other)
    one, two = aggregate([run]), aggregate([run, other])
    assert two["latency_ms"]["response"]["count"] == 2 * one["latency_ms"]["response"]["count"]
    assert two["counts"]["interruptions"] == 12


def test_json_output_and_baseline_regression_exit_code(run, tmp_path, capsys):
    assert meet_main(["stats", str(run), "--json"]) == 0
    base = json.loads(capsys.readouterr().out)
    base_file = tmp_path / "base.json"
    base_file.write_text(json.dumps(base), encoding="utf-8")

    assert meet_main(["stats", str(run), "--baseline", str(base_file), "--fail-on-regress", "20"]) == 0
    assert "no regressions" in capsys.readouterr().out

    # Baseline was 30% faster: current P95 is ~43% worse than it.
    for k in base["latency_ms"].values():
        for stat in ("p50", "p95", "max"):
            if k[stat] is not None:
                k[stat] = round(k[stat] * 0.7, 1)
    base_file.write_text(json.dumps(base), encoding="utf-8")
    assert meet_main(["stats", str(run), "--baseline", str(base_file), "--fail-on-regress", "20"]) == 1
    assert "REGRESSIONS" in capsys.readouterr().out
    assert meet_main(["stats", str(run), "--baseline", str(base_file), "--fail-on-regress", "60"]) == 0
    assert meet_main(["stats", str(run), "--baseline", str(run)]) == 0   # run dir as baseline, no threshold
    assert meet_main(["stats", str(run), "--fail-on-regress", "5"]) == 2


def test_room_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_HOME", str(tmp_path))
    runs = tmp_path / "runs"
    for name in ("myroom-20260101-090000", "myroom-20260102-080000", "myroom-extra-20260109-000000", "other-20260105-000000"):
        (runs / name).mkdir(parents=True)
    assert find_run_by_room("myroom").name == "myroom-20260102-080000"
    legacy = runs / "20250101-000000"
    legacy.mkdir()
    (legacy / "clock.json").write_text(json.dumps({"t0_epoch_ms": 1, "room": "legacy-room"}), encoding="utf-8")
    assert find_run_by_room("legacy-room") == legacy
    assert find_run_by_room("nope") is None
    assert resolve_run("other-20260105-000000") == runs / "other-20260105-000000"


def test_stats_and_followup_accept_room(run, tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    (home / "runs").mkdir(parents=True)
    shutil.copytree(run, home / "runs" / "standup-20260301-101500")
    monkeypatch.setenv("MEETING_HOME", str(home))
    assert meet_main(["stats", "--room", "standup"]) == 0
    assert "standup-20260301-101500" in capsys.readouterr().out
    assert meet_main(["followup", "--room", "standup"]) == 0
    assert "eng_bot" in capsys.readouterr().out
    assert meet_main(["followup", "--room", "missing"]) == 2


def test_parse_reply_positional_and_by_id():
    ids = ["c1", "c2", "c3"]
    items, problems = parse_reply("confirm\n- correct: ship Friday\n3. retract\n", ids)
    assert problems == []
    assert items == [
        {"commitment_id": "c1", "verdict": "confirm"},
        {"commitment_id": "c2", "verdict": "correct", "correction": "ship Friday"},
        {"commitment_id": "c3", "verdict": "retract"},
    ]
    items, problems = parse_reply("c2: retract\nc1: correct: later\n", ids)
    assert items == [{"commitment_id": "c2", "verdict": "retract"},
                     {"commitment_id": "c1", "verdict": "correct", "correction": "later"}]
    _, problems = parse_reply("maybe\ncorrect:\nconfirm\nconfirm\n", ["only"])
    assert len(problems) == 4


def _events(d):
    return [json.loads(line) for line in (d / "events.jsonl").read_text("utf-8").splitlines() if line.strip()]


def test_apply_reply_appends_event_and_rerenders(run):
    before = _events(run)
    res = apply_reply(run, "pm_bot", [{"commitment_id": "cmt-2", "verdict": "retract"}])
    assert len(res["applied"]) == 1
    after = _events(run)
    assert len(after) == len(before) + 1
    ev = after[-1]
    assert ev["type"] == "ReconcileResult" and ev["seq"] == before[-1]["seq"] + 1
    assert ev["at"] >= before[-1]["at"] and ev["meeting_id"] == before[-1]["meeting_id"]
    assert ev["data"] == {"participantId": "pm_bot", "items": [{"commitment_id": "cmt-2", "verdict": "retract"}]}
    assert "整理需求文档（retracted）" in (run / "minutes.md").read_text("utf-8")
    assert json.loads((run / "actions" / "pm_bot.json").read_text("utf-8"))[0]["status"] == "retracted"
    assert json.loads((run / "reconcile" / "pm_bot.json").read_text("utf-8"))["items"][0]["verdict"] == "retract"


def test_apply_confirmed_and_corrected_statuses(run):
    apply_reply(run, "eng_bot", [{"commitment_id": "cmt-1", "verdict": "correct", "correction": "estimate by Friday"}])
    apply_reply(run, "pm_bot", [{"commitment_id": "cmt-2", "verdict": "confirm"}])
    md = (run / "minutes.md").read_text("utf-8")
    assert "出技术评估（corrected）" in md and "整理需求文档（confirmed）" in md
    eng = json.loads((run / "actions" / "eng_bot.json").read_text("utf-8"))[0]
    assert eng["status"] == "corrected" and eng["correction"] == "estimate by Friday"


def test_apply_is_idempotent_and_rejects_bad_ids(run, tmp_path, capsys):
    reply = tmp_path / "reply.txt"
    reply.write_text("retract\n", encoding="utf-8")
    n0 = len(_events(run))
    assert meet_main(["followup", str(run), "--apply", "pm_bot", str(reply)]) == 0
    md = (run / "minutes.md").read_text("utf-8")
    assert meet_main(["followup", str(run), "--apply", "pm_bot", str(reply)]) == 0
    assert "unchanged 1" in capsys.readouterr().out
    assert len(_events(run)) == n0 + 1
    assert (run / "minutes.md").read_text("utf-8") == md

    res = apply_reply(run, "pm_bot", [{"commitment_id": "cmt-1", "verdict": "confirm"},   # eng_bot's
                                      {"commitment_id": "nope", "verdict": "confirm"},
                                      {"commitment_id": "cmt-2", "verdict": "maybe"}])
    assert len(res["skipped"]) == 3 and res["applied"] == []
    assert len(_events(run)) == n0 + 1
    with pytest.raises(ValueError):
        apply_reply(run, "ghost", [])
    # A changed answer is a new verdict.
    apply_reply(run, "pm_bot", [{"commitment_id": "cmt-2", "verdict": "confirm"}])
    assert len(_events(run)) == n0 + 2
    assert "整理需求文档（confirmed）" in (run / "minutes.md").read_text("utf-8")


class _Stub(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append(body)
        if body["owner"] == "pm_bot":
            reply = {"items": [{"commitment_id": body["commitments"][0]["id"], "verdict": "correct",
                                "correction": "after the review"}, {"commitment_id": "zzz", "verdict": "confirm"}]}
        else:
            reply = {}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def test_push_to_stub_webhook_applies_reply(run, capsys):
    _Stub.seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/hook"
        assert meet_main(["followup", str(run), "--push", url]) == 0
    finally:
        srv.shutdown()
        srv.server_close()
    out = capsys.readouterr().out
    assert {b["owner"] for b in _Stub.seen} == {"eng_bot", "pm_bot"}
    body = next(b for b in _Stub.seen if b["owner"] == "pm_bot")
    assert body["run"] == run.name and body["commitments"][0]["id"] == "cmt-2" and "pm_bot" in body["prompt"]
    assert "skipped 1" in out and "zzz" in out
    assert "整理需求文档（corrected）" in (run / "minutes.md").read_text("utf-8")


def test_push_failure_returns_nonzero(run, capsys):
    assert meet_main(["followup", str(run), "--push", "http://127.0.0.1:9/hook"]) == 1
    assert "failed" in capsys.readouterr().out
    assert meet_main(["followup", str(run), "--push", "file:///etc/passwd"]) == 1
