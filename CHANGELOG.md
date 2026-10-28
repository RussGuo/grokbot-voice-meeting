# Changelog

## Unreleased

Hardening pass: what the worker does when nobody shows up, where secrets travel, and who decides what a bot says.

- The worker lives in `meet/worker.py` (installed with the package); `main.py` is a shim. It waits `MEETING_JOIN_WAIT_S` (default 1800) for the human and ends the run as `NoShow` on timeout. `run_id` is `<room>-<YYYYmmdd-HHMMSS>` (`MEETING_RUN_ID` overrides). `runs/` resolves from `MEETING_HOME` or the current directory.
- `MEETING_PRINCIPAL=defer` records in-meeting principal questions for the minutes and never answers. It is now the default for meetings built by `meet`; sample scenarios and `meet demo` keep `mock`, whose placeholder reply follows the meeting language. An explicit `MEETING_PRINCIPAL` always wins.
- Join links carry `liveKitUrl` and `token` in the URL fragment; the page strips them from the address bar. Room metadata holds only `{"v":1,"room":{"topic","who"}}`. The full scene goes only in dispatch metadata and `links/<room>.scene.json`; `scripts/dispatch.py` sends that file by default (`--scene`, `--no-scene`). The `meet.livekit.io` fallback still sends the token to a third party. `web/token_server.py` lists replayable runs, accepts the access key as `Authorization: Bearer` (`?k=` still works, discouraged) and keeps it out of its access log.
- `livekit-client` in the join page is pinned to 2.22.4 instead of a floating `@2`.
- LLM planner (`orchestrator/planner.py`): the model chooses the content and intent through a one-line JSON header followed by the speech. Guards stay in code: `must_ask`, `can_commit`, verbatim relay, low confidence. Any LLM failure falls back to the FakeLLM lines.
- Agenda decisions get a fallback text at once and a background LLM summary (`DecisionSummarized`). Orchestrator and minutes strings for `zh` and `en` live in `orchestrator/i18n.py`. Prompt section labels (`ChatCtxRecipe.language`) and brief compaction follow the meeting language.
- Rule scoring understands English. `MEETING_SCORER=llm` adds an async LLM batch re-scorer.
- `meet stats` reports latency percentiles and reliability counts with `--baseline` and `--fail-on-regress`. `meet followup` takes `--room`, `--push` (or `MEETING_FOLLOWUP_WEBHOOK`) and `--apply`, which record a `ReconcileResult`. `docs/OPERATIONS.md` describes both. A manual end-to-end workflow is in `.github/workflows/e2e.yml`.
- CI runs `ruff check .` over the whole repository. The ruff config ignores the one-line style rules E701, E702, E731 and E741 on purpose.
- `scripts/run_sim.sh` dispatches with `--no-scene` so a stale `links/<room>.scene.json` cannot replace the simulation scenario.

## 0.1.0

First open-source cut of the multi-bot voice meeting prototype.

- `meet` starts a meeting from `--who` and `--topic`, summarizes briefs without inventing facts, assigns voices, and prints a join link.
- The worker reads the scene from LiveKit job metadata.
- ASR, TTS, LLM, and the realtime transport sit behind a provider registry. DeepSeek, Doubao, and LiveKit stay the defaults. Any OpenAI-compatible LLM works. OpenAI speech is an extra option.
- Grok Bot skill at `skills/voice-meeting/SKILL.md`. Principal follow-up is `mock`, `file`, or `webhook`. `meet followup` turns minutes into prompts for the real bots.
- Language `zh` or `en`. Human display name is `MEETING_HUMAN_NAME`. The host for new meetings is `moderator`.
- Static join page (`web/index.html`) accepts a pre-signed token so it can be hosted on GitHub Pages. `meet.livekit.io` remains the fallback.
- `meet doctor`, `meet demo`, Dockerfile, and GitHub Actions.
- Fixes: a spoken directive that arrives on end-of-turn (or just after a barge-in) is executed; the acceptance audio check measures bot tracks instead of the worker's silent track; spoken commitments that match a bot's mandate become action items.
