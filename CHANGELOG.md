# Changelog

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
