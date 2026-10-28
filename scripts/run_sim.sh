#!/usr/bin/env bash
# 一键跑一场 sim_v1 端到端模拟：worker → recorder → sim_human → 等会议结束 → 收尾 → room_cleanup → 对齐录音 → acceptance
# 用法：scripts/run_sim.sh [run_id]      （一次只跑一场；不读不打印 .env 的值）
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
RUN_ID=${1:-e2e-$(date +%Y%m%d-%H%M%S)}
ROOM=${SIM_ROOM:-sim-meeting-01}
MAX_S=${SIM_MAX_S:-420}
PY=$ROOT/.venv/bin/python           # 编排器 worker（livekit-agents）
SIMPY=$ROOT/web/.venv/bin/python    # sim/ 工具（livekit rtc + numpy）
RUN=$ROOT/runs/$RUN_ID
mkdir -p "$RUN/audio"
echo "run_id=$RUN_ID room=$ROOM"

if pgrep -f "main.py start" >/dev/null; then echo "已有 worker 在跑，拒绝并行"; exit 2; fi
$SIMPY sim/room_cleanup.py --room "$ROOM" >"$RUN/cleanup_before.log" 2>&1

MEETING_RUN_ID=$RUN_ID MEETING_SCENARIO=sim_v1 $PY main.py start >"$RUN/worker.log" 2>&1 &
WPID=$!
for i in $(seq 1 60); do grep -q "registered worker" "$RUN/worker.log" && break; sleep 1; done
grep -q "registered worker" "$RUN/worker.log" || { echo "worker 未注册"; tail -20 "$RUN/worker.log"; kill -INT $WPID; exit 3; }
echo "worker 已注册"

(cd sim && $SIMPY recorder.py --room "$ROOM" --out "$RUN/audio" --seconds $((MAX_S + 30)) >"$RUN/recorder.log" 2>&1) &
RPID=$!
sleep 3
(cd sim && $SIMPY sim_human.py --room "$ROOM" --lines "$ROOT/runs/script/human_lines.json" --out "$RUN" --seconds "$MAX_S" --no-dispatch >"$RUN/sim_human.log" 2>&1) &
HPID=$!
sleep 3
$SIMPY scripts/dispatch.py --room "$ROOM" >"$RUN/dispatch.log" 2>&1 || { echo "派发失败"; cat "$RUN/dispatch.log"; }

T0=$(date +%s)
while [ ! -f "$RUN/done.json" ] && [ $(( $(date +%s) - T0 )) -lt "$MAX_S" ]; do sleep 2; done
[ -f "$RUN/done.json" ] && echo "会议结束（$(( $(date +%s) - T0 ))s）" || echo "超时 ${MAX_S}s，强制收尾"
sleep 2
kill -INT $HPID 2>/dev/null; pkill -INT -f "sim_human.py --room $ROOM" 2>/dev/null
pkill -INT -f "recorder.py --room $ROOM" 2>/dev/null
for i in $(seq 1 20); do kill -0 $RPID 2>/dev/null || break; sleep 1; done
kill -INT $WPID 2>/dev/null
for i in $(seq 1 20); do kill -0 $WPID 2>/dev/null || break; sleep 1; done
kill -9 $WPID 2>/dev/null
pkill -f "sim_human.py --room $ROOM" 2>/dev/null
$SIMPY sim/room_cleanup.py --room "$ROOM" >"$RUN/cleanup_after.log" 2>&1; tail -2 "$RUN/cleanup_after.log"

if [ -f "$RUN/events.jsonl" ] && [ -f "$RUN/audio/meta.json" ]; then
  $PY scripts/align_recording.py "$RUN" >/dev/null || echo "对齐失败"
fi
$PY scripts/acceptance.py "$RUN"
echo "产物：$RUN"
