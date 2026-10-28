"""文本版会议模拟器：用 FakeVoiceLayer + fake LLM + 模拟本体，跑一场脚本化的 3 分钟会议并打印事件流。

运行：python -m orchestrator.sim  [--jsonl runs/sim.jsonl] [--all]
不依赖任何 key 或网络。
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .driver import SimDriver
from .events import project

QUIET = {"SpeakerPublished", "PhaseChanged", "InviteSent", "InviteAnswered"}


def fmt(e, orch) -> str:
    d = e.data
    t = f"{e.at / 1000:7.2f}s #{e.seq:<4}"
    if e.type == "UtteranceFinal":
        u = d["utterance"]
        if u.get("interrupted"):
            return f"{t} ✂  {orch.name(u['speaker_id'])}：{u['text']}"
        tag = f" [{u.get('provenance') or 'human'}{'/conf=' + str(u['confidence']) if u.get('confidence') is not None else ''}]"
        return f"{t} 🗣  {orch.name(u['speaker_id'])}{tag}：{u['text']}"
    if e.type in ("FloorGranted", "FloorReleased", "FloorRevoked"):
        k = d["token"]
        extra = f" reason={d.get('reason')}" if d.get("reason") else ""
        return f"{t} {e.type:<15} {k['id']} → {k['holder_id']} ({k['source']}{'/' + k['intent'] if k.get('intent') else ''}){extra}"
    if e.type in ("HandRaised", "HandWithdrawn", "HandExpired"):
        h = d["hand"]
        return f"{t} {e.type:<15} {h['participant_id']} S={h['score']['total']} 点名={h['score']['addressed']}"
    if e.type.startswith("Principal"):
        q = d["query"]
        return f"{t} {e.type:<15} {q['id']} {q['asked_by']} status={q['status']} 问「{q['question'][:24]}」"
    if e.type == "SpeakerPublished":
        return f"{t} 📡 {d['topic']} {d['payload']}"
    if e.type == "PhaseChanged":
        return f"{t} [{d['layer']}] {d['from']} → {d['to']}"
    if e.type == "BriefSubmitted":
        return f"{t} BriefSubmitted   {d['brief']['participant_id']}"
    short = {k: v for k, v in d.items() if k not in ("token", "brief")}
    return f"{t} {e.type:<15} {short}"


async def scripted(driver: SimDriver) -> None:
    d = driver
    await d.start()
    # —— 议程 1：离线同步 ——（主持人开场约 10s）
    d.say(11_000, "离线同步的冲突率现在怎么样？工程分身说说。")
    d.blip(30_000)                                        # 产品分身发言时一声“嗯”：duck → 恢复
    d.say(38_000, "产品分身，离线同步的预算能批多少？")       # mustAsk → 慢路径问本体
    d.say(52_000, "工程分身，能不能下周出技术评估？")         # canCommit → binding 承诺
    d.say(62_000, "工程分身，能不能做原型？顺便说下离线同步的工期。")
    d.say(68_000, "等一下，工程分身先说工期。", dur_ms=1500)   # 真打断：duck → confirmed
    # 议程 1 timebox 90s → 决策 → 议程 2；pq-1 的本体答案约 95s 回来，跨议程插回 + 主持人回溯
    d.say(125_000, "AI 摘要的灰度你们怎么看？")
    d.say(131_000, "工程分身，推理成本能降多少？")
    d.say(146_000, "好，摘要先按 1% 灰度，产品分身整理需求文档。")
    # 最后一项冷场：主持人先问「是否结束」（约 170s），用户确认后才结束
    d.say(177_000, "好，结束吧。")
    await d.run_until(185_000)
    if d.orch.sm.phase.value == "InAgenda":
        await d.run_until(200_000)
        if d.orch.sm.phase.value == "InAgenda" and d.orch.token is None:
            await d.orch.advance_agenda()
    cmt = next(iter(d.orch.commitments), None)
    if cmt:
        await d.orch.reconcile("eng_bot", [{"commitment_id": cmt, "verdict": "confirm"}])


async def run_lines(driver: SimDriver, path: Path, t_end: int | None = None) -> None:
    """按 human_lines.json 注入用户台词（文本模拟，不涉及音频）。"""
    import json
    spec = json.loads(Path(path).read_text("utf-8"))
    await driver.start()
    for ln in spec["lines"]:
        driver.say(ln["t_ms"], ln["text"], dur_ms=ln.get("dur_ms"))
    await driver.run_until(t_end or spec.get("meeting_max_ms", 300_000))
    o = driver.orch
    if o.sm.phase.value == "InAgenda" and o.token is None:   # 到总时长兜底收尾
        await o.advance_agenda()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", type=Path)
    ap.add_argument("--all", action="store_true", help="连 SpeakerPublished/PhaseChanged 一起打印")
    ap.add_argument("--scenario", choices=["example", "sim_v1"], default="example")
    ap.add_argument("--lines", type=Path, help="用户台词脚本 JSON（如 runs/script/human_lines.json），给定时替代内置脚本")
    ap.add_argument("--run-dir", type=Path, help="导出 events.jsonl、minutes.md、actions/ 到该目录")
    ap.add_argument("--moderator", choices=["chiefOfStaff", "neutral"], default=None,
                    help="主持模式，默认用示例文件的 chiefOfStaff（v0 默认）")
    a = ap.parse_args(argv)
    d = SimDriver(principal_delay_ms=45_000 if not a.lines else 20_000, moderator_mode=a.moderator,
                  scenario=a.scenario,
                  principal_answers={"principal-pm": "离线同步本期预算按示例口径可批 2 人月（虚构），超出需用户批。",
                                     "principal-growth": "推广预算按示例口径本月可用 3000 元（虚构），超出需用户批。"})
    asyncio.run(run_lines(d, a.lines) if a.lines else scripted(d))
    for e in d.orch.log:
        if a.all or e.type not in QUIET:
            print(fmt(e, d.orch))
    v = project(d.orch.log)
    print("\n—— 汇总（由事件流重放得到）——")
    print(f"事件数 {v['last_seq']}，发言 {len(v['utterances'])}，打断 {v['interruptions']}，duck {v['ducks']}/恢复 {v['restores']}，"
          f"agent 切换 {v['agent_switches']}，回溯 {v['recaps']}，丢弃过期输出 {v['dropped_outputs']}")
    print(f"承诺 {v['commitments']}；本体问答 {v['queries']}；决策 {v['decisions']}")
    print(f"最终阶段 {d.orch.sm.phase.value}；speaker 消息 {sum(1 for t, _ in d.voice.published if t == 'meeting.speaker')} 条，"
          f"roster 消息 {sum(1 for t, _ in d.voice.published if t == 'meeting.roster')} 条")
    if a.run_dir:
        from .minutes import export
        export(d.orch.log, a.run_dir)
        print(f"已导出 {a.run_dir}/events.jsonl、minutes.md、actions/")
    if a.jsonl:
        a.jsonl.parent.mkdir(parents=True, exist_ok=True)
        d.orch.log.dump(a.jsonl)
        print(f"事件日志已写入 {a.jsonl}")


if __name__ == "__main__":
    main()
