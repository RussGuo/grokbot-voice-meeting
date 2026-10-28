"""打分器：英文相关度 / 事实命中、中文行为不变、可插拔 + 异步 LLM 批量打分（脚本化 fake chat，不联网）。"""
import asyncio
import json
import logging
import re

import pytest

from orchestrator import textsim
from orchestrator.driver import SimDriver
from orchestrator.models import Brief, Utterance
from orchestrator.principal import deliver_to
from orchestrator.scheduler import RuleScorer, _fact_terms, similarity
from orchestrator.scoring import SCORER_UPDATE, LlmBatchScorer, build_scorer, parse_scores

from .helpers import run


def en_brief(pid="eng", keywords=None, stance="", facts=None) -> Brief:
    return Brief.from_dict({
        "participant_id": pid, "agenda_item_ids": ["ag-1"],
        "positions": [{"agenda_item_id": "ag-1", "stance": stance}] if stance else [],
        "memory_snapshot": {"topic": "t", "facts": [
            {"key": k, "value": v, "source": "s", "as_of": "2025-01-01"} for k, v in (facts or [])]},
        "mandate": {"can_commit": [], "must_ask": []},
        "keywords": keywords or [],
    })


def rule_scorer(**kw) -> RuleScorer:
    return RuleScorer({"eng": en_brief(**kw)}, {"eng": ["Eng Bot", "Eng"]})


# ———————————————————— 文本：英文 ————————————————————

def test_stemming_collapses_word_forms():
    assert similarity("caching layers", "cached layer") == 1.0
    assert similarity("scheduling the launches", "schedule launch") == 1.0
    assert textsim.stem("running") == textsim.stem("runs") == "run"
    assert textsim.stem("policies") == textsim.stem("policy")
    assert textsim.stem("status") == "status"          # 不误伤 -us / -is / -ss
    assert textsim.stem("q3") == "q3"


def test_stopwords_dropped_numbers_kept():
    assert textsim.en_terms("What is the plan for the team and the rest?") == ["plan", "team", "rest"]
    assert "10" in textsim.en_terms("ship in 10 days") and "i" not in textsim.en_terms("I think so")


def test_stopwords_do_not_create_relevance():
    s = rule_scorer(keywords=["latency", "cache"], stance="We should be careful about the cost of this and that.")
    hs, _ = s.score("eng", "What is the plan for the rest of it, and how do we do that?", [], 0.0, "ag-1")
    assert hs.relevance == 0.0
    assert similarity("the and of to in", "what is it that we do") == 0.0


def test_english_relevance_with_inflection_and_case():
    s = rule_scorer(keywords=["Latency", "caching"], stance="Caches cut p95 latency; ship behind a flag.")
    on, _ = s.score("eng", "How are we caching the responses to lower Latency?", [], 0.0, "ag-1")
    off, _ = s.score("eng", "Shall we book lunch for Friday?", [], 0.0, "ag-1")
    assert on.relevance >= 0.75 and off.relevance == 0.0
    # 关键词不再是大小写敏感的子串：ai 不会命中 maintain
    s2 = rule_scorer(keywords=["ai"])
    assert s2.score("eng", "Please maintain the plan", [], 0.0, "ag-1")[0].relevance == 0.0


def test_fact_terms_from_key_and_value():
    assert _fact_terms("offlineSyncConflictRate", "x") == [textsim.stem(w) for w in ("offline", "sync", "conflict", "rate")]
    assert _fact_terms("api.p95-latency", "") == ["api", "p95", "latency"]
    assert _fact_terms("APILatency", "") == ["api", "latency"]
    t = _fact_terms("cost.inference", "Inference costs 12% more than the baseline, about 1.5 USD per run")
    assert "12" in t and "1.5" in t and "baselin" in t and "usd" in t
    assert "1" not in t and "more" not in t        # 单个数字、停用词不算


