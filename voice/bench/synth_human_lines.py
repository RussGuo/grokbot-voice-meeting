"""用豆包 TTS 2.0（模拟用户专用音色 VoiceConfig.bots["sim_human"]）把人类台词合成 16k 单声道 16-bit wav。

输入：<repo>/runs/script/human_lines.json（架构师维护；本脚本只读不写）。
      不存在时用内置 4 句示例台词。
      接受格式：[{"id":..,"text":..}, ...] 或 {"lines":[...]} / {"cues":[...]}；
      每项取 id（缺省 line_<序号>）与 text（或 say / content）；其余字段（at_ms 等）原样写进 manifest。
输出：<out>/<id>.wav（默认 sim/lines/），同时复制到 runs/script/<id>.wav；manifest.json 记录 id/text/文件/时长。

用法（在 voice/ 下）：
  .venv/bin/python bench/synth_human_lines.py                # 默认输入输出
  .venv/bin/python bench/synth_human_lines.py --json X.json --out DIR --copy-to DIR2 --speaker ID
需要 VOLC_API_KEY（或 VOLC_APP_ID+VOLC_ACCESS_TOKEN），从 ../.env 读取；不打印 key。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import wave
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from voice_layer.envfile import load_env  # noqa: E402
from voice_layer.config import load_config  # noqa: E402
from volc_tts_smoke import synth_once  # noqa: E402

REPO = HERE.parent.parent
SAMPLE_LINES = [
    {"id": "h01_open", "text": "大家好，今天就一个议题：这个插件的 v1 先做什么。"},
    {"id": "h02_barge", "text": "先停一下，v1 只做语音会议，不做纪要分发。"},
    {"id": "h03_ask", "text": "这个要问一下本体：上线前预算的上限是多少？"},
    {"id": "h04_close", "text": "好，就按这个定，散会。"},
]


def load_lines(p: Path) -> tuple[list[dict], str]:
    if not p.exists():
        return SAMPLE_LINES, "builtin-sample"
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("lines") or data.get("cues") or data.get("items") or []
    out = []
    for i, it in enumerate(data, 1):
        text = it.get("text") or it.get("say") or it.get("content")
        if not text:
            continue
        out.append({**it, "id": str(it.get("id") or f"line_{i:02d}"), "text": text})
    return out, str(p)


def write_wav(path: Path, pcm: bytes, sr: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=str(REPO / "runs" / "script" / "human_lines.json"))
    ap.add_argument("--out", default=str(REPO / "sim" / "lines"))
    ap.add_argument("--copy-to", default=str(REPO / "runs" / "script"))
    ap.add_argument("--speaker", default=None, help="默认 VoiceConfig.bots['host'].volc_speaker")
    ap.add_argument("--sr", type=int, default=16000)
    a = ap.parse_args()
    load_env()
    cfg = load_config()
    speaker = a.speaker or cfg.bots["sim_human"].volc_speaker
    lines, src = load_lines(Path(a.json))
    out, cp = Path(a.out), Path(a.copy_to) if a.copy_to else None
    out.mkdir(parents=True, exist_ok=True)
    if cp:
        cp.mkdir(parents=True, exist_ok=True)
    manifest = {"source": src, "speaker": speaker, "resource_id": cfg.volc_tts_resource_id,
                "sample_rate": a.sr, "channels": 1, "lines": []}
    async with aiohttp.ClientSession() as s:
        for ln in lines:
            r = await synth_once(s, ln["text"], speaker, cfg.volc_tts_resource_id, sample_rate=a.sr, char_stream=False)
            if not r.get("ok"):
                print(json.dumps({"id": ln["id"], "ok": False, "err": r.get("err") or r.get("msg"),
                                  "logid": r.get("logid")}, ensure_ascii=False), flush=True)
                continue
            f = out / f"{ln['id']}.wav"
            write_wav(f, r["pcm"], a.sr)
            if cp:
                shutil.copy2(f, cp / f.name)
            rec = {**ln, "file": f.name, "duration_ms": round(r["audio_s"] * 1000)}
            manifest["lines"].append(rec)
            print(json.dumps({"id": ln["id"], "ok": True, "file": str(f), "duration_ms": rec["duration_ms"],
                              "ttfb_ms": round(r["ttfb_from_first_text_ms"])}, ensure_ascii=False), flush=True)
    for d in [out] + ([cp] if cp else []):
        (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
