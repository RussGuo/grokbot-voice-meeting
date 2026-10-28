"""Meeting language. Chinese is the default; English is selected with MEETING_LANG=en or scene.language.

Spoken rules, the moderator script, and end-of-meeting phrases go through here.
Sample scenarios stay Chinese unless the meeting's language field says otherwise.
"""
from __future__ import annotations

SUPPORTED = ("zh", "en")


def normalize(lang: str | None) -> str:
    lang = (lang or "zh").lower().strip()
    if lang.startswith("zh"):
        return "zh"
    if lang.startswith("en"):
        return "en"
    return "zh"


def rules(lang: str | None) -> str:
    if normalize(lang) == "en":
        return ("You are speaking in a multi-bot voice meeting. Be spoken and brief (at most 3 sentences). "
                "Use only the brief and the shared snapshots. If you cannot answer, say you will ask your principal.")
    return ("你在一场多 bot 语音会议里发言。口语化、简短（不超过 3 句）。"
            "只根据 brief 与快照回答，答不了就说“我问一下本体”。")


def style(lang: str | None) -> str:
    if normalize(lang) == "en":
        return ("You are speaking aloud in a meeting. Output only the words to say: one or two spoken sentences, "
                "no lists, no markdown, no parenthetical notes, do not call yourself an AI. "
                "Do not promise anything that is not in the notes, and do not offer to ask your principal unless the notes say so.")
    return ("你在一场多人语音会议里发言。只输出要说出口的话：口语中文，一到两句，不超过 50 字，"
            "不要列表、不要 markdown、不要括号注释、不要自称 AI；"
            "要点里没有的承诺或「去问本体」不要说。")


def turn(lang: str | None, intent: str) -> str:
    en = {
        "answer": "Respond to what was just said.",
        "relay": "Start with \"My principal replied\" and relay the answer.",
        "recap": "In one sentence, return to the earlier agenda item and introduce the principal's answer.",
        "fill_silence": "Summarize and invite additions.",
        "ask_human": "Hand the turn to the human.",
        "confirm_end": "The agenda is done. Ask the human whether to end the meeting.",
        "open": "Read out the agenda and the rules.",
        "open_item": "Announce the next agenda item.",
        "directive": "The human just directed you to speak. Address that request.",
        "cue": "Give a one-sentence position on the current item.",
        "warn": "Say that about ten seconds remain.",
    }
    zh = {
        "answer": "回应刚才的发言。",
        "relay": "以“本体回复了”开头转述本体答案。",
        "recap": "一句话回溯原议题，引出本体答案。",
        "fill_silence": "小结并邀请补充。",
        "ask_human": "把话轮交给用户。",
        "confirm_end": "议程已完成，询问用户是否结束会议。",
        "open": "宣读议程与规则。",
        "open_item": "宣布进入下一议程项。",
        "directive": "用户刚点名让你说。直接回应这个要求。",
        "cue": "用一句话说明你在当前议题上的立场。",
        "warn": "还剩大约十秒。",
    }
    table = en if normalize(lang) == "en" else zh
    return table.get(intent, intent)


def unknown(lang: str | None, reason: str) -> str:
    if normalize(lang) == "en":
        return f"unknown: {reason}"
    return f"未知：{reason}"


def is_unknown(value: str) -> bool:
    v = (value or "").strip().lower()
    return v.startswith("未知") or v.startswith("unknown")


def moderator_lines(lang: str | None, *, chief: bool) -> dict[str, str]:
    """Fixed moderator scripts keyed by intent. Format fields are filled by the caller."""
    if normalize(lang) == "en":
        opening = ("I'm the chief of staff, chairing this meeting, and I have my own view. I'll keep summaries neutral. "
                   if chief else "Welcome. ")
        return {
            "open": opening + "Today's topic is \"{item}\". You can interrupt any time. Commitments made here are binding and reconciled afterwards.",
            "confirm_end": "That's the agenda. Shall we end?",
            "open_item": "Next item: \"{item}\".",
            "recap": "Back to \"{recap}\" for a moment — a principal reply came in.",
            "fill_silence": "Quick recap of \"{item}\". Anything to add?",
            "ask_human": "The bots have gone a few rounds. What do you think?",
            "cue": "Please go ahead, {target}.",
            "warn": "About ten seconds left.",
        }
    opening = ("我是 Chief of Staff，兼任本场主持，我有自己的立场，小结会尽量中立。" if chief else "欢迎。")
    return {
        "open": opening + "今天的议题是「{item}」。规则：用户随时可以打断，分身承诺当场生效、会后对账。",
        "confirm_end": "议程都过完了。用户，我们是否结束？",
        "open_item": "进入下一项：「{item}」。",
        "recap": "插一句，回到刚才「{recap}」那一项，有本体的答复要补充。",
        "fill_silence": "我小结一下「{item}」目前的讨论，还有谁要补充？",
        "ask_human": "bot 们已经连续讨论了好几轮，用户，你怎么看？",
        "cue": "请{target}说说。",
        "warn": "还剩 10 秒。",
    }


def human_name_default(lang: str | None) -> str:
    return "You" if normalize(lang) == "en" else "用户"


def moderator_name_default(lang: str | None) -> str:
    return "Host" if normalize(lang) == "en" else "主持人"
