"""Pull spoken commitments out of a bot utterance.

The planner sometimes forgets to flag a promise that was actually said.
We only accept phrases the bot's mandate already allows (can_commit), and only
when the utterance itself uses promise language. Nothing is invented.
"""
from __future__ import annotations

_PROMISE_CUES = (
    "答应", "承诺", "我来做", "我负责", "我来负责", "包在我", "我来跟进",
    "i will", "i'll", "i commit", "i promise", "i'll take", "leave it to me", "leave it with me",
)


def commitments_in_speech(text: str, can_commit: list[str] | None) -> list[str]:
    if not text or not can_commit:
        return []
    low = text.lower()
    if not any(cue.lower() in low for cue in _PROMISE_CUES):
        return []
    found: list[str] = []
    for item in can_commit:
        if item and item in text and item not in found:
            found.append(item)
    return found
