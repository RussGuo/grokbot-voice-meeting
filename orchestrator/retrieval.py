"""会中检索本体的完整上下文：分段 + 每个 bot 一份 BM25 倒排索引，纯内存、离线、无依赖。

分身只带 ~800 字的 brief，brief 里没有的内容会说「我要问本体」。`meet` 把每个 bot 的完整 context 切成段落
（`links/<room>.context.json`，场景里只带路径与 sha256），worker 启动时建索引；发言规划（planner）、
FakeLLM 回退、举手打分（RuleScorer）都用 `ContextIndex.search` 取回自己笔记里的相关原文。

- 分词复用 textsim（中文字二元组 + 英文去停用词词干），这里只加 BM25 与归一化。
- 每个 bot 只能搜自己的笔记：search(bot_id, ...) 不会跨 bot。
- 归一化分数 = BM25 / 「查询词全部以饱和词频命中」的理论上限，落在 [0, 1]：衡量的是这段覆盖了查询里多少
  （按 idf 加权的）信息；分母取主查询里 idf 最高的 IDEAL_TERMS 个词，所以长查询不会被稀释。索引里根本没有的词 idf 最高，
  仍留在分母里（打 UNKNOWN_WEIGHT 折扣），一堆闲聊词只会拉低分数而不是抬高。
- 辅助查询 extra（举手要点、议程标题）的词只给命中加分，不进分母：议程标题里没出现在笔记里的词不会把分数稀释掉。
- 会中加 bot：add_bot 在调用线程里建好再整体换入（dict 赋值原子），可以在 to_thread 里调用，
  事件循环上的 search 不会看到半成品。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from . import textsim

log = logging.getLogger("retrieval")

BUNDLE_VERSION = 1
CHUNK_TARGET = 400                 # 段落目标长度（字符）
CHUNK_LIMIT = 500                  # 单段硬上限
PER_BOT_BYTES = 200_000            # 每个 bot 的文本上限（UTF-8 字节）
TOTAL_BYTES = 2_000_000            # 整个 bundle 的文本上限
MAX_BUNDLE_FILE = 8_000_000        # 读文件的上限（含 JSON 开销）
MIN_SCORE = 0.2                    # 归一化分数低于此值不返回（经验值，见 test_retrieval 的标定样例）
ZH_SCALE = 2.0                     # 含汉字的查询把分数乘上它：字二元组比英文词散，同样相关的原始分数只有英文的一半左右
IDEAL_TERMS = 8                    # 归一化分母只取查询里 idf 最高的这么多个词，长查询不会被稀释
UNKNOWN_WEIGHT = 0.5               # 索引里根本没有的查询词（闲聊、寒暄）在分母里按这个折扣计，别让它们把真正的命中压下去
K1, B = 1.2, 0.75

# 查询里含这些虚字的汉字二元组不参与检索（「怎么」「你们」「这个」…）；只过滤查询，文档里的二元组原样保留
_ZH_FUNC = frozenset("的了吗呢吧啊么什怎样这那个们我你他她它是在和就都也很还把被让给跟与及或但而却又再才只")
_HEADING_PUNCT = "。.!?！？;；,，"
_SENT_SPLIT = re.compile(r"(?<=[。！？；!?;])|(?<=[.!?])\s+|\n")
_BLOCK_SPLIT = re.compile(r"\n\s*\n")
_LIST_ITEM = re.compile(r"^(?:[-*•·]\s|\d+[.)、]\s*)")
_MD_HEAD = re.compile(r"^#{1,6}\s*")


@dataclass(frozen=True)
class Chunk:
    id: str
    text: str


@dataclass(frozen=True)
class Hit:
    id: str
    text: str
    score: float            # 归一化到 [0, 1]

    def first_sentence(self, limit: int = 120) -> str:
        return first_sentence(self.text, limit)


# ———————————————————— 分段 ————————————————————

def _is_heading(block: str) -> bool:
    """单行短块：markdown 标题，或以冒号结尾，或不带句末标点的短行（且不是列表项）。"""
    if "\n" in block or len(block) > 60:
        return False
    if block.startswith("#"):
        return True
    if block.endswith((":", "：")):
        return True
    return len(block) <= 40 and not block.endswith(tuple(_HEADING_PUNCT)) and not _LIST_ITEM.match(block)


def _clean_heading(h: str) -> str:
    return "\n".join(_MD_HEAD.sub("", ln).strip() for ln in h.split("\n") if ln.strip())


def _hard_split(s: str, limit: int) -> list[str]:
    out = []
    while len(s) > limit:
        cut = max(s.rfind(c, limit // 2, limit) for c in (" ", "，", ",", "、"))
        cut = cut + 1 if cut > 0 else limit
        out.append(s[:cut].strip())
        s = s[cut:]
    if s.strip():
        out.append(s.strip())
    return out


def _pack(block: str, target: int, limit: int) -> list[str]:
    """长块按句子装箱到 ≤ target；单句超过 limit 再硬切。"""
    sents = [s.strip() for s in _SENT_SPLIT.split(block) if s and s.strip()]
    out: list[str] = []
    cur = ""
    for s in sents:
        if len(s) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.extend(_hard_split(s, limit))
            continue
        if cur and len(cur) + 1 + len(s) > target:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur}\n{s}" if cur else s
    if cur:
        out.append(cur)
    return out


def chunk_text(text: str, target: int = CHUNK_TARGET, limit: int = CHUNK_LIMIT) -> list[str]:
    """按空行 / 标题 / 句子把文本切成 ~target 字的段落。短标题并进它后面的正文；小段落合并到 target，但不跨标题合并。"""
    blocks = [b.strip() for b in _BLOCK_SPLIT.split((text or "").replace("\r\n", "\n").replace("\r", "\n")) if b.strip()]
    units: list[tuple[str, bool]] = []          # (文本, 以标题开头)
    heading = ""
    for b in blocks:
        if _is_heading(b):
            heading = f"{heading}\n{_clean_heading(b)}" if heading else _clean_heading(b)
            if len(heading) > 120:
                units.append((heading, True))
                heading = ""
            continue
        body = f"{heading}\n{b}" if heading else b
        pieces = [body] if len(body) <= limit else _pack(body, target, limit)
        for i, piece in enumerate(pieces):
            units.append((piece, bool(heading) and i == 0))
        heading = ""
    if heading:
        units.append((heading, True))
    out: list[str] = []
    cur = ""
    for piece, headed in units:
        if cur and not headed and len(cur) + 1 + len(piece) <= target:
            cur = f"{cur}\n{piece}"
        else:
            if cur:
                out.append(cur)
            cur = piece
    if cur:
        out.append(cur)
    return [c for c in out if c.strip()]


def chunk_bot(bot_id: str, text: str, max_bytes: int = PER_BOT_BYTES) -> tuple[list[Chunk], bool]:
    """一个 bot 的全部 context → Chunk 列表（id = <bot_id>#<n>，n 从 1 起）。超过 max_bytes 丢弃尾部，返回 truncated。"""
    chunks: list[Chunk] = []
    used = 0
    truncated = False
    for piece in chunk_text(text):
        size = len(piece.encode("utf-8"))
        if used + size > max_bytes:
            truncated = True
            break
        used += size
        chunks.append(Chunk(f"{bot_id}#{len(chunks) + 1}", piece))
    return chunks, truncated


