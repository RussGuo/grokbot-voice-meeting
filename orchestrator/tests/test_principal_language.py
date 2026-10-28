"""MockPrincipal's default reply follows the meeting language; principal_from_env reads orch.m.language."""
import asyncio
import re
from types import SimpleNamespace

from orchestrator.models import PrincipalQuery
from orchestrator.principal import MockPrincipal, principal_from_env

CJK = re.compile(r"[一-鿿（）「」]")


class FakeOrch:
    def __init__(self, language=None):
        self.timers = []
        if language is not None:
            self.m = SimpleNamespace(language=language)

    def schedule_timer(self, delay, ev):
        self.timers.append((delay, ev))


def _q():
    return PrincipalQuery(id="pq-1", asked_by="pm", principal_bot_id="pm", question="What is the budget?",
                          agenda_item_id="ag-1", context_event_seq=1, asked_at=0, ttl_ms=5_000)


def _answer(orch) -> str:
    p = principal_from_env(orch)
    assert isinstance(p, MockPrincipal)
    asyncio.run(p.ask(_q()))
    return orch.timers[0][1]["text"]


def test_english_meeting_gets_english_placeholder():
    text = _answer(FakeOrch("en"))
    assert "What is the budget?" in text and not CJK.search(text)


def test_chinese_and_missing_language_stay_chinese():
    assert "模拟本体答复" in _answer(FakeOrch("zh"))
    assert "模拟本体答复" in _answer(FakeOrch())          # no orch.m at all
