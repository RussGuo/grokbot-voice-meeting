import numpy as np
from voice_layer.levels import LevelMeter


def tone(db, n=1600):
    amp = 10 ** (db / 20) * 32767 * np.sqrt(2)
    return (amp * np.sin(np.arange(n) / 5)).astype(np.int16).tobytes()


def test_level_and_speaking_hysteresis():
    t = [0.0]
    m = LevelMeter(clock=lambda: t[0])
    m.add("pm_bot", tone(-20)); out = m.tick()
    lv = [x for x in out if x["type"] == "level"][0]
    assert lv["participant_id"] == "pm_bot" and abs(lv["dbfs"] + 20) < 1.0
    assert {"type": "speaking", "participant_id": "pm_bot", "speaking": True, "t_ms": 0} in out
    # 静音 200ms 还不算闭口，300ms 才算
    stops = []
    for i in range(1, 6):
        t[0] = i * 100.0
        stops += [x for x in m.tick() if x["type"] == "speaking"]
    assert len(stops) == 1 and stops[0]["speaking"] is False and stops[0]["t_ms"] == 400
    # 静音后只报一次 -100，之后不再刷
    t[0] = 1000.0
    assert m.tick() == []
