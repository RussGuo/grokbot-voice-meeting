---
name: voice-meeting
description: Start a multi-bot voice meeting on the user's Linux box and return a join link. Use when the user asks to meet by voice with their bots, start a voice meeting, or send meeting minutes back to the real bots.
---

# Voice meeting

A human and several bots talk in one LiveKit room. You do not join the audio yourself.
You export context, run `meet` on the machine that has the repo and the keys, and return the join link.

## Start a meeting

Trigger phrases: "start a voice meeting with my X and Y bots about Z", "开一场语音会".

1. Decide `who` (registry ids), `topic`, and optional agenda items. Ask only if more than one bot could match a name.
2. For each participating bot, export **only context you actually have** into `contexts/<id>.md`.
   - Copy facts, decisions, and open questions from that bot's notes or from what the user just said.
   - Export the full relevant notes, not only a summary. The stand-in searches this file during the meeting, so details left out cannot be found later. Only the brief is summarized.
   - If you do not know something, write `unknown: ...` (or `未知：...`). Do not invent numbers, dates, or owners.
   - If the bot may promise something, list it under a `Can commit:` / `可承诺:` heading, one item per line. Otherwise leave that heading off.
3. Make sure `bots.yaml` (see `bots.example.yaml`) has a `display_name` and `aliases` for each id. Point `context_file` at the file you just wrote, or pass `--context-dir contexts`.
4. The worker must be up: `meet worker` (long-running). Then:

```bash
meet --who pm,eng --topic "..." --agenda "scope;risks" --lang zh --json
```

5. Return `join_url` to the user. If `unknowns` is non-empty, say which fields were left unknown.
6. The link contains a join token (in the URL fragment, which the page strips after opening). Do not commit it, and do not paste it into a public channel. Prefer a `MEETING_JOIN_PAGE` of your own: the `meet.livekit.io` fallback link sends the token to that third-party site.

`meet` summarizes each context with the configured LLM, caches the brief, and refuses numbers that were not in the source. Without an API key it stores the excerpt and marks stances unknown. That is the correct outcome.

## During the meeting

Questions a stand-in cannot answer go to the real bot through `PrincipalClient` (`orchestrator/principal.py`):

- `MEETING_PRINCIPAL=file` writes `runs/principal/outbox/<query_id>.json`.
- Reply by writing `runs/principal/inbox/<query_id>.json` as `{"text": "..."}` before the query TTL.
- `MEETING_PRINCIPAL=webhook` POSTs the same JSON. A response body `{"text": "..."}` is delivered immediately; otherwise the file inbox is used.
- `defer` is the default for meetings started with `meet`. The stand-in says it will check, the question is recorded unanswered, and it shows up in the minutes for the follow-up. Nothing is invented.
- `mock` answers with a made-up placeholder after `MEETING_PRINCIPAL_DELAY_MS`. It is the default only for the sample scenarios and `meet demo`; do not set it for a real meeting.
- An explicit `MEETING_PRINCIPAL` always wins over the default.
- With `file` or `webhook`, a reply that arrives within `MEETING_PRINCIPAL_INLINE_MS` (default 5000 ms) is spoken in the same turn. If you are the principal, answer fast and short; a later reply is still used, just in a later turn.

The worker waits `MEETING_JOIN_WAIT_S` (default 1800) for the human to open the link. If nobody joins, the run ends as `NoShow` and you must start a new meeting with `meet`.

## After the meeting

The worker writes `runs/<run_id>/minutes.md` and `runs/<run_id>/actions/<bot_id>.json`.

```bash
meet followup runs/<run_id>          # or: meet followup --room <room>   (newest run of that room)
```

That prints one prompt per bot. Send each prompt to the **real** bot (not the voice stand-in) and ask them to confirm, correct, or retract. Do not apply a commitment to a bot who was not in the room.

Close the loop in one of two ways. Both record a `ReconcileResult` event and re-render `minutes.md` and `actions/`.

```bash
meet followup runs/<run_id> --push https://example.com/hook   # POSTs each prompt; a JSON reply is applied (default URL: MEETING_FOLLOWUP_WEBHOOK)
meet followup runs/<run_id> --apply <bot_id> reply.txt        # '-' reads stdin
```

`reply.txt` has one line per commitment, in the order the prompt listed them: `confirm`, `correct: <new text>` or `retract` (or `cmt-2: retract` with explicit ids). Apply only what the real bot actually said. Unknown ids and lines are skipped and reported.

Minutes are not pushed back into a bot's memory by this repo. You do that by delivering the follow-up prompt.

To judge how the meeting went technically: `meet stats runs/<run_id>` (or `--room <room>`) prints latency percentiles and counts. See `docs/OPERATIONS.md`.

## Language and names

- `MEETING_LANG=zh` (default) or `en` — prompts, host script, and unknown markers.
- `MEETING_HUMAN_NAME` — how the human is addressed. `MEETING_HUMAN_IDENTITY` must match the LiveKit identity on the join token (default `host`).
- The host is `moderator` unless `MEETING_MODERATOR_ID` says otherwise. Do not assume a chief-of-staff bot.

## If keys are missing

`meet doctor` says what is unset without printing values. `meet demo` runs a simulated meeting with no keys and no network.
