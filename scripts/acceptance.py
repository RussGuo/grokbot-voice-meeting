#!/usr/bin/env python3
"""v1 端到端验收：读 runs/<run_id>/ 的事件日志与各音轨 wav，逐项输出 PASS/FAIL。

目录约定（录音由语音队友产出，这里只读）：
  runs/<run_id>/events.jsonl              编排器事件日志（EventLog.dump）
  runs/<run_id>/minutes.md                纪要
  runs/<run_id>/actions/<owner_id>.json   按 bot 拆分的行动项
  runs/<run_id>/audio/<participant>.wav   每个参与者一条音轨（16-bit PCM）
  runs/<run_id>/audio/manifest.json       可选：{"tracks": {"<pid>": {"file": "x.wav", "start_ms": 0}}}
                                          start_ms = 该 wav 第 0 帧对应的会议时间（事件 at），默认 0

6 条标准：
  1 开场主持人后 3 个 bot 依次发言，且 tts_voice 互不相同
  2 至少一次 moderatorCue 授予和一次 handRaise 授予
  3 人类插话：事件日志法与音频能量法分别算「人声起点 → bot 音轨静音」的时间，都 ≤ 阈值；之后 HumanDirective 被执行
  4 PrincipalAsked → PrincipalAnswered → pendingAnswer 优先举手插回
  5 生成纪要和按 bot 拆分的行动项文件
  6 录音与事件日志存在

用法：python scripts/acceptance.py runs/<run_id> [--max-stop-ms 700] [--json]
退出码：全部 PASS 为 0，否则 1。
"""
from __future__ import annotations

import argparse
import array
import json
import math
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path

FRAME_MS = 20


@dataclass
class Check:
    no: int
    name: str
    ok: bool = False
    detail: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def line(self) -> str:
        return f"[{'PASS' if self.ok else 'FAIL'}] {self.no}. {self.name}" + "".join(f"\n       - {d}" for d in self.detail)


# —— 读取 ——
def load_events(run: Path) -> list[dict]:
    out = []
    for ln in (run / "events.jsonl").read_text("utf-8").splitlines():
        if ln.strip():
            out.append(json.loads(ln))
    return out


def meeting_meta(events: list[dict]) -> dict:
    return next((e["data"].get("payload", {}) for e in events if e["type"] == "MeetingCreated"), {})


class Track:
    """One wav plus when its first sample sits on the meeting clock.

    `shared` marks the worker's own participant. With per-bot identities that track is silence
    (RoutingAudioOutput keeps the agent session alive with silent frames). Measuring it reports
    a stop time that has nothing to do with the bot who was actually talking.

    A plain class, not a dataclass: acceptance.py is loaded by path in tests, and dataclass
    looks up the module in sys.modules.
    """

    def __init__(self, path: Path, start_ms: int = 0, shared: bool = False):
        self.path = path
        self.start_ms = start_ms
        self.shared = shared

    def __iter__(self):
        yield self.path
        yield self.start_ms

    def __getitem__(self, i: int):
        return (self.path, self.start_ms)[i]


def tracks(run: Path) -> dict[str, Track]:
    """participant_id → Track. A bare wav is also indexed by its filename stem."""
    adir = run / "audio"
    res: dict[str, Track] = {}
    if not adir.is_dir():
        return res
    man = {}
    if (adir / "manifest.json").exists():
        man = json.loads((adir / "manifest.json").read_text("utf-8")).get("tracks", {})
    for pid, t in man.items():
        res[pid] = Track(adir / t["file"], int(t.get("start_ms", 0)), shared=bool(t.get("shared_identity")))
    for f in adir.glob("*.wav"):
        res.setdefault(f.stem, Track(f, 0, shared=False))
    return res


