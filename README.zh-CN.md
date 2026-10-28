# grokbot-voice-meeting

人和若干 AI 智能体在同一个房间里**用语音开会**。每个智能体有自己的 brief、自己的音色、自己的头像格。主持人按议程推进：开场、点名、举手。你一开口，所有 bot 立刻停下。

任何 Grok Bot 都可以从「谁 + 议题」开一场会，并把入会链接交还给你。默认是 LiveKit Cloud、火山引擎豆包语音、DeepSeek。每一层都可以换。

![会议界面占位图。请换成截图或 GIF](docs/assets/demo.svg)

## 快速开始

需要 Python 3.11+。在克隆下来的仓库里：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"          # 要用本地 sherpa 语音再加 ".[all]"
cp .env.example .env             # 填写密钥；只想看模拟会议可以先不填
meet demo                        # 不需要密钥，也不联网
meet doctor --offline
```

真开会，开两个终端：

```bash
# 终端 A：一直开着。每场会的场景从 job metadata 读
meet worker

# 终端 B
cp bots.example.yaml bots.yaml   # 写上真实上下文，不要编造事实
meet --who pm,eng --topic "这个月发什么" --agenda "范围;风险"
```

`meet` 会打印入会链接。先有 worker，再跑 `meet`，否则房间里没有人说话。worker 最多等你 30 分钟入会（`MEETING_JOIN_WAIT_S`），超时这一场按 `NoShow` 结束。每场会写一个 `runs/<room>-<YYYYmmdd-HHMMSS>/`（事件、纪要、行动项）。

`meet demo` 用文本模拟一整场会（轮次、插话、纪要），不调用任何付费接口。

## 架构

```mermaid
flowchart LR
  user[浏览器里的你]
  lk[LiveKit 房间]
  worker[meet worker]
  host[主持人 / 轮次]
  bots[各 bot 音轨]
  llm[LLM]
  asr[ASR]
  tts[TTS]
  principal[会后问真实 bot]

  user -->|麦克风| lk
  lk --> asr --> worker
  worker --> host
  host --> llm
  llm --> tts --> bots
  bots -->|各自的音轨| lk
  lk --> user
  host -->|纪要与行动项| principal
