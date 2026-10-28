#!/usr/bin/env bash
# 用户亲自试：worker + token_server（口令保护）+ cloudflared quick tunnel；空闲 30 分钟自动全部关闭。
# 不打印任何 key / 口令；带口令的链接只写进 TRY_LINK.txt（chmod 600）。
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd); LIVE=$ROOT/runs/live; mkdir -p "$LIVE"
ROOM=${1:-meeting-live-$(date +%m%d%H%M)}
PORT=${WEB_PORT:-8787}
if pgrep -f "main.py start" >/dev/null; then echo "已有 worker 在跑，拒绝并行"; exit 2; fi
KEY=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
set -a; . ./.env; set +a
export LIVEKIT_AGENT_NAME=${LIVEKIT_AGENT_NAME:-voice-meeting} MEETING_SCENARIO=${MEETING_SCENARIO:-sim_v1}
unset MEETING_RUN_ID

nohup .venv/bin/python main.py start >"$LIVE/worker.log" 2>&1 & echo $! >"$LIVE/worker.pid"
MEETING_ACCESS_KEY=$KEY MEETING_ROOM=$ROOM WEB_HOST=127.0.0.1 WEB_PORT=$PORT \
  nohup web/.venv/bin/python web/token_server.py >"$LIVE/token_server.log" 2>&1 & echo $! >"$LIVE/token.pid"
nohup ~/.local/bin/cloudflared tunnel --no-autoupdate --protocol http2 --url "http://127.0.0.1:$PORT" >"$LIVE/tunnel.log" 2>&1 & echo $! >"$LIVE/tunnel.pid"

for i in $(seq 1 60); do
  URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$LIVE/tunnel.log" | head -1); [ -n "$URL" ] && break; sleep 1; done
for i in $(seq 1 60); do grep -q "registered worker" "$LIVE/worker.log" && break; sleep 1; done
[ -n "${URL:-}" ] || { echo "隧道未就绪"; exit 3; }
grep -q "registered worker" "$LIVE/worker.log" || { echo "worker 未注册"; exit 3; }
umask 077
printf '%s/?room=%s&k=%s\n' "$URL" "$ROOM" "$KEY" >"$ROOT/TRY_LINK.txt"; chmod 600 "$ROOT/TRY_LINK.txt"
echo "$ROOM" >"$LIVE/room.txt"
echo "ok room=$ROOM tunnel=$URL"

# 空闲看门狗：worker.log 与 token_server.log 30 分钟没有新内容 → 全部关闭并删除链接文件
nohup bash -c "
while sleep 60; do
  now=\$(date +%s); w=\$(stat -c %Y '$LIVE/worker.log'); t=\$(stat -c %Y '$LIVE/token_server.log')
  last=\$(( w > t ? w : t ))
  if [ \$(( now - last )) -ge 1800 ]; then
    for f in worker token tunnel; do kill -INT \$(cat '$LIVE/'\$f.pid) 2>/dev/null; done
    rm -f '$ROOT/TRY_LINK.txt'; echo \"idle shutdown \$(date)\" >>'$LIVE/watchdog.log'; exit 0
  fi
done" >/dev/null 2>&1 & echo $! >"$LIVE/watchdog.pid"
