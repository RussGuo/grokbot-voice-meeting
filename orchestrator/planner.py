"""真实 LLM 规划器：由模型决定「说什么」和「是什么意图」，确定性护栏留在代码里。

协议：每轮只调一次流式 LLM，输出「单行 JSON 头 + 换行 + 口语正文」：
  {"act":"speak|ask_principal|commit","question":str|null,"commit":str|null,
   "confidence":0..1,"provenance":"brief|snapshot|model","addressees":[id]}
头一到齐（通常在前 ~40 个 token 内）就能校验意图和承诺，头本身不会被念出来；头之后的正文边到边送 TTS。
选头+正文而不是 function tool call 的原因：content 流对所有 OpenAI 兼容供应商一致、复用现有 astream_text，
并且 tool call 的参数是 JSON 增量，正文要等整个调用结束才有，首句延迟更差；头先于正文也保证了
「未授权承诺 / 低把握」在第一个字出口之前就能拦下。

护栏（全在代码里，不靠 prompt）：
  a. last/directive 命中 mandate.must_ask → 不调 LLM，直接走问本体；
  b. 头里的 commit 必须匹配 mandate.can_commit，否则中止流，改说安全话术并 ask_principal；
  c. relay（转述本体答案）不调 LLM，原样念；
  d. 被点名且 confidence < 0.6 → 问本体。
  e. 笔记检索：有 context_index 时，成员发言前从「该 bot 自己」的完整笔记里取回相关原文放进提示词（上限 ~1200 字，每条带 id）；
     头里的 sources 只保留真的取回过的 id，provenance=context 没有有效 sources 就降成 model。
任何 LLM 错误 / 超时 / 头格式错 / 正文为空 → 回退 FakeLLM 的规划（文本与字段），原因记在 stats["fallback"]。
主持人的程序性话术仍用模板；只有 fill_silence 可选用 LLM（失败回模板）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, AsyncIterator, Callable, Optional, Protocol, runtime_checkable

from .chunker import FLUSH
from .fake_llm import CONFIDENCE_THRESHOLD, FakeLLM
from .i18n import inline_principal_lines, normalize
from .models import ID, Brief, ChatCtxRecipe, Meeting
from .retrieval import Hit, format_excerpts, turn_query

log = logging.getLogger("planner")

ACTS = ("speak", "ask_principal", "commit")
PROVENANCES = ("brief", "snapshot", "model", "context")
HEADER_MAX_CHARS = 400
HEADER_TIMEOUT_S = 2.5
STALL_TIMEOUT_S = 8.0        # 正文开始后，相邻两块之间最长等待；超时就地收尾（不再回退，已经开口了）
DEFAULT_MAX_TOKENS = 160     # 含头
# principal_query_id / principal_pending：当场问过本体（见 InlineAsk），编排器据此不再重复提问
FIELD_KEYS = ("provenance", "confidence", "commitments", "addressees", "ask_principal",
              "principal_query_id", "principal_pending", "sources")
DEFAULT_INLINE_MS = 5000     # MEETING_PRINCIPAL_INLINE_MS：同一轮里等本体答复的预算，0 = 关闭
EXCERPT_K, EXCERPT_CHARS = 3, 1200     # 每轮最多取回几段、提示词里原文总长上限

_SAFE_ASK = {
    "zh": "这个我需要和本体确认后再答复。",
    "en": "I need to check that with my principal and get back to you.",
}


# ———————————————————— 小工具 ————————————————————
def _norm(s: str) -> str:
    """比较用归一化：小写，只留字母数字（中日韩字符算字母），丢空白与标点。"""
    return "".join(c for c in (s or "").lower() if c.isalnum())


def _contains(a: str, b: str) -> bool:
    """归一化后任一方向的子串匹配；过短（<2）的项不参与，避免一个字符就命中。"""
    na, nb = _norm(a), _norm(b)
    if len(na) < 2 or len(nb) < 2:
        return False
    return nb in na or na in nb


def match_commit(commit: str, can_commit: list[str]) -> Optional[str]:
    """头里的 commit 命中 can_commit 中的哪一项（返回 can_commit 里的原文，便于编排器去重），不中返回 None。"""
    for item in can_commit or []:
        if item and _contains(commit, item):
            return item
    return None


def hit_must_ask(text: str, must_ask: list[str]) -> Optional[str]:
    nt = _norm(text)
    for term in must_ask or []:
        nterm = _norm(term)
        if nterm and nterm in nt:
            return term
    return None


def safe_ask_line(lang: str) -> str:
    return _SAFE_ASK[normalize(lang)]


def _clamp01(v: Any, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:      # NaN
        return default
    return max(0.0, min(1.0, f))


# ———————————————————— 头解析 ————————————————————
class HeaderError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class HeaderParser:
    """增量解析首个 JSON 对象。可能被切成任意多块；容忍前导空白和 ```json 围栏。

    feed() 返回 None 表示还要更多数据；返回 (header_dict, rest_text) 表示完成，rest 是头之后已收到的正文。
    """

    def __init__(self, cap: int = HEADER_MAX_CHARS):
        self.cap = cap
        self.buf = ""
        self._held: Optional[dict] = None
        self._dec = json.JSONDecoder()

    def feed(self, piece: str) -> Optional[tuple[dict, str]]:
        self.buf += piece
        s = self.buf.lstrip()
        fenced = False
        if s.startswith("```"):
            nl = s.find("\n")
            if nl < 0:
                return self._check_cap(None)
            fenced, s = True, s[nl + 1:].lstrip()
        if not s:
            return self._check_cap(None)
        if not s.startswith("{"):
            raise HeaderError("no_header")
        try:
            obj, end = self._dec.raw_decode(s)
        except json.JSONDecodeError:
            return self._check_cap(None)
        if not isinstance(obj, dict):
            raise HeaderError("bad_header")
        rest = s[end:]
        if fenced:                       # 头后可能跟着收尾围栏：等它到齐再放行（流提前结束由 finish() 兜底）
            r = rest.lstrip()
            if r.startswith("```"):
                nl = r.find("\n")
                if nl < 0:
                    self._held = obj
                    return self._check_cap(None)
                rest = r[nl + 1:]
            elif not r or "```".startswith(r):
                self._held = obj
                return self._check_cap(None)
        return obj, rest

    def finish(self) -> Optional[tuple[dict, str]]:
        """流结束时调用：头已完整、只是在等收尾围栏，就按「头 + 空正文」放行。"""
        held, self._held = self._held, None
        return (held, "") if held is not None else None

    def _check_cap(self, v: None) -> None:
        if len(self.buf) > self.cap:
            raise HeaderError("header_too_long")
        return v


