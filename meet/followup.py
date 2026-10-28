"""After the meeting: turn minutes and action files into prompts for the real bots."""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from orchestrator.events import EventLog, MeetingEvent
from orchestrator.minutes import build, render_md

VERDICTS = ("confirm", "correct", "retract")
_VERB = re.compile(r"^(confirm|correct|retract)\b\s*[:\uff1a-]?\s*(.*)$", re.I)
_BY_ID = re.compile(r"^([A-Za-z0-9_.\-]+)\s*[:\uff1a]\s*(confirm|correct|retract)\b\s*[:\uff1a-]?\s*(.*)$", re.I)
_BULLET = re.compile(r"^(?:[-*]|\d+[.)])\s+")
MAX_REPLY_BYTES = 1_000_000


def build_followups(run_dir: Path) -> dict[str, str]:
    run_dir = Path(run_dir)
    minutes = ""
    md = run_dir / "minutes.md"
    if md.is_file():
        minutes = md.read_text(encoding="utf-8")
    actions_dir = run_dir / "actions"
    prompts: dict[str, str] = {}
    files = sorted(actions_dir.glob("*.json")) if actions_dir.is_dir() else []
    if not files and not minutes:
        raise FileNotFoundError(f"no minutes.md or actions/ under {run_dir}")
    owners = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in files}
    if not owners:
        owners = {"meeting": []}
    for owner, items in owners.items():
        lines = [
            f"You are the real bot `{owner}`. A voice stand-in attended a meeting for you.",
            "Confirm or correct each commitment. Do not invent new ones.",
            "",
            "Commitments recorded for you:",
        ]
        if not items:
            lines.append("- (none filed under your id)")
        for item in items:
            title = item.get("title") or item.get("text") or ""
            status = item.get("status") or "binding"
            lines.append(f"- [{status}] {title}")
        lines += ["", "Minutes:", minutes or "(minutes.md was not written)", "",
                  "Reply with one line per commitment: confirm, correct: <text>, or retract."]
        prompts[owner] = "\n".join(lines).rstrip() + "\n"
    out = run_dir / "followups"
    out.mkdir(parents=True, exist_ok=True)
    for owner, text in prompts.items():
        (out / f"{owner}.md").write_text(text, encoding="utf-8")
    return prompts


def _events_path(run_dir: Path) -> Path:
    return Path(run_dir) / "events.jsonl"


def owner_commitments(events: list[MeetingEvent], owner: str) -> list[dict]:
    return [e.data["commitment"] for e in events
            if e.type == "CommitmentMade" and e.data["commitment"].get("owner_id") == owner]


def parse_reply(text: str, commitment_ids: list[str]) -> tuple[list[dict], list[str]]:
    """Reply text to items. Lines are `confirm | correct: <text> | retract`, in commitment
    order, or `<commitment_id>: confirm ...`. Returns (items, problems)."""
    items: list[dict] = []
    problems: list[str] = []
    pos = 0
    for n, raw in enumerate(text.splitlines(), 1):
        line = _BULLET.sub("", raw.strip())
        if not line or line.startswith("#"):
            continue
        if m := _VERB.match(line):
            if pos >= len(commitment_ids):
                problems.append(f"line {n}: more verdicts than commitments ({len(commitment_ids)})")
                continue
            cid, verdict, rest = commitment_ids[pos], m.group(1).lower(), m.group(2).strip()
            pos += 1
        elif m := _BY_ID.match(line):
            cid, verdict, rest = m.group(1), m.group(2).lower(), m.group(3).strip()
        else:
            problems.append(f"line {n}: cannot parse {raw.strip()!r}")
            continue
        if verdict == "correct" and not rest:
            problems.append(f"line {n}: `correct` needs the corrected text")
            continue
        item = {"commitment_id": cid, "verdict": verdict}
        if verdict == "correct":
            item["correction"] = rest
        items.append(item)
    return items, problems


def _latest_verdicts(events: list[MeetingEvent]) -> dict[str, tuple[str, str | None]]:
    cur: dict[str, tuple[str, str | None]] = {}
    for e in events:
        if e.type == "ReconcileResult":
            for it in e.data.get("items", []):
                if it.get("commitment_id"):
                    cur[it["commitment_id"]] = (it.get("verdict"), it.get("correction"))
    return cur


