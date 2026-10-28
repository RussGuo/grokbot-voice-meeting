from orchestrator.config import llm_config
from providers.registry import registry, selected_names


def test_builtins_registered():
    assert {"deepseek", "openai", "mock"} <= set(registry.names("llm"))
    assert {"volc", "aliyun", "openai", "local"} <= set(registry.names("asr"))
    assert "livekit" in registry.names("transport")


def test_auto_llm_is_mock_without_keys():
    cfg = llm_config({})
    assert cfg.provider == "mock"
    assert cfg.base_url == "https://api.deepseek.com"


def test_openai_compatible_selection():
    cfg = llm_config({
        "LLM_PROVIDER": "openai",
        "LLM_API_KEY": "sk-test",
        "LLM_BASE_URL": "https://example.invalid/v1",
        "LLM_MODEL": "any-model",
    })
    assert cfg.provider == "openai"
    assert cfg.base_url == "https://example.invalid/v1"
    assert cfg.model == "any-model"
    assert cfg.api_key == "sk-test"


def test_selected_names_follow_env(monkeypatch):
    monkeypatch.setenv("VOICE_ASR_PROVIDER", "local")
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "local")
    names = selected_names()
    assert names["llm"] == "mock"
    assert names["asr"] == "local"
    assert names["tts"] == "local"
    assert names["transport"] == "livekit"


def test_unknown_transport():
    try:
        registry.get("transport", "nope")
        assert False
    except KeyError as e:
        assert "nope" in str(e)
