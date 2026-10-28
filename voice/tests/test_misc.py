import json

from voice_layer.config import VoiceConfig, load_config, _resolve
from voice_layer.events import make_event, VOICE_EVENT_TYPES
from voice_layer.volc import protocol as P
from voice_layer.volc.auth import auth_headers, redact, VolcAuthMissing


def test_provider_fallback(monkeypatch):
    for k in ("VOLC_API_KEY", "VOLC_APP_ID", "VOLC_ACCESS_TOKEN", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert _resolve("auto") == "local"
    assert _resolve("volc") == "local"          # 缺凭证优雅回退
    monkeypatch.setenv("VOLC_API_KEY", "x")
    assert _resolve("auto") == "volc"


def test_auth_prefers_api_key(monkeypatch):
    monkeypatch.setenv("VOLC_APP_ID", "a")
    monkeypatch.setenv("VOLC_ACCESS_TOKEN", "b")
    monkeypatch.delenv("VOLC_API_KEY", raising=False)
    assert set(auth_headers("asr")) == {"X-Api-App-Key", "X-Api-Access-Key"}
    assert set(auth_headers("tts")) == {"X-Api-App-Id", "X-Api-Access-Key"}
    monkeypatch.setenv("VOLC_API_KEY", "k")
    assert auth_headers("tts") == {"X-Api-Key": "k"}
    assert redact(auth_headers("tts")) == {"X-Api-Key": "***"}
    monkeypatch.delenv("VOLC_API_KEY")
    monkeypatch.delenv("VOLC_APP_ID")
    try:
        auth_headers("tts")
        assert False
    except VolcAuthMissing:
        pass


def test_tts_frame_roundtrip():
    b = P.tts_event(P.Ev.StartSession, {"a": 1}, "sid-1")
    f = P.parse(b)
    assert f.msg_type == P.FULL_CLIENT_REQUEST and f.event == P.Ev.StartSession
    assert f.session_id == "sid-1" and f.json() == {"a": 1}
    f2 = P.parse(P.tts_event(P.Ev.StartConnection, {}))
    assert f2.event == P.Ev.StartConnection and f2.session_id is None


def test_asr_frames():
    f = P.parse(P.asr_full_request({"x": 1}))
    assert f.msg_type == P.FULL_CLIENT_REQUEST and f.sequence == 1 and f.json() == {"x": 1}
    last = P.parse(P.asr_audio(b"\x00\x01" * 10, seq=7, last=True))
    assert last.flags == P.NEG_SEQ and last.sequence == -7 and last.payload == b"\x00\x01" * 10


def test_bot_voices_distinct_and_aliases(tmp_path):
    cfg = VoiceConfig()
    ids = {cfg.bot_voice(b).volc_speaker for b in ("moderator", "pm_bot", "eng_bot")}
    sids = {cfg.bot_voice(b).local_sid for b in ("moderator", "pm_bot", "eng_bot")}
    assert len(ids) == 3 and len(sids) == 3
    assert cfg.bot_voice("voice_eng_male_calm").bot_id == "eng_bot"
    p = tmp_path / "v.json"
    p.write_text(json.dumps({"bots": {"pm_bot": {"volc_speaker": "zh_female_vv_uranus_bigtts"}},
                             "interruption": {"min_duration": 0.3}}))
    c2 = load_config(p)
    assert c2.bot_voice("pm_bot").volc_speaker == "zh_female_vv_uranus_bigtts"
    assert c2.interruption.min_duration == 0.3


def test_event_shape():
    e = make_event("vad.start", "__HUMAN__")
    assert e["type"] in VOICE_EVENT_TYPES and e["participant_id"] == "__HUMAN__" and "at" in e


def test_implements_orchestrator_interface():
    import inspect
    from orchestrator.voice_iface import VoiceLayer
    from voice_layer.livekit_layer import LiveKitVoiceLayer
    assert issubclass(LiveKitVoiceLayer, VoiceLayer)
    abstract = {n for n, v in inspect.getmembers(VoiceLayer) if getattr(v, "__isabstractmethod__", False)}
    assert not (abstract & set(LiveKitVoiceLayer.__abstractmethods__))


def test_volc_asr_hotwords_payload():
    import json
    from voice_layer.asr.volc import request_payload
    p = request_payload(600, ["对账", "多音轨"])
    ctx = json.loads(p["request"]["corpus"]["context"])
    assert [h["word"] for h in ctx["hotwords"]] == ["对账", "多音轨"]
    assert "corpus" not in request_payload(600)["request"]