def _now_at(run_dir: Path, last_at: int) -> int:
    try:
        t0 = json.loads((Path(run_dir) / "clock.json").read_text(encoding="utf-8"))["t0_epoch_ms"]
        return max(last_at, int(time.time() * 1000) - int(t0))
    except (OSError, ValueError, KeyError, TypeError):
        return last_at


def render_outputs(run_dir: Path) -> None:
    """Rewrite minutes.md and actions/ from the event log."""
    run_dir = Path(run_dir)
    events = EventLog.load(_events_path(run_dir))
    b = build(events)
    (run_dir / "minutes.md").write_text(render_md(b), encoding="utf-8")
    (run_dir / "actions").mkdir(exist_ok=True)
    verdicts = _latest_verdicts(events)
    for owner, items in b["actions"].items():
        for a in items:
            corr = verdicts.get(a["id"], (None, None))[1]
            if corr and a["status"] == "corrected":
                a["correction"] = corr
        (run_dir / "actions" / f"{owner}.json").write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_reply(run_dir: Path, owner: str, items: list[dict], source: str = "manual") -> dict:
    """Validate `items` against `owner`'s commitments and append one ReconcileResult.

    Unknown ids, other owners' ids and unknown verdicts are skipped and reported. An item identical to the
    commitment's current verdict is not appended again, so re-applying a reply is a no-op."""
    run_dir = Path(run_dir)
    path = _events_path(run_dir)
    if not path.is_file():
        raise FileNotFoundError(f"no events.jsonl under {run_dir}")
    events = EventLog.load(path)
    mine = {c["id"] for c in owner_commitments(events, owner)}
    if not mine:
        owners = sorted({e.data["commitment"]["owner_id"] for e in events if e.type == "CommitmentMade"})
        raise ValueError(f"no commitments for owner {owner!r}; owners: {', '.join(owners) or '(none)'}")
    current = _latest_verdicts(events)
    valid, skipped, unchanged = [], [], []
    for it in items:
        cid, verdict = it.get("commitment_id"), it.get("verdict")
        corr = (it.get("correction") or "").strip() or None
        if cid not in mine:
            skipped.append({"commitment_id": cid, "reason": "unknown commitment id for this owner"})
        elif verdict not in VERDICTS:
            skipped.append({"commitment_id": cid, "reason": f"unknown verdict {verdict!r}"})
        elif verdict == "correct" and not corr:
            skipped.append({"commitment_id": cid, "reason": "correct without text"})
        else:
            clean = {"commitment_id": cid, "verdict": verdict}
            if verdict == "correct":
                clean["correction"] = corr
            valid.append(clean)
            if current.get(cid) == (verdict, corr if verdict == "correct" else None):
                unchanged.append(cid)
    fresh = [v for v in valid if v["commitment_id"] not in unchanged]
    if fresh:
        last = events[-1]
        ev = MeetingEvent(seq=last.seq + 1, at=_now_at(run_dir, last.at), meeting_id=last.meeting_id,
                          type="ReconcileResult", data={"participantId": owner, "items": fresh})
        ev = MeetingEvent.from_json(ev.to_json())
        raw = path.read_bytes()
        with path.open("ab") as f:
            if raw and not raw.endswith(b"\n"):
                f.write(b"\n")
            f.write((ev.to_json() + "\n").encode("utf-8"))
    rec_dir = run_dir / "reconcile"
    rec_dir.mkdir(exist_ok=True)
    (rec_dir / f"{owner}.json").write_text(json.dumps(
        {"owner": owner, "source": source, "items": valid, "skipped": skipped},
        ensure_ascii=False, indent=2), encoding="utf-8")
    render_outputs(run_dir)
    return {"owner": owner, "applied": fresh, "unchanged": unchanged, "skipped": skipped}


