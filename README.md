# grokbot-voice-meeting

> 🚧 **Work in progress** — this repo is an early prototype snapshot and is being polished into a product. Docs, setup and APIs will change.

Multi-bot voice meetings for Grok Bot: your agents meet with you by voice, with a host (moderator), agenda, turn-taking / hand-raising, and instant human barge-in. After the meeting, minutes and per-bot action items are generated.

**Stack (current prototype):** LiveKit (rooms + Agents), Volcengine Doubao streaming ASR/TTS (local sherpa-onnx fallback), DeepSeek (OpenAI-compatible) LLM.

## Layout
- `orchestrator/` — meeting state machine, floor/turn scheduler, minutes, example scenarios (`orchestrator/bots/`)
- `voice/` — voice layer (ASR/TTS adapters, ducking, barge-in, per-bot identities)
- `main.py` — LiveKit agent worker entrypoint
- `scripts/` — dispatch, meeting link generation (`mklink.py`), acceptance checks
- `sim/` — simulated human participant + recorder for end-to-end tests
- `web/` — meeting UI + token server
- `docs/DEV_NOTES.md` — original prototype notes (Chinese)

## Quick start (dev)
```bash
cp .env.example .env   # fill in your own keys
pip install -r requirements.txt -r voice/requirements.txt
python -m pytest orchestrator/tests
(cd voice && python -m pytest)
```

## License
MIT — see [LICENSE](LICENSE).
