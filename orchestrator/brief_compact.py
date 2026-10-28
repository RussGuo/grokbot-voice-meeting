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

# 段落标题与行前缀，随会议语言切换（未知语言按 zh）。
_T = {
    "zh": {"colon": "：", "none": "无", "sep": "、", "stance": "立场", "facts": "关键数字（会前快照）",
           "mandate": "授权", "red": "红线", "ask": "想问别人", "misc": "其他",
           "can": "可当场承诺", "must": "必须先问本体", "to": "问 ", "todo": "待办", "dec": "近期决策"},
    "en": {"colon": ": ", "none": "none", "sep": "; ", "stance": "Stance", "facts": "Key figures (pre-meeting snapshot)",
           "mandate": "Mandate", "red": "Red lines", "ask": "Questions for others", "misc": "Other",
           "can": "May commit on the spot", "must": "Must ask the principal first", "to": "Ask ", "todo": "To-do",
           "dec": "Recent decision"},
}


def _t(language: str) -> dict[str, str]:
    return _T.get((language or "zh").strip().lower()[:2], _T["zh"])


def _facts(snapshot: dict, language: str = "zh") -> list[str]:
    facts = snapshot.get("facts", [])
    # 带数字的事实优先（“关键数字”）
    facts = sorted(facts, key=lambda f: 0 if _NUM.search(f.get("value", "")) else 1)
    colon = _t(language)["colon"]
    return [f"- {f['key']}{colon}{f['value']}" for f in facts]


def compact_brief(brief: dict[str, Any], limit: int = BRIEF_LIMIT, language: str = "zh") -> tuple[str, bool, int]:
    """返回 (压缩后文本, 是否截断, 原始全量文本长度)。language 只影响标题和前缀，不影响 brief 内容。"""
    t = _t(language)
    pos = [f"- [{p['agenda_item_id']}] {p['stance']}" for p in brief.get("positions", [])]
    snap = brief.get("memory_snapshot", {}) or {}
    facts = _facts(snap, language)
    m = brief.get("mandate", {}) or {}
    c = t["colon"]
    mandate = [f"- {t['can']}{c}{t['sep'].join(m.get('can_commit', [])) or t['none']}",
               f"- {t['must']}{c}{t['sep'].join(m.get('must_ask', [])) or t['none']}"]
    red = [f"- {r}" for r in snap.get("red_lines", [])]
    qs = [f"- {t['to']}{q['to']}{c}{q['q']}" for q in brief.get("questions_for_others", [])]
    misc = [f"- {t['todo']}{c}{x.get('title')}" for x in snap.get("open_todos", [])] + \
           [f"- {t['dec']}{c}{d}" for d in snap.get("recent_decisions", [])]
    sections = [(t["stance"], pos), (t["facts"], facts), (t["mandate"], mandate),
                (t["red"], red), (t["ask"], qs), (t["misc"], misc)]
    br = ("【", "】") if t is _T["zh"] else ("[", "]")

    def render(secs) -> str:
        return "\n".join(f"{br[0]}{h}{br[1]}\n" + "\n".join(items) for h, items in secs if items)

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


def compact_snapshot(snapshot: dict[str, Any], limit: int = SNAPSHOT_LIMIT, language: str = "zh") -> tuple[str, bool]:
    lines = _facts(snapshot, language)
    text = "\n".join(lines)
    if len(text) <= limit:
        return text, False
    while lines and len("\n".join(lines)) > limit:
        lines.pop()
    return ("\n".join(lines) or text[:limit]), True