# ———————————————————— 接口 ————————————————————
class PlanStream:
    """一轮规划的结果：fields 在头解析完（或回退/护栏决定）之后即定稿，正文是异步迭代器。

    fields 的键见 FIELD_KEYS（ask_principal 仅在需要问本体时出现）。fields 一定在第一段正文产出之前定稿；
    `await ready()` 会驱动生成器直到那一刻（预取第一段，之后迭代时先吐出它）。
    stats：path / header_ms / fallback / guard / act / truncated 等，供 live_voice 写入 llm_stats。
    """

    def __init__(self, gen: Optional[AsyncIterator[str]], fields: Optional[dict] = None,
                 stats: Optional[dict] = None, planned_text: str = ""):
        self._gen = gen
        self._peek: list[str] = []
        self._started = False
        self.fields: dict[str, Any] = fields if fields is not None else {
            "provenance": "model", "confidence": 0.5, "commitments": [], "addressees": []}
        self.stats: dict[str, Any] = stats if stats is not None else {}
        self.planned_text = planned_text      # 一个字没说出口就被打断时，bot.utterance 用它兜底

    def attach(self, gen: AsyncIterator[str]) -> "PlanStream":
        """生成器要引用 PlanStream 本身（写 fields / stats）时，先建对象再挂生成器。"""
        self._gen = gen
        return self

    async def ready(self) -> dict[str, Any]:
        if not self._started:
            self._started = True
            try:
                self._peek.append(await self._gen.__anext__())
            except StopAsyncIteration:
                pass
        return self.fields

    def __aiter__(self) -> AsyncIterator[str]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[str]:
        self._started = True
        while self._peek:
            yield self._peek.pop(0)
        async for p in self._gen:
            yield p

    async def aclose(self) -> None:
        try:
            await self._gen.aclose()
        except Exception:  # noqa: BLE001  生成器正在被另一处驱动时 aclose 会抛，忽略
            pass


def static_stream(plan: dict, stats: Optional[dict] = None) -> PlanStream:
    """把一个现成的 plan dict（FakeLLM 输出）包成 PlanStream。"""
    async def gen():
        yield plan["text"]

    fields = {k: plan[k] for k in FIELD_KEYS if k in plan}
    fields.setdefault("commitments", [])
    fields.setdefault("addressees", [])
    return PlanStream(gen(), fields, {"path": "static", **(stats or {})}, plan["text"])


@runtime_checkable
class Planner(Protocol):
    async def plan(self, pid: ID, ctx: ChatCtxRecipe) -> PlanStream: ...


