"""举手打分用的文本相似度：中文字二元组 + 英文词（小写、去停用词、粗粒度词干）。

纯内存、无依赖。中文部分与原先完全一致（字二元组）；英文部分是新增的：
- 小写后只留 [a-z0-9]+，丢掉停用词和单字母（数字保留）
- 粗词干：复数 s/es/ies、-ing、-ed、结尾 e、叠辅音（running→run），只求「同一个词的几种形态落到同一个串」，不求词典正确
- 事实匹配：key 按 ._-/空白 和 camelCase 拆，value 里取数字和关键词，LLM 写的英文 brief 也能命中
"""
from __future__ import annotations

import re

_EN_STOP = frozenset("""
a an the and or but if so nor yet of to in on at for with by from as into onto over under about
above after again against all also am any are aren be been being both can cannot could couldn did didn do does doesn
doing don down during each either else ever few get gets got had hadn has hasn have haven having he her here hers him his
how i is isn it its itself just let lets like ll may maybe me might more most much must my myself no not now off ok okay
once only other our ours out own re really same say said shall she should shouldn since some such than that the their
them then there these they this those though through thus too up upon us very ve was wasn we well were weren what when
where whether which while who whom whose why will with won would wouldn yes you your yours yourself
""".split())

# 事实 value 里常见的占位词，不能当「关键词」
_VALUE_NOISE = frozenset("""
example sample data fictional illustrative unknown unspecified none approx approximately around roughly
""".split())

_WORD = re.compile(r"[a-z0-9]+")
_NUM = re.compile(r"\d+(?:\.\d+)?")
_HAN = re.compile(r"[一-鿿]")
_VOWEL = re.compile(r"[aeiouy]")


def stem(w: str) -> str:
    """粗词干。含数字的词（q3、v2、4k）原样返回。"""
    if len(w) <= 3 or any(c.isdigit() for c in w):
        return w
    if len(w) > 4 and w.endswith(("ies", "ied")):
        return w[:-3] + "y"
    if w.endswith(("sses", "shes", "ches", "xes", "zes")):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    for suf in ("ing", "ed"):
        if w.endswith(suf) and len(w) - len(suf) >= 3 and _VOWEL.search(w[:-len(suf)]):
            w = w[:-len(suf)]
            if w[-1] == w[-2] and w[-1] not in "lsz":     # running → run
                w = w[:-1]
            break
    if w.endswith("e") and len(w) > 3:                     # schedule/scheduling 落到同一个串
        w = w[:-1]
    return w


def has_han(s: str) -> bool:
    return bool(_HAN.search(s))


def en_terms(text: str) -> list[str]:
    """英文内容词（已小写、去停用词、词干化）。数字原样保留。"""
    out = []
    for w in _WORD.findall(text.lower()):
        if w in _EN_STOP or (len(w) == 1 and not w.isdigit()):
            continue
        out.append(stem(w))
    return out


def tokens(text: str) -> set[str]:
    """中文按字二元组 + 英文内容词。"""
    text = text.lower()
    han = "".join(_HAN.findall(text))
    bigrams = {han[i:i + 2] for i in range(len(han) - 1)}
    return set(en_terms(text)) | bigrams


def overlap(ta: set[str], tb: set[str]) -> float:
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def similarity(a: str, b: str) -> float:
    return overlap(tokens(a), tokens(b))


def match_words(text: str) -> set[str]:
    """用于关键词 / 事实命中：英文内容词 + 数字（含小数，如 1.5）。"""
    low = text.lower()
    return set(en_terms(low)) | set(_NUM.findall(low))


# ———————————————————— 关键词与事实命中 ————————————————————

def keyword_hit(k: str, low: str, words: set[str]) -> bool:
    """low = 小写原文，words = match_words(原文)。含汉字的关键词按子串，英文按词干化后的词集合。"""
    k = k.strip().lower()
    if not k:
        return False
    if _HAN.search(k):
        return k in low
    ks = en_terms(k)
    if ks:
        return all(t in words for t in ks)
    return False if _WORD.fullmatch(k.replace(" ", "")) else k in low   # 如 "c++"、"v2.0"


def _split_key(key: str) -> list[str]:
    key = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", key)             # offlineSync → offline Sync
    key = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", key)          # APILatency → API Latency
    return [p for p in re.split(r"[._\-/\s,，:：;；]+", key) if p]


def fact_terms(key: str, value: str) -> list[str]:
    """一条事实可被谈话命中的词：key 的片段 + value 里的数字与关键词。

    含汉字的片段按子串匹配（原有行为）；其余是小写词干，按词集合匹配。value 里的汉字不取
    （「示例数据：…」这类套话会让一切都命中），但取其中的英文词和数字。单个数字（2、3）太泛，不取。
    """
    terms: list[str] = []
    for part in _split_key(key):
        if _HAN.search(part):
            if len(part) >= 2:
                terms.append(part.lower())
        else:
            terms += [t for t in en_terms(part) if len(t) >= 2]
    for t in en_terms(value):
        if t.isdigit() or t in _VALUE_NOISE or (len(t) < 3 and not any(c.isdigit() for c in t)):
            continue
        terms.append(t)
    for n in _NUM.findall(value):
        if len(n) >= 2:
            terms.append(n)
    return list(dict.fromkeys(terms))


def fact_hits(terms: list[str], low: str, words: set[str]) -> int:
    return sum(1 for t in terms if (t in low if _HAN.search(t) else t in words))
