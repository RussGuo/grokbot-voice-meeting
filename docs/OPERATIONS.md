# Operations

Tools for judging a finished meeting: latency stats, baselines, closing the follow-up loop, and the manual end-to-end workflow.

A run is the directory `runs/<run_id>/`. Every command below accepts a path, a bare run id (looked up under `$MEETING_HOME/runs` or `./runs`), or `--room ROOM`, which picks the newest `runs/<ROOM>-<YYYYmmdd-HHMMSS>` directory and falls back to the `room` field in each run's `clock.json`.

## Stats

```
meet stats runs/<id> [runs/<id> ...]
meet stats --room standup
meet stats runs/<id> --json > stats.json
```

Several runs are pooled before percentiles are taken. The table reports count, P50, P95 and max (linear interpolation between ranks) for:

| Metric | Source |
| --- | --- |
| agent switch ms | `voice_stats.json` `switch`, else `AgentSwitched.switchMs` in events |
| LLM ttft ms, first segment ms | `voice_stats.json` `llm` |
| bot response ms | `HumanVad` end to the next `BotFirstAudio`; a new `HumanVad` start in between, or a gap over 10 s, means the turn got no reply and is counted as "without a bot reply" |
| barge-in stop ms | `Interrupted.at` minus the latest `HumanVad` start before it |

It also counts interruptions, ducks and restores, dropped stale outputs (`OutputDropped`), LLM fallbacks (`ok: false`) and the error types behind them. Anything missing prints `n/a`.

## Baselines

```
meet stats runs/<new> --baseline runs/<good>            # prints P95 deltas
meet stats runs/<new> --baseline docs/baseline/stats.json --fail-on-regress 20
```

A baseline is a run directory or a file saved with `--json`. With `--fail-on-regress PCT` the command exits 1 when any latency P95 is more than PCT percent above the baseline. Metrics missing on either side are not compared. To record a new baseline from a run you trust:

```
mkdir -p docs/baseline && meet stats runs/<good> --json > docs/baseline/stats.json
```

## Follow-up: closing the loop

`meet followup RUN` writes `followups/<owner>.md`, one prompt per real bot. The reply is ingested in one of two ways, both ending in a `ReconcileResult` event appended to `events.jsonl`, `reconcile/<owner>.json`, and a re-rendered `minutes.md` and `actions/`.

Push to a webhook (or set `MEETING_FOLLOWUP_WEBHOOK`):

```
meet followup runs/<id> --push https://example.com/hook
```

For each owner with commitments it POSTs `{"run", "owner", "prompt", "commitments": [{"id", "text", "status"}]}`. A JSON reply `{"items": [{"commitment_id", "verdict", "correction"?}]}` is applied; an empty reply just records that the prompt was delivered. A delivery failure makes the command exit 1.

Apply a reply by hand:

```
meet followup runs/<id> --apply pm_bot reply.txt     # '-' reads stdin
```

One line per commitment, in the order the prompt listed them:

```
confirm
correct: ship Friday instead
retract
```

or with explicit ids: `cmt-2: retract`. Verdicts are `confirm`, `correct: <text>` and `retract`. Unknown ids, other owners' ids, unknown verdicts and unparseable lines are reported and skipped; commitments without a line stay as they were. Applying the same reply twice changes nothing; a different verdict for a commitment is recorded as a new event and wins.

## End-to-end workflow

`.github/workflows/e2e.yml` is manual (`workflow_dispatch`). It needs the repository secrets `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `VOLC_API_KEY` and `DEEPSEEK_API_KEY` and stops with a message naming any that are missing. It then:

1. installs the package into `.venv` (also linked as `web/.venv`, which `scripts/run_sim.sh` expects) and ffmpeg,
2. synthesizes the simulated human's lines with `voice/bench/synth_human_lines.py`,
3. runs `scripts/run_sim.sh <run_id>`, then `scripts/acceptance.py --max-stop-ms <input>` as the pass/fail gate,
4. runs `meet stats` and, if `docs/baseline/stats.json` is committed, compares against it with `--fail-on-regress <regress_pct>`,
5. uploads `runs/<run_id>` as an artifact, with the audio as a separate artifact only when it is 50 MB or less.

The baseline lives in the repository rather than in a previous run's artifact: it needs no token or run lookup, it changes only through a reviewed commit, and a flaky run cannot silently become the new reference. Only one e2e run executes at a time because the simulation uses a fixed room.