def first_sentence(text: str, limit: int = 120) -> str:
    """取第一句（遇到句末标点为止），超过 limit 截断。开头并进来的短标题行跳过（念出来不通顺）。"""
    lines = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
    while len(lines) > 1 and _is_heading(lines[0]):
        lines.pop(0)
    s = " ".join(" ".join(lines).split())
    m = re.search(r"[。！？；!?;]|[.!?](?=\s|$)", s)
    if m and m.end() >= 4:
        s = s[:m.end()]
    return s if len(s) <= limit else s[:limit].rstrip() + "…"


# ———————————————————— 索引 ————————————————————

def query_terms(query: str) -> set[str]:
    """查询词：textsim 的词项，去掉含虚字的中文二元组。"""
    return {t for t in textsim.token_list(query)
            if not (len(t) == 2 and textsim.has_han(t) and (t[0] in _ZH_FUNC or t[1] in _ZH_FUNC))}


class _BotIndex:
    """一个 bot 的 BM25 倒排索引。构建后只读。"""

    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self.postings: dict[str, list[tuple[int, int]]] = {}
        self.dl: list[int] = []
        for i, c in enumerate(chunks):
            toks = textsim.token_list(c.text)
            self.dl.append(len(toks))
            for t, n in Counter(toks).items():
                self.postings.setdefault(t, []).append((i, n))
        self.avgdl = (sum(self.dl) / len(self.dl)) if self.dl else 1.0

    def search(self, query: str, k: int, min_score: float, extra: str = "") -> list[Hit]:
        n = len(self.chunks)
        main = query_terms(query)
        side = query_terms(extra) - main
        if not main:                      # 主查询没有可检索的词（「好的，请讲」）：辅助词顶上来
            main, side = side, set()
        if not n or not main:
            return []
        scale = ZH_SCALE if textsim.has_han(query + extra) else 1.0
        scores: dict[int, float] = {}
        weights: list[float] = []
        for t in main | side:
            plist = self.postings.get(t)
            df = len(plist) if plist else 0
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            if t in main:                 # 辅助词（要点、议程标题）只给命中加分，不进分母
                weights.append(idf * (K1 + 1) * (1.0 if df else UNKNOWN_WEIGHT))
            for i, tf in plist or ():
                norm = tf + K1 * (1 - B + B * self.dl[i] / self.avgdl)
                scores[i] = scores.get(i, 0.0) + idf * tf * (K1 + 1) / norm
        ideal = sum(sorted(weights, reverse=True)[:IDEAL_TERMS])
        if not scores or ideal <= 0:
            return []
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        out = []
        for i, s in ranked[:max(k, 0)]:
            score = min(scale * s / ideal, 1.0)
            if score < min_score:
                break
            out.append(Hit(self.chunks[i].id, self.chunks[i].text, round(score, 4)))
        return out


