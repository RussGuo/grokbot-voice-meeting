"""ASR 适配层工厂：统一成 livekit `stt.STT`（流式，INTERIM=partial / FINAL=final），
VoiceLayer 再把 framework 的 user_input_transcribed 转成 asr.partial / asr.final 事件。

provider:
  volc   豆包流式语音识别 2.0（bigmodel_async，首选）
  aliyun Paraformer 实时（骨架，未实现）
  local  sherpa-onnx 流式 zipformer 中英双语（CPU，无 key）
"""
from __future__ import annotations

from typing import Optional

from livekit.agents import stt as lk_stt

from ..config import VoiceConfig


def make_stt(cfg: VoiceConfig, provider: Optional[str] = None) -> lk_stt.STT:
    p = provider or cfg.resolve_asr_provider()
    if p == "volc":
        from .volc import VolcSTT
        return VolcSTT(resource_id=cfg.volc_asr_resource_id, hotwords=cfg.asr_hotwords)
    if p == "aliyun":
        from .aliyun import AliyunParaformerSTT
        return AliyunParaformerSTT()
    from .local_sherpa import SherpaStreamingSTT
    return SherpaStreamingSTT(models_dir=cfg.models_dir)