def _summary(res: dict) -> str:
    parts = [f"{res['owner']}: applied {len(res['applied'])}, unchanged {len(res['unchanged'])}, "
             f"skipped {len(res['skipped'])}"]
    parts += [f"  skipped {s['commitment_id']}: {s['reason']}" for s in res["skipped"]]
    return "\n".join(parts)


def apply_file(run_dir: Path, owner: str, file: str) -> int:
    text = sys.stdin.read() if file == "-" else Path(file).read_text(encoding="utf-8")
    ids = [c["id"] for c in owner_commitments(EventLog.load(_events_path(run_dir)), owner)]
    items, problems = parse_reply(text, ids)
    res = apply_reply(run_dir, owner, items, source=f"file:{Path(file).name}" if file != "-" else "stdin")
    print(_summary(res))
    for p in problems:
        print(f"  ignored {p}")
    unanswered = [i for i in ids if i not in {x["commitment_id"] for x in items}]
    if unanswered:
        print(f"  no verdict for: {', '.join(unanswered)}")
    return 1 if problems or res["skipped"] else 0


def _post(url: str, payload: dict, timeout: float = 30.0) -> dict | None:
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"webhook must be http(s): {url}")
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — scheme checked above
        body = resp.read(MAX_REPLY_BYTES + 1)
    if not body.strip():
        return None
    if len(body) > MAX_REPLY_BYTES:
        raise ValueError("webhook reply too large")
    d = json.loads(body)
    return d if isinstance(d, dict) else None


def push(run_dir: Path, prompts: dict[str, str], url: str) -> int:
    run_dir = Path(run_dir)
    events = EventLog.load(_events_path(run_dir)) if _events_path(run_dir).is_file() else []
    failed = 0
    for owner, prompt in prompts.items():
        cmts = owner_commitments(events, owner)
        if not cmts:
            print(f"push {owner}: no commitments, skipped")
            continue
        payload = {"run": run_dir.name, "owner": owner, "prompt": prompt,
                   "commitments": [{"id": c["id"], "text": c["text"], "status": c["status"]} for c in cmts]}
        try:
            reply = _post(url, payload)
        except (OSError, ValueError, urllib.error.URLError) as e:
            print(f"push {owner}: failed: {e}")
            failed += 1
            continue
        items = (reply or {}).get("items")
        if not isinstance(items, list) or not items:
            print(f"push {owner}: sent, no reply to apply")
            continue
        items = [i for i in items if isinstance(i, dict)]
        print("push " + _summary(apply_reply(run_dir, owner, items, source="push")))
    return 1 if failed else 0


def add_arguments(ap) -> None:
    import os
    ap.add_argument("run_dir", nargs="?", help="run directory (or run id under runs/)")
    ap.add_argument("--room", help="use the newest run recorded for this LiveKit room")
    ap.add_argument("--push", metavar="URL", default=os.environ.get("MEETING_FOLLOWUP_WEBHOOK") or None,
                    help="POST each owner's prompt here; apply the JSON reply (env MEETING_FOLLOWUP_WEBHOOK)")
    ap.add_argument("--apply", nargs=2, metavar=("OWNER", "FILE"),
                    help="ingest an owner's reply file ('-' for stdin) into the event log and minutes")


def run(args) -> int:
    from meet.runs import find_run_by_room, resolve_run

    run_dir = None
    if args.run_dir:
        run_dir = resolve_run(args.run_dir)
        if run_dir is None:
            print(f"meet followup: no such run: {args.run_dir}", file=sys.stderr)
            return 2
    elif args.room:
        run_dir = find_run_by_room(args.room)
        if run_dir is None:
            print(f"meet followup: no run found for room {args.room!r}", file=sys.stderr)
            return 2
    else:
        print("meet followup: give a run directory or --room", file=sys.stderr)
        return 2
    try:
        if args.apply:
            return apply_file(run_dir, *args.apply)
        prompts = build_followups(run_dir)
        if args.push:
            return push(run_dir, prompts, args.push)
    except (OSError, ValueError) as e:
        print(f"meet followup: {e}", file=sys.stderr)
        return 2
    for owner, text in prompts.items():
        print(f"----- {owner} -----\n{text}")
    return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="meet followup")
    add_arguments(ap)
    return run(ap.parse_args(argv))
