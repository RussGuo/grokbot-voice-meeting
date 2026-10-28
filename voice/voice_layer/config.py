"""集中配置：打断参数、duck 参数、provider 选择、每个 bot 的音色。

环境变量（只读名字/是否存在，绝不打印值）：
  火山（首选）  VOLC_API_KEY（新版控制台，优先）或 VOLC_APP_ID + VOLC_ACCESS_TOKEN（旧版控制台）
               VOLC_ASR_RESOURCE_ID（默认 volc.seedasr.sauc.duration）
               VOLC_TTS_RESOURCE_ID（默认 seed-tts-2.0；克隆音色用 seed-icl-2.0）
  阿里（可选骨架） DASHSCOPE_API_KEY
  选择器        VOICE_ASR_PROVIDER / VOICE_TTS_PROVIDER = auto|volc|aliyun|local（默认 auto）
  本地模型目录   VOICE_MODELS_DIR（默认 voice/models）
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

VOICE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MODELS_DIR = VOICE_DIR / "models"


def _env(name: str) -> Optional[str]:
    v = os.environ.get(name)
    return v if v else None


def volc_credentials_present() -> bool:
    return bool(_env("VOLC_API_KEY") or (_env("VOLC_APP_ID") and _env("VOLC_ACCESS_TOKEN")))


def dashscope_credentials_present() -> bool:
    return bool(_env("DASHSCOPE_API_KEY"))


@dataclass
class InterruptionConfig:
    """人声打断（硬规则：人声优先，任何状态下用户开口都上报并 duck）。"""
    min_duration: float = 0.25          # VAD 连续语音达到该秒数 => 真打断（bargein.confirmed）
    auto_duck: bool = True              # 语音层在 vad.start 立刻 duck（不等编排器），编排器再调 duck() 幂等
    auto_stop: bool = True              # min_duration 成立时语音层直接停 TTS（编排器调 interrupt() 也幂等）
    false_interruption_timeout: float = 2.0   # 传给 livekit 的同名参数
    resume_false_interruption: bool = True
    framework_mode: str = "vad"         # livekit interruption.mode；adaptive 依赖 LiveKit 推理，MVP 用 vad
    framework_enabled: bool = True      # livekit 自带打断；main.py（编排器）选择关掉，由语音层/编排器自己判定
    discard_audio_if_uninterruptible: bool = False   # 必须 False：否则框架打断关闭时 bot 说话期间人声被丢弃，ASR 收不到


@dataclass
class DuckConfig:
    gain_db: float = -18.0              # 软打断压低量
    ramp_ms: float = 10.0               # 增益渐变时长，避免爆音
    pace_lead_s: float = 0.12           # tts_node 输出节奏：最多领先实时播放多少秒（决定 duck 生效延迟上限）


@dataclass
class VadConfig:
    min_speech_duration: float = 0.05   # silero 默认 0.05
    min_silence_duration: float = 0.40  # silero 默认 0.55，会议场景稍短
    activation_threshold: float = 0.5
    prefix_padding_duration: float = 0.5


@dataclass
class BotVoice:
    bot_id: str
    provider: str = "auto"              # auto|volc|aliyun|local
    local_sid: int = 47                 # kokoro v1.1 speaker id
    volc_speaker: str = "zh_female_vv_uranus_bigtts"
    aliyun_voice: str = "longxiaochun_v3"
    speed: float = 1.0


DEFAULT_BOT_VOICES: dict[str, BotVoice] = {
    # volc_speaker：豆包语音合成模型 2.0 官方音色列表 https://www.volcengine.com/docs/6561/1257544
    #   （2026-10-27 用 seed-tts-2.0 实际合成验证过，试听见 voice/samples/）
    # local_sid：sherpa-onnx kokoro-int8-multi-lang-v1_1 说话人表（45-52 为中文音色）
    "moderator": BotVoice("moderator", local_sid=50, volc_speaker="zh_male_huolixiaoge_uranus_bigtts"),   # 活力小哥 2.0（活泼年轻）
    "pm_bot": BotVoice("pm_bot", local_sid=47, volc_speaker="zh_female_qingxinnvsheng_uranus_bigtts"),   # 清新女声 2.0（清亮女声）
    "eng_bot": BotVoice("eng_bot", local_sid=49, volc_speaker="zh_male_m191_uranus_bigtts"),             # 云舟 2.0（沉稳男声）
    "bot_c": BotVoice("bot_c", local_sid=48, volc_speaker="zh_female_vv_uranus_bigtts"),                 # Vivi 2.0（成本 bot）
    "bot_e": BotVoice("bot_e", local_sid=45, volc_speaker="zh_female_xiaohe_uranus_bigtts"),             # xiaohe 2.0（2026-10-28 实测可用）
    "bot_f": BotVoice("bot_f", local_sid=51, volc_speaker="zh_male_taocheng_uranus_bigtts"),             # taocheng 2.0（真人用户参会时可用；再跑模拟人类时与 sim_human 冲突）
    "bot_g": BotVoice("bot_g", local_sid=52, volc_speaker="zh_female_shuangkuaisisi_uranus_bigtts"),     # shuangkuaisisi 2.0（备用，2026-10-28 实测可用）
    "voice_ruyayichen": BotVoice("voice_ruyayichen", local_sid=51, volc_speaker="zh_male_ruyayichen_uranus_bigtts"),  # 儒雅逸辰 2.0，2026-10-28 实测可用
    "voice_shuangkuaisisi": BotVoice("voice_shuangkuaisisi", local_sid=52, volc_speaker="zh_female_shuangkuaisisi_uranus_bigtts"),  # 爽快思思 2.0，2026-10-28 实测可用
    # 模拟用户（不是 bot，仅用于预合成人类台词；与上面 4 个互不共用）
    "sim_human": BotVoice("sim_human", local_sid=46, volc_speaker="zh_male_taocheng_uranus_bigtts"),           # 2026-10-27 seed-tts-2.0 实测可用
}

# meeting_example.json 里 agent_profile.tts_voice 的别名 → 上面的 bot 音色
VOICE_ALIASES = {
    "voice_moderator_neutral": "moderator",
    "voice_pm_female_warm": "pm_bot",
    "voice_eng_male_calm": "eng_bot",
    "voice_cos_female_crisp": "bot_c",
    "voice_sim_human": "sim_human",
    "voice_slot_e": "bot_e",
    "voice_slot_f": "bot_f",
    "voice_slot_g": "bot_g",
    # orchestrator/bots/sim_v1：增长分身用活力小哥（明亮男声），与 sim_v1 其余 3 个音色互不共用
    "voice_growth_male_bright": "moderator",
    "growth_bot": "moderator",
    "chief_of_staff": "bot_c",
}


@dataclass
class VoiceConfig:
    human_identity: str = field(default_factory=lambda: os.environ.get("MEETING_HUMAN_IDENTITY", "host"))  # RoomIO 只链接这个参与者
    asr_provider: str = field(default_factory=lambda: os.environ.get("VOICE_ASR_PROVIDER", "auto"))
    tts_provider: str = field(default_factory=lambda: os.environ.get("VOICE_TTS_PROVIDER", "auto"))
    models_dir: Path = field(default_factory=lambda: Path(os.environ.get("VOICE_MODELS_DIR", str(DEFAULT_MODELS_DIR))))
    interruption: InterruptionConfig = field(default_factory=InterruptionConfig)
    duck: DuckConfig = field(default_factory=DuckConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    bots: dict[str, BotVoice] = field(default_factory=lambda: {k: BotVoice(**v.__dict__) for k, v in DEFAULT_BOT_VOICES.items()})
    volc_asr_resource_id: str = field(default_factory=lambda: os.environ.get("VOLC_ASR_RESOURCE_ID", "volc.seedasr.sauc.duration"))
    # ASR 热词，逗号分隔（VOLC_ASR_HOTWORDS）；默认是本项目会议的高频术语
    asr_hotwords: list = field(default_factory=lambda: [w.strip() for w in os.environ.get(
        "VOLC_ASR_HOTWORDS", "对账,本体,分身,多音轨,慢路径,需求文档,v1,行动项,Chief of Staff").split(",") if w.strip()])
    volc_tts_resource_id: str = field(default_factory=lambda: os.environ.get("VOLC_TTS_RESOURCE_ID", "seed-tts-2.0"))
    tts_sample_rate: int = 24000

    def resolve_asr_provider(self) -> str:
        return _resolve(self.asr_provider)

    def resolve_tts_provider(self, bot_id: Optional[str] = None) -> str:
        p = self.tts_provider
        if bot_id and bot_id in self.bots and self.bots[bot_id].provider != "auto":
            p = self.bots[bot_id].provider
        return _resolve(p)

    def bot_voice(self, bot_id: str) -> BotVoice:
        if bot_id not in self.bots and bot_id in VOICE_ALIASES:
            return self.bots[VOICE_ALIASES[bot_id]]
        if bot_id not in self.bots:
            # 未登记的 bot：按 id 稳定分配一个本地中文音色，保证可区分
            sids = [45, 46, 47, 48, 49, 50, 51, 52]
            self.bots[bot_id] = BotVoice(bot_id, local_sid=sids[sum(map(ord, bot_id)) % len(sids)])
        return self.bots[bot_id]


def _resolve(p: str) -> str:
    """auto：火山有凭证用火山，否则阿里有 key 用阿里，否则本地。显式指定但缺凭证 => 回退本地并记录原因。"""
    p = (p or "auto").lower()
    if p == "auto":
        if volc_credentials_present():
            return "volc"
        if dashscope_credentials_present():
            return "aliyun"
        return "local"
    if p == "volc" and not volc_credentials_present():
        return "local"
    if p == "aliyun" and not dashscope_credentials_present():
        return "local"
    return p


def load_config(path: Optional[str | Path] = None) -> VoiceConfig:
    """可选 JSON 覆盖：{"bots": {"bot_a": {"local_sid": 46, "volc_speaker": "..."}}, "interruption": {...}, "duck": {...}}"""
    cfg = VoiceConfig()
    path = path or os.environ.get("VOICE_CONFIG")
    if not path:
        return cfg
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for k, v in data.get("bots", {}).items():
        base = cfg.bot_voice(k).__dict__.copy()
        base.update(v)
        base["bot_id"] = k
        cfg.bots[k] = BotVoice(**base)
    for section in ("interruption", "duck", "vad"):
        for k, v in data.get(section, {}).items():
            setattr(getattr(cfg, section), k, v)
    for k in ("human_identity", "asr_provider", "tts_provider"):
        if k in data:
            setattr(cfg, k, data[k])
    return cfg


# —— 自动分配音色（meet.py 用）——
# 只放 2026-10-27/28 用 seed-tts-2.0 实际合成验证过的音色（官方列表 https://www.volcengine.com/docs/6561/1257544）
VERIFIED_VOICE_POOL: list[tuple[str, str]] = [   # (volc_speaker, 性别 M/F)
    ("zh_female_vv_uranus_bigtts", "F"),
    ("zh_male_m191_uranus_bigtts", "M"),
    ("zh_female_qingxinnvsheng_uranus_bigtts", "F"),
    ("zh_male_huolixiaoge_uranus_bigtts", "M"),
    ("zh_female_xiaohe_uranus_bigtts", "F"),
    ("zh_male_ruyayichen_uranus_bigtts", "M"),
    ("zh_female_shuangkuaisisi_uranus_bigtts", "F"),
    ("zh_male_taocheng_uranus_bigtts", "M"),        # 模拟用户也用它：sim_human=True 时不分配
]
SIM_HUMAN_SPEAKER = "zh_male_taocheng_uranus_bigtts"


def assign_voices(bot_ids: list[str], *, sim_human: bool = False,
                  preferred: dict[str, str] | None = None) -> dict[str, str]:
    """给参会 bot 分配互不重复的音色，按发言人顺序男女交替。
    preferred: {bot_id: volc_speaker} 先占（须在池内且不重复）。人数超过池子直接报错，不重复分配。"""
    pool = [(s, g) for s, g in VERIFIED_VOICE_POOL if not (sim_human and s == SIM_HUMAN_SPEAKER)]
    gender = dict(pool)
    out: dict[str, str] = {}
    for bid, spk in (preferred or {}).items():
        if bid in bot_ids and spk in gender and spk not in out.values():
            out[bid] = spk
    free = [p for p in pool if p[0] not in out.values()]
    need = [b for b in bot_ids if b not in out]
    if len(need) > len(free):
        raise ValueError(f"已验证音色只剩 {len(free)} 个，不够 {len(need)} 个 bot（sim_human={sim_human}）")
    for i, bid in enumerate(bot_ids):
        if bid in out:
            continue
        prev = gender.get(out.get(bot_ids[i - 1], "")) if i > 0 else None
        pick = next((p for p in free if p[1] != prev), free[0])
        free.remove(pick)
        out[bid] = pick[0]
    return out


def apply_voices(cfg: "VoiceConfig", mapping: dict[str, str]) -> None:
    """把 assign_voices 的结果写进 cfg.bots（覆盖别名），worker 读场景后调用。"""
    for bid, spk in mapping.items():
        cfg.bots[bid] = BotVoice(bid, volc_speaker=spk)
