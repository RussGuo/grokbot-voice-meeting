# voice-meeting：多 Bot 语音会议插件原型

设计依据：v0.2 设计页（单 AgentSession + 编排器令牌 `update_agent` 切换）。

## 目录分工

| 目录 | 负责人 | 内容 |
|---|---|---|
| `main.py`、`orchestrator/`、`scripts/`、顶层文件 | 会议编排架构师 | 状态机、令牌调度、事件日志、chat_ctx 组装、LiveKit agent 入口 |
| `voice/` | 语音技术调研员 | ASR / TTS 适配（火山·豆包）、发送端 duck 增益、打断参数、延迟打点 |
| `web/` | 平台可行性分析师 | 最简 LiveKit 网页前端 + token 服务（只绑 127.0.0.1） |

## 运行

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python -m pytest -q                       # 单测：不读 .env，不依赖任何 key 或网络
python -m orchestrator.sim                # 文本版 3 分钟模拟会议（FakeVoiceLayer + fake LLM）
python -m orchestrator.sim --jsonl runs/sim.jsonl   # 同时导出事件日志
python scripts/llm_ttft_probe.py -n 10    # DeepSeek 首 token 延迟探针（读 .env，只打印变量是否存在）
python scripts/ctx_smoke.py --bot eng_bot # 用示例 brief 组装 chat_ctx，真实 LLM 发言一次
python -m orchestrator.sim --scenario sim_v1 --lines runs/script/human_lines.json --run-dir runs/sim_v1_fake
                                          # v1 场景 + 用户 4 句台词（纯文本 fake），导出 events.jsonl/minutes.md/actions/
python scripts/acceptance.py runs/<run_id> # 6 条验收逐项 PASS/FAIL（读事件日志 + audio/*.wav）
python main.py dev                        # LiveKit agent（出站连 LiveKit Cloud，不在本机开端口）——待联调
```

## 环境变量（只写变量名；值放 `.env`，`.env` 已被 .gitignore 排除，绝不入库）

| 变量 | 用途 |
|---|---|
| `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | LiveKit Cloud 项目 |
| `VOLC_API_KEY` | 火山（豆包）语音，优先使用 |
| `VOLC_APP_ID` / `VOLC_ACCESS_TOKEN` | 火山语音备选鉴权（没有 `VOLC_API_KEY` 时） |
| `DEEPSEEK_API_KEY` | DeepSeek（OpenAI 兼容）；缺省时自动用 mock |
| `DEEPSEEK_MODEL` | 模型名，默认 `deepseek-flash`（官方文档 2026-10-27）；当前 .env 配的是 `deepseek-chat` |
| `DEEPSEEK_BASE_URL` | 默认 `https://api.deepseek.com` |
| `MEETING_SCENARIO` | `example`（默认）或 `sim_v1` |
| `MEETING_RUN_ID` | 产物目录 `runs/<run_id>/`，默认按启动时间生成 |
| `MEETING_HUMAN_IDENTITY` | 用户的 LiveKit identity，默认 `host`；= `linked_human_id` = RoomIO `participant_identity` |

## 给 web/ 的数据约定（LiveKit data message，JSON，reliable 通道）

- topic **`meeting.speaker`**：`{"speaker_id": "...", "display_name": "...", "state": "speaking" | "idle" | "ducking"}`
  - 令牌授予 / 切到某 bot（`update_agent` 完成）→ `speaking`
  - 用户开口、bot 被压低音量 → 该 bot `ducking`；误触发恢复 → 该 bot `speaking`
  - 令牌释放 / 被收回 / 被打断 → `idle`
  - 用户的 `speaker_id` = `MEETING_HUMAN_IDENTITY`（默认 `host`），开口拿到令牌 → `speaking`，EOT → `idle`
- topic **`meeting.roster`**：`{"participants": [{"speaker_id": "...", "display_name": "..."}]}`，会议开始与参会者变化时发
- 转写沿用 livekit-agents 默认 text stream `lk.transcription`

## 给 voice/ 的接口约定（`orchestrator/voice_iface.py` 的 `VoiceLayer`）

