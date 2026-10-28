"""本地流式 ASR：sherpa-onnx streaming zipformer bilingual zh-en（2023-02-20，int8，CPU）。
模型：https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20.tar.bz2
license：模型 README 标注 apache-2.0；sherpa-onnx Apache-2.0。
真 partial：每 100ms 解码一次，文本变化即 INTERIM；端点检测（尾静音）=> FINAL。
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import numpy as np
from livekit import rtc
from livekit.agents import stt as lk_stt
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions, NotGivenOr

MODEL_SUBDIR = "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20"
_REC = None
_LOCK = threading.Lock()


def get_recognizer(models_dir: Path, num_threads: int = 2, trailing_silence: float = 0.6):
    global _REC
    with _LOCK:
        if _REC is None:
            import sherpa_onnx
            d = Path(models_dir) / MODEL_SUBDIR
            if not (d / "tokens.txt").exists():
                raise FileNotFoundError(f"ASR 模型不存在：{d}（见 README 下载步骤）")
            _REC = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=str(d / "tokens.txt"),
                encoder=str(d / "encoder-epoch-99-avg-1.int8.onnx"),
                decoder=str(d / "decoder-epoch-99-avg-1.onnx"),
                joiner=str(d / "joiner-epoch-99-avg-1.int8.onnx"),
                num_threads=num_threads, sample_rate=16000, feature_dim=80,
                enable_endpoint_detection=True, rule1_min_trailing_silence=2.4,
                rule2_min_trailing_silence=trailing_silence, rule3_min_utterance_length=30,
                decoding_method="greedy_search", provider="cpu",
            )
        return _REC


class SherpaStreamingSTT(lk_stt.STT):
    def __init__(self, *, models_dir: Path) -> None:
        super().__init__(capabilities=lk_stt.STTCapabilities(streaming=True, interim_results=True, offline_recognize=False))
        self._models_dir = Path(models_dir)

    @property
    def model(self) -> str:
        return "zipformer-bilingual-zh-en-int8"

    @property
    def provider(self) -> str:
        return "sherpa-onnx-local"

    def prewarm(self) -> None:
        get_recognizer(self._models_dir)

    async def _recognize_impl(self, buffer, *, language=NOT_GIVEN, conn_options=DEFAULT_API_CONNECT_OPTIONS):
        raise NotImplementedError

    def stream(self, *, language: NotGivenOr[str] = NOT_GIVEN,
               conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS) -> "SherpaRecognizeStream":
        return SherpaRecognizeStream(stt=self, conn_options=conn_options)


class SherpaRecognizeStream(lk_stt.RecognizeStream):
    def __init__(self, *, stt: SherpaStreamingSTT, conn_options: APIConnectOptions) -> None:
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=16000)
        self._s = stt

    async def _run(self) -> None:
        rec = await asyncio.to_thread(get_recognizer, self._s._models_dir)
        st = rec.create_stream()
        last = ""
        buf = np.zeros(0, dtype=np.float32)

        def decode(samples: np.ndarray):
            st.accept_waveform(16000, samples)
            while rec.is_ready(st):
                rec.decode_stream(st)
            text = rec.get_result(st).strip()
            ep = rec.is_endpoint(st)
            if ep:
                rec.reset(st)
            return text, ep

        def emit(kind, text):
            self._event_ch.send_nowait(lk_stt.SpeechEvent(type=kind, alternatives=[lk_stt.SpeechData(language="zh", text=text)]))

        async for item in self._input_ch:
            if not isinstance(item, rtc.AudioFrame):
                continue
            buf = np.concatenate([buf, np.frombuffer(item.data, dtype=np.int16).astype(np.float32) / 32768.0])
            if len(buf) < 1600:  # 100ms 解一次
                continue
            chunk, buf = buf, np.zeros(0, dtype=np.float32)
            text, ep = await asyncio.to_thread(decode, chunk)
            if ep:
                if text:
                    emit(lk_stt.SpeechEventType.FINAL_TRANSCRIPT, text)
                last = ""
            elif text and text != last:
                last = text
                emit(lk_stt.SpeechEventType.INTERIM_TRANSCRIPT, text)
        # 输入结束：补最后一段
        st.input_finished()
        text, _ = await asyncio.to_thread(decode, np.zeros(0, dtype=np.float32))
        if text:
            emit(lk_stt.SpeechEventType.FINAL_TRANSCRIPT, text)
