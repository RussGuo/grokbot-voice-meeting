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

from .fake_llm import CONFIDENCE_THRESHOLD, FakeLLM
from .i18n import normalize
from .models import ID, Brief, ChatCtxRecipe, Meeting

log = logging.getLogger("planner")

ACTS = ("speak", "ask_principal", "commit")
PROVENANCES = ("brief", "snapshot", "model")
HEADER_MAX_CHARS = 400
HEADER_TIMEOUT_S = 2.5
STALL_TIMEOUT_S = 8.0        # 正文开始后，相邻两块之间最长等待；超时就地收尾（不再回退，已经开口了）
DEFAULT_MAX_TOKENS = 160     # 含头
FIELD_KEYS = ("provenance", "confidence", "commitments", "addressees", "ask_principal")

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


class CallablePlanner:
    """包装老式同步规划函数（FakeLLM 等）。测试 / sim / 没有 LLM key 时用。"""

    def __init__(self, fn: Callable[[ID, Any], dict]):
        self.fn = fn

    async def plan(self, pid: ID, ctx: ChatCtxRecipe) -> PlanStream:
        return static_stream(self.fn(pid, ctx))


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
                 fake: Optional[Callable[[ID, Any], dict]] = None):
        self.meeting = meeting
        self.briefs = briefs
        self.chat = chat
        self.lang = normalize(language or getattr(meeting, "language", "zh"))
        self.fake = fake or FakeLLM(meeting, briefs)
        self.header_timeout = header_timeout
        self.header_max_chars = header_max_chars
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.stall_timeout = stall_timeout
        self.moderator_llm = moderator_llm
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
            return self._track(static_stream({"text": line, **fields}, {"path": "must_ask", "guard": "must_ask"}))
        return self._track(self._member_stream(pid, ctx, base, brief, last))

    def _track(self, ps: PlanStream) -> PlanStream:
        self.stats.append(ps.stats)
        if len(self.stats) > 200:
            del self.stats[:100]
        return ps

    # —— 提示词 ——
    def _member_messages(self, pid: ID, ctx: ChatCtxRecipe, brief: Brief, last: str) -> list[dict]:
        meta = ctx.turn_meta
        people = "; ".join(f"{p.id}={p.display_name}" for p in self.meeting.participants if p.id != pid)
        msgs = ctx.to_messages()
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
        try:
            # 阶段 1：读到完整的头（带截止时间与长度上限）
            reason: Optional[str] = None
            header: Optional[dict] = None
            rest = ""
            try:
                it = self._open(self._member_messages(pid, ctx, brief, last))
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
                yield self._fallback(ps, base, reason or "no_header")
                return
            ps.stats["header_ms"] = (time.perf_counter() - t0) * 1000

            # 阶段 2：校验头 + 确定性护栏
            verdict = self._validate(header, pid, ctx, brief, last)
            if verdict[0] == "bad":
                await self._close(it)
                yield self._fallback(ps, base, verdict[1])
                return
            ps.stats["act"] = header.get("act")
            if verdict[0] == "ask":
                _, fields, guard, line = verdict
                await self._close(it)          # 中止流：模型生成的正文一个字也不说
                ps.fields = fields
                ps.stats["guard"] = guard
                yield line
                return
            ps.fields = verdict[1]

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
                yield self._fallback(ps, base, err or "empty_speech")
            elif err:
                ps.stats["truncated"] = err
        finally:
            if it is not None:
                await self._close(it)

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
                  language: Optional[str] = None, **kw: Any) -> Planner:
    """chat 不为 None → LLM 规划器（回退用 FakeLLM）；否则就是 FakeLLM 的包装（测试 / sim / 无 key）。"""
    if chat is None:
        return CallablePlanner(FakeLLM(meeting, briefs))
    return LLMPlanner(meeting, briefs, chat, language, **kw)
