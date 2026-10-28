# sim/：模拟会议用的房间工具（平台可行性分析师维护）

全部从 `../.env` 读 LiveKit 配置，不打印任何密钥，不使用 LiveKit Egress（付费）。
解释器：`../web/.venv/bin/python`（已装 livekit、livekit-api、numpy、python-dotenv）。

1. 录音 + 事件（先起）：`python sim/recorder.py --room R --out runs/<dir> --seconds 900`
   隐藏参会者 `recorder`，产出：每条音轨 `<identity>__<track>.wav`（从录音开始时刻对齐）、`mix.wav`、
   `events.jsonl`（数据消息/参会者/转写，墙钟 ms）、`levels.jsonl`（每 10ms 每轨 dBFS）、`meta.json`。
2. 模拟人类：`python sim/sim_human.py --room R --script sim/script.json --out runs/<dir>`
   identity = `MEETING_HUMAN_IDENTITY`（默认 host），token 带 agent dispatch（agent_name `voice-meeting`，
   可用 `LIVEKIT_AGENT_NAME` 改）。脚本格式见文件头：`at_ms` 定时开口；`on_speaking` 在 bot 说话中途插话。
   台词 wav 放 `sim/lines/`。日志：`sim_human.jsonl`。
3. 插话延迟：`python sim/bargein.py runs/<dir>` → 从房间里收到用户人声的首帧，到 bot 音轨压低（-6dB）/静音（≤-50dBFS 持续 300ms）的毫秒数，写 `bargein.json`。
