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

## 加入链接与回放入口
- `MEETING_JOIN_PAGE` 指向静态托管的 `index.html` 时，`meet` 写出的链接是 `<页面>#liveKitUrl=...&token=...`。token 在 URL fragment 里：浏览器不会把 fragment 发给服务器，托管方日志和 Referer 看不到它；页面读完立刻用 `history.replaceState` 把它从地址栏抹掉（所以刷新后要重新打开链接）。旧的 `?liveKitUrl=...&token=...` 链接仍可用，同样会被抹掉。页面带 `<meta name="referrer" content="no-referrer">`。
- 链接文件里的第二个 URL 是 `https://meet.livekit.io/custom?liveKitUrl=...&token=...` 备用入口。那是第三方页面，只认查询串，token 会出现在它的请求里；能用静态页就别用它。
- 本地 `token_server.py` 打开 `/`（没有 token、没有 `?replay=`）时，页面会列出 `runs/` 下可回放的运行，点一下进入回放。静态托管上没有 `/api/replay`，这个列表自动隐藏。

## 安全
- 房间 metadata 对所有持 token 的参会者可见，所以 `meet` 只往里写 `{"v":1,"room":{"topic","who"}}`；完整场景（含 brief）只走派发 metadata，并存在 `links/<room>.scene.json`（600）。链接可以转发，brief 不会随链接泄露。
- 用户的 identity 默认取环境变量 `MEETING_HUMAN_IDENTITY`（缺省 `host`），与编排器一致；URL 参数 `identity` 可覆盖。
- token 服务默认只绑 127.0.0.1；room/identity 做了白名单校验；密钥只读环境变量。
- 设了 `MEETING_ACCESS_KEY` 即「公网模式」：口令用 `Authorization: Bearer <key>` 头，或首访后下发的 HttpOnly cookie；旧的 `?k=<key>` 仍可用但不推荐（会留在地址栏、浏览器历史和代理日志里）。访问日志只记路径，不记查询串，口令不会写进日志。
- 需要别的设备访问时，走 SSH 隧道或带鉴权的反代，不要直接把 8787 暴露到公网。
