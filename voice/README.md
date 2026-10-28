# voice/ — 语音层（ASR / TTS / VAD / 发送端 duck / 打断）

实现 `orchestrator/voice_iface.py` 的 `VoiceLayer`：单 AgentSession + `update_agent` 切 bot，RoomIO 只绑定 `host`。
livekit-agents 1.8.9（API 以 `.venv` 里的源码为准）。

## 给 orchestrator / main.py 的入口
- `from voice import adapters`（main.py 已按此约定写）：
  - `adapters.make_stt()` → livekit `stt.STT`（火山流式 ASR 2.0；缺凭证回退本地 sherpa-onnx）
  - `adapters.make_tts(voice_name)` → livekit `tts.TTS`；voice_name 可为 meeting_example 的别名或 bot_id
  - `adapters.make_vad()` → Silero VAD（参数来自 VoiceConfig.vad）
- 完整实现：`voice_layer/livekit_layer.py` 的 `LiveKitVoiceLayer`、`build_session(cfg, vad, stt=)`、`room_options(cfg)`
  - `subscribe(cb)`；事件 dict：`type / participant_id / at(monotonic ms)`，bot 侧 `tts.first_audio / bot.utterance / tts.done` 带 `token_id`
  - `switch_agent(pid, ctx)` → `{"switch_ms"}`；`speak(token_id, pid, ctx)`（拿到令牌才 generate_reply）；`say(...)` 直接念
  - `duck(gain_db) / restore() / interrupt()`：发送端增益（在 Agent.tts_node 输出帧上乘增益 + 限速推帧，最多领先实时 120ms）
  - `publish_data / publish_current_speaker`
- 硬规则：人声 VAD 由独立 `HumanVad` 直接订阅 host 音轨，任何状态（空闲/bot 在说/切换中）都立即上报 `vad.start`，语音层不做状态过滤。

## 音色（seed-tts-2.0，2026-10-27 实测可合成；互不共用）
| 角色 | bot_id / 别名 | speaker |
|---|---|---|
| 主持人 | moderator / voice_moderator_neutral | zh_male_huolixiaoge_uranus_bigtts |
| 产品 | pm_bot / voice_pm_female_warm | zh_female_qingxinnvsheng_uranus_bigtts |
| 工程 | eng_bot / voice_eng_male_calm | zh_male_m191_uranus_bigtts |
| 成本 | bot_c / voice_cos_female_crisp | zh_female_vv_uranus_bigtts |
| 模拟用户（只用于预合成台词） | host / voice_sim_human | zh_male_taocheng_uranus_bigtts |

## 环境变量（只读名字，不打印值；从 ../.env 读）
- 火山豆包语音：`VOLC_API_KEY`（新版控制台，优先）或 `VOLC_APP_ID` + `VOLC_ACCESS_TOKEN`（旧版）
  - 可选：`VOLC_TTS_RESOURCE_ID`（默认 seed-tts-2.0）、`VOLC_ASR_RESOURCE_ID`（默认 volc.seedasr.sauc.duration）
- LiveKit：`LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET`
- LLM：`DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL`
- 阿里（骨架未实现）：`DASHSCOPE_API_KEY`
- 切换：`VOICE_ASR_PROVIDER / VOICE_TTS_PROVIDER`（volc|aliyun|local），`VOICE_CONFIG=xx.json` 覆盖参数

## 打断与框架设置
main.py 关掉了 LiveKit 自动打断：`interruption={"enabled": False, "discard_audio_if_uninterruptible": False}`。
实测（见下）此设置下 bot 说话时 host 音频仍进 VAD 和 ASR，无需回退。**`discard_audio_if_uninterruptible` 必须保持 False。**

## 脚本
- `bench/volc_tts_smoke.py --n 5 [--speaker ID]`：TTS TTFB
- `bench/volc_asr_smoke.py --wav X.wav --n 3`：ASR partial/final 延迟
- `bench/e2e_probe.py --llm mock|deepseek --n 5 [--framework-interrupt on|off] [--discard on|off]`：
  真房间（独立探针房间 voice-probe-xxxx，结束后自动删除）测 s2s / 打断 / 切换 / 截断
- `bench/synth_human_lines.py`：读 `runs/script/human_lines.json`（无则用 4 句示例），用 host 音色合成 16k 单声道 wav
  到 `sim/lines/<id>.wav` 并复制到 `runs/script/`，附 `manifest.json`
- 测试：`.venv/bin/python -m pytest -q`（不需要 key）