# ———————————————————— 当场问本体 ————————————————————
AskHook = Callable[[ID, str], "Optional[asyncio.Future[str]]"]


class InlineAsk:
    """要问本体的话轮，在同一轮里等答复：先垫一句，最多等 budget_s 秒，答到了就原样念出来。

    hook(pid, question) 返回 Future[str]（Orchestrator.request_principal）。规划器只拿 Future，不碰编排器状态。
    hook 为 None / 返回 None / Future 一开始就带异常（阶段不对、pending 已满）→ 不垫那句话，退回旧行为：
    安全话术 + ask_principal 字段，由 bot.utterance 尾部走慢路径。
    话轮结束时（超时、被打断、生成器被关）一律 cancel Future：此后到的答复编排器看到等待者已取消，走 pendingAnswer。
    用 asyncio.wait 而不是 shield+wait_for：超时不会取消 Future，Future 被编排器 cancel（TTL）也不会冒充成「本任务被取消」，
    本任务被取消时 CancelledError 原样传播（3.11 的 wait_for 可能吞掉取消）。
    """

    def __init__(self, hook: Optional[AskHook], budget_ms: int, lang: str):
        self.hook = hook
        self.budget_s = max(0, budget_ms) / 1000
        self.lines = inline_principal_lines(lang)

    @property
    def enabled(self) -> bool:
        """hook 可带 active 属性（worker 里晚绑定的 hook 在编排器就绪、本体支持当场答复前为 False）。"""
        return self.hook is not None and self.budget_s > 0 and bool(getattr(self.hook, "active", True))

    def _open(self, pid: ID, question: str) -> "Optional[asyncio.Future[str]]":
        if not self.enabled or not question:
            return None
        try:
            fut = self.hook(pid, question)             # type: ignore[misc]
        except Exception:  # noqa: BLE001
            log.exception("ask_hook 失败，退回旧的问本体路径")
            return None
        if fut is None:
            return None
        if fut.done():                                 # 立刻被拒：取走异常，免得 asyncio 报 never retrieved
            if not fut.cancelled():
                fut.exception()
            return None
        return fut

    @staticmethod
    def _outcome(fut: "asyncio.Future[str]") -> tuple[str, str]:
        if not fut.done() or fut.cancelled():
            return "timeout", ""
        if fut.exception() is not None:
            return "error", ""
        text = (fut.result() or "").strip()
        return ("answered", text) if text else ("timeout", "")

    async def run(self, ps: "PlanStream", pid: ID, fields: dict, safe_text: str) -> AsyncIterator[str]:
        """fields 带 ask_principal。ps.fields 在第一段文本产出前定稿（与 PlanStream 的约定一致）。"""
        question = fields.get("ask_principal") or ""
        fut = self._open(pid, question)
        if fut is None:
            ps.fields = fields
            ps.stats["inline"] = "off" if not self.enabled else "unavailable"
            yield safe_text
            return
        qid = getattr(fut, "query_id", None)
        waiting = {k: v for k, v in fields.items() if k != "ask_principal"}
        waiting.update(principal_query_id=qid, principal_pending=True)
        ps.fields = waiting
        ps.stats["inline"] = "cancelled"               # 先按「被打断」记；走完各分支会改写
        t0 = time.perf_counter()
        try:
            yield self.lines["filler"]
            yield FLUSH                                # 垫的那句现在就送 TTS，别等到下一块
            await asyncio.wait({fut}, timeout=self.budget_s)
            kind, text = self._outcome(fut)            # 超时后再看一眼：答复可能恰好同一刻到
            ps.stats["inline_wait_ms"] = (time.perf_counter() - t0) * 1000
        finally:
            if not fut.done():
                fut.cancel()
        ps.stats["inline"] = kind
        if kind == "answered":
            ps.fields = {**{k: v for k, v in waiting.items() if k != "principal_pending"},
                         "provenance": "principal", "confidence": 0.95}
            yield self.lines["answer"].format(answer=text)
        elif kind == "error":                          # 编排器在处理时又判了「不能问」：旧行为，问题由 utterance 尾部发出
            ps.fields = fields
            yield safe_text
        else:
            yield self.lines["timeout"]

    def stream(self, pid: ID, plan: dict, stats: dict) -> "PlanStream":
        """已定稿的 plan（带 ask_principal）包成当场问的流；plan["text"] 是退回旧行为时的安全话术。"""
        fields = {k: plan[k] for k in FIELD_KEYS if k in plan}
        fields.setdefault("commitments", [])
        fields.setdefault("addressees", [])
        ps = PlanStream(None, fields, {"path": "static", **stats}, plan["text"])
        return ps.attach(self.run(ps, pid, fields, plan["text"]))