```

一个 LiveKit agent 会话拿着发言权。每个 bot 再以自己的身份进房（`BotSpeakers`），正在说话的格子会亮。编排器自己那条音轨是静音，验收脚本量的是 bot 的音轨。

说什么、是发言还是问本体还是做承诺，由 LLM 规划器（`orchestrator/planner.py`）决定。每轮一次流式调用：先一行 JSON 头，再是口语正文。护栏在代码里，不在 prompt 里：`must_ask` 的话题在调用 LLM 之前就转去问本体；承诺必须匹配 `can_commit`，否则在第一个字出口前拦下；转述本体答案原样念；被点名但把握低就去问。LLM 出任何错都退回规则版 FakeLLM 台词；没有 LLM key 时规划器不启用。举手打分默认用规则（已支持英文），`MEETING_SCORER=llm` 再加一层异步 LLM 重打分。议程结论先给一句兜底文本，LLM 摘要在后台补上（`DecisionSummarized`）。

## 接到 Grok Bot

把 [`skills/voice-meeting/SKILL.md`](skills/voice-meeting/SKILL.md) 装进负责开会的那个 bot。用户说「叫我的 X 和 Y 开一场语音会，议题是 Z」。bot 把每个参会者**真实拥有**的上下文导出成文件，在自己的 Linux 上跑 `meet`，把链接交回来。

会中答不上来的问题走 `PrincipalClient`（`orchestrator/principal.py`）：

| `MEETING_PRINCIPAL` | 行为 |
|---|---|
| `defer`（`meet` 开的会默认） | 只记下问题，会中不作答，在纪要里标为未答。 |
| `mock`（样例场景、`meet demo`、测试默认） | 按会议语言给占位答复。答复是编的，真实会议不要用。 |
| `file` | 问题写在 `outbox/`，真实 bot 把答复写进 `inbox/`。 |
| `webhook` | POST 问题。响应里有 `{"text": "..."}` 就当场采用，否则仍看文件收件箱。 |

显式设置 `MEETING_PRINCIPAL` 永远优先于默认值。

会后 `meet followup runs/<id>`（或 `--room ROOM`，取该房间最新一次）根据 `minutes.md` 和 `actions/<bot>.json` 给每个 bot 写一段提示词。把这段交给**真实的** bot 去确认、更正或撤回。回写有两条路，都会追加 `ReconcileResult` 事件并重新生成纪要和行动项：

- `meet followup runs/<id> --push URL`（默认取 `MEETING_FOLLOWUP_WEBHOOK`）把每段提示词 POST 出去，并采用 JSON 答复。
- `meet followup runs/<id> --apply <bot> reply.txt` 手工写入答复：每条承诺一行 `confirm`、`correct: ...` 或 `retract`。

这个仓库不会自行写入 bot 的记忆。`meet stats runs/<id>` 给出 P50/P95 延迟（切换、LLM 首 token、bot 响应、插话停止）和可靠性计数；`--baseline` 与 `--fail-on-regress` 用来和一次好的运行对比。详见 [docs/OPERATIONS.md](docs/OPERATIONS.md)。

## 提供者

默认：LiveKit、豆包流式 ASR 与 seed-tts、DeepSeek。别的 OpenAI 兼容接口用 `LLM_PROVIDER=openai` 加 `LLM_BASE_URL`。OpenAI Whisper / TTS 用 `VOICE_ASR_PROVIDER=openai` 和 `VOICE_TTS_PROVIDER=openai`。音色 id 在 [`config/voices.json`](config/voices.json)，不写死在代码里。

新增适配器：[docs/PROVIDERS.md](docs/PROVIDERS.md)。

### LiveKit Cloud

1. 在 [LiveKit Cloud](https://cloud.livekit.io/) 建项目。Build 计划是免费档，额度以官网为准，会变。
2. 把 WebSocket 地址、API key、secret 填进 `.env` 的 `LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET`。
3. `meet worker` 以 `voice-meeting` 这个名字注册（`LIVEKIT_AGENT_NAME`）。`meet` 派发的就是这个名字。

### 火山引擎 / 豆包

1. 打开[火山引擎语音](https://www.volcengine.com/product/speech)，创建 API key（或旧版 app id + access token）。
2. 设置 `VOLC_API_KEY`。ASR 资源默认 `volc.seedasr.sauc.duration`，TTS 默认 `seed-tts-2.0`。
3. 新账号通常有试用额度。`config/voices.json` 里的 speaker id 来自豆包公开音色表，上线前在你自己的账号里确认还能用。

### DeepSeek

1. 在 [DeepSeek](https://platform.deepseek.com/) 创建 key。
2. `DEEPSEEK_API_KEY`，可选 `DEEPSEEK_BASE_URL`（默认 `https://api.deepseek.com`）和 `DEEPSEEK_MODEL`（默认 `deepseek-flash`）。
3. 按 token 计费。每个 bot 的 brief 摘要只打一次，缓存在 `~/.cache/grokbot-voice-meeting/briefs/`。

### 费用

`meet demo` 和 `pytest` 不打付费接口。真开会会消耗 LiveKit 的参会分钟、语音的音频秒数和 LLM 的 token。免费档和试用额度够开几场短会，具体额度以各家官网为准。`meet doctor` 只报告缺哪些变量、以及到 LiveKit 和 LLM 主机的 TCP 是否通，不会打印密钥。

## 不打隧道的入会页

设置了 `MEETING_JOIN_PAGE` 时，`meet` 会给出两个地址：

- 你的静态页面：`<页面>#liveKitUrl=...&token=...`。token 在 URL fragment 里，浏览器不会把它发给托管方，服务器日志和 Referer 都看不到。页面读完就把它从地址栏抹掉，所以刷新后要重新打开链接。旧的 `?liveKitUrl=&token=` 链接仍可用，同样会被抹掉。
- 备用的 `https://meet.livekit.io/custom?...`。那是第三方页面，只认查询串：**token 会发给那个站点**。没有静态页时才用。

`web/index.html` 是静态文件。链接里已经有 token 就直接入会，否则才请求 `/api/token`。页面把 `livekit-client` 固定在 jsDelivr 上的某个精确版本（ES module 的 import 没法加 SRI）。可以放到任意静态托管，包括 GitHub Pages：

1. 为仓库打开 Pages，发布根目录或 `web/`。
2. `MEETING_JOIN_PAGE` 设成那个 `index.html` 的 URL。
3. 打开 `meet` 打印的链接。浏览器直连 LiveKit Cloud，不必把 token 服务暴露到公网。

房间 metadata 对所有持 token 的人可见，所以只写 `{"v":1,"room":{"topic","who"}}`。完整场景（含 brief）只走派发 metadata，并存在 `links/<room>.scene.json`（权限 600）。`scripts/dispatch.py --room R` 默认发送这个文件（`--scene FILE` 指定，`--no-scene` 不带）。链接可以转发，brief 不会跟着走。

