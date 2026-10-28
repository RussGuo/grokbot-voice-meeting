"""Latency and reliability numbers for finished runs.

    meet stats runs/<id> [runs/<id> ...] [--baseline runs/<id>|stats.json] [--json] [--fail-on-regress 20]
    meet stats --room my-room

Latencies come from events.jsonl and, when present, voice_stats.json. Missing data is reported as n/a.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

# A bot turn that starts later than this after the human stopped is not an answer to that turn.
RESPONSE_WINDOW_MS = 10_000

LATENCIES = [
    ("switch", "agent switch ms"),
    ("llm_ttft", "LLM ttft ms"),
    ("llm_first_seg", "LLM first segment ms"),
    ("response", "bot response ms"),
    ("bargein_stop", "barge-in stop ms"),
]
COUNTS = [
    ("interruptions", "interruptions"),
    ("ducks", "ducks"),
    ("restores", "duck restores"),
    ("dropped_outputs", "stale outputs dropped"),
    ("llm_calls", "LLM calls"),
    ("llm_fallbacks", "LLM fallbacks"),
    ("unanswered_turns", "human turns without a bot reply"),
]


def percentile(values: list[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (same as numpy's default)."""
    if not values:
        return None
    xs = sorted(values)
    pos = (len(xs) - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def summarize(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "p50": _r(percentile(values, 50)),
        "p95": _r(percentile(values, 95)),
        "max": _r(max(values)) if values else None,
    }


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _read_events(run: Path) -> list[dict]:
    path = run / "events.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and "type" in e:
            out.append(e)
    out.sort(key=lambda e: e.get("seq", 0))
    return out


def _read_voice_stats(run: Path) -> dict:
    try:
        d = json.loads((run / "voice_stats.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _error_type(err: Any) -> str:
    text = str(err or "unknown").strip() or "unknown"
    return text.split(":", 1)[0].strip()[:60] or "unknown"


def collect_run(run: Path) -> dict[str, Any]:
    """Raw samples and counters for one run."""
    run = Path(run)
    events = _read_events(run)
    vs = _read_voice_stats(run)
    s: dict[str, list[float]] = {k: [] for k, _ in LATENCIES}
    c = {k: 0 for k, _ in COUNTS}
    errors: dict[str, int] = {}

    pending_end: float | None = None
    last_vad_start: float | None = None
    event_switch: list[float] = []
    for e in events:
        t, at, d = e["type"], _num(e.get("at")), e.get("data") or {}
        if at is None:
            continue
        if t == "HumanVad":
            if d.get("stage") == "start":
                last_vad_start = at
                if pending_end is not None:
                    c["unanswered_turns"] += 1
                pending_end = None
            elif d.get("stage") == "end":
                pending_end = at
        elif t == "BotFirstAudio":
            if pending_end is not None:
                gap = at - pending_end
                if 0 <= gap <= RESPONSE_WINDOW_MS:
                    s["response"].append(gap)
                else:
                    c["unanswered_turns"] += 1
                pending_end = None
        elif t == "Interrupted":
            c["interruptions"] += 1
            if last_vad_start is not None and at >= last_vad_start:
                s["bargein_stop"].append(at - last_vad_start)
        elif t == "Ducked":
            c["ducks"] += 1
        elif t == "DuckRestored":
            c["restores"] += 1
        elif t == "OutputDropped":
            c["dropped_outputs"] += 1
        elif t == "AgentSwitched":
            ms = _num(d.get("switchMs"))
            if ms is not None:
                event_switch.append(ms)
    if pending_end is not None:
        c["unanswered_turns"] += 1

    voice_switch = [ms for r in vs.get("switch") or [] if isinstance(r, dict)
                    if r.get("kind", "switch") == "switch" and (ms := _num(r.get("ms"))) is not None]
    metrics = vs.get("metrics") if isinstance(vs.get("metrics"), dict) else {}
    s["switch"] = voice_switch or [x for x in (_num(v) for v in metrics.get("switch_ms") or []) if x is not None] \
        or event_switch
    if not s["bargein_stop"]:
        s["bargein_stop"] = [x for x in (_num(v) for v in metrics.get("bargein_stop_ms") or []) if x is not None]

    for r in vs.get("llm") or []:
        if not isinstance(r, dict):
            continue
        c["llm_calls"] += 1
        for key, name in (("llm_ttft", "ttft_ms"), ("llm_first_seg", "first_seg_ms")):
            v = _num(r.get(name))
            if v is not None:
                s[key].append(v)
        if r.get("ok") is False:
            c["llm_fallbacks"] += 1
            kind = _error_type(r.get("error"))
            errors[kind] = errors.get(kind, 0) + 1
    return {"name": run.name, "samples": s, "counts": c, "llm_errors": errors,
            "has_events": bool(events), "has_voice_stats": bool(vs)}


def aggregate(runs: Iterable[Path]) -> dict[str, Any]:
    names: list[str] = []
    samples: dict[str, list[float]] = {k: [] for k, _ in LATENCIES}
    counts = {k: 0 for k, _ in COUNTS}
    errors: dict[str, int] = {}
    notes: list[str] = []
    for run in runs:
        r = collect_run(run)
        names.append(r["name"])
        for k, v in r["samples"].items():
            samples[k] += v
        for k, v in r["counts"].items():
            counts[k] += v
        for k, v in r["llm_errors"].items():
            errors[k] = errors.get(k, 0) + v
        if not r["has_events"]:
            notes.append(f"{r['name']}: no events.jsonl")
        if not r["has_voice_stats"]:
            notes.append(f"{r['name']}: no voice_stats.json (LLM numbers n/a)")
    return {
        "runs": names,
        "latency_ms": {k: summarize(v) for k, v in samples.items()},
        "counts": counts,
        "llm_errors": dict(sorted(errors.items())),
        "notes": notes,
    }


def load_baseline(arg: str | Path) -> dict[str, Any]:
    p = Path(arg)
    if p.is_dir():
        return aggregate([p])
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"baseline {arg}: {e}") from e
    if not isinstance(d, dict) or not isinstance(d.get("latency_ms"), dict):
        raise ValueError(f"baseline {arg}: not a `meet stats --json` file or a run directory")
    return d


def compare(cur: dict, base: dict, fail_pct: float | None = None) -> dict[str, Any]:
    deltas: dict[str, dict] = {}
    regressions: list[str] = []
    for key, label in LATENCIES:
        a = (cur["latency_ms"].get(key) or {})
        b = (base.get("latency_ms", {}).get(key) or {})
        row = {}
        for stat in ("p50", "p95"):
            x, y = a.get(stat), b.get(stat)
            if x is None or y is None:
                row[stat] = {"base": y, "cur": x, "delta": None, "pct": None}
                continue
            pct = None if y <= 0 else round((x - y) / y * 100.0, 1)
            row[stat] = {"base": y, "cur": x, "delta": round(x - y, 1), "pct": pct}
        deltas[key] = row
        p95 = row["p95"]
        if fail_pct is not None and p95["pct"] is not None and p95["pct"] > fail_pct:
            regressions.append(f"{label} P95 {p95['base']} -> {p95['cur']} ms (+{p95['pct']}%)")
    return {"deltas": deltas, "regressions": regressions}


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    return f"{v:g}" if isinstance(v, float) else str(v)


def _pct(row: dict) -> str:
    if row.get("delta") is None:
        return "n/a"
    pct = "" if row["pct"] is None else f" ({row['pct']:+g}%)"
    return f"{row['delta']:+g}{pct}"


def render(report: dict) -> str:
    out = [f"runs: {', '.join(report['runs']) or '(none)'}", ""]
    cmp_ = report.get("baseline")
    head = f"{'latency (ms)':<24}{'n':>5}{'p50':>9}{'p95':>9}{'max':>9}"
    if cmp_:
        head += f"{'base p95':>10}  {'delta p95':<16}"
    out.append(head)
    for key, label in LATENCIES:
        s = report["latency_ms"][key]
        line = f"{label:<24}{s['count']:>5}{_fmt(s['p50']):>9}{_fmt(s['p95']):>9}{_fmt(s['max']):>9}"
        if cmp_:
            row = cmp_["deltas"][key]["p95"]
            line += f"{_fmt(row['base']):>10}  {_pct(row):<16}"
        out.append(line.rstrip())
    out += ["", "counts"]
    for key, label in COUNTS:
        out.append(f"  {label:<34}{report['counts'][key]:>5}")
    if report["llm_errors"]:
        out.append("LLM error types")
        out += [f"  {k:<34}{v:>5}" for k, v in report["llm_errors"].items()]
    for n in report.get("notes", []):
        out.append(f"note: {n}")
    if cmp_:
        out.append("")
        if cmp_["regressions"]:
            out += ["REGRESSIONS"] + [f"  {r}" for r in cmp_["regressions"]]
        else:
            out.append("no regressions over threshold" if report.get("fail_on_regress") is not None
                       else "baseline comparison shown; no threshold set")
    return "\n".join(out) + "\n"


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("runs", nargs="*", help="run directories (or run ids under runs/)")
    ap.add_argument("--room", help="use the newest run recorded for this LiveKit room")
    ap.add_argument("--baseline", help="run directory or a saved `meet stats --json` file")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--fail-on-regress", type=float, metavar="PCT",
                    help="exit 1 if any latency P95 is worse than the baseline by more than PCT percent")


def run(args: argparse.Namespace) -> int:
    from meet.runs import find_run_by_room, resolve_run

    dirs: list[Path] = []
    for a in args.runs:
        d = resolve_run(a)
        if d is None:
            print(f"meet stats: no such run: {a}", file=sys.stderr)
            return 2
        dirs.append(d)
    if args.room:
        d = find_run_by_room(args.room)
        if d is None:
            print(f"meet stats: no run found for room {args.room!r}", file=sys.stderr)
            return 2
        dirs.append(d)
    if not dirs:
        print("meet stats: give a run directory or --room", file=sys.stderr)
        return 2
    if args.fail_on_regress is not None and not args.baseline:
        print("meet stats: --fail-on-regress needs --baseline", file=sys.stderr)
        return 2
    report = aggregate(dirs)
    if args.baseline:
        try:
            base = load_baseline(args.baseline)
        except ValueError as e:
            print(f"meet stats: {e}", file=sys.stderr)
            return 2
        report["baseline"] = compare(report, base, args.fail_on_regress)
        report["fail_on_regress"] = args.fail_on_regress
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report), end="")
    if report.get("baseline", {}).get("regressions"):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="meet stats")
    add_arguments(ap)
    return run(ap.parse_args(argv))
