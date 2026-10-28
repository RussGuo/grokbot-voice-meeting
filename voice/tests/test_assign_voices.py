import pytest

from voice_layer.config import SIM_HUMAN_SPEAKER, VERIFIED_VOICE_POOL, VoiceConfig, apply_voices, assign_voices

G = dict(VERIFIED_VOICE_POOL)


def test_unique_alternating():
    ids = ["chief_of_staff", "bot_a", "bot_b", "bot_c", "bot_d", "bot_e"]
    m = assign_voices(ids, sim_human=True)
    assert len(set(m.values())) == len(ids)
    assert SIM_HUMAN_SPEAKER not in m.values()
    gs = [G[m[i]] for i in ids]
    assert all(a != b for a, b in zip(gs, gs[1:]))


def test_preferred_and_overflow():
    m = assign_voices(["a", "b"], preferred={"a": "zh_male_m191_uranus_bigtts"})
    assert m["a"] == "zh_male_m191_uranus_bigtts" and G[m["b"]] == "F"
    with pytest.raises(ValueError):
        assign_voices([f"b{i}" for i in range(8)], sim_human=True)


def test_apply():
    cfg = VoiceConfig()
    apply_voices(cfg, {"chief_of_staff": "zh_male_m191_uranus_bigtts"})
    assert cfg.bot_voice("chief_of_staff").volc_speaker == "zh_male_m191_uranus_bigtts"
