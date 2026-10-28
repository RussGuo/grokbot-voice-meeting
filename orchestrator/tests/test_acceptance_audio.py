"""The barge-in audio check must measure the bot track, not the worker's silent bus."""
import array
import importlib.util
import json
import sys
import wave
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parents[2] / "scripts" / "acceptance.py"
    spec = importlib.util.spec_from_file_location("acceptance_mod", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["acceptance_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


def _wav(path: Path, samples: list[int], rate: int = 16000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        buf = array.array("h", samples)
        w.writeframes(buf.tobytes())


def test_pick_bot_track_skips_silent_agent(tmp_path):
    acc = _load()
    audio = tmp_path / "audio"
    rate = 16000
    frame = rate * acc.FRAME_MS // 1000
    silent = [0] * (frame * 50)
    loud = [0] * (frame * 10) + [8000] * (frame * 20) + [0] * (frame * 20)
    _wav(audio / "agent-AJ_x__TR_silent.wav", silent, rate)
    _wav(audio / "eng_bot__TR_voice.wav", loud, rate)
    _wav(audio / "host__TR_mic.wav", [4000] * (frame * 15) + [0] * (frame * 35), rate)
    (audio / "manifest.json").write_text(json.dumps({"tracks": {
        "host": {"file": "host__TR_mic.wav", "start_ms": 0},
        # Old align pointed every bot at the worker track, which is silence.
        "eng_bot": {"file": "agent-AJ_x__TR_silent.wav", "start_ms": 0, "shared_identity": "agent-AJ_x"},
    }}), encoding="utf-8")
    trk = acc.tracks(tmp_path)
    picked = acc.pick_bot_track(trk, "eng_bot", "host", around_ms=400)
    assert picked is not None
    assert picked.path.name == "eng_bot__TR_voice.wav"
    assert acc.window_peak_db(picked, 400) > -40