class CallablePlanner:
    """包装老式同步规划函数（FakeLLM 等）。测试 / sim / 没有 LLM key 时用。"""

    def __init__(self, fn: Callable[[ID, Any], dict], ask: Optional[InlineAsk] = None):
        self.fn = fn
        self.ask = ask

    async def plan(self, pid: ID, ctx: ChatCtxRecipe) -> PlanStream:
        plan = self.fn(pid, ctx)
        if self.ask is not None and self.ask.enabled and plan.get("ask_principal"):
            return self.ask.stream(pid, plan, {})
        return static_stream(plan)


def as_planner(p: Any) -> Planner:
    """MeetingVoiceLayer 用：接受 Planner 或旧的同步可调用对象。"""
    if hasattr(p, "plan") and callable(p.plan):
        return p
    if callable(p):
        return CallablePlanner(p)
    raise TypeError(f"planner 必须是 Planner 或可调用对象，收到 {type(p).__name__}")


# ———————————————————— 英文回退本地化 ————————————————————
_RE_SNAP = re.compile(r"^按会前快照（(.*?)）：(.*)$", re.S)
_RE_POS = re.compile(r"^我的立场是：(.*)$", re.S)


def localize_fake(plan: dict, lang: str, meta: dict) -> dict:
    """FakeLLM 的台词是中文；英文会议回退时换成英文等价句（字段不动）。"""
    if normalize(lang) != "en":
        return plan
    text = plan["text"]
    if plan.get("ask_principal"):
        text = safe_ask_line("en")
    elif plan.get("provenance") == "principal":
        text = f"My principal replied: {meta.get('answer', '')}"
    elif plan.get("commitments"):
        text = f"Sure, I commit to this: {plan['commitments'][0]}."
    elif (m := _RE_SNAP.match(text)):
        text = f"Per the pre-meeting snapshot ({m.group(1)}): {m.group(2)}"
    elif (m := _RE_POS.match(text)):
        text = f"My position: {m.group(1)}"
    return {**plan, "text": text}


# ———————————————————— 提示词 ————————————————————
def _turn_prompt(lang: str, *, mandate_commit: list[str], mandate_ask: list[str], people: str, item: str,
                 directive: str, last: str, gist: str, addressed: bool) -> str:
    if lang == "en":
        lines = [
            "[Output format] First output ONE line of JSON (single line, no code fence), then a newline, then the words "
            "you will say out loud. The JSON line is never spoken.",
            'JSON: {"act":"speak","question":null,"commit":null,"confidence":0.8,"provenance":"brief","addressees":[]}',
            "- act: \"speak\" (normal), \"ask_principal\" (the brief/snapshots/meeting do not cover it, so you need your "
            "principal; put the full question for the principal in \"question\" and say you will check and follow up), "
            "or \"commit\" (you promise something right now; \"commit\" must be copied verbatim from the allowed list).",
            "- confidence 0..1: how well your words are supported (brief/snapshot/meeting >= 0.7; a guess < 0.5).",
            "- provenance: \"brief\", \"snapshot\" (a shared pre-meeting fact) or \"model\" (your own inference).",
            f"- addressees: ids of the people you are speaking to, may be empty. Participants: {people}",
            f"You MAY commit to (choose only from these; promise nothing else): {json.dumps(mandate_commit, ensure_ascii=False)}",
            f"You MUST ask your principal first about: {json.dumps(mandate_ask, ensure_ascii=False)}",
            "Speaking rules: use only your brief and what was said in the meeting; if you do not know, say so instead of "
            "inventing numbers or promises; 1-3 short spoken sentences (under 45 words), no lists, no markdown, "
            "do not call yourself an AI.",
        ]
        if item:
            lines.append(f"Current agenda item: {item}")
        if directive:
            lines.append(f"The human directed you to speak: {directive}")
        elif last:
            lines.append(f"Latest utterance to respond to: {last}")
        if gist:
            lines.append(f"The point you wanted to make when you raised your hand: {gist}")
        if addressed:
            lines.append("You were addressed directly, so answer the question.")
        return "\n".join(lines)
    lines = [
        "【输出格式】先输出一行 JSON 头（单行，不要代码块），换行，然后是你要说出口的话。JSON 头不会被念出来。",
        'JSON 头：{"act":"speak","question":null,"commit":null,"confidence":0.8,"provenance":"brief","addressees":[]}',
        "- act：\"speak\" 正常发言；\"ask_principal\" 表示 brief、快照和会上内容都没有依据、需要问本体——"
        "把要问本体的完整问题写进 question，正文说一句你会去确认、稍后补充；"
        "\"commit\" 表示你当场答应某事，commit 必须原样抄下面「可当场答应」里的一项。",
        "- confidence（0 到 1）：你所说内容有多大把握（brief/快照/会上有依据 ≥0.7；靠推测 <0.5）。",
        "- provenance：依据来自 \"brief\"、\"snapshot\"（会前共享的事实）还是 \"model\"（你自己的推断）。",
        f"- addressees：你在对谁说话（参会者 id，可为空）。参会者：{people}",
        f"你可以当场答应（只能从中选，其余一律不得答应）：{json.dumps(mandate_commit, ensure_ascii=False)}",
        f"涉及这些必须先问本体：{json.dumps(mandate_ask, ensure_ascii=False)}",
        "发言要求：只根据 brief 和会上已说的内容；不知道就直说不知道，不要编造数字或承诺；"
        "口语，1 到 3 句短句，总共不超过 60 字；不要列表、不要 markdown、不要自称 AI。",
    ]
    if item:
        lines.append(f"当前议题：{item}")
    if directive:
        lines.append(f"用户点名让你说：{directive}")
    elif last:
        lines.append(f"需要回应的上一条发言：{last}")
    if gist:
        lines.append(f"你举手时想说的要点：{gist}")
    if addressed:
        lines.append("你被直接点名了，请回答对方的问题。")
    return "\n".join(lines)