def window_peak_db(track: Track, around_ms: int, radius_ms: int = 2_000) -> float:
    db = frame_db(track.path)
    i0 = max(0, (around_ms - radius_ms - track.start_ms) // FRAME_MS)
    i1 = min(len(db), (around_ms + radius_ms - track.start_ms) // FRAME_MS + 1)
    if i0 >= i1 or not db:
        return -120.0
    return max(db[i0:i1])


def pick_bot_track(trk: dict[str, Track], bot: str, human: str | None, around_ms: int) -> Track | None:
    """Prefer a wav that belongs to `bot` and actually contains speech.

    Manifest entries flagged shared_identity point at the worker's silent bus. Those lose to any
    dedicated bot file (identity or filename contains the bot id).
    """
    dedicated: list[Track] = []
    shared: list[Track] = []
    seen: set[Path] = set()
    for key, t in trk.items():
        if human and key == human:
            continue
        if t.path in seen:
            continue
        seen.add(t.path)
        name_hit = key == bot or bot in t.path.name
        if t.shared and not name_hit:
            shared.append(t)
            continue
        if t.shared and name_hit:
            shared.append(t)
            continue
        if name_hit:
            dedicated.append(t)
    pool = dedicated or shared
    if not pool:
        pool = [t for k, t in trk.items() if k != human]
    best: Track | None = None
    best_db = -1e9
    for t in pool:
        try:
            db = window_peak_db(t, around_ms)
        except (OSError, ValueError, wave.Error):
            continue
        # A dedicated track wins a tie against the silent agent bus.
        score = db + (5.0 if not t.shared else 0.0)
        if score > best_db:
            best, best_db = t, score
    return best


def frame_db(path: Path) -> list[float]:
    """每 20ms 一帧的 RMS（dBFS）。只支持 16-bit PCM；多声道取均值。"""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{path.name}: 只支持 16-bit PCM")
        ch, sr = w.getnchannels(), w.getframerate()
        a = array.array("h", w.readframes(w.getnframes()))
    if sys.byteorder == "big":
        a.byteswap()
    n = max(1, sr * FRAME_MS // 1000) * ch
    try:                                   # 有 numpy 就向量化（48k 长录音纯 Python 太慢）
        import numpy as np
        x = np.frombuffer(a.tobytes(), dtype=np.int16).astype(np.float64)
        m = len(x) // n
        if m == 0:
            return []
        rms = np.sqrt((x[:m * n].reshape(m, n) ** 2).mean(axis=1)) / 32768.0
        return [20 * math.log10(r) if r > 0 else -120.0 for r in rms]
    except ImportError:
        pass
    out = []
    for i in range(0, len(a) - n + 1, n):
        seg = a[i:i + n]
        rms = math.sqrt(sum(x * x for x in seg) / len(seg)) / 32768.0
        out.append(20 * math.log10(rms) if rms > 0 else -120.0)
    return out


def wav_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as w:
        return int(w.getnframes() * 1000 / w.getframerate())


def audio_onset(db: list[float], start_ms: int, from_ms: int, to_ms: int, thr: float, min_frames: int = 3):
    """[from,to] 内第一个连续 min_frames 帧高于 thr 的起点（会议时间 ms）。"""
    i0 = max(0, (from_ms - start_ms) // FRAME_MS)
    i1 = min(len(db), (to_ms - start_ms) // FRAME_MS + 1)
    run = 0
    for i in range(i0, i1):
        run = run + 1 if db[i] > thr else 0
        if run >= min_frames:
            return start_ms + (i - min_frames + 1) * FRAME_MS
    return None


def audio_silence(db: list[float], start_ms: int, from_ms: int, thr: float, hold_ms: int = 200, limit_ms: int = 10_000):
    """from 之后 bot 音轨第一次连续 hold_ms 低于 thr 的起点（会议时间 ms）。"""
    need = hold_ms // FRAME_MS
    i0 = max(0, (from_ms - start_ms) // FRAME_MS)
    i1 = min(len(db), (from_ms + limit_ms - start_ms) // FRAME_MS)
    run = 0
    for i in range(i0, i1):
        run = run + 1 if db[i] <= thr else 0
        if run >= need:
            return start_ms + (i - need + 1) * FRAME_MS
    return None


# —— 6 条标准 ——
def c1_opening(ev: list[dict]) -> Check:
    c = Check(1, "开场：主持人后 3 个 bot 依次发言，tts_voice 互不相同")
    meta = meeting_meta(ev)
    parts = {p["id"]: p for p in meta.get("participants", [])}
    mod = meta.get("moderator", {}).get("participant_id")
    members = [p for p, v in parts.items() if v.get("kind") == "bot" and p != mod]
    order = []
    for e in ev:
        if e["type"] == "UtteranceFinal":
            sp = e["data"]["utterance"]["speaker_id"]
            if parts.get(sp, {}).get("kind") == "bot" and sp not in order:
                order.append(sp)
        if len(order) >= 1 + len(members):
            break
    voices: dict[str, str] = {}
    for e in ev:
        if e["type"] == "AgentSwitched" and e["data"].get("tts_voice"):
            voices.setdefault(e["data"]["to"], e["data"]["tts_voice"])
    for p, v in parts.items():
        if v.get("tts_voice"):
            voices.setdefault(p, v["tts_voice"])
    c.detail.append("开场发言顺序：" + " → ".join(order))
    first_ok = bool(order) and order[0] == mod
    bots_ok = len(members) >= 3 and set(order[1:1 + len(members)]) == set(members)
    vs = [voices.get(p) for p in [mod] + members]
    voice_ok = all(vs) and len(set(vs)) == len(vs)
    c.detail.append(f"tts_voice：{dict(zip([mod] + members, vs))}")
    if not first_ok:
        c.detail.append("第一个发言的不是主持人")
    if not bots_ok:
        c.detail.append(f"主持人之后应依次出现 {len(members)} 个成员 bot（需 ≥3）")
    if not voice_ok:
        c.detail.append("tts_voice 缺失或有重复")
    c.ok = first_ok and bots_ok and voice_ok
    return c


def c2_grants(ev: list[dict], run: Path | None = None) -> Check:
    c = Check(2, "至少一次 moderatorCue 授予和一次 handRaise 授予")
    src = [e["data"]["token"]["source"] for e in ev if e["type"] == "FloorGranted"]
    n_cue, n_hand = src.count("moderatorCue"), src.count("handRaise")
    c.metrics = {"moderatorCue": n_cue, "handRaise": n_hand}
    c.detail.append(f"moderatorCue={n_cue}，handRaise={n_hand}")
    md_ok = True
    if run is not None:
        md = Path(run) / "minutes.md"
        txt = md.read_text("utf-8") if md.exists() else ""
        md_ok = n_hand == 0 or ("【举手】" in txt and "## 发言秩序" in txt)
        c.detail.append(f"纪要里可见举手发言：{'是' if md_ok else '否'}")
    c.ok = n_cue >= 1 and n_hand >= 1 and md_ok
    return c


def c3_bargein(ev: list[dict], trk: dict, max_stop_ms: int, thr_db: float) -> Check:
    c = Check(3, f"人类插话：人声起点 → bot 静音 ≤ {max_stop_ms}ms（日志 + 音频），之后 HumanDirective 被执行")
    meta = meeting_meta(ev)
    human = meta.get("linked_human_id")
    ints = [e for e in ev if e["type"] == "Interrupted" and e["data"].get("by") == human]
    if not ints:
        c.detail.append("事件日志里没有人类打断（Interrupted by 人类）")
        return c
    vad = [e["at"] for e in ev if e["type"] == "HumanVad" and e["data"].get("stage") == "start"]
    dirs = [e for e in ev if e["type"] == "HumanDirective"]
    best = None
    for it in ints:
        bot, t_stop = it["data"]["speakerId"], it["at"]
        onset = max((t for t in vad if t <= t_stop), default=None)
        if onset is None:     # 兼容旧日志：用 Ducked 时刻作为人声起点
            onset = max((e["at"] for e in ev if e["type"] == "Ducked" and e["at"] <= t_stop), default=None)
        executed = next((d for d in dirs if d["data"].get("stage") == "executed" and d["at"] >= t_stop), None)
        best = best or (it, bot, onset, executed)
        if executed:
            best = (it, bot, onset, executed)
            break
    it, bot, onset, executed = best
    t_stop = it["at"]
    log_ms = None if onset is None else t_stop - onset
    c.metrics["log_ms"] = log_ms
    c.detail.append(f"打断对象 {bot}；事件日志法：人声起点 {onset}ms → bot 停 {t_stop}ms，用时 {log_ms}ms")
    log_ok = log_ms is not None and 0 <= log_ms <= max_stop_ms

    audio_ok, audio_ms = False, None
    bot_track = pick_bot_track(trk, bot, human, t_stop)
    if human in trk and bot_track is not None:
        hp, hs = trk[human].path, trk[human].start_ms
        bp, bs = bot_track.path, bot_track.start_ms
        try:
            hdb, bdb = frame_db(hp), frame_db(bp)
            a_on = audio_onset(hdb, hs, (onset or t_stop) - 1500, t_stop + 500, thr_db)
            a_off = audio_silence(bdb, bs, a_on, thr_db, hold_ms=1000) if a_on is not None else None   # 1s：避开句中自然停顿
            if a_on is not None and a_off is not None:
                audio_ms = a_off - a_on
                audio_ok = 0 <= audio_ms <= max_stop_ms
            c.detail.append(f"音频能量法（阈值 {thr_db}dBFS，音轨 {bp.name}）：人声起点 {a_on}ms → bot 音轨静音 {a_off}ms，用时 {audio_ms}ms")
        except (OSError, ValueError, wave.Error) as exc:
            c.detail.append(f"音频读取失败：{exc}")
    else:
        c.detail.append(f"缺少音轨：需要 audio/{human}.wav 和 {bot} 的音轨")
    c.metrics["audio_ms"] = audio_ms

    dir_ok = False
    if executed:
        tgt, tok = executed["data"].get("target"), executed["data"].get("token_id")
        granted = any(e["type"] == "FloorGranted" and e["data"]["token"]["id"] == tok
                      and e["data"]["token"]["holder_id"] == tgt for e in ev)
        spoke = any(e["type"] == "UtteranceFinal" and e["data"]["utterance"].get("token_id") == tok
                    and not e["data"]["utterance"].get("interrupted") for e in ev)
        dir_ok = granted and spoke
        c.detail.append(f"HumanDirective → {tgt}（{tok}）：授予={granted}，完整发言={spoke}")
    else:
        c.detail.append("打断之后没有 HumanDirective(stage=executed)")
    c.ok = log_ok and audio_ok and dir_ok
    return c


def c4_principal(ev: list[dict]) -> Check:
    c = Check(4, "PrincipalAsked → PrincipalAnswered → pendingAnswer 优先举手插回")
    asked = [e for e in ev if e["type"] == "PrincipalAsked"]
    if not asked:
        c.detail.append("没有 PrincipalAsked")
        return c
    for a in asked:
        q = a["data"]["query"]
        qid, who = q["id"], q["asked_by"]
        ans = next((e for e in ev if e["type"] == "PrincipalAnswered" and e["data"]["query"]["id"] == qid
                    and e["data"]["query"]["status"] == "answered"), None)
        if not ans:
            c.detail.append(f"{qid}（{who}）：没有收到 PrincipalAnswered")
            continue
        after = [e for e in ev if e["seq"] > ans["seq"] and e["type"] == "FloorGranted"]
        bot_grants = [e for e in after if e["data"]["token"]["source"] != "human"]
        first = bot_grants[0] if bot_grants else None
        pend = first is not None and first["data"]["token"]["source"] == "pendingAnswer" \
            and first["data"]["token"]["holder_id"] == who
        delivered = any(e["type"] == "PrincipalAnswered" and e["data"]["query"]["id"] == qid
                        and e["data"]["query"]["status"] == "deliveredInMeeting" for e in ev)
        rt = ans["at"] - a["at"]
        c.detail.append(f"{qid}（{who}）：问 {a['at']}ms → 答 {ans['at']}ms（往返 {rt}ms）；"
                        f"答后首个 bot 授予={first['data']['token']['source'] + '/' + first['data']['token']['holder_id'] if first else '无'}；"
                        f"会上送达={delivered}")
        if pend and delivered:
            c.ok = True
            c.metrics = {"query_id": qid, "round_trip_ms": rt, "reinsert_ms": first["at"] - ans["at"]}
            break
    return c


def c5_outputs(run: Path, ev: list[dict]) -> Check:
    c = Check(5, "生成纪要和按 bot 拆分的行动项文件")
    meta = meeting_meta(ev)
    bots = {p["id"] for p in meta.get("participants", []) if p.get("kind") == "bot"}
    md = run / "minutes.md"
    md_ok = md.exists() and md.stat().st_size > 0
    c.detail.append(f"minutes.md：{'有' if md_ok else '缺失或为空'}")
    files = sorted((run / "actions").glob("*.json")) if (run / "actions").is_dir() else []
    owners_needed = {e["data"]["commitment"]["owner_id"] for e in ev if e["type"] == "CommitmentMade"}
    act_ok = bool(files)
    for f in files:
        try:
            items = json.loads(f.read_text("utf-8"))
        except json.JSONDecodeError:
            c.detail.append(f"{f.name}：不是合法 JSON")
            act_ok = False
            continue
        good = f.stem in bots and isinstance(items, list) and items and all(i.get("owner_id") == f.stem for i in items)
        act_ok &= bool(good)
        c.detail.append(f"actions/{f.name}：{len(items) if isinstance(items, list) else '?'} 项{'' if good else '（owner 不符或非 bot）'}")
    missing = owners_needed - {f.stem for f in files}
    if missing:
        c.detail.append(f"这些 owner 有承诺但没有行动项文件：{sorted(missing)}")
    if not files:
        c.detail.append("actions/ 下没有行动项文件")
    c.ok = md_ok and act_ok and not missing
    return c


def c6_artifacts(run: Path, ev: list[dict] | None, trk: dict) -> Check:
    c = Check(6, "录音与事件日志存在")
    ev_ok = ev is not None and len(ev) > 0
    ended = bool(ev) and any(e["type"] == "MeetingEnded" for e in ev)
    c.detail.append(f"events.jsonl：{len(ev) if ev else 0} 条事件，MeetingEnded={ended}")
    need = set()
    if ev:
        meta = meeting_meta(ev)
        need = {meta.get("linked_human_id")} | {e["data"]["utterance"]["speaker_id"] for e in ev if e["type"] == "UtteranceFinal"}
        need.discard(None)
    au_ok = bool(need)
    for pid in sorted(need):
        if pid not in trk or not trk[pid][0].exists():
            c.detail.append(f"缺少音轨 audio/{pid}.wav")
            au_ok = False
            continue
        try:
            ms = wav_ms(trk[pid][0])
        except (OSError, wave.Error) as exc:
            c.detail.append(f"audio/{pid}.wav 读取失败：{exc}")
            au_ok = False
            continue
        if ms <= 0:
            au_ok = False
        c.detail.append(f"audio/{trk[pid][0].name}：{ms / 1000:.1f}s")
    end_ok = True
    if ev:
        cmd = [e for e in ev if e["type"] == "EndConfirm" and e["data"].get("stage") == "human_command"]
        end_ev = next((e for e in ev if e["type"] == "MeetingEnded"), None)
        if cmd and end_ev:
            lag = end_ev["at"] - cmd[0]["at"]
            after = [e for e in ev if e["type"] == "UtteranceFinal" and e["at"] > cmd[0]["at"]
                     and e["data"]["utterance"]["speaker_id"] != meeting_meta(ev).get("linked_human_id")]
            end_ok = lag <= 5_000 and not after
            c.detail.append(f"用户结束指令 → MeetingEnded：{lag}ms；指令后 bot 发言 {len(after)} 句")
        # 主持人同一句程序性话术在同一议程项内不应重复超过 2 次
        seen: dict[tuple, int] = {}
        for e in ev:
            if e["type"] == "UtteranceFinal":
                u = e["data"]["utterance"]
                k = (u["agenda_item_id"], u["speaker_id"], u["text"])
                seen[k] = seen.get(k, 0) + 1
        rep_max = max(seen.values(), default=0)
        c.detail.append(f"同一议程项内同一句话最多重复 {rep_max} 次")
        end_ok &= rep_max <= 2
    c.ok = ev_ok and ended and au_ok and end_ok
    return c


def run_checks(run: Path, max_stop_ms: int = 700, thr_db: float = -40.0) -> list[Check]:
    run = Path(run)
    trk = tracks(run)
    try:
        ev = load_events(run)
    except (OSError, json.JSONDecodeError) as exc:
        fails = [Check(i, n, False, [f"事件日志不可读：{exc}"]) for i, n in
                 [(1, "开场"), (2, "授予来源"), (3, "人类插话"), (4, "本体慢路径"), (5, "纪要与行动项")]]
        return fails + [c6_artifacts(run, None, trk)]
    return [c1_opening(ev), c2_grants(ev, run), c3_bargein(ev, trk, max_stop_ms, thr_db),
            c4_principal(ev), c5_outputs(run, ev), c6_artifacts(run, ev, trk)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--max-stop-ms", type=int, default=700, help="人声起点到 bot 静音的上限（默认 700ms，设计目标）")
    ap.add_argument("--silence-db", type=float, default=-40.0, help="音频能量阈值 dBFS")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    checks = run_checks(a.run_dir, a.max_stop_ms, a.silence_db)
    if a.json:
        print(json.dumps([{"no": c.no, "name": c.name, "ok": c.ok, "detail": c.detail, "metrics": c.metrics}
                          for c in checks], ensure_ascii=False, indent=2))
    else:
        print(f"验收 {a.run_dir}")
        for c in checks:
            print(c.line())
        print(f"合计 {sum(c.ok for c in checks)}/{len(checks)} PASS")
    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