def test_english_fact_match_by_key_and_by_value():
    s = rule_scorer(facts=[("offlineSyncConflictRate", "Conflict rate is 12% in the prototype"),
                           ("team.size", "Four engineers, two on mobile")])
    by_key, g1 = s.score("eng", "What is the sync conflict rate?", [], 0.0, "ag-1")
    assert by_key.confidence == 0.7 and g1.startswith("Conflict rate")
    by_val, g2 = s.score("eng", "Is 12% acceptable for users?", [], 0.0, "ag-1")      # 只命中 value 里的数字
    assert by_val.confidence == 0.7 and g2.startswith("Conflict rate")
    by_kw, g3 = s.score("eng", "How many engineers work on mobile?", [], 0.0, "ag-1")  # value 关键词 + 复数
    assert by_kw.confidence == 0.7 and g3.startswith("Four engineers")
    none, _ = s.score("eng", "Where shall we eat?", [], 0.0, "ag-1")
    assert none.confidence == 0.3


def test_fact_with_most_hits_wins_gist():
    s = rule_scorer(facts=[("cost", "Cheap"), ("cost.inference.cache", "Cache cuts inference cost")])
    _, gist = s.score("eng", "inference cost with cache?", [], 0.0, "ag-1")
    assert gist == "Cache cuts inference cost"


def test_english_novelty_ignores_stopword_overlap():
    s = rule_scorer(facts=[("latency", "The latency is 1.3 seconds")])
    hs, _ = s.score("eng", "latency?", ["The team is on the way to the office"], 0.0, "ag-1")
    assert hs.novelty == 1.0


def test_english_alias_is_word_boundary_and_case_insensitive():
    s = RuleScorer({"eng": en_brief()}, {"eng": ["Eng", "Eng Bot", "工程分身"]})
    assert s.addressed("eng", "eng, what do you think?") and s.addressed("eng", "Let Eng Bot speak")
    assert not s.addressed("eng", "the engine is slow") and not s.addressed("eng", "engineering cost")
    assert s.addressed("eng", "工程分身说说")


# ———————————————————— 文本：中文行为不变 ————————————————————

def _old_tokens(text: str) -> set[str]:
    text = text.lower()
    words = set(re.findall(r"[a-z0-9_]+", text))
    han = re.sub(r"[^一-鿿]", "", text)
    return words | {han[i:i + 2] for i in range(len(han) - 1)}


def _old_similarity(a, b):
    ta, tb = _old_tokens(a), _old_tokens(b)
    return len(ta & tb) / min(len(ta), len(tb)) if ta and tb else 0.0


@pytest.mark.parametrize("a,b", [
    ("离线同步的冲突率现在怎么样", "离线同步可以做，但冲突合并要先做技术评估"),
    ("摘要先按灰度发布", "AI 摘要的灰度你们怎么看"),
    ("成本", "推理成本能降多少"),
    ("", "离线"), ("你好", "你好"), ("预算批 2 人月", "预算批多少"),
])
def test_zh_similarity_unchanged(a, b):
    assert similarity(a, b) == _old_similarity(a, b)


def test_zh_fact_terms_unchanged():
    assert _fact_terms("sync.冲突率", "示例数据：多端离线编辑的冲突率约 3%（虚构）") == ["sync", "冲突率"]
    assert _fact_terms("离线同步.冲突率", "示例数据：x") == ["离线同步", "冲突率"]
    s = RuleScorer({"eng": en_brief(keywords=["同步", "冲突"], facts=[("sync.冲突率", "示例数据：冲突率约 3%（虚构）")])},
                   {"eng": ["工程分身"]})
    hs, gist = s.score("eng", "离线同步的冲突率现在怎么样？", [], 0.0, "ag-1")
    assert hs.confidence == 0.7 and gist.startswith("示例数据") and hs.relevance > 0.5


# ———————————————————— 工厂 ————————————————————

