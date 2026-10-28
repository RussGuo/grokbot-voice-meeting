"""本地中文 TTS：sherpa-onnx + Kokoro-82M v1.1-zh int8（模型 Apache-2.0，sherpa-onnx Apache-2.0，可商用）。

模型：https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-int8-multi-lang-v1_1.tar.bz2
说话人表：https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/kokoro.html
  45 zf_xiaobei 46 zf_xiaoni 47 zf_xiaoxiao 48 zf_xiaoyi 49 zm_yunjian 50 zm_yunxi 51 zm_yunxia 52 zm_yunyang

可中断：sherpa 的 generate 回调返回 0 即停止生成；ChunkedStream 被 aclose/取消时置位。
"""
from __future__ import annotations

import asyncio
import threading
import uuid
from pathlib import Path
from typing import Optional

import numpy as np
from livekit.agents import tts as lk_tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

MODEL_SUBDIR = "kokoro-int8-multi-lang-v1_1"
_ENGINE = None
_ENGINE_LOCK = threading.Lock()
_GEN_LOCK = threading.Lock()


def get_engine(models_dir: Path, num_threads: int = 2):
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            import sherpa_onnx
            d = Path(models_dir) / MODEL_SUBDIR
            if not (d / "model.int8.onnx").exists():
                raise FileNotFoundError(f"Kokoro 模型不存在：{d}（见 README 下载步骤）")
            kcfg = sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(d / "model.int8.onnx"), voices=str(d / "voices.bin"), tokens=str(d / "tokens.txt"),
                lexicon=f"{d / 'lexicon-us-en.txt'},{d / 'lexicon-zh.txt'}",
                data_dir=str(d / "espeak-ng-data"), dict_dir=str(d / "dict"),
            )
            mcfg = sherpa_onnx.OfflineTtsModelConfig(kokoro=kcfg, num_threads=num_threads, provider="cpu")
            cfg = sherpa_onnx.OfflineTtsConfig(
                model=mcfg,
                rule_fsts=f"{d / 'phone-zh.fst'},{d / 'date-zh.fst'},{d / 'number-zh.fst'}",
                max_num_sentences=1,
            )
            _ENGINE = sherpa_onnx.OfflineTts(cfg)
        return _ENGINE


class KokoroTTS(lk_tts.TTS):
    def __init__(self, *, models_dir: Path, sid: int, speed: float = 1.0) -> None:
        super().__init__(capabilities=lk_tts.TTSCapabilities(streaming=False), sample_rate=24000, num_channels=1)
        self._models_dir = Path(models_dir)
        self.sid = sid
        self.speed = speed

    @property
    def model(self) -> str:
        return "kokoro-82m-v1.1-zh-int8"

    @property
    def provider(self) -> str:
        return "sherpa-onnx-local"

    def prewarm(self) -> None:
        get_engine(self._models_dir)

    def synthesize(self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS) -> "KokoroChunkedStream":
        return KokoroChunkedStream(tts=self, input_text=text, conn_options=conn_options)


class KokoroChunkedStream(lk_tts.ChunkedStream):
    def __init__(self, *, tts: KokoroTTS, input_text: str, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._k = tts
        self._cancel = threading.Event()

    async def aclose(self) -> None:
        self._cancel.set()
        await super().aclose()

    async def _run(self, output_emitter: lk_tts.AudioEmitter) -> None:
        engine = await asyncio.to_thread(get_engine, self._k._models_dir)
        output_emitter.initialize(request_id=str(uuid.uuid4()), sample_rate=24000, num_channels=1,
                                  mime_type="audio/pcm", frame_size_ms=50)
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        cancel = self._cancel

        def cb(samples, _progress) -> int:
            pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1, 1) * 32767).astype(np.int16).tobytes()
            loop.call_soon_threadsafe(q.put_nowait, pcm)
            return 0 if cancel.is_set() else 1

        def work() -> None:
            try:
                with _GEN_LOCK:
                    if not cancel.is_set():
                        engine.generate(self._input_text, sid=self._k.sid, speed=self._k.speed, callback=cb)
            finally:
                loop.call_soon_threadsafe(q.put_nowait, None)

        fut = loop.run_in_executor(None, work)
        try:
            while True:
                chunk = await q.get()
                if chunk is None:
                    break
                output_emitter.push(chunk)
            output_emitter.flush()
        finally:
            cancel.set()
            await asyncio.wrap_future(fut) if isinstance(fut, asyncio.Future) is False else await fut