- `subscribe(cb)`：推送 dict 事件，`type` ∈ `vad.start / vad.end / asr.partial / asr.final / eot / bargein.confirmed / bargein.rejected / tts.first_audio / bot.utterance / tts.done`，字段 `participant_id`、`at`（毫秒）；bot 侧事件必须带 `token_id`
- `switch_agent(pid, ChatCtxRecipe) -> {"switch_ms"}`：封装 `session.update_agent`；`speak(token_id, pid, ctx)`：拿到令牌后才生成
- `duck(gain_db)` / `restore()`：**发送端**增益，立即生效；`interrupt() -> {"played_chars"}`
- `publish_data(topic, payload)`（reliable）；`main.py` 里的 `LiveKitVoiceLayer` 是最小实现，voice/ 可提供 `voice.adapters.make_stt()` / `make_tts(voice_name)` 接入火山
- 音色占位名在 `orchestrator/bots/meeting_example.json` 的 `agent_profile.tts_voice`

## 编排器模块

`models.py` 数据结构 · `events.py` 事件日志（append-only、JSONL、`project()` 回放）· `state_machine.py` 状态机（非法转移抛错）·
`scheduler.py` 优先级/举手打分/冷却/防 bot 链/插回配额 · `orchestrator.py` 单线程事件循环 · `principal.py` 本体慢路径（模拟）·
`fake_llm.py` 测试用 LLM · `llm.py` DeepSeek 适配 · `bots/` 示例会议与 brief（全部虚构）· `sim.py` 文本模拟器 · `tests/` 单测

## v1 验收（sim_v1 场景）

- 场景：`orchestrator/bots/sim_v1/`（Chief of Staff 主持 + 产品/工程/增长分身，议题「多 Bot 语音会议插件 v1 先做什么」，2 个议程项，总时长 ≤ 5 分钟；数字全是示例数据）。
- 用户台词：`runs/script/human_lines.json`（4 句，`t_ms` 相对会议开始；L1 是 bot 发言中途插话的指令，L3 触发问本体；`trigger_hint` 给模拟人类发布器用事件触发替代固定时间）。`runs/` 下只有 `script/` 入库。
- 产物目录 `runs/<run_id>/`（编排器写）：`events.jsonl`、`minutes.md`、`actions/<owner_id>.json`、`clock.json`（`t0_epoch_ms`）。
- **给录音队友**：每个参与者一条 16-bit PCM wav 放 `runs/<run_id>/audio/<participant_id>.wav`（人类用 `MEETING_HUMAN_IDENTITY`）；若 wav 首帧不在会议 t0，写 `audio/manifest.json`：`{"tracks": {"<pid>": {"file": "<pid>.wav", "start_ms": <首帧墙钟 - t0_epoch_ms>}}}`。
- 事件 `HumanVad`（stage start/end）记录用户人声起止；验收 3 的事件日志法 = `Interrupted.at - HumanVad(start).at`，音频法 = 人声音轨能量起点 → bot 音轨连续 200ms 低于 -40dBFS 的起点，阈值默认 1500ms（`--max-stop-ms`，待和语音队友对齐）。

## 真实端到端（sim_v1）

`scripts/run_sim.sh [run_id]`：清房 → worker（`main.py start`，健康检查端口只绑 127.0.0.1）→ recorder（`runs/<id>/audio/`）
→ sim_human（`--lines runs/script/human_lines.json --no-dispatch`）→ `scripts/dispatch.py` 显式派发 agent
（房间由 recorder 先建，token 里的 room_config 派发不会触发）→ 等 `done.json` → 收尾 → room_cleanup
→ `scripts/align_recording.py`（生成 `audio/manifest.json`；单 AgentSession 所有 bot 共用 agent 音轨）→ `acceptance.py`（插话通过线 700ms）。

- 语音层：`orchestrator/live_voice.py` 继承 voice/ 的 `LiveKitVoiceLayer`，只覆盖 `speak()`：意图（问本体/承诺/来源）由规则策略
  决定，成员 bot 台词由 DeepSeek（`DEEPSEEK_MODEL`，thinking 关闭）流式生成按句送 TTS；主持固定话术、转述本体答案不调 LLM。
- 编排器 agent 等 `MEETING_HUMAN_IDENTITY` 入会后才开场；会议进入 PostMeeting 3 秒后导出产物并关闭 job。
- 产物：`events.jsonl`、`minutes.md`、`actions/`、`clock.json`、`voice_stats.json`（切换/LLM/打点）、`worker.log`、`audio/`（录音，不入库）。
