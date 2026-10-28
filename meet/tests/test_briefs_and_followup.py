import json

import pytest

from meet.briefs import guard_brief, heuristic_brief, list_unknowns, summarize
from meet.followup import build_followups
from meet.registry import load_registry
from orchestrator.i18n import is_unknown


def test_heuristic_marks_unknown_instead_of_inventing(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_BRIEF_CACHE", str(tmp_path))
    brief = heuristic_brief(bot_id="pm", context="", topic="Roadmap", agenda=[{"id": "ag-1", "title": "Scope"}], lang="en")
    assert is_unknown(brief["memory_snapshot"]["facts"][0]["value"])
    assert is_unknown(brief["positions"][0]["stance"])
    assert brief["mandate"]["can_commit"] == []
    assert "pm" in list_unknowns(brief) or list_unknowns(brief)


def test_labeled_commitments_are_kept_and_numbers_are_not_invented(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_BRIEF_CACHE", str(tmp_path))
    context = "Can commit:\n- write the spec\n\nThe beta has 12 teams."
    brief = heuristic_brief(
        bot_id="pm", context=context, topic="Roadmap",
        agenda=[{"id": "ag-1", "title": "Scope"}], lang="en")
    assert brief["mandate"]["can_commit"] == ["write the spec"]
    assert "12" in brief["memory_snapshot"]["facts"][0]["value"]


def test_guard_drops_numbers_missing_from_context():
    brief = {"memory_snapshot": {"facts": [
        {"key": "teams", "value": "There are 99 teams.", "source": "provided context"},
        {"key": "note", "value": "beta is open", "source": "provided context"},
    ]}}
    guarded = guard_brief(brief, "beta is open", "en")
    assert guarded["memory_snapshot"]["facts"][0]["value"].lower().startswith("unknown")
    assert guarded["memory_snapshot"]["facts"][1]["value"] == "beta is open"


class _Chat:
    def complete(self, messages, **_):
        return json.dumps({
            "positions": [{"agenda_item_id": "ag-1", "stance": "ship it", "evidence": []}],
            "memory_snapshot": {"topic": "T", "facts": [
                {"key": "n", "value": "99 users", "source": "provided context", "as_of": "unspecified"},
            ]},
            "mandate": {"can_commit": [], "must_ask": []},
        })


def test_summarize_caches_and_guards(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_BRIEF_CACHE", str(tmp_path))
    agenda = [{"id": "ag-1", "title": "Scope"}]
    first = summarize(bot_id="pm", context="no numbers here", topic="T", agenda=agenda, lang="en", chat=_Chat(), model="fake")
    assert first["memory_snapshot"]["facts"][0]["value"].lower().startswith("unknown")
    assert len(list(tmp_path.glob("*.json"))) == 1
    second = summarize(bot_id="pm", context="no numbers here", topic="T", agenda=agenda, lang="en", chat=_Chat(), model="fake")
    assert second == first


def test_registry_roundtrip(tmp_path):
    path = tmp_path / "bots.yaml"
    path.write_text("bots:\n  pm:\n    display_name: Product\n    aliases: [PM]\n", encoding="utf-8")
    bots = load_registry(path)
    assert bots["pm"]["display_name"] == "Product"
    assert bots["pm"]["aliases"] == ["PM"]


def test_followup_prompts(tmp_path):
    (tmp_path / "minutes.md").write_text("# Minutes\n- decided\n", encoding="utf-8")
    (tmp_path / "actions").mkdir()
    (tmp_path / "actions" / "eng.json").write_text(
        json.dumps([{"title": "write the spec", "status": "binding", "owner_id": "eng"}]), encoding="utf-8")
    prompts = build_followups(tmp_path)
    assert "write the spec" in prompts["eng"]
    assert (tmp_path / "followups" / "eng.md").is_file()
    assert "confirm" in prompts["eng"].lower()


def test_missing_registry_file():
    with pytest.raises(FileNotFoundError):
        load_registry("/no/such/bots.yaml")
