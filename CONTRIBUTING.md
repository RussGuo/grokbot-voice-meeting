# Contributing

Issues and pull requests are welcome. Please:

- Run `pytest` and `pytest voice/tests` before opening a PR. Both must pass with no API keys and no network.
- Run `ruff check .` (CI does). The config ignores the one-line style rules E701/E702/E731/E741 on purpose; see `pyproject.toml`.
- Do not commit `.env`, tokens, join links, or real briefs. `bots.yaml` is gitignored on purpose.
- Keep vendor SDKs behind `providers/`. A new ASR, TTS, LLM, or transport is a registry entry plus a test that does not call the network. See [docs/PROVIDERS.md](docs/PROVIDERS.md).
- Speaker ids belong in `config/voices.json`.
- If you change turn-taking, add or adjust a test under `orchestrator/tests/`. The simulated meeting (`meet demo`) should still finish offline.
- Match the language of the file you are editing. User-facing docs exist in English (`README.md`) and Chinese (`README.zh-CN.md`); update both when the behavior changes.

The sample scenarios under `orchestrator/bots/` are fixtures. New product behavior belongs on the `meet` scene path, whose default host is the neutral `moderator`.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
