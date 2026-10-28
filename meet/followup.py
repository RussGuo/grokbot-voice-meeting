"""After the meeting: turn minutes and action files into prompts for the real bots."""
from __future__ import annotations

import json
from pathlib import Path


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


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="meet followup")
    ap.add_argument("run_dir", type=Path)
    args = ap.parse_args(argv)
    prompts = build_followups(args.run_dir)
    for owner, text in prompts.items():
        print(f"----- {owner} -----\n{text}")
    return 0
