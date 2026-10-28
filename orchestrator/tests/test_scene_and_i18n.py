from orchestrator.fake_llm import FakeLLM
from orchestrator.i18n import normalize
from orchestrator.models import ChatCtxRecipe
from orchestrator.scene import build_scene, scene_to_meeting


def test_scene_moderator_is_generic_not_chief_of_staff():
    scene = build_scene(
        who=["pm", "eng"], topic="Ship the plugin", agenda=["scope", "risk"],
        briefs={}, voices={"moderator": "zh_male_m191_uranus_bigtts", "pm": "zh_female_vv_uranus_bigtts",
                           "eng": "zh_male_huolixiaoge_uranus_bigtts"},
        registry={"pm": {"display_name": "PM", "aliases": ["product"]},
                  "eng": {"display_name": "Eng"}},
        language="en", human_name="Ada",
    )
    assert scene["moderator"]["id"] == "moderator"
    assert scene["moderator"]["mode"] == "neutral"
    assert "chief_of_staff" not in {p["id"] for p in scene["participants"]}
    meeting, briefs = scene_to_meeting(scene)
    assert meeting.language == "en"
    assert meeting.moderator.participant_id == "moderator"
    assert meeting.participant(meeting.voice.linked_human_id).display_name == "Ada"
    assert "moderator" in briefs
    out = FakeLLM(meeting, briefs)("moderator", ChatCtxRecipe(
        participant_id="moderator", system="", brief=None, topic_snapshots={}, shared_minutes=[],
        turn_meta={"intent": "open", "agenda_title": "scope"}))
    assert "Welcome" in out["text"] or "Today's topic" in out["text"]
    assert "Chief of Staff" not in out["text"]


def test_language_normalize():
    assert normalize("zh-CN") == "zh" and normalize("en-US") == "en" and normalize(None) == "zh"
