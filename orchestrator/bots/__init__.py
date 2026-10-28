"""示例会议与 brief 加载（示例数据，全部虚构）。"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import human_identity
from ..models import Brief, Meeting

HERE = Path(__file__).parent


def _replace_human(obj, hid: str):
    if isinstance(obj, str):
        return hid if obj == "__HUMAN__" else obj
    if isinstance(obj, list):
        return [_replace_human(x, hid) for x in obj]
    if isinstance(obj, dict):
        return {k: _replace_human(v, hid) for k, v in obj.items() if not k.startswith("_")}
    return obj


SCENARIOS = {"example": (HERE, "meeting_example.json"), "sim_v1": (HERE / "sim_v1", "meeting.json")}


def load_example(human_id: str | None = None, moderator_mode: str | None = None,
                 scenario: str = "example") -> tuple[Meeting, dict[str, Brief]]:
    """human_id 默认取 MEETING_HUMAN_IDENTITY（缺省 host），保证等于用户的 LiveKit identity。

    moderator_mode：None 用示例文件默认（v0 = chiefOfStaff）；"neutral" 加入内置中立主持人并由它主持，
    Chief of Staff 此时作为普通成员参会。
    """
    hid = human_id or human_identity()
    folder, fname = SCENARIOS[scenario]
    raw = _replace_human(json.loads((folder / fname).read_text("utf-8")), hid)
    opts = raw.pop("moderator_options", {})
    if moderator_mode == "neutral":
        if "neutral" not in opts:
            raise ValueError(f"场景 {scenario} 没有 neutral 主持配置")
        n = opts["neutral"]
        raw["moderator"] = n["moderator"]
        raw["participants"].insert(1, n["participant"])
        for p in raw["participants"]:
            if p["id"] == "chief_of_staff":
                p["role"] = "member"
    elif moderator_mode not in (None, "chiefOfStaff"):
        raise ValueError(f"未知主持模式 {moderator_mode}")
    meeting = Meeting.from_dict(raw)
    meeting.voice.linked_human_id = hid
    briefs = {}
    for f in sorted(folder.glob("*.brief.json")):
        b = Brief.from_dict(_replace_human(json.loads(f.read_text("utf-8")), hid))
        briefs[b.participant_id] = b
    return meeting, briefs
