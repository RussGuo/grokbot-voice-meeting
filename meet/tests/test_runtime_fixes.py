"""Offline tests for the worker-side fixes: principal default, run ids, join wait, task retention."""
import asyncio
import gc
import json
import time

import pytest

from meet.runtime import DEFAULT_JOIN_WAIT_S, join_wait_s, make_run_id, runs_root
from orchestrator.models import PrincipalQuery
from orchestrator.principal import (DeferPrincipal, FilePrincipal, MockPrincipal, WebhookPrincipal,
                                    principal_from_env)


class FakeOrch:
    def __init__(self):
        self.timers = []
        self.inbox = None

    def schedule_timer(self, delay, ev):
        self.timers.append((delay, ev))


def _q(i="pq-1", ttl=5_000):
    return PrincipalQuery(id=i, asked_by="pm", principal_bot_id="pm", question="预算呢？",
                          agenda_item_id="ag-1", context_event_seq=1, asked_at=0, ttl_ms=ttl)


# —— principal default selection ——

def test_default_is_mock_for_sample_scenarios():
    assert isinstance(principal_from_env(FakeOrch()), MockPrincipal)


def test_scene_default_is_defer():
    assert isinstance(principal_from_env(FakeOrch(), default="defer"), DeferPrincipal)


def test_explicit_env_beats_default(monkeypatch, tmp_path):
    monkeypatch.setenv("MEETING_PRINCIPAL", "mock")
    assert isinstance(principal_from_env(FakeOrch(), default="defer"), MockPrincipal)
    monkeypatch.setenv("MEETING_PRINCIPAL", "file")
    monkeypatch.setenv("MEETING_PRINCIPAL_DIR", str(tmp_path))
    assert isinstance(principal_from_env(FakeOrch(), default="defer"), FilePrincipal)
    monkeypatch.setenv("MEETING_PRINCIPAL", "DEFER")
    assert isinstance(principal_from_env(FakeOrch(), default="mock"), DeferPrincipal)


def test_webhook_requires_url_and_unknown_kind_is_rejected(monkeypatch):
    monkeypatch.setenv("MEETING_PRINCIPAL", "webhook")
    with pytest.raises(RuntimeError):
        principal_from_env(FakeOrch())
    monkeypatch.setenv("MEETING_PRINCIPAL_WEBHOOK", "https://example.invalid/p")
    assert isinstance(principal_from_env(FakeOrch()), WebhookPrincipal)
    monkeypatch.setenv("MEETING_PRINCIPAL", "mok")
    with pytest.raises(ValueError):
        principal_from_env(FakeOrch())


def test_defer_principal_records_and_never_answers():
    orch = FakeOrch()
    p = principal_from_env(orch, default="defer")
    asyncio.run(p.ask(_q()))
    assert [q.id for q in p.asked] == ["pq-1"]
    assert orch.timers == []          # nothing scheduled: no fabricated answer, the TTL timer expires it


# —— task retention ——

def test_file_principal_keeps_poll_tasks_alive_and_releases_them(tmp_path):
    delivered = []
    client = FilePrincipal(lambda d, ev: delivered.append(ev), tmp_path, poll_s=0.02)

    async def go():
        await client.ask(_q("pq-a"))
        gc.collect()
        assert len(client._tasks) == 1            # strongly referenced while polling
        (tmp_path / "inbox").mkdir()
        (tmp_path / "inbox" / "pq-a.json").write_text(json.dumps({"text": "好。"}), encoding="utf-8")
        for _ in range(100):
            if not client._tasks:
                break
            await asyncio.sleep(0.02)
        assert not client._tasks                  # discarded on completion
        await client.ask(_q("pq-b"))
        assert len(client._tasks) == 1
        await client.aclose()                     # cancel path
        assert not client._tasks

    asyncio.run(go())
    assert [e["query_id"] for e in delivered] == ["pq-a"]


def test_webhook_principal_aclose_cancels_file_fallback(tmp_path, monkeypatch):
    client = WebhookPrincipal(lambda d, ev: None, "https://example.invalid/p", directory=tmp_path)
    client.files.poll_s = 0.02

    async def go():
        await client.files.ask(_q())
        assert client.files._tasks
        await client.aclose()
        assert not client.files._tasks

    asyncio.run(go())


# —— run id / runs dir / join wait ——

def test_run_id_is_room_plus_timestamp():
    t = time.mktime((2026, 3, 4, 5, 6, 7, 0, 0, -1))
    assert make_run_id("m-budget-ab12", now=t, env={}) == "m-budget-ab12-20260304-050607"


def test_run_id_env_override_is_verbatim():
    assert make_run_id("room", env={"MEETING_RUN_ID": "e2e-20260101-000000"}) == "e2e-20260101-000000"
    assert make_run_id("room", now=0, env={"MEETING_RUN_ID": "  "}).startswith("room-")


def test_run_id_sanitizes_room_and_differs_by_room():
    t = 1_800_000_000
    assert make_run_id("a/b c", now=t, env={}).startswith("a-b-c-")
    assert make_run_id("", now=t, env={}).startswith("room-")
    assert make_run_id("r1", now=t, env={}) != make_run_id("r2", now=t, env={})


def test_runs_root_follows_meeting_home_then_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("MEETING_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    assert runs_root().resolve() == (tmp_path / "runs").resolve()
    monkeypatch.setenv("MEETING_HOME", str(tmp_path / "home"))
    assert runs_root() == tmp_path / "home" / "runs"


def test_join_wait_default_override_and_bad_values():
    assert join_wait_s({}) == DEFAULT_JOIN_WAIT_S == 1800
    assert join_wait_s({"MEETING_JOIN_WAIT_S": "90"}) == 90
    assert join_wait_s({"MEETING_JOIN_WAIT_S": "abc"}) == 1800
    assert join_wait_s({"MEETING_JOIN_WAIT_S": "-5"}) == 1800


def test_worker_and_main_shim_import_cleanly():
    pytest.importorskip("livekit.agents")
    import main
    from meet import worker
    assert main.server is worker.server and main.entrypoint is worker.entrypoint