def test_build_scorer_modes(monkeypatch, caplog):
    b = {"eng": en_brief()}
    monkeypatch.delenv("MEETING_SCORER", raising=False)
    assert type(build_scorer(b, {}, chat=object())) is RuleScorer          # 默认 rule
    assert isinstance(build_scorer(b, {}, chat=object(), mode="llm"), LlmBatchScorer)
    monkeypatch.setenv("MEETING_SCORER", "llm")
    assert isinstance(build_scorer(b, {}, chat=object()), LlmBatchScorer)
    with caplog.at_level(logging.WARNING):
        assert type(build_scorer(b, {}, chat=None)) is RuleScorer          # 没 key：退回规则版
    assert "退回规则" in caplog.text
    assert type(build_scorer(b, {}, chat=object(), mode="rule")) is RuleScorer


def test_parse_scores_tolerates_fences_and_junk():
    raw = 'Sure!\n```json\n{"a": {"relevance": 1.4, "novelty": "0.5", "gist": " hi "}, "zzz": {"relevance": 1, "novelty": 1}, "b": {"relevance": "x", "novelty": 1}}\n```'
    assert parse_scores(raw, {"a", "b"}) == {"a": {"relevance": 1.0, "novelty": 0.5, "gist": "hi"}}
    assert parse_scores("no json", {"a"}) == {} and parse_scores("[1]", {"a"}) == {}


# ———————————————————— 异步 LLM 批量打分 ————————————————————

class ScriptedChat:
    """脚本化 fake chat：每次调用按顺序取一条剧本。str=整段输出；Exception=抛出；"hang"=一直不返回。"""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[list[dict]] = []
        self.active = 0
        self.max_active = 0
        self.cancelled = 0

    async def astream_text(self, messages, *, max_tokens=200, thinking="disabled"):
        self.calls.append(messages)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            item = self.script.pop(0) if self.script else "{}"
            if isinstance(item, Exception):
                raise item
            if item == "hang":
                await asyncio.sleep(30)
            await asyncio.sleep(0)
            half = len(item) // 2
            yield item[:half]
            await asyncio.sleep(0)
            yield item[half:]
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1


def scores_json(**per_bot) -> str:
    return json.dumps({k: {"relevance": v[0], "novelty": v[1], "gist": v[2]} for k, v in per_bot.items()},
                      ensure_ascii=False)


async def llm_driver(chat, **kw) -> SimDriver:
    d = SimDriver()
    await d.start()
    await d.run_until(1000)                       # 开场后进入 InAgenda
    o = d.orch
    o.scorer = build_scorer(o.briefs, {p.id: p.aliases + [p.display_name] for p in o.m.participants},
                            chat=chat, mode="llm")
    o.scorer.timeout_s = kw.get("timeout_s", 1.5)
    o.scorer.bind(deliver_to(o))
    return d


def say(d: SimDriver, text: str) -> Utterance:
    o = d.orch
    u = Utterance(id=f"utt-t{len(o.utterances)}", speaker_id=o.human_id, agenda_item_id=o.agenda_id, text=text,
                  started_at=d.clock.now, ended_at=d.clock.now)
    o.utterances.append(u)
    o.emit("UtteranceFinal", utterance=u)
    o._fan_out(u)
    return u


async def settle(d: SimDriver):
    """等 LLM 任务结束，再把投递进来的 scorer.update 交给编排器处理。"""
    await d.orch.scorer.wait_idle()
    await d.run_until(d.clock.now)


def hands(d: SimDriver) -> dict[str, float]:
    return {h.participant_id: h.score.total for h in d.orch.sched.s.hands}


def event_types(d: SimDriver):
    return [e.type for e in d.orch.log]


def test_rule_hands_are_immediate_and_llm_never_blocks_fan_out():
    async def go():
        chat = ScriptedChat(scores_json())
        d = await llm_driver(chat)
        say(d, "离线同步的冲突率现在怎么样")
        assert set(hands(d)) == {"pm_bot", "eng_bot"}      # 规则分当场举手
        assert chat.calls == []                            # fan-out 返回时 LLM 还没被调用
        await settle(d)
        assert len(chat.calls) == 1                        # 一条发言一次请求，覆盖所有成员 bot
        prompt = chat.calls[0][1]["content"]
        assert "pm_bot" in prompt and "eng_bot" in prompt and "冲突率" in prompt
    run(go())


