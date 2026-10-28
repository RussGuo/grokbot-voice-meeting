"""ChatCtxRecipe 的段落标题和 brief 压缩的前缀随会议语言切换；缺省仍是 zh。"""
import re

from orchestrator.brief_compact import compact_brief, compact_snapshot
from orchestrator.models import ChatCtxRecipe

CJK = re.compile(r"[一-鿿【】：]")


def _recipe(**kw) -> ChatCtxRecipe:
    base = dict(participant_id="pm", system="sys", brief=None, topic_snapshots={}, shared_minutes=["a said x"],
                turn_instruction="Answer briefly.", brief_text="b", snapshots_text={"eng": "- k: v"})
    base.update(kw)
    return ChatCtxRecipe(**base)


def test_default_language_is_zh_labels():
    msgs = _recipe().to_messages()
    text = "\n".join(m["content"] for m in msgs)
    assert "【你的会前 brief】" in text and "【最近会议纪要】" in text and "【本轮任务】" in text
    assert "eng：" in text


def test_english_labels_have_no_chinese():
    msgs = _recipe(language="en").to_messages()
    text = "\n".join(m["content"] for m in msgs)
    assert "[Your pre-meeting brief]" in text and "[Recent meeting minutes]" in text
    assert "eng:" in text
    assert not CJK.search(text)


def test_english_full_labels_and_unknown_language_falls_back():
    r = _recipe(language="en", brief={"x": 1}, topic_snapshots={"eng": {"y": 2}})
    text = "\n".join(m["content"] for m in r.to_messages(compact=False))
    assert not CJK.search(text) and "visible to everyone" in text
    assert "【你的会前 brief】" in "\n".join(m["content"] for m in _recipe(language="fr").to_messages())


def test_language_round_trips_and_old_dicts_load():
    d = _recipe(language="en").to_dict()
    assert ChatCtxRecipe.from_dict(d).language == "en"
    d.pop("language")
    assert ChatCtxRecipe.from_dict(d).language == "zh"


BRIEF = {
    "positions": [{"agenda_item_id": "a1", "stance": "ship"}],
    "memory_snapshot": {"facts": [{"key": "users", "value": "120"}], "red_lines": ["no refunds"],
                        "open_todos": [{"title": "fix login"}], "recent_decisions": ["use postgres"]},
    "mandate": {"can_commit": ["send notes"], "must_ask": []},
    "questions_for_others": [{"to": "eng", "q": "when?"}],
}


def test_compact_brief_english_has_no_chinese_and_zh_unchanged():
    en, _, _ = compact_brief(BRIEF, language="en")
    assert not CJK.search(en) and "[Stance]" in en and "users: 120" in en and "Ask eng: when?" in en
    zh, _, _ = compact_brief(BRIEF)
    assert "【立场】" in zh and "users：120" in zh and "必须先问本体：无" in zh
    assert compact_brief(BRIEF, language="zh") == compact_brief(BRIEF)


def test_compact_snapshot_language():
    snap = BRIEF["memory_snapshot"]
    assert compact_snapshot(snap, language="en")[0] == "- users: 120"
    assert compact_snapshot(snap)[0] == "- users：120"
