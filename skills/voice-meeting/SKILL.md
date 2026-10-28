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
   - If you do not know something, write `unknown: ...` (or `未知：...`). Do not invent numbers, dates, or owners.
   - If the bot may promise something, list it under a `Can commit:` / `可承诺:` heading, one item per line. Otherwise leave that heading off.
3. Make sure `bots.yaml` (see `bots.example.yaml`) has a `display_name` and `aliases` for each id. Point `context_file` at the file you just wrote, or pass `--context-dir contexts`.
4. The worker must be up: `meet worker` (long-running). Then:

```bash
meet --who pm,eng --topic "..." --agenda "scope;risks" --lang zh --json
```

5. Return `join_url` to the user. If `unknowns` is non-empty, say which fields were left unknown.
6. The link contains a join token. Do not commit it, and do not paste it into a public channel.

`meet` summarizes each context with the configured LLM, caches the brief, and refuses numbers that were not in the source. Without an API key it stores the excerpt and marks stances unknown. That is the correct outcome.

## During the meeting

Questions a stand-in cannot answer go to the real bot through `PrincipalClient` (`orchestrator/principal.py`):

- `MEETING_PRINCIPAL=file` writes `runs/principal/outbox/<query_id>.json`.
- Reply by writing `runs/principal/inbox/<query_id>.json` as `{"text": "..."}` before the query TTL.
- `MEETING_PRINCIPAL=webhook` POSTs the same JSON. A response body `{"text": "..."}` is delivered immediately; otherwise the file inbox is used.
- `mock` (default) answers with a placeholder after `MEETING_PRINCIPAL_DELAY_MS`. Use it for demos only.

## After the meeting

The worker writes `runs/<run_id>/minutes.md` and `runs/<run_id>/actions/<bot_id>.json`.

```bash
meet followup runs/<run_id>
```

That prints one prompt per bot. Send each prompt to the **real** bot (not the voice stand-in) and ask them to confirm, correct, or retract. Do not apply a commitment to a bot who was not in the room.

Minutes are not pushed back into a bot's memory by this repo. You do that by delivering the follow-up prompt.

## Language and names

- `MEETING_LANG=zh` (default) or `en` — prompts, host script, and unknown markers.
- `MEETING_HUMAN_NAME` — how the human is addressed. `MEETING_HUMAN_IDENTITY` must match the LiveKit identity on the join token (default `host`).
- The host is `moderator` unless `MEETING_MODERATOR_ID` says otherwise. Do not assume a chief-of-staff bot.

## If keys are missing

`meet doctor` says what is unset without printing values. `meet demo` runs a simulated meeting with no keys and no network.