def _excerpt_prompt(lang: str, excerpts: str) -> str:
    if lang == "en":
        return ("Excerpts from your own notes (each starts with its id in brackets):\n" + excerpts + "\n"
                "Besides your brief, you may use these excerpts when they answer the point. Do not state anything that is "
                "not in your brief, these excerpts or what was said in the meeting. If you rely on an excerpt, set "
                "provenance to \"context\" and add \"sources\":[ids] to the JSON line, e.g. \"sources\":[\"pm#3\"].")
    return ("你自己笔记里的相关原文（每条以方括号里的 id 开头）：\n" + excerpts + "\n"
            "除 brief 外，这些原文能回答时可以直接用。不要说 brief、这些原文和会上内容之外的事。"
            "用了某条原文，就在 JSON 头里把 provenance 写成 \"context\"，并加上 \"sources\":[id]，例如 \"sources\":[\"pm#3\"]。")


def _silence_prompt(lang: str, item: str) -> str:
    if lang == "en":
        return (f"As the neutral host, give a spoken recap of the discussion on \"{item}\" so far, using only the "
                "minutes above, then invite anyone to add. One or two short sentences, no new facts, no opinions, "
                "no lists, no markdown. Output only the words to say.")
    return (f"你是中立主持人。只根据上面的纪要，口语化小结一下「{item}」目前的讨论，然后邀请大家补充。"
            "一到两句短句，不加新事实、不表态，不要列表、不要 markdown。只输出要说出口的话。")


