# grokbot-voice-meeting

A human and several AI agents meet **by voice** in one room. Each agent has its own brief, its own voice, and its own tile. A host runs the agenda: opening round, cues, raised hands. When you speak, every bot stops.

Built so any [Grok Bot](https://github.com/RussGuo/grokbot-voice-meeting) can start a meeting from "who + topic" and hand you a join link. LiveKit Cloud, Volcengine Doubao speech, and DeepSeek are the defaults. Each layer can be swapped.

![Meeting UI placeholder — replace docs/assets/demo.svg with a screenshot or GIF](docs/assets/demo.svg)

## Quickstart

Python 3.11+. From a clone:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"          # add ".[all]" if you want local sherpa ASR/TTS
cp .env.example .env             # then fill in keys, or skip them and use the demo
meet demo                        # simulated meeting, no keys, no network
meet doctor --offline            # what a live meeting still needs
```

Live meeting, two terminals:

```bash
# terminal A — leave this running; it reads each meeting from job metadata
meet worker

# terminal B
cp bots.example.yaml bots.yaml   # put real context in; do not invent facts
meet --who pm,eng --topic "What ships this month" --agenda "scope;risks"
```

`meet` prints a join URL. Open it. The worker must be running before `meet`, or the room will be empty. The worker waits up to 30 minutes for you to join (`MEETING_JOIN_WAIT_S`), then ends the run as `NoShow`. Each meeting writes `runs/<room>-<YYYYmmdd-HHMMSS>/` (events, minutes, actions).

`meet demo` writes a text transcript with `FakeVoiceLayer`. It is the no-keys walkthrough of turn-taking, barge-in, and minutes.

## Architecture

```mermaid
flowchart LR
  user[You in the browser]
  lk[LiveKit room]
  worker[meet worker]
  host[Host / turn taking]
  bots[Bot speakers]
  llm[LLM]
  asr[ASR]
  tts[TTS]
  principal[Real bot follow-up]

  user -->|mic| lk
  lk --> asr --> worker
  worker --> host
  host --> llm
  llm --> tts --> bots
  bots -->|own audio track| lk
  lk --> user
  host -->|minutes and actions| principal
```

One LiveKit agent session holds the floor. Each bot also joins as its own participant (`BotSpeakers`), so the active tile lights up. The worker's own track stays silent on purpose; the acceptance check measures the bot tracks.

The LLM planner (`orchestrator/planner.py`) decides what a bot says and whether it speaks, asks its principal, or commits. One streaming call per turn: a one-line JSON header, then the spoken text. The guards are code, not prompt: a topic in `must_ask` goes to the principal before any LLM call, a commitment must match `can_commit` or it is stopped before the first word, relayed answers are read verbatim, and low confidence when addressed means ask. Any LLM error falls back to the rule-based FakeLLM lines; without an LLM key the planner is off. Each stand-in also searches its own full exported context during the meeting: `meet` writes it, chunked, to `links/<room>.context.json` (mode 600; the scene carries only its path and sha256), and the worker builds an offline BM25 index per bot. The planner adds the best excerpts of that bot's own notes to the prompt, and the minutes show which excerpt ids a line came from. Hand scoring is rule-based and English-aware by default; `MEETING_SCORER=llm` adds an async LLM re-scorer. Agenda decisions get a fallback line at once and an LLM summary in the background (`DecisionSummarized`).

## Grok Bot

Install [`skills/voice-meeting/SKILL.md`](skills/voice-meeting/SKILL.md) into the bot that should start meetings. The user says "start a voice meeting with my X and Y bots about Z". The bot exports each participant's real context, runs `meet` on its Linux box, and returns the join link.

In-meeting questions the stand-in cannot answer use `PrincipalClient` (`orchestrator/principal.py`):

| `MEETING_PRINCIPAL` | Behavior |
|---|---|
| `defer` (default for meetings from `meet`) | Records the question and never answers in the meeting. It lands in the minutes as unanswered. |
| `mock` (default for sample scenarios, `meet demo`, tests) | Placeholder reply in the meeting language. It makes the answer up; never for a real meeting. |
| `file` | Question JSON in `outbox/`, answer JSON in `inbox/`. |
| `webhook` | POST the question. `{"text": "..."}` is delivered; otherwise the file inbox is used. |

An explicit `MEETING_PRINCIPAL` always wins over the default.

With `file` or `webhook`, a stand-in that has to ask says "One moment, let me check with my principal", waits up to `MEETING_PRINCIPAL_INLINE_MS`, and reads the reply verbatim ("My principal says: ...") in the same turn. If it is not back in time the stand-in says so and moves on; the reply then arrives the usual way, as a later pending answer. The minutes mark in-turn replies.

Afterward, `meet followup runs/<id>` (or `--room ROOM` for the newest run of a room) writes a prompt per bot from `minutes.md` and `actions/<bot>.json`. Send that prompt to the real bot. Close the loop two ways, both recorded as a `ReconcileResult` event with minutes and actions re-rendered:

- `meet followup runs/<id> --push URL` (default `MEETING_FOLLOWUP_WEBHOOK`) POSTs each prompt and applies a JSON reply.
- `meet followup runs/<id> --apply <bot> reply.txt` applies a reply by hand: one `confirm`, `correct: ...` or `retract` per commitment.

This repo does not write into a bot's memory by itself. `meet stats runs/<id>` reports P50/P95 latencies (agent switch, LLM first token, bot response, barge-in stop) and reliability counts; `--baseline` and `--fail-on-regress` compare against a good run. Details: [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Providers

Defaults: LiveKit, Volcengine Doubao streaming ASR and seed-tts, DeepSeek. Any OpenAI-compatible chat endpoint is `LLM_PROVIDER=openai` plus `LLM_BASE_URL`. OpenAI Whisper / TTS is `VOICE_ASR_PROVIDER=openai` and `VOICE_TTS_PROVIDER=openai`. Speaker ids are [`config/voices.json`](config/voices.json), not Python constants.

How to add an adapter: [docs/PROVIDERS.md](docs/PROVIDERS.md).

### LiveKit Cloud

1. Create a project on [LiveKit Cloud](https://cloud.livekit.io/). The Build plan is the free tier; limits are on their pricing page and change, so check there before a public launch.
2. Copy the WebSocket URL, API key, and secret into `.env` as `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`.
3. `meet worker` registers the agent name `voice-meeting` (`LIVEKIT_AGENT_NAME`). `meet` dispatches that name.

### Volcengine / Doubao

1. Open the [Volcengine speech console](https://www.volcengine.com/product/speech) and create an API key (or the older app id + access token).
2. Set `VOLC_API_KEY`. ASR resource defaults to `volc.seedasr.sauc.duration`, TTS to `seed-tts-2.0`.
3. New accounts usually receive trial credit. The catalog in `config/voices.json` lists speaker ids from Doubao's public voice list. Confirm the id still exists in your account before a demo.

### DeepSeek

1. Create a key at [DeepSeek](https://platform.deepseek.com/).
2. `DEEPSEEK_API_KEY`, optional `DEEPSEEK_BASE_URL` (default `https://api.deepseek.com`) and `DEEPSEEK_MODEL` (default `deepseek-flash`).
3. Billing is per token. Brief summarization is one short call per bot and is cached under `~/.cache/grokbot-voice-meeting/briefs/`.

### Cost

`meet demo` and `pytest` call no paid API. A live meeting spends LiveKit participant-minutes, ASR/TTS audio seconds, and LLM tokens. Free tiers and trial credit are enough for a handful of short meetings; they are not a quota this repo can promise. `meet doctor` tells you which keys are missing and whether TCP to LiveKit and the LLM host succeeds. It never prints secret values.

## Join page without a tunnel

`meet` writes two URLs when `MEETING_JOIN_PAGE` is set:

- your static page: `<page>#liveKitUrl=...&token=...`. The token sits in the URL fragment, which browsers do not send to the host, so it stays out of server logs and Referer headers. The page reads it and strips it from the address bar, so reloading needs the link again. Old `?liveKitUrl=&token=` links still work and are stripped too.
- `https://meet.livekit.io/custom?...` as the fallback. That is a third-party page that only reads the query string: **the token is sent to that site.** Use it only when you have no static page.

`web/index.html` is static. It uses the token in the link when present and only calls `/api/token` otherwise. The page pins `livekit-client` to an exact version on jsDelivr (ES-module imports cannot carry an SRI hash). Host it anywhere, including GitHub Pages:

1. Enable Pages for this repo on the `/` (or `/docs`) root, or publish the `web/` directory.
2. Set `MEETING_JOIN_PAGE` to that `index.html` URL.
3. Open the link `meet` prints. The browser talks to LiveKit Cloud directly. You do not expose the token server.

Room metadata is readable by everyone holding a token, so it carries only `{"v":1,"room":{"topic","who"}}`. The full scene, briefs included, goes only in dispatch metadata and in `links/<room>.scene.json` (mode 600). `scripts/dispatch.py --room R` sends that file by default (`--scene FILE` to pick one, `--no-scene` to send none). The link can be forwarded; the briefs do not travel with it. The same goes for the full contexts: `links/<room>.context.json` (mode 600) stays on the machine, and the scene holds only its path and hash.

`python web/token_server.py` remains for local development (binds `127.0.0.1` unless `WEB_HOST` says otherwise). Its `/` lists runs you can replay. Behind a tunnel, set `MEETING_ACCESS_KEY`: send it as `Authorization: Bearer <key>` or use the cookie it sets. The old `?k=<key>` still works but is discouraged, since the query string lands in address bars and proxy logs. The server never logs the key.

## Configuration

| Variable | Default | Role |
|---|---|---|
| `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | empty | LiveKit project |
| `LIVEKIT_AGENT_NAME` | `voice-meeting` | Worker name `meet` dispatches |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | mock if unset | Default LLM |
| `LLM_PROVIDER` / `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | `auto` | Any OpenAI-compatible LLM |
| `VOICE_ASR_PROVIDER` / `VOICE_TTS_PROVIDER` | `auto` | `volc` `aliyun` `openai` `local` |
| `VOLC_API_KEY` | empty | Doubao speech |
| `OPENAI_API_KEY` | empty | OpenAI speech, or `LLM_API_KEY` |
| `VOICE_CATALOG` | `config/voices.json` | Speaker ids |
| `MEETING_HUMAN_IDENTITY` | `host` | Must match the join token identity |
| `MEETING_HUMAN_NAME` | `You` / `用户` | Spoken and on-screen name |
| `MEETING_LANG` | `zh` | `zh` or `en` prompts and host script |
| `MEETING_MODERATOR_ID` | `moderator` | Neutral host. Not a persona. |
| `MEETING_JOIN_PAGE` | empty | Static join page; else meet.livekit.io (sends the token to that site) |
| `MEETING_JOIN_WAIT_S` | `1800` | Seconds the worker waits for the human; then the run ends as `NoShow` |
| `MEETING_PRINCIPAL` | `defer` for `meet` meetings, `mock` for samples | `defer` `mock` `file` `webhook` |
| `MEETING_PRINCIPAL_DIR` / `MEETING_PRINCIPAL_WEBHOOK` | `runs/principal` / empty | For `file` and `webhook` |
| `MEETING_PRINCIPAL_DELAY_MS` | `20000` | Delay of the `mock` reply |
| `MEETING_PRINCIPAL_INLINE_MS` | `5000` | How long a stand-in waits for its principal inside the same turn (`file`/`webhook`; `0` = off). `MEETING_PRINCIPAL_MOCK_INLINE=1` lets `mock` do it too |
| `MEETING_SCORER` | `rule` | `rule`, or `llm` to add an async LLM re-scorer for raised hands |
| `MEETING_FOLLOWUP_WEBHOOK` | empty | Default `--push` URL for `meet followup` |
| `MEETING_BOTS` | `./bots.yaml` | Registry of bots and context |
| `MEETING_HOME` | empty | Directory holding `.env`, `runs/` and `links/`; else the current directory |
| `MEETING_RUN_ID` | `<room>-<YYYYmmdd-HHMMSS>` | Fixes the run id verbatim |
| `MEETING_SCENARIO` | `sim_v1` | Sample scenario when a job carries no scene |
| `MEETING_MULTI_IDENTITY` | `1` | `0` keeps all bots on the worker's one track |
| `MEETING_BRIEF_CACHE` | `~/.cache/grokbot-voice-meeting/briefs/` | Brief summary cache |
| `MEETING_TRANSPORT` | `livekit` | Realtime transport |
| `MEETING_ACCESS_KEY` / `MEETING_ROOM` | empty | Token server public mode: key, and the one room it signs for |

Sample scenarios under `orchestrator/bots/` still include a chief-of-staff host. That is fixture data for the original tests. New meetings from `meet` use `moderator`.

## Docker

```bash
docker compose up worker
```

The image installs the package, reads `.env`, and runs `meet worker`. It does not bake in keys. See [Dockerfile](Dockerfile) and [docker-compose.yml](docker-compose.yml).

## Development

```bash
pip install -e ".[dev]"
pytest                          # orchestrator, meet, providers — no keys
pytest voice/tests              # voice layer — no keys
ruff check .
meet demo --run-dir runs/demo
```

GitHub Actions runs the same lint and tests on push. `ruff` ignores the one-line style rules (E701, E702, E731, E741) on purpose; see `pyproject.toml`. A manual end-to-end workflow (`.github/workflows/e2e.yml`) needs real keys; see [docs/OPERATIONS.md](docs/OPERATIONS.md).

## License

MIT. See [LICENSE](LICENSE).

Contributing, security reports, and the code of conduct: [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