class ContextIndex:
    """所有 bot 的笔记索引。空索引（没有任何 bot）的行为等同于没有索引：search 一律返回 []。"""

    def __init__(self) -> None:
        self._bots: dict[str, _BotIndex] = {}

    # —— 构建 ——
    def add_chunks(self, bot_id: str, chunks: Iterable[Chunk]) -> int:
        """登记一个 bot 的现成段落（来自 bundle）。已有同名 bot 则整体替换。返回段数。"""
        chunks = list(chunks)
        if chunks:
            self._bots[bot_id] = _BotIndex(chunks)      # 先建好再换入：别的线程不会看到半成品
        else:
            self._bots.pop(bot_id, None)
        return len(chunks)

    def add_bot(self, bot_id: str, text: str) -> int:
        """会中加 bot：对 text 分段并建索引（超过每 bot 上限会截断并 warning）。返回段数。"""
        chunks, truncated = chunk_bot(bot_id, text)
        if truncated:
            log.warning("bot %s 的笔记超过 %d KB，已截断", bot_id, PER_BOT_BYTES // 1000)
        return self.add_chunks(bot_id, chunks)

    # —— 查询 ——
    def has(self, bot_id: str) -> bool:
        return bot_id in self._bots

    def bots(self) -> list[str]:
        return list(self._bots)

    def chunk_count(self, bot_id: Optional[str] = None) -> int:
        if bot_id is not None:
            ix = self._bots.get(bot_id)
            return len(ix.chunks) if ix else 0
        return sum(len(ix.chunks) for ix in list(self._bots.values()))

    def __bool__(self) -> bool:
        return bool(self._bots)

    def search(self, bot_id: str, query: str, k: int = 3, min_score: float = MIN_SCORE, extra: str = "") -> list[Hit]:
        """只在 bot_id 自己的笔记里搜。没有这个 bot / 没有命中返回 []。分数已归一化到 [0, 1]，中英文可比。
        extra 是辅助查询（举手要点、议程标题）：它的词只给命中加分，不进归一化分母，免得一个议程标题把分数稀释掉。"""
        ix = self._bots.get(bot_id)
        if ix is None or not query or not query.strip():
            return []
        return ix.search(query, k, min_score, extra)

    # —— 读 bundle ——
    @classmethod
    def load(cls, path: str | Path, sha256: str) -> Optional["ContextIndex"]:
        """读 `links/<room>.context.json`，校验 sha256，建索引。文件缺失 / 哈希不符 / 内容不合法一律返回 None（并 warning），
        调用方继续开会，只是没有笔记检索。"""
        try:
            p = Path(path)
            if p.stat().st_size > MAX_BUNDLE_FILE:
                log.warning("context bundle 过大（%s），忽略", p.name)
                return None
            raw = p.read_bytes()
        except (OSError, TypeError, ValueError) as e:
            log.warning("读不到 context bundle（%s），本场没有笔记检索", type(e).__name__)
            return None
        if hashlib.sha256(raw).hexdigest() != str(sha256).strip().lower():
            log.warning("context bundle 的 sha256 不符，忽略（文件被改过或不是这场的）")
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
            index = cls()
            if not isinstance(data, dict) or data.get("v") != BUNDLE_VERSION or not isinstance(data.get("bots"), dict):
                raise ValueError("bad shape")
            for bid, blob in data["bots"].items():
                items = blob.get("chunks") if isinstance(blob, dict) else None
                if not isinstance(items, list):
                    raise ValueError("bad chunks")
                index.add_chunks(str(bid), [Chunk(str(c["id"]), str(c["text"])) for c in items
                                            if isinstance(c, dict) and c.get("id") and c.get("text")])
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as e:
            log.warning("context bundle 内容不合法（%s），忽略", type(e).__name__)
            return None
        return index


# ———————————————————— 给发言 / 打分用的小工具 ————————————————————

def turn_query(meta: dict[str, Any]) -> tuple[str, str]:
    """一轮发言的检索词 (主, 辅)：主 = 用户点名内容或上一条发言；辅 = 举手时的要点 + 议程标题。search(bot, *turn_query(meta))。"""
    main = str(meta.get("directive_text") or meta.get("last_text") or "")
    return main, " ".join(str(x) for x in (meta.get("gist"), meta.get("agenda_title")) if x)


def format_excerpts(hits: list[Hit], budget: int = 1200) -> str:
    """每条以 [id] 开头，总长不超过 budget；放不下的最后一条截断，剩余不足 80 字就不要了。"""
    lines: list[str] = []
    left = budget
    for h in hits:
        body = " ".join(h.text.split())
        head = f"[{h.id}] "
        room = left - len(head)
        if room < 80 and lines:
            break
        if len(body) > room:
            body = body[:max(room - 1, 0)].rstrip() + "…"
        lines.append(head + body)
        left -= len(lines[-1]) + 1
        if left <= 0:
            break
    return "\n".join(lines)
