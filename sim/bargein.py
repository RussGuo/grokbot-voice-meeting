"""从 runs/<dir> 计算插话延迟：模拟人类开口(在房间里收到的 host 音轨首个高能量帧) -> bot 音轨压低/静音。
用法: python sim/bargein.py runs/<dir> [--speech-db -40] [--silence-db -50]"""
import argparse
import json
from collections import defaultdict
from pathlib import Path
from common import HUMAN_ID

ap = argparse.ArgumentParser(); ap.add_argument("run"); ap.add_argument("--speech-db", type=float, default=-40)
ap.add_argument("--silence-db", type=float, default=-50); ap.add_argument("--hold-ms", type=float, default=300)
a = ap.parse_args(); run = Path(a.run)
lv = defaultdict(list)
for l in open(run / "levels.jsonl"): d = json.loads(l); lv[d["id"]].append((d["t_ms"], d["db"]))
# sim_human.jsonl 可能在 run 目录或其上一级（runs/<id>/audio 布局时在 runs/<id>/）
sh = next((p for p in (run / "sim_human.jsonl", run.parent / "sim_human.jsonl") if p.exists()), None)
if sh is None: raise SystemExit(f"找不到 sim_human.jsonl（查过 {run} 和 {run.parent}）")
starts = [json.loads(l) for l in open(sh)]
starts = [s for s in starts if s["kind"] == "speak_start" and s.get("interrupting")]
human = lv.get(HUMAN_ID, []); bots = {k: v for k, v in lv.items() if k != HUMAN_ID}
res = []
for s in starts:
    onset = next((t for t, db in human if t >= s["t_ms"] - 50 and db > a.speech_db), None)
    if onset is None: res.append({"start": s, "error": "人声未在房间里检测到"}); continue
    best = None
    for bid, arr in bots.items():
        active = [t for t, db in arr if onset - 500 <= t <= onset and db > a.silence_db]
        if not active: continue
        # 第一个持续 hold_ms 都低于 silence_db 的时刻
        after = [(t, db) for t, db in arr if t >= onset]
        for i, (t, db) in enumerate(after):
            if db <= a.silence_db and all(d <= a.silence_db for tt, d in after[i:] if tt - t <= a.hold_ms):
                best = {"bot": bid, "stop_ms": round(t - onset)}; break
        # 压低（较开口前中位电平下降 >=6dB）
        pre = sorted(db for t, db in arr if onset - 500 <= t <= onset)
        if pre and best:
            ref = pre[len(pre) // 2]
            duck = next((t for t, db in after if db <= ref - 6), None)
            best["duck_ms"] = round(duck - onset) if duck else None
    res.append({"human_onset_ms": onset, **(best or {"error": "开口时没有 bot 在出声"})})
print(json.dumps(res, ensure_ascii=False, indent=1))
(run / "bargein.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
