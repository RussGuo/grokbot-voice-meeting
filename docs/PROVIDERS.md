# Providers

ASR, TTS, the LLM, and the realtime transport are selected by environment variables.
Meeting code calls the registry in `providers/`. It does not import a vendor SDK directly.

| Layer | Env | Default | Built-in options |
|---|---|---|---|
| LLM | `LLM_PROVIDER` | `auto` → DeepSeek if `DEEPSEEK_API_KEY` is set, else OpenAI-compatible if `LLM_API_KEY` / `OPENAI_API_KEY` is set, else `mock` | `deepseek`, `openai`, `mock` |
| ASR | `VOICE_ASR_PROVIDER` | `auto` → Volcengine, else Aliyun if `DASHSCOPE_API_KEY`, else local sherpa | `volc`, `aliyun`, `openai`, `local` |
| TTS | `VOICE_TTS_PROVIDER` | same cascade as ASR | `volc`, `aliyun`, `openai`, `local` |
| Transport | `MEETING_TRANSPORT` | `livekit` | `livekit` |

`auto` does not pick OpenAI speech. Set `VOICE_ASR_PROVIDER=openai` and `VOICE_TTS_PROVIDER=openai` to use Whisper and OpenAI TTS through `livekit-plugins-openai`. That plugin also honors `OPENAI_BASE_URL` / `LLM_BASE_URL`.

Speaker ids live in [`config/voices.json`](../config/voices.json). `meet` calls `assign_voices()` and writes the chosen id into the scene. Override the file with `VOICE_CATALOG`.

## OpenAI-compatible LLM

DeepSeek is one OpenAI-compatible endpoint. Any other is the same adapter:

```bash
LLM_PROVIDER=openai
LLM_BASE_URL=https://api.example.com/v1
LLM_API_KEY=...
LLM_MODEL=...
```

`orchestrator.llm.DeepSeekChat` (also exported as `OpenAICompatChat`) posts to `base_url`. The DeepSeek `thinking` body is sent only when the provider name is `deepseek`.

## Write an adapter

1. Implement the shape in `providers/base.py`.
   - Chat: `complete(messages, max_tokens=...)` and `async astream_text(messages, max_tokens=...)`.
   - Speech: a factory that returns a `livekit.agents` `stt.STT` or `tts.TTS`.
   - Transport: `ensure_room`, `dispatch`, `access_token`, `join_urls`.
2. Register it:

```python
from providers.registry import registry

def build():
    return MyTransport()

registry.add("transport", "my-transport", build)
```

Import that module from `providers/registry.py` inside `_register_builtins()` (or from your own entrypoint before `meet` runs) so the name is visible to `MEETING_TRANSPORT=my-transport`.

3. For speech, extend `voice/voice_layer/asr/__init__.py` or `tts/__init__.py` with a branch on the provider name, the way `openai` is branched. Keep credential checks in `voice/voice_layer/config.py:_resolve` so a missing key falls back to `local` instead of crashing a meeting.
4. Add the speaker id fields you need to `BotVoice` and to `config/voices.json`. Do not hardcode ids in Python.
5. Cover the factory with a test that does not call the network. `providers/tests/test_registry.py` is the pattern.

Aliyun Paraformer / CosyVoice modules are stubs. They show the file layout; they raise `NotImplementedError` until someone finishes them.

## Local speech

`pip install -e ".[local]"` adds sherpa-onnx. Download the models listed in `voice/README.md` into `voice/models/` (or `VOICE_MODELS_DIR`). With no cloud keys, `auto` already selects `local`.
