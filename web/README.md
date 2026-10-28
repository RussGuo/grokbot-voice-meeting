# web/ — 最简 LiveKit 网页前端 + token 服务

```bash
cd web && python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
export LIVEKIT_URL=... LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=...   # 由用户配置，勿入库
python token_server.py   # 仅监听 127.0.0.1:8787
```
打开 http://127.0.0.1:8787/?room=meeting-dev ，点「加入会议」。`/healthz` 可查缺哪些环境变量。

## 与编排器的数据约定（LiveKit data message，JSON，reliable）
- topic `meeting.speaker`：`{"speaker_id","display_name","state":"speaking|idle|ducking"}`，网页高亮当前发言者
- topic `meeting.roster`（可选）：`{"participants":[{"speaker_id","display_name"}]}`
- 转写：沿用 livekit-agents 默认 text stream `lk.transcription`

## 安全
- 用户的 identity 默认取环境变量 `MEETING_HUMAN_IDENTITY`（缺省 `host`），与编排器一致；URL 参数 `identity` 可覆盖。
- token 服务默认只绑 127.0.0.1；room/identity 做了白名单校验；密钥只读环境变量。
- 需要别的设备访问时，走 SSH 隧道或带鉴权的反代，不要直接把 8787 暴露到公网。