`python web/token_server.py` 仍留给本机开发（默认只绑 `127.0.0.1`）。它的 `/` 会列出可回放的运行。放到隧道后面时设置 `MEETING_ACCESS_KEY`：用 `Authorization: Bearer <key>` 头，或用它下发的 cookie。旧的 `?k=<key>` 仍可用但不推荐，查询串会留在地址栏和代理日志里。服务端不会把口令写进日志。

## 配置

| 变量 | 默认 | 作用 |
|---|---|---|
| `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | 空 | LiveKit 项目 |
| `LIVEKIT_AGENT_NAME` | `voice-meeting` | worker 名字，`meet` 按这个派发 |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | 不填则 mock | 默认 LLM |
| `LLM_PROVIDER` / `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | `auto` | 任意 OpenAI 兼容 LLM |
| `VOICE_ASR_PROVIDER` / `VOICE_TTS_PROVIDER` | `auto` | `volc` `aliyun` `openai` `local` |
| `VOLC_API_KEY` | 空 | 豆包语音 |
| `OPENAI_API_KEY` | 空 | OpenAI 语音，也可复用 `LLM_API_KEY` |
| `VOICE_CATALOG` | `config/voices.json` | 音色 id |
| `MEETING_HUMAN_IDENTITY` | `host` | 必须和入会 token 的 identity 一致 |
| `MEETING_HUMAN_NAME` | `你` / `用户` | 显示名和称呼 |
| `MEETING_LANG` | `zh` | `zh` 或 `en`，提示词和主持稿 |
| `MEETING_MODERATOR_ID` | `moderator` | 中立主持人，不是某个固定人设 |
| `MEETING_JOIN_PAGE` | 空 | 静态入会页；空则用 meet.livekit.io（token 会发给该站点） |
| `MEETING_JOIN_WAIT_S` | `1800` | worker 等人入会的秒数，超时这一场按 `NoShow` 结束 |
| `MEETING_PRINCIPAL` | `meet` 开的会 `defer`，样例 `mock` | `defer` `mock` `file` `webhook` |
| `MEETING_PRINCIPAL_DIR` / `MEETING_PRINCIPAL_WEBHOOK` | `runs/principal` / 空 | `file` 与 `webhook` 用 |
| `MEETING_PRINCIPAL_DELAY_MS` | `20000` | `mock` 答复的延迟 |
| `MEETING_SCORER` | `rule` | `rule`；`llm` 为举手再加一层异步 LLM 重打分 |
| `MEETING_FOLLOWUP_WEBHOOK` | 空 | `meet followup --push` 的默认地址 |
| `MEETING_BOTS` | `./bots.yaml` | bot 注册表和上下文 |
| `MEETING_HOME` | 空 | 放 `.env`、`runs/`、`links/` 的目录；空则用当前目录 |
| `MEETING_RUN_ID` | `<room>-<YYYYmmdd-HHMMSS>` | 原样指定 run id |
| `MEETING_SCENARIO` | `sim_v1` | job 里没带场景时用的样例 |
| `MEETING_MULTI_IDENTITY` | `1` | `0` 则所有 bot 共用 worker 的一条音轨 |
| `MEETING_BRIEF_CACHE` | `~/.cache/grokbot-voice-meeting/briefs/` | brief 摘要缓存 |
| `MEETING_TRANSPORT` | `livekit` | 实时传输层 |
| `MEETING_ACCESS_KEY` / `MEETING_ROOM` | 空 | token 服务的公网模式：口令，以及它只给哪个房间签发 |

`orchestrator/bots/` 里的样例场景仍由 Chief of Staff 主持，那是原来的测试数据。`meet` 开的新会议用 `moderator`。

## Docker

```bash
docker compose up worker
```

镜像安装本包，读取 `.env`，运行 `meet worker`。密钥不会打进镜像。见 [Dockerfile](Dockerfile) 和 [docker-compose.yml](docker-compose.yml)。

## 开发

```bash
pip install -e ".[dev]"
pytest
pytest voice/tests
ruff check .
meet demo --run-dir runs/demo
```

GitHub Actions 在推送时跑同样的检查。`ruff` 有意忽略单行写法类规则（E701、E702、E731、E741），见 `pyproject.toml`。手动触发的端到端工作流（`.github/workflows/e2e.yml`）需要真实密钥，见 [docs/OPERATIONS.md](docs/OPERATIONS.md)。

## 许可

MIT，见 [LICENSE](LICENSE)。

贡献、安全报告、行为准则：[CONTRIBUTING.md](CONTRIBUTING.md)、[SECURITY.md](SECURITY.md)、[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
