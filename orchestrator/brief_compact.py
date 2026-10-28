"""brief / 主题快照压缩：系统提示里的 brief 压到 800 字以内。

保留优先级（高→低）：立场 stance > 关键数字（快照 facts 的 key/value）> mandate（canCommit/mustAsk）> 红线 >
想问别人的问题 > 待办 / 近期决策。超长时从低优先级开始丢，仍超长则逐条截短 facts、最后截短立场；
截断时返回 truncated=True，调用方记一条日志（事件 ContextTruncated）。
"""
from __future__ import annotations

import re
from typing import Any

BRIEF_LIMIT = 800
SNAPSHOT_LIMIT = 300      # 其他参会方共享快照，每方上限（只留关键数字）

_NUM = re.compile(r"\d")


def _facts(snapshot: dict) -> list[str]:
    facts = snapshot.get("facts", [])
    # 带数字的事实优先（“关键数字”）
    facts = sorted(facts, key=lambda f: 0 if _NUM.search(f.get("value", "")) else 1)
    return [f"- {f['key']}：{f['value']}" for f in facts]


def compact_brief(brief: dict[str, Any], limit: int = BRIEF_LIMIT) -> tuple[str, bool, int]:
    """返回 (压缩后文本, 是否截断, 原始全量文本长度)。"""
    pos = [f"- [{p['agenda_item_id']}] {p['stance']}" for p in brief.get("positions", [])]
    snap = brief.get("memory_snapshot", {}) or {}
    facts = _facts(snap)
    m = brief.get("mandate", {}) or {}
    mandate = [f"- 可当场承诺：{'、'.join(m.get('can_commit', [])) or '无'}",
               f"- 必须先问本体：{'、'.join(m.get('must_ask', [])) or '无'}"]
    red = [f"- {r}" for r in snap.get("red_lines", [])]
    qs = [f"- 问 {q['to']}：{q['q']}" for q in brief.get("questions_for_others", [])]
    misc = [f"- 待办：{t.get('title')}" for t in snap.get("open_todos", [])] + \
           [f"- 近期决策：{d}" for d in snap.get("recent_decisions", [])]
    sections = [("立场", pos), ("关键数字（会前快照）", facts), ("授权", mandate),
                ("红线", red), ("想问别人", qs), ("其他", misc)]

    def render(secs) -> str:
        return "\n".join(f"【{h}】\n" + "\n".join(items) for h, items in secs if items)

    full = render(sections)
    if len(full) <= limit:
        return full, False, len(full)
    secs = list(sections)
    # 1) 从低优先级整段丢弃（立场、关键数字、授权必留）
    while len(render(secs)) > limit and len(secs) > 3:
        secs.pop()
    # 2) 仍超长：从后往前丢 facts（不带数字的在后）
    facts_items = list(secs[1][1])
    while len(render(secs)) > limit and len(facts_items) > 1:
        facts_items.pop()
        secs[1] = (secs[1][0], facts_items)
    # 3) 仍超长：硬截断（立场放最前，尽量保住）
    text = render(secs)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text, True, len(full)


def compact_snapshot(snapshot: dict[str, Any], limit: int = SNAPSHOT_LIMIT) -> tuple[str, bool]:
    lines = _facts(snapshot)
    text = "\n".join(lines)
    if len(text) <= limit:
        return text, False
    while lines and len("\n".join(lines)) > limit:
        lines.pop()
    return ("\n".join(lines) or text[:limit]), True
