"""LiveKit agent 入口（包内模块；`meet worker` 与根目录 main.py 都从这里取 server）：一个 AgentSession，多个 Agent，按编排器令牌调用 session.update_agent。

运行（需要 .env 里的 LIVEKIT_* 等变量；只连 LiveKit Cloud 房间，不在本机暴露任何端口）：
    meet worker dev            # 或  .venv/bin/python main.py dev

已核实的 livekit-agents 1.8.9 API（源码 .venv/.../livekit/agents/voice/）：
- AgentServer / @server.rtc_session / cli.run_app(server)
- AgentSession(vad=, stt=, llm=, tts=, turn_handling={...})；session.start(agent=, room=, room_options=room_io.RoomOptions(...))
- room_io.RoomOptions(participant_identity=..., participant_kinds=...)：RoomIO 只链接用户
- session.update_agent(agent)（同步，内部起任务切换 activity）；新 Agent 默认不带历史，靠 Agent(chat_ctx=...) 注入
- Agent(instructions=, chat_ctx=, llm=, tts=, ...)；Agent.on_enter() / on_user_turn_completed()；raise StopResponse 阻止自动回复
- session.say(text: str | AsyncIterable[str]) → SpeechHandle（add_done_callback、interrupted）；本层自己流式调 LLM、按句切段喂给 say
- session.interrupt(force=True)
- 事件：user_state_changed、user_input_transcribed(is_final)、agent_state_changed
- room.local_participant.publish_data(payload, reliable=True, topic=...)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from functools import partial
from pathlib import Path

from dotenv import load_dotenv

import voice as _voice_pkg  # noqa: F401  # voice/__init__.py 把 voice/ 加进 sys.path，使顶层包 voice_layer 可导入（源码仓库与安装包都一样）

load_dotenv(Path.cwd() / ".env")  # 先读当前目录，不覆盖已有变量
if os.environ.get("MEETING_HOME"):
    load_dotenv(Path(os.environ["MEETING_HOME"]) / ".env")
_checkout = Path(__file__).resolve().parents[1]
if (_checkout / "main.py").is_file():    # 源码仓库里跑；安装包里 parents[1] 是 site-packages，跳过
    load_dotenv(_checkout / ".env")      # 绝不打印任何值

from livekit.agents import AgentServer, JobContext  # noqa: E402

from meet.runtime import join_wait_s, make_run_id, runs_root  # noqa: E402
from orchestrator.bots import load_example  # noqa: E402
from orchestrator.config import llm_config, report  # noqa: E402
from orchestrator.fake_llm import FakeLLM  # noqa: E402
from orchestrator.orchestrator import Orchestrator, monotonic_ms  # noqa: E402
from orchestrator.principal import principal_from_env  # noqa: E402
from orchestrator.scene import load_scene_metadata  # noqa: E402

log = logging.getLogger("voice-meeting")
logging.basicConfig(level=logging.INFO)

# 打断参数（与语音调研对齐）：编排器自己做 duck→判定→interrupt，所以关掉 LiveKit 的自动打断。
# TODO(待验证)：enabled=False 时 discard_audio_if_uninterruptible 必须为 False，否则 bot 说话期间用户的音频
# 会被丢弃、VAD/STT 收不到；另一种做法是保留 LiveKit 自动打断（min_duration=0.25）并把它当作 bargein.confirmed。
TURN_HANDLING = {
    "interruption": {"enabled": False, "discard_audio_if_uninterruptible": False},
    "preemptive_generation": {"enabled": False},
}


def load_voice_stack():
    """语音层归 voice/（火山·豆包）：adapters + LiveKitVoiceLayer。导入失败给清楚提示（单测不 import main.py）。"""
    try:
        from voice import adapters  # type: ignore  # voice/__init__.py 把 voice/ 加进 sys.path
        from voice_layer.livekit_layer import build_session, room_options  # type: ignore
        from orchestrator.live_voice import MeetingVoiceLayer
        return adapters, build_session, room_options, MeetingVoiceLayer
    except Exception as e:  # noqa: BLE001
        log.error("无法导入语音层（%s: %s）。需要 voice/adapters.py 与 voice/voice_layer/livekit_layer.py。",
                  type(e).__name__, e)
        raise


server = AgentServer(host="127.0.0.1", port=0)   # 健康检查端口只绑本机，不对外暴露


async def _accept(req):
    await req.accept(name=os.environ.get("MEETING_AGENT_DISPLAY_NAME", "会议编排"))


def _runtime_meeting(ctx: JobContext):
    """Job metadata (from `meet`) wins. Sample scenarios remain the fallback for the old scripts."""
    job_meta = getattr(getattr(ctx, "job", None), "metadata", None)
    loaded = load_scene_metadata(job_meta)
    if loaded:
        return loaded, "scene"
    scenario = os.environ.get("MEETING_SCENARIO", "sim_v1")
    mode = os.environ.get("MEETING_MODERATOR_MODE") or None
    meeting, briefs = load_example(scenario=scenario, moderator_mode=mode)
    return (meeting, briefs, {}), scenario


def _apply_env_overrides(meeting) -> None:
    lang = os.environ.get("MEETING_LANG")
    if lang in ("zh", "en"):
        meeting.language = lang
    name = (os.environ.get("MEETING_HUMAN_NAME") or "").strip()
    if name:
        meeting.participant(meeting.voice.linked_human_id).display_name = name


@server.rtc_session(agent_name=os.environ.get("LIVEKIT_AGENT_NAME", "voice-meeting"), on_request=_accept)
async def entrypoint(ctx: JobContext):
    log.info("环境变量是否存在：%s", report())
    adapters, build_session, room_options, MeetingVoiceLayer = load_voice_stack()
    room_name = getattr(getattr(getattr(ctx, "job", None), "room", None), "name", "") or ctx.room.name
    run_id = make_run_id(room_name)          # <room>-<时间戳>；MEETING_RUN_ID 原样优先（run_sim.sh 用）
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (meeting, briefs, voices), scenario = _runtime_meeting(ctx)
    if scenario != "scene":
        # Dispatch metadata was empty. Room metadata is a second chance, but only after connect.
        pass
    _apply_env_overrides(meeting)
    meeting.voice.room = ctx.room.name
    human = meeting.voice.linked_human_id
    mod = meeting.moderator.participant_id
    log.info("场景 %s，run_id %s，房间 %s，主持 %s(%s)", scenario, run_id, ctx.room.name, mod, meeting.moderator.mode)

    await ctx.connect()
    if scenario != "scene":
        loaded = load_scene_metadata(getattr(ctx.room, "metadata", None))
        if loaded:
            meeting, briefs, voices = loaded
            _apply_env_overrides(meeting)
            meeting.voice.room = ctx.room.name
            scenario = "scene"
            human = meeting.voice.linked_human_id
            mod = meeting.moderator.participant_id
            log.info("房间 metadata 提供了场景，主持 %s", mod)
    wait_s = join_wait_s()
    log.info("等待 %s 入会（最长 %d 秒，MEETING_JOIN_WAIT_S）", human, int(wait_s))
    try:
        await asyncio.wait_for(ctx.wait_for_participant(identity=human), timeout=wait_s)
    except asyncio.TimeoutError:
        log.warning("等了 %d 秒 %s 仍未入会，放弃本场（NoShow）；链接若稍后才打开，需要重新 `meet` 一场", int(wait_s), human)
        (run_dir / "done.json").write_text(json.dumps({"phase": "NoShow"}), "utf-8")
        ctx.shutdown(reason="no show")
        return
    cfg = adapters.config()
    cfg.human_identity = human
    if voices:
        from voice_layer.config import apply_voices  # type: ignore
        apply_voices(cfg, voices)
    cfg.interruption.framework_enabled = False             # 打断由编排器 + 语音层 HumanVad 判定
    cfg.interruption.discard_audio_if_uninterruptible = False   # 必须 False（语音队友实测）
    session = build_session(cfg, adapters.make_vad(), stt=adapters.make_stt())
    lc = llm_config()
    chat = None
    if lc.provider != "mock":
        from orchestrator.llm import DeepSeekChat
        chat = DeepSeekChat(lc)
    log.info("LLM：%s（model=%s，thinking=disabled）；ASR=%s；TTS=%s", lc.provider, lc.model,
             cfg.resolve_asr_provider(), cfg.resolve_tts_provider())
    voice = MeetingVoiceLayer(cfg, session, vad=adapters.make_vad(), room=ctx.room, human_id=human,
                              planner=FakeLLM(meeting, briefs), chat=chat, moderator_id=mod,
                              language=getattr(meeting, "language", "zh"))
    await session.start(voice.agent_for(mod), room=ctx.room, room_options=room_options(cfg))
    speakers = None
    if os.environ.get("MEETING_MULTI_IDENTITY", "1") != "0":   # 每个 bot 独立参会者 + 独立音轨
        from voice_layer.multi_identity import BotSpeakers, RoutingAudioOutput  # type: ignore
        speakers = BotSpeakers(ctx.room.name)
        await speakers.connect([(p.id, p.display_name) for p in meeting.participants if p.kind == "bot"])
        session.output.audio = RoutingAudioOutput(session.output.audio, speakers,
                                                  lambda: voice.current.bot_id if voice.current else None)
    for p in meeting.participants:                    # 预热各 bot 的 TTS 连接
        if p.kind == "bot":
            voice.agent_for(p.id)
    await asyncio.sleep(1.0)

    # 事件 at = 相对会议开始的 ms；clock.json 给录音对齐（start_ms = wav 首帧墙钟 - t0_epoch_ms）
    t0, t0_epoch = monotonic_ms(), time.time() * 1000
    (run_dir / "clock.json").write_text(json.dumps({"t0_epoch_ms": t0_epoch, "room": ctx.room.name}), "utf-8")
    # 真实会议（meet 的场景）默认不回答本体问题，留给纪要；样例场景（sim）保留 mock。MEETING_PRINCIPAL 显式设置永远优先。
    orch = Orchestrator(meeting, briefs, voice,
                        principal_factory=partial(principal_from_env, default="defer" if scenario == "scene" else "mock"),
                        clock=lambda: monotonic_ms() - t0)
    voice.token_valid = orch.token_valid

    # —— 给网页：领域事件原样转发到 meeting.events；网页按钮经 meeting.control 进来（只认用户）——
    pending_pub: set = set()
    def _pub(topic: str, payload: str) -> None:
        t = asyncio.ensure_future(ctx.room.local_participant.publish_data(payload.encode("utf-8"), reliable=True, topic=topic))
        pending_pub.add(t)
        t.add_done_callback(pending_pub.discard)
    unsub_ev = orch.log.subscribe(lambda e: _pub("meeting.events", e.to_json()))

    def _on_data(packet) -> None:
        if getattr(packet, "topic", "") != "meeting.control":
            return
        sender = packet.participant.identity if packet.participant else ""
        if sender != human:
            log.warning("忽略非用户的控制消息（来自 %s）", sender or "?")
            return
        try:
            action = json.loads(bytes(packet.data).decode("utf-8")).get("action")
        except Exception:  # noqa: BLE001
            return
        if action in ("end", "interrupt") and getattr(orch, "inbox", None) is not None:
            log.info("网页控制：%s", action)
            orch.inbox.put_nowait({"type": f"control.{action}"})
    ctx.room.on("data_received", _on_data)
    stop = asyncio.Event()
    async def _on_shutdown(reason: str) -> None:   # livekit 按 co_argcount 传入关闭原因
        stop.set()
    ctx.add_shutdown_callback(_on_shutdown)

    async def watch_end():
        while not stop.is_set():
            if orch.sm.phase.value == "PostMeeting":
                await asyncio.sleep(3.0)          # 让最后一句播完、录音收尾
                stop.set()
                return
            await asyncio.sleep(0.5)
    watcher = asyncio.create_task(watch_end())
    try:
        await orch.run(stop)      # 内部先 orch.start()（发 roster、主持人开场），再进入事件循环
    finally:
        watcher.cancel()
        unsub_ev()
        from orchestrator.minutes import export, render_md
        b = export(orch.log, run_dir)
        try:
            await ctx.room.local_participant.publish_data(
                json.dumps({"markdown": render_md(b), "actions": b["actions"]}, ensure_ascii=False, default=str).encode("utf-8"),
                reliable=True, topic="meeting.minutes")
            await asyncio.sleep(0.5)
        except Exception as e:  # noqa: BLE001
            log.warning("推送纪要失败：%s", type(e).__name__)
        stats = {"switch": voice.timings, "llm": voice.llm_stats, "marks": voice.marks,
                 "metrics": {k: v for k, v in orch.metrics.items()}}
        (run_dir / "voice_stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1, default=str), "utf-8")
        (run_dir / "done.json").write_text(json.dumps({"phase": orch.sm.phase.value}), "utf-8")
        log.info("已导出 %s", run_dir)
        try:
            if speakers is not None:
                try:
                    session.output.audio.clear_buffer()
                except Exception:
                    pass
                await speakers.aclose()
            await voice.aclose()          # 关闭人声 VAD 与所有 TTS 连接（voice/ 74faae1）
        except Exception as e:  # noqa: BLE001
            log.warning("voice.aclose 失败：%s", type(e).__name__)
        try:
            await orch.principal.aclose()   # 取消还在轮询 inbox 的任务
        except Exception as e:  # noqa: BLE001
            log.warning("principal.aclose 失败：%s", type(e).__name__)
        ctx.shutdown(reason="meeting ended")

