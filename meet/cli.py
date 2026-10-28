"""meet — from "who + topic" to a join link.

    meet --who pm,eng --topic "What ships this month" --agenda "scope;risks"
    meet doctor
    meet worker
    meet demo
    meet followup runs/<id>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv

_SLUG = re.compile(r"[^A-Za-z0-9]+")


def repo_root() -> Path:
    env = os.environ.get("MEETING_HOME")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "main.py").is_file() and (p / "orchestrator").is_dir():
            return p
    return Path.cwd()


def _load_env() -> None:
    root = repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    voice = root / "voice"
    if voice.is_dir() and str(voice) not in sys.path:
        sys.path.insert(0, str(voice))
    load_dotenv(Path.cwd() / ".env")
    load_dotenv(root / ".env")


def _room_name(topic: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    slug = _SLUG.sub("-", topic).strip("-")[:24] or "meet"
    return f"m-{slug}-{secrets.token_hex(2)}"[:64]


def cmd_start(args: argparse.Namespace) -> int:
    _load_env()
    from meet.briefs import list_unknowns, summarize
    from meet.registry import context_from_dir, load_registry, read_context, registry_path
    from orchestrator.config import llm_config
    from orchestrator.i18n import normalize
    from orchestrator.scene import build_scene
    from voice_layer.config import assign_voices

    who = [w.strip() for w in args.who.split(",") if w.strip()]
    if not who:
        print("meet: --who is empty", file=sys.stderr)
        return 2
    topic = (args.topic or "").strip()
    if not topic:
        print("meet: --topic is required", file=sys.stderr)
        return 2
    lang = normalize(args.lang or os.environ.get("MEETING_LANG") or "zh")
    path = registry_path(args.registry)
    try:
        registry = load_registry(path) if path else {}
    except (OSError, ValueError, RuntimeError) as e:
        print(f"meet: registry: {e}", file=sys.stderr)
        return 2
    missing = [b for b in who if b not in registry and not args.context_dir]
    if missing and path is None and not args.context_dir:
        print("meet: no bots.yaml. Copy bots.example.yaml and fill in context, or pass --context-dir.", file=sys.stderr)
        return 2
    agenda_titles = [t.strip() for t in (args.agenda or "").split(";") if t.strip()] or [topic]
    # Agenda ids are assigned inside build_scene; the summarizer needs the same ids.
    agenda = [{"id": f"ag-{i}", "title": t} for i, t in enumerate(agenda_titles, 1)]
    cfg = llm_config()
    chat = None
    model = "heuristic"
    if cfg.provider != "mock" and not args.no_llm:
        from orchestrator.llm import make_chat
        chat = make_chat(cfg)
        model = cfg.model
    briefs = {}
    unknown_notes = []
    for bid in who:
        meta = dict(registry.get(bid) or {"display_name": bid})
        meta["id"] = bid
        text = context_from_dir(bid, args.context_dir, read_context(meta))
        brief = summarize(bot_id=bid, context=text, topic=topic, agenda=agenda, lang=lang, chat=chat, model=model)
        briefs[bid] = brief
        unknowns = list_unknowns(brief)
        if unknowns:
            unknown_notes.append(f"{bid}: {', '.join(unknowns)}")
    mod_meta = registry.get("__moderator__") or {}
    preferred = {bid: registry[bid]["voice"] for bid in who if registry.get(bid, {}).get("voice")}
    mod_id = args.moderator or mod_meta.get("id") or os.environ.get("MEETING_MODERATOR_ID") or "moderator"
    if mod_meta.get("voice"):
        preferred[mod_id] = mod_meta["voice"]
    try:
        voices = assign_voices([mod_id, *who], preferred=preferred or None)
    except ValueError as e:
        print(f"meet: voices: {e}", file=sys.stderr)
        return 2
    scene = build_scene(
        who=who, topic=topic, agenda=agenda_titles, briefs=briefs, voices=voices, registry=registry,
        language=lang, moderator_id=mod_id, moderator_name=mod_meta.get("display_name"),
    )
    room = _room_name(topic, args.room)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", room):
        print("meet: room name must match [A-Za-z0-9_-]{1,64}", file=sys.stderr)
        return 2
    from scripts.mklink import make_link
    try:
        link_file = asyncio.run(make_link(room, scene, hours=args.hours, dispatch=not args.no_dispatch))
    except KeyError as e:
        print(f"meet: missing environment variable {e}. Copy .env.example and run `meet doctor`.", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 — surface the provider error without a traceback by default
        print(f"meet: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    urls = [ln.strip() for ln in link_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    payload = {
        "room": room,
        "join_url": urls[0] if urls else "",
        "fallback_url": urls[1] if len(urls) > 1 else "",
        "link_file": str(link_file),
        "language": lang,
        "who": who,
        "moderator": mod_id,
        "voices": voices,
        "unknowns": unknown_notes,
        "worker": "meet worker",
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"room: {room}")
        print(f"join: {payload['join_url']}")
        if payload["fallback_url"]:
            print(f"also: {payload['fallback_url']}")
        if unknown_notes:
            print("unknowns (not invented):")
            for line in unknown_notes:
                print(f"  {line}")
        print("The worker must already be running: meet worker")
        print(f"link file: {link_file}")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    _load_env()
    root = repo_root()
    os.chdir(root)
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "voice"))
    import main
    from livekit.agents import cli
    sys.argv = ["meet-worker", args.mode]
    cli.run_app(main.server)
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    _load_env()
    from orchestrator.sim import main as sim_main
    argv = ["--scenario", args.scenario]
    if args.run_dir:
        argv += ["--run-dir", args.run_dir]
    if args.lines:
        argv += ["--lines", args.lines]
    sim_main(argv)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="meet", description="Start a multi-bot voice meeting and print a join link.")
    ap.add_argument("--who", help="comma-separated bot ids from the registry")
    ap.add_argument("--topic")
    ap.add_argument("--agenda", help='agenda titles separated by ";"')
    ap.add_argument("--registry", help="bots.yaml path")
    ap.add_argument("--context-dir", help="directory of <bot-id>.md context files exported by a Grok Bot")
    ap.add_argument("--lang", choices=["zh", "en"])
    ap.add_argument("--room")
    ap.add_argument("--moderator", help="moderator participant id (default: moderator)")
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--no-dispatch", action="store_true")
    ap.add_argument("--no-llm", action="store_true", help="skip brief summarization even if an API key is set")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    doc = sub.add_parser("doctor", help="check keys and connectivity without printing secrets")
    doc.add_argument("--offline", action="store_true")
    doc.add_argument("--strict", action="store_true")
    worker = sub.add_parser("worker", help="long-running LiveKit agent; reads the scene from job metadata")
    worker.add_argument("mode", nargs="?", default="dev", choices=["dev", "start", "download-files"])
    demo = sub.add_parser("demo", help="simulated meeting, no API keys")
    demo.add_argument("--scenario", default="example", choices=["example", "sim_v1"])
    demo.add_argument("--run-dir")
    demo.add_argument("--lines")
    follow = sub.add_parser("followup", help="write prompts so the real bots can confirm commitments")
    follow.add_argument("run_dir")
    return ap


def main(argv=None) -> int:
    # Load .env before subcommands read the environment, including `meet doctor`.
    _load_env()
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd == "doctor":
        from meet.doctor import main as doctor_main
        extra = []
        if args.offline:
            extra.append("--offline")
        if args.strict:
            extra.append("--strict")
        return doctor_main(extra)
    if args.cmd == "worker":
        return cmd_worker(args)
    if args.cmd == "demo":
        return cmd_demo(args)
    if args.cmd == "followup":
        from meet.followup import main as follow_main
        return follow_main([args.run_dir])
    if args.who or args.topic:
        return cmd_start(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