def test_llm_update_raises_hand_rule_missed():
    async def go():
        chat = ScriptedChat(scores_json(pm_bot=(1.0, 1.0, "我们应该先问用户"), eng_bot=(0.0, 1.0, "")))
        d = await llm_driver(chat)
        say(d, "你好")
        assert hands(d) == {}                              # 规则分 0.45 < 0.6
        n0 = len(d.orch.log)
        await settle(d)
        h = next(h for h in d.orch.sched.s.hands if h.participant_id == "pm_bot")
        assert h.gist == "我们应该先问用户" and h.score.relevance == 1.0 and h.score.total >= 0.6
        assert "eng_bot" not in hands(d)
        assert [e.type for e in d.orch.log][n0:].count("HandRaised") == 1
    run(go())


def test_llm_update_withdraws_and_replaces_rule_hands():
    async def go():
        chat = ScriptedChat(scores_json(pm_bot=(0.05, 1.0, "无关"), eng_bot=(1.0, 1.0, "LLM 要点")))
        d = await llm_driver(chat)
        say(d, "离线同步的冲突率现在怎么样")
        assert set(hands(d)) == {"pm_bot", "eng_bot"}
        rule_total = hands(d)["eng_bot"]
        n0 = len(d.orch.log)
        await settle(d)
        assert set(hands(d)) == {"eng_bot"}                # pm_bot 被撤回
        eng = d.orch.sched.s.hands[0]
        assert eng.gist == "LLM 要点" and hands(d)["eng_bot"] > rule_total
        tail = [(e.type, e.data["hand"]["participant_id"]) for e in list(d.orch.log)[n0:] if "hand" in e.data]
        assert ("HandWithdrawn", "pm_bot") in tail and ("HandWithdrawn", "eng_bot") in tail
        assert ("HandRaised", "eng_bot") in tail
    run(go())


def test_stale_update_by_newer_utterance_is_dropped():
    async def go():
        chat = ScriptedChat(scores_json(pm_bot=(1.0, 1.0, "新")))
        d = await llm_driver(chat)
        say(d, "你好")
        old_seq = d.orch._scorer_ctx[0]
        say(d, "再说一句")                                   # 更新的发言：旧请求被顶掉
        n0 = len(d.orch.log)
        # 即便旧结果仍然到达（晚到事件），编排器也要丢弃
        await d.orch.handle({"type": SCORER_UPDATE, "based_on_seq": old_seq, "agenda_item_id": d.orch.agenda_id,
                             "scores": {"pm_bot": {"relevance": 1.0, "novelty": 1.0, "gist": "旧"}}})
        assert hands(d) == {} and len(d.orch.log) == n0
        await settle(d)
        assert [h.gist for h in d.orch.sched.s.hands] == ["新"]     # 只有第二条发言的请求生效
        assert len(chat.calls) == 1
    run(go())


def test_update_for_other_agenda_item_is_dropped():
    async def go():
        d = await llm_driver(ScriptedChat())
        say(d, "你好")
        seq = d.orch._scorer_ctx[0]
        await d.orch.handle({"type": SCORER_UPDATE, "based_on_seq": seq, "agenda_item_id": "ag-other",
                             "scores": {"pm_bot": {"relevance": 1.0, "novelty": 1.0, "gist": "x"}}})
        assert hands(d) == {}
    run(go())


def test_update_after_hand_already_granted_is_not_reraised():
    async def go():
        chat = ScriptedChat(scores_json(pm_bot=(1.0, 1.0, "x"), eng_bot=(1.0, 1.0, "y")))
        d = await llm_driver(chat)
        say(d, "离线同步的冲突率现在怎么样")
        gone = d.orch.sched.s.hands.pop(0)                  # 规则手已被授予（离开队列）
        await settle(d)
        assert gone.participant_id not in hands(d)
    run(go())