# ———————————————————— LLM 规划器 ————————————————————
class LLMPlanner:
    def __init__(self, meeting: Meeting, briefs: dict[ID, Brief], chat: Any, language: Optional[str] = None, *,
                 header_timeout: float = HEADER_TIMEOUT_S, header_max_chars: int = HEADER_MAX_CHARS,
                 max_tokens: int = DEFAULT_MAX_TOKENS, temperature: float = 0.4,
                 stall_timeout: float = STALL_TIMEOUT_S, moderator_llm: bool = True,
                 fake: Optional[Callable[[ID, Any], dict]] = None, context_index: Any = None,
                 ask_hook: Optional[AskHook] = None, inline_ms: int = DEFAULT_INLINE_MS):
        self.meeting = meeting
        self.briefs = briefs
        self.chat = chat
        self.lang = normalize(language or getattr(meeting, "language", "zh"))
        self.context_index = context_index       # retrieval.ContextIndex | None：每个 bot 自己的完整笔记
        self.fake = fake or FakeLLM(meeting, briefs, context_index)
        self.header_timeout = header_timeout
        self.header_max_chars = header_max_chars
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.stall_timeout = stall_timeout
        self.moderator_llm = moderator_llm
        self.ask = InlineAsk(ask_hook, inline_ms, self.lang)     # 没有 hook / 预算为 0 = 关闭，行为与改动前一致
        self.stats: list[dict] = []          # 每轮 PlanStream.stats 的引用，便于外部汇总

    # —— 入口 ——
    async def plan(self, pid: ID, ctx: ChatCtxRecipe) -> PlanStream:
        meta = ctx.turn_meta
        intent = meta.get("intent", "answer")
        base = localize_fake(self.fake(pid, ctx), self.lang, meta)
        if pid == self.meeting.moderator.participant_id:
            if intent == "fill_silence" and self.moderator_llm and self.chat is not None:
                return self._track(self._moderator_stream(ctx, base))
            return self._track(static_stream(base, {"path": "moderator"}))
        if intent == "relay":                                  # 护栏 c：本体答案原样转述
            return self._track(static_stream(base, {"path": "relay"}))
        brief = self.briefs.get(pid)
        if brief is None or self.chat is None:
            return self._track(static_stream(base, {"path": "static", "fallback": "no_brief_or_chat"}))
        last = meta.get("directive_text") or meta.get("last_text", "")
        term = hit_must_ask(last, brief.mandate.must_ask)
        if term:                                               # 护栏 a：mustAsk 不经 LLM
            line = (f"「{term}」这个我需要问一下本体，稍后补充。" if self.lang == "zh"
                    else f"I need to ask my principal about \"{term}\" and will follow up.")
            fields = {"provenance": "model", "confidence": 0.3, "commitments": [], "addressees": [],
                      "ask_principal": last}
            plan = {"text": line, **fields}
            if self.ask.enabled:
                return self._track(self.ask.stream(pid, plan, {"path": "must_ask", "guard": "must_ask"}))
            return self._track(static_stream(plan, {"path": "must_ask", "guard": "must_ask"}))
        return self._track(self._member_stream(pid, ctx, base, brief, last))

    def _track(self, ps: PlanStream) -> PlanStream:
        self.stats.append(ps.stats)
        if len(self.stats) > 200:
            del self.stats[:100]
        return ps

    # —— 提示词 ——
    def _retrieve(self, pid: ID, ctx: ChatCtxRecipe) -> list[Hit]:
        """从 pid 自己的笔记里取回与这一轮相关的原文；没有索引 / 没命中 / 出任何错都是 []，不影响发言。"""
        if not self.context_index:
            return []
        try:
            main, extra = turn_query(ctx.turn_meta)
            return self.context_index.search(pid, main, k=EXCERPT_K, extra=extra)
        except Exception:  # noqa: BLE001
            log.exception("笔记检索失败，本轮不带原文")
            return []

    @staticmethod
    def _with_sources(fields: dict, header: dict, hits: list[Hit]) -> dict:
        """头里的 sources 只认真的取回过的 id；provenance=context 没有有效来源就降成 model，有有效来源的 model 升成 context。"""
        raw = header.get("sources")
        known = {h.id for h in hits}
        srcs = list(dict.fromkeys(s for s in raw if isinstance(s, str) and s in known)) if isinstance(raw, list) else []
        if srcs:
            fields["sources"] = srcs
            if fields.get("provenance") == "model":
                fields["provenance"] = "context"
        elif fields.get("provenance") == "context":
            fields["provenance"] = "model"
        return fields

    def _member_messages(self, pid: ID, ctx: ChatCtxRecipe, brief: Brief, last: str,
                         hits: tuple[Hit, ...] | list[Hit] = ()) -> list[dict]:
        meta = ctx.turn_meta
        people = "; ".join(f"{p.id}={p.display_name}" for p in self.meeting.participants if p.id != pid)
        msgs = ctx.to_messages()
        if hits:
            msgs.append({"role": "user", "content": _excerpt_prompt(self.lang, format_excerpts(list(hits), EXCERPT_CHARS))})
        msgs.append({"role": "user", "content": _turn_prompt(
            self.lang, mandate_commit=list(brief.mandate.can_commit), mandate_ask=list(brief.mandate.must_ask),
            people=people, item=meta.get("agenda_title", ""),
            directive=meta.get("directive_text", ""), last=meta.get("last_text", "") if not last else last,
            gist=meta.get("gist", ""), addressed=bool(meta.get("addressed")))})
        return msgs

    # —— 底层读流 ——
    def _open(self, msgs: list[dict]):
        return self.chat.astream_text(msgs, max_tokens=self.max_tokens, thinking="disabled",
                                      temperature=self.temperature)

    @staticmethod
    async def _close(it: Any) -> None:
        try:
            if hasattr(it, "aclose"):
                await it.aclose()
        except Exception:  # noqa: BLE001
            pass

    # —— 成员发言 ——
    def _member_stream(self, pid: ID, ctx: ChatCtxRecipe, base: dict, brief: Brief, last: str) -> PlanStream:
        ps = PlanStream(None, None, {"path": "llm", "llm": True}, base["text"])
        return ps.attach(self._member_gen(ps, pid, ctx, base, brief, last))

    def _fallback(self, ps: PlanStream, base: dict, reason: str) -> str:
        ps.stats["fallback"] = reason
        ps.fields = {k: base[k] for k in FIELD_KEYS if k in base}
        ps.fields.setdefault("commitments", [])
        ps.fields.setdefault("addressees", [])
        log.warning("planner 回退到策略台词：%s", reason)
        return base["text"]

    def _validate(self, header: dict, pid: ID, ctx: ChatCtxRecipe, brief: Brief, last: str):
        """返回 ("ok", fields) | ("ask", fields, 护栏名, 安全话术) | ("bad", 原因)。"""
        act = header.get("act", "speak")
        if act not in ACTS:
            return ("bad", "bad_act")
        conf = _clamp01(header.get("confidence"), 0.7)
        prov = header.get("provenance")
        prov = prov if prov in PROVENANCES else "model"
        known = {p.id for p in self.meeting.participants} - {pid}
        addr = header.get("addressees")
        addressees = [a for a in addr if isinstance(a, str) and a in known] if isinstance(addr, list) else []
        question = header.get("question") if isinstance(header.get("question"), str) else ""
        question = question.strip()
        commit = header.get("commit") if isinstance(header.get("commit"), str) else ""
        commit = commit.strip()
        fields: dict[str, Any] = {"provenance": prov, "confidence": conf, "commitments": [],
                                  "addressees": addressees}
        zh = self.lang == "zh"
        if act == "commit" or commit:                          # 护栏 b：承诺必须在 canCommit 里
            matched = match_commit(commit, brief.mandate.can_commit) if commit else None
            if matched is None:
                what = commit or question or last
                q = (f"是否可以答应：{what}" if zh else f"May I commit to: {what}") if what else (
                    "是否可以做出承诺" if zh else "May I make this commitment")
                fields.update(confidence=min(conf, 0.3), provenance="model", ask_principal=q)
                return ("ask", fields, "invalid_commit", safe_ask_line(self.lang))
            fields["commitments"] = [matched]
            if conf < CONFIDENCE_THRESHOLD and ctx.turn_meta.get("addressed"):
                fields.update(commitments=[], ask_principal=f"是否可以答应：{matched}" if zh
                              else f"May I commit to: {matched}")
                return ("ask", fields, "low_confidence", safe_ask_line(self.lang))
            return ("ok", fields)
        if act == "ask_principal":
            fields["ask_principal"] = question or last or ctx.turn_meta.get("agenda_title", "") or "?"
            return ("ok", fields)
        if conf < CONFIDENCE_THRESHOLD and ctx.turn_meta.get("addressed"):     # 护栏 d
            fields["ask_principal"] = question or last or ctx.turn_meta.get("agenda_title", "") or "?"
            return ("ask", fields, "low_confidence", safe_ask_line(self.lang))
        return ("ok", fields)

    async def _member_gen(self, ps: PlanStream, pid: ID, ctx: ChatCtxRecipe, base: dict, brief: Brief,
                          last: str) -> AsyncIterator[str]:
        t0 = time.perf_counter()
        it = None
        hits = self._retrieve(pid, ctx)
        if hits:
            ps.stats["retrieved"] = [h.id for h in hits]
        try:
            # 阶段 1：读到完整的头（带截止时间与长度上限）
            reason: Optional[str] = None
            header: Optional[dict] = None
            rest = ""
            try:
                it = self._open(self._member_messages(pid, ctx, brief, last, hits))
                parser = HeaderParser(self.header_max_chars)
                deadline = t0 + self.header_timeout
                while header is None:
                    left = deadline - time.perf_counter()
                    if left <= 0:
                        raise asyncio.TimeoutError
                    piece = await asyncio.wait_for(it.__anext__(), left)
                    got = parser.feed(piece)
                    if got:
                        header, rest = got
            except StopAsyncIteration:
                got = parser.finish()
                if got:
                    header, rest = got
                else:
                    reason = "no_header"
            except asyncio.TimeoutError:
                reason = "header_timeout"
            except HeaderError as e:
                reason = e.reason
            except Exception as e:  # noqa: BLE001
                reason = f"error:{type(e).__name__}"
            if header is None:
                await self._close(it)
                async for p in self._fallback_gen(ps, pid, base, reason or "no_header"):
                    yield p
                return
            ps.stats["header_ms"] = (time.perf_counter() - t0) * 1000

            # 阶段 2：校验头 + 确定性护栏
            verdict = self._validate(header, pid, ctx, brief, last)
            if verdict[0] == "bad":
                await self._close(it)
                async for p in self._fallback_gen(ps, pid, base, verdict[1]):
                    yield p
                return
            ps.stats["act"] = header.get("act")
            if verdict[0] == "ask":
                _, fields, guard, line = verdict
                await self._close(it)          # 中止流：模型生成的正文一个字也不说
                ps.fields = fields
                ps.stats["guard"] = guard
                async for p in self._ask_or_say(ps, pid, fields, line):
                    yield p
                return
            ps.fields = self._with_sources(verdict[1], header, hits)
            if ps.fields.get("ask_principal") and self.ask.enabled:    # act=ask_principal：不念模型的正文，当场问
                await self._close(it)
                async for p in self.ask.run(ps, pid, ps.fields, safe_ask_line(self.lang)):
                    yield p
                return

            # 阶段 3：正文边到边送出
            spoken = False
            err: Optional[str] = None
            pending = rest.lstrip()
            while True:
                if pending:
                    spoken = True
                    yield pending
                    pending = ""
                try:
                    piece = await asyncio.wait_for(it.__anext__(), self.stall_timeout)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    err = "stall_timeout"
                    break
                except Exception as e:  # noqa: BLE001
                    err = f"error:{type(e).__name__}"
                    break
                pending = piece if spoken else piece.lstrip()
            if not spoken:
                async for p in self._fallback_gen(ps, pid, base, err or "empty_speech"):
                    yield p
            elif err:
                ps.stats["truncated"] = err
        finally:
            if it is not None:
                await self._close(it)

    async def _ask_or_say(self, ps: PlanStream, pid: ID, fields: dict, text: str) -> AsyncIterator[str]:
        """fields 带 ask_principal 时：能当场问就当场问，否则说安全话术（fields 交给 utterance 尾部走慢路径）。"""
        if fields.get("ask_principal") and self.ask.enabled:
            async for p in self.ask.run(ps, pid, fields, text):
                yield p
        else:
            ps.fields = fields
            yield text

    async def _fallback_gen(self, ps: PlanStream, pid: ID, base: dict, reason: str) -> AsyncIterator[str]:
        text = self._fallback(ps, base, reason)
        async for p in self._ask_or_say(ps, pid, ps.fields, text):
            yield p

    # —— 主持人 fill_silence ——
    def _moderator_stream(self, ctx: ChatCtxRecipe, base: dict) -> PlanStream:
        fields = {k: base[k] for k in FIELD_KEYS if k in base}
        ps = PlanStream(None, fields, {"path": "moderator_llm", "llm": True}, base["text"])
        return ps.attach(self._moderator_gen(ps, ctx, base))

    async def _moderator_gen(self, ps: PlanStream, ctx: ChatCtxRecipe, base: dict) -> AsyncIterator[str]:
        t0 = time.perf_counter()
        it = None
        try:
            msgs = ctx.to_messages()
            msgs.append({"role": "user", "content": _silence_prompt(self.lang, ctx.turn_meta.get("agenda_title", ""))})
            spoken = False
            err: Optional[str] = None
            try:
                it = self._open(msgs)
                while True:
                    limit = self.stall_timeout if spoken else max(0.1, self.header_timeout - (time.perf_counter() - t0))
                    piece = await asyncio.wait_for(it.__anext__(), limit)
                    if not spoken:
                        piece = piece.lstrip()
                        if not piece:
                            continue
                        ps.stats["header_ms"] = (time.perf_counter() - t0) * 1000
                        spoken = True
                    yield piece
            except StopAsyncIteration:
                pass
            except asyncio.TimeoutError:
                err = "timeout"
            except Exception as e:  # noqa: BLE001
                err = f"error:{type(e).__name__}"
            if not spoken:
                yield self._fallback(ps, base, err or "empty_speech")
            elif err:
                ps.stats["truncated"] = err
        finally:
            if it is not None:
                await self._close(it)


def build_planner(meeting: Meeting, briefs: dict[ID, Brief], chat: Any = None,
                  language: Optional[str] = None, *, context_index: Any = None, ask_hook: Optional[AskHook] = None,
                  inline_ms: int = DEFAULT_INLINE_MS, **kw: Any) -> Planner:
    """chat 不为 None → LLM 规划器（回退用 FakeLLM）；否则就是 FakeLLM 的包装（测试 / sim / 无 key）。
    context_index：retrieval.ContextIndex，成员发言时检索自己的完整笔记（两条路径都用）。
    ask_hook（Orchestrator.request_principal）打开「同一轮里问本体」；不给或 inline_ms=0 则保持旧行为。"""
    if chat is None:
        ask = InlineAsk(ask_hook, inline_ms, language or getattr(meeting, "language", "zh"))
        return CallablePlanner(FakeLLM(meeting, briefs, context_index), ask if ask.enabled else None)
    return LLMPlanner(meeting, briefs, chat, language, context_index=context_index, ask_hook=ask_hook,
                      inline_ms=inline_ms, **kw)