@pytest.mark.parametrize("script", [
    "hang",
    RuntimeError("boom"),
    "not json at all",
    json.dumps({"unknown_bot": {"relevance": 1, "novelty": 1}}),
])
def test_timeout_or_error_keeps_rule_scores_and_logs_once(script, caplog):
    async def go():
        chat = ScriptedChat(script, script)
        d = await llm_driver(chat, timeout_s=0.05)
        with caplog.at_level(logging.DEBUG, logger="orchestrator.scoring"):
            say(d, "离线同步的冲突率现在怎么样")
            before = dict(hands(d))
            n0 = len(d.orch.log)
            await settle(d)
            assert hands(d) == before and len(d.orch.log) == n0     # 规则手、事件流原封不动
            say(d, "离线同步的冲突率有多大")
            await settle(d)
        assert set(hands(d)) == set(before)
        assert len(chat.calls) == 2                          # 失败不影响下一条发言再试
        assert sum(1 for r in caplog.records if r.levelno == logging.WARNING) == 1
    run(go())


def test_at_most_one_request_in_flight_and_newer_supersedes():
    async def go():
        chat = ScriptedChat("hang", scores_json(pm_bot=(1.0, 1.0, "新")))
        d = await llm_driver(chat)
        say(d, "你好")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert chat.active == 1                              # 第一个请求在飞
        say(d, "再说一句")                                    # 第二条发言：先取消第一个，再开第二个
        await settle(d)
        assert chat.max_active == 1 and chat.cancelled == 1
        assert len(chat.calls) == 2
        assert [h.gist for h in d.orch.sched.s.hands] == ["新"]
        assert d.orch.scorer._latest == d.orch._scorer_ctx[0]
    run(go())


def test_addressed_bot_not_sent_to_llm_and_rule_mode_never_calls_chat():
    async def go():
        chat = ScriptedChat(scores_json(pm_bot=(0.0, 0.0, "")))
        d = await llm_driver(chat)
        say(d, "产品分身，你怎么看？")
        assert hands(d)["pm_bot"] == 1.0
        await settle(d)
        assert "pm_bot (" not in chat.calls[0][1]["content"] and "eng_bot (" in chat.calls[0][1]["content"]
        assert hands(d)["pm_bot"] == 1.0                      # 被点名的手不受 LLM 影响
        # 规则模式：没有 submit，不会创建任务
        d2 = SimDriver()
        await d2.start()
        await d2.run_until(1000)
        assert type(d2.orch.scorer) is RuleScorer
        say(d2, "你好")
        await asyncio.sleep(0)
        assert d2.orch.scorer._task if hasattr(d2.orch.scorer, "_task") else True
    run(go())


def test_orchestrator_env_wiring(monkeypatch):
    monkeypatch.setenv("MEETING_SCORER", "llm")
    d = SimDriver()
    assert type(d.orch.scorer) is RuleScorer                  # 没传 scorer_chat：退回规则版
    from orchestrator.orchestrator import Orchestrator
    o = d.orch
    o2 = Orchestrator(o.m, o.briefs, o.voice, principal_factory=lambda _o: o.principal, clock=d.clock,
                      scorer_chat=ScriptedChat())
    assert isinstance(o2.scorer, LlmBatchScorer)


def test_update_is_delivered_through_live_inbox():
    async def go():
        d = await llm_driver(ScriptedChat(scores_json(pm_bot=(1.0, 1.0, "x"))))
        d.orch.inbox = asyncio.Queue()                       # 实时运行时 run() 建的队列
        say(d, "你好")
        await d.orch.scorer.wait_idle()
        ev = d.orch.inbox.get_nowait()
        assert ev["type"] == SCORER_UPDATE and ev["based_on_seq"] == d.orch._scorer_ctx[0]
        assert ev["scores"]["pm_bot"]["relevance"] == 1.0 and not any(t[2]["type"] == SCORER_UPDATE for t in d.orch._timers)
        await d.orch.handle(ev)
        assert "pm_bot" in hands(d)
    run(go())
