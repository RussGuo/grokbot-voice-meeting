"""笔记检索：分段、BM25 排序、速度、planner 注入原文并校验 sources、FakeLLM 回退、举手打分、纪要里的来源（全部离线）。"""
import json
import time

from orchestrator.models import Brief
from orchestrator.planner import LLMPlanner, build_planner
from orchestrator.retrieval import (CHUNK_LIMIT, ContextIndex, chunk_bot, chunk_text, first_sentence, format_excerpts)
from orchestrator.scheduler import RuleScorer
from orchestrator.scoring import build_scorer

from .helpers import run, started
from .planner_helpers import ScriptedChat, collect, make_ctx, make_meeting

EN_NOTES = """# Offline sync

We chose a CRDT-based merge for offline sync after the conflict-rate spike in March. Conflict rate on mobile was 3.1% before the change and 0.4% after.

## Rollout

The rollout is staged: 1% of users in week one, 10% in week two, then everyone. Rollback needs a feature flag flip and takes about five minutes.

## Pricing

Enterprise pricing is not decided. Finance owns the pricing spreadsheet; do not quote numbers.

## Team

Two engineers are on the sync project. The tech lead is Dana. Hiring for a third engineer is blocked until Q4 budget approval.
"""

ZH_NOTES = """# 离线同步

三月冲突率飙升之后，我们决定用 CRDT 做合并。移动端冲突率改造前是 3.1%，改造后是 0.4%。

## 灰度计划

灰度分阶段：第一周 1% 用户，第二周 10%，之后全量。回滚只需要切一个开关，大约五分钟。

## 定价

企业版定价还没定。财务负责定价表，不要报具体数字。

## 团队

同步项目有两位工程师，技术负责人是小周。第三位工程师的招聘要等第四季度预算批下来。
"""


def norm(s: str) -> str:
    return "".join(s.split())


# ———————————————————— 分段 ————————————————————

def test_chunking_keeps_heading_with_its_paragraph_and_drops_markdown_marks():
    chunks = chunk_text(EN_NOTES)
    assert len(chunks) == 4                                        # 每个标题一段，标题不跨段合并
    assert chunks[0].startswith("Offline sync\nWe chose a CRDT")   # '#' 去掉，标题并进正文
    assert chunks[1].startswith("Rollout\nThe rollout is staged")
    assert all("#" not in c for c in chunks)
    assert norm("".join(chunks)) == norm(EN_NOTES.replace("#", ""))


def test_chunking_chinese_headings_and_sentences():
    chunks = chunk_text(ZH_NOTES)
    assert len(chunks) == 4 and chunks[2].startswith("定价\n企业版定价还没定")
    assert norm("".join(chunks)) == norm(ZH_NOTES.replace("#", ""))


def test_small_paragraphs_merge_up_to_target_but_not_across_headings():
    text = "\n\n".join(f"Short note number {i} about topic alpha." for i in range(12))
    chunks = chunk_text(text, target=200)
    assert 1 < len(chunks) < 12 and all(len(c) <= 200 for c in chunks)
    headed = "Plain note one is here.\n\n## Next\n\nNote under heading."
    assert len(chunk_text(headed)) == 2                            # 标题开新段


def test_long_paragraph_splits_on_sentences_within_limit():
    sents = [f"Sentence number {i} says something specific about the project." for i in range(60)]
    chunks = chunk_text(" ".join(sents))
    assert len(chunks) >= 5 and all(len(c) <= CHUNK_LIMIT for c in chunks)
    assert all(c.rstrip().endswith(".") for c in chunks)           # 没有把句子切碎
    assert norm("".join(chunks)) == norm(" ".join(sents))


def test_long_unpunctuated_run_is_hard_split():
    chunks = chunk_text("我们讨论了很多事情" * 300)
    assert len(chunks) >= 5 and all(len(c) <= CHUNK_LIMIT for c in chunks)
    assert norm("".join(chunks)) == "我们讨论了很多事情" * 300


def test_chunking_empty_and_ids_and_caps():
    assert chunk_text("") == [] and chunk_text("  \n\n \n") == []
    chunks, cut = chunk_bot("pm", EN_NOTES)
    assert [c.id for c in chunks] == ["pm#1", "pm#2", "pm#3", "pm#4"] and not cut
    big = "\n\n".join(f"Paragraph {i}: " + "word " * 60 for i in range(100))
    chunks, cut = chunk_bot("pm", big, max_bytes=5000)
    assert cut and 0 < sum(len(c.text.encode()) for c in chunks) <= 5000
    chunks, cut = chunk_bot("pm", "中文" * 4000, max_bytes=3000)   # 上限按 UTF-8 字节算
    assert cut and sum(len(c.text.encode()) for c in chunks) <= 3000


def test_first_sentence():
    assert first_sentence("Conflict rate is 0.4%. It was 3.1% before.") == "Conflict rate is 0.4%."
    assert first_sentence("冲突率现在是 0.4%。之前是 3.1%。") == "冲突率现在是 0.4%。"
    assert first_sentence("Pricing\nEnterprise pricing is not decided. Finance owns it.") == "Enterprise pricing is not decided."
    assert first_sentence("定价\n企业版定价还没定。财务负责。") == "企业版定价还没定。"
    assert first_sentence("Just a short heading") == "Just a short heading"       # 只有标题时保留
    s = first_sentence("x" * 500, limit=120)
    assert len(s) <= 121 and s.endswith("…")


# ———————————————————— BM25 ————————————————————

def index_of(**notes) -> ContextIndex:
    ix = ContextIndex()
    for bid, text in notes.items():
        ix.add_bot(bid, text)
    return ix


def test_search_ranks_english_and_scores_are_normalized():
    ix = index_of(alice=EN_NOTES)
    hits = ix.search("alice", "What is the conflict rate on mobile now?")
    assert hits and hits[0].text.startswith("Offline sync") and hits[0].id == "alice#1"
    assert all(0.0 <= h.score <= 1.0 for h in hits) and hits == sorted(hits, key=lambda h: -h.score)
    assert ix.search("alice", "who is the tech lead")[0].id == "alice#4"
    assert ix.search("alice", "Is the rollout staged by percent of users?")[0].id == "alice#2"
    assert ix.search("alice", "enterprise pricing", k=1)[0].id == "alice#3"


def test_search_ranks_chinese():
    ix = index_of(alice=ZH_NOTES)
    assert ix.search("alice", "现在的冲突率是多少？")[0].id == "alice#1"
    assert ix.search("alice", "企业版定价我们聊聊")[0].id == "alice#3"
    assert ix.search("alice", "招聘的预算怎么样了")[0].id == "alice#4"
    assert ix.search("alice", "灰度分阶段推进，回滚怎么做？")[0].id == "alice#2"


def test_search_returns_nothing_for_offtopic_chatter_and_blank_queries():
    ix = index_of(alice=EN_NOTES, bob=ZH_NOTES)
    assert ix.search("alice", "What do you think about the logo colors?") == []
    assert ix.search("alice", "so what do you guys think about this") == []
    assert ix.search("bob", "你们觉得 logo 的颜色怎么样") == []
    assert ix.search("bob", "你们怎么看这个事情") == []
    assert ix.search("alice", "") == [] and ix.search("alice", "   ") == []
    assert ix.search("nobody", "conflict rate") == []


def test_search_is_per_bot_never_cross_bot():
    ix = index_of(alice=EN_NOTES, bob="Bob keeps the release calendar. Launch day is the first of June.")
    assert ix.search("bob", "what is the conflict rate on mobile") == []
    assert ix.search("alice", "when is launch day") == []
    assert ix.search("bob", "when is launch day")[0].id == "bob#1"


def test_long_query_is_not_diluted():
    ix = index_of(alice=EN_NOTES)
    q = ("Thanks everyone for joining. Before we move on to the next point on the agenda, quickly, "
         "what is the conflict rate on mobile after the CRDT change?")
    assert ix.search("alice", q)[0].id == "alice#1"


def test_min_score_and_k():
    ix = index_of(alice=EN_NOTES)
    q = "enterprise pricing and hiring budget for engineers"
    assert len(ix.search("alice", q, k=1)) == 1
    assert len(ix.search("alice", q, k=3, min_score=0.0)) >= 2
    assert ix.search("alice", q, k=3, min_score=0.99) == []
    assert ix.search("alice", q, k=0) == []


def test_add_bot_replaces_and_empty_text_removes():
    ix = index_of(alice="Old note about apples.")
    assert ix.search("alice", "apples")[0].id == "alice#1"
    ix.add_bot("alice", "New note about oranges.")
    assert ix.search("alice", "apples") == [] and ix.search("alice", "oranges")
    ix.add_bot("alice", "")
    assert not ix and ix.bots() == []


def test_search_is_fast_on_hundreds_of_chunks():
    paras = [f"Section {i}: the {w} subsystem handles {w} requests with latency budget {i} ms and owner team{i % 7}."
             for i, w in enumerate(["cache", "queue", "auth", "billing", "search", "export", "sync", "notify"] * 50)]
    ix = ContextIndex()
    t0 = time.perf_counter()
    n = ix.add_bot("alice", "\n\n".join(paras))
    build_s = time.perf_counter() - t0
    assert n >= 40 and build_s < 3.0                                                  # 建索引在会前，粗界即可
    queries = ["what is the latency budget of the billing subsystem", "who owns the cache requests", "tell me about export"]
    t0 = time.perf_counter()
    for _ in range(100):
        for q in queries:
            ix.search("alice", q, k=3)
    per_ms = (time.perf_counter() - t0) / 300 * 1000
    assert per_ms < 5.0, f"search {per_ms:.2f} ms"


# ———————————————————— planner ————————————————————

def hdr(**kw):
    h = {"act": "speak", "question": None, "commit": None, "confidence": 0.9, "provenance": "brief", "addressees": []}
    h.update(kw)
    return json.dumps(h)


def mk(chat, lang="en", notes=None, **kw):
    m, b = make_meeting(lang)
    ix = ContextIndex()
    for bid, text in (notes or {}).items():
        ix.add_bot(bid, text)
    return LLMPlanner(m, b, chat, context_index=ix, **kw), m, b


def excerpt_msgs(chat, call=0):
    return [m for m in chat.calls[call][0] if m["content"].startswith(("Excerpts from your own notes", "你自己笔记里的相关原文"))]


async def plan_text(planner, pid, ctx):
    ps = await planner.plan(pid, ctx)
    return await collect(ps), ps


async def test_planner_adds_one_excerpt_message_with_ids_and_cap():
    chat = ScriptedChat([hdr(provenance="context", sources=["alice#2"]) + "\n", "We roll out in stages."])
    p, _, _ = mk(chat, notes={"alice": EN_NOTES})
    text, ps = await plan_text(p, "alice", make_ctx(last_text="How is the staged rollout of users going?"))
    assert text == "We roll out in stages."
    ex = excerpt_msgs(chat)
    assert len(ex) == 1 and ex[0]["role"] == "user"
    body = ex[0]["content"]
    assert "[alice#2] Rollout" in body and "must not invent" not in body and "Do not state anything" in body
    assert 'provenance to "context"' in body and '"sources"' in body
    assert len(body) < 1200 + 600                                                    # 原文 ≤ 1200，加说明文字
    assert chat.calls[0][0][-1]["content"].startswith("[Output format]")             # 轮次提示词仍是最后一条
    assert ps.fields["provenance"] == "context" and ps.fields["sources"] == ["alice#2"]
    assert ps.stats["retrieved"][0] == "alice#2"


async def test_planner_excerpt_prompt_is_localized_and_capped():
    long_notes = "\n\n".join(f"## Rollout {i}\n\n" + ("The rollout plan is staged and we watch conflict rate. " * 12) for i in range(8))
    chat = ScriptedChat([hdr() + "\n", "ok"])
    p, _, _ = mk(chat, lang="zh", notes={"alice": long_notes})
    await plan_text(p, "alice", make_ctx(last_text="the rollout plan conflict rate"))
    body = excerpt_msgs(chat)[0]["content"]
    assert body.startswith("你自己笔记里的相关原文") and "[alice#" in body
    first, _, rest = body.partition("\n")
    listing = rest.split("\n除 brief 外")[0]
    assert len(listing) <= 1200 and listing.count("[alice#") <= 3


async def test_planner_drops_unknown_sources_and_downgrades_unsupported_context():
    chat = ScriptedChat([hdr(provenance="context", sources=["alice#2", "alice#99", "bob#1", 7]) + "\n", "Staged."])
    p, _, _ = mk(chat, notes={"alice": EN_NOTES})
    _, ps = await plan_text(p, "alice", make_ctx(last_text="How is the staged rollout of users?"))
    assert ps.fields["sources"] == ["alice#2"] and ps.fields["provenance"] == "context"

    chat = ScriptedChat([hdr(provenance="context", sources=["alice#99"]) + "\n", "Staged."])
    p, _, _ = mk(chat, notes={"alice": EN_NOTES})
    _, ps = await plan_text(p, "alice", make_ctx(last_text="How is the staged rollout of users?"))
    assert "sources" not in ps.fields and ps.fields["provenance"] == "model"          # 引用不了的 context 不算 context

    chat = ScriptedChat([hdr(provenance="context", sources="alice#2") + "\n", "Staged."])
    p, _, _ = mk(chat, notes={"alice": EN_NOTES})
    _, ps = await plan_text(p, "alice", make_ctx(last_text="How is the staged rollout of users?"))
    assert "sources" not in ps.fields and ps.fields["provenance"] == "model"

    chat = ScriptedChat([hdr(provenance="model", sources=["alice#2"]) + "\n", "Staged."])
    p, _, _ = mk(chat, notes={"alice": EN_NOTES})
    _, ps = await plan_text(p, "alice", make_ctx(last_text="How is the staged rollout of users?"))
    assert ps.fields["provenance"] == "context" and ps.fields["sources"] == ["alice#2"]


async def test_planner_without_hits_or_index_is_unchanged():
    base = ScriptedChat([hdr() + "\n", "Fine."])
    p0 = LLMPlanner(*make_meeting("en"), base)
    await plan_text(p0, "alice", make_ctx())
    for notes, last in (({"alice": EN_NOTES}, "What do you think about the logo colors?"),    # 没命中
                        ({"bob": EN_NOTES}, "How is the staged rollout of users going?"),  # 别人的笔记不给 alice
                        ({}, "How is the staged rollout of users?")):                                 # 空索引
        chat = ScriptedChat([hdr(provenance="context", sources=["bob#2"]) + "\n", "Fine."])
        p, _, _ = mk(chat, notes=notes)
        _, ps = await plan_text(p, "alice", make_ctx(last_text=last))
        assert excerpt_msgs(chat) == [] and len(chat.calls[0][0]) == len(base.calls[0][0])
        assert "sources" not in ps.fields and ps.fields["provenance"] == "model"
        assert "retrieved" not in ps.stats


async def test_planner_query_uses_directive_gist_and_agenda_title():
    chat = ScriptedChat([hdr() + "\n", "ok"])
    p, _, _ = mk(chat, notes={"alice": EN_NOTES})
    await plan_text(p, "alice", make_ctx(last_text="hmm", directive_text="tell us the tech lead", gist="",
                                         agenda_title="Team"))
    assert "[alice#4]" in excerpt_msgs(chat)[0]["content"]
    chat = ScriptedChat([hdr() + "\n", "ok"])
    p, _, _ = mk(chat, notes={"alice": EN_NOTES})
    await plan_text(p, "alice", make_ctx(last_text="go on", gist="hiring for a third engineer"))
    assert "[alice#4]" in excerpt_msgs(chat)[0]["content"]


async def test_planner_ask_principal_guard_still_wins_over_excerpts():
    chat = ScriptedChat([hdr(act="ask_principal", question="what is the price?", confidence=0.4) + "\n", "x"])
    p, _, _ = mk(chat, notes={"alice": EN_NOTES})
    _, ps = await plan_text(p, "alice", make_ctx(last_text="enterprise pricing for the rollout"))   # 命中 must_ask("pricing")
    assert ps.stats["path"] == "must_ask" and chat.calls == []                        # 不经 LLM，也就没有原文


async def test_planner_llm_failure_falls_back_to_notes_line():
    chat = ScriptedChat([], error_after=0)
    p, _, _ = mk(chat, notes={"alice": EN_NOTES})
    text, ps = await plan_text(p, "alice", make_ctx(last_text="How is the staged rollout of users going?"))
    assert text.startswith("My notes say: Rollout") or text.startswith("My notes say: The rollout")
    assert ps.fields["provenance"] == "context" and ps.fields["sources"] == ["alice#2"]
    assert ps.stats["fallback"]


def test_build_planner_passes_index_to_fake_llm_without_chat():
    m, b = make_meeting("en")
    ix = index_of(alice=EN_NOTES)
    planner = build_planner(m, b, None, "en", context_index=ix)
    out = planner.fn("alice", make_ctx(last_text="Who is the tech lead?"))
    assert out["provenance"] == "context" and out["sources"] == ["alice#4"] and out["text"].startswith("My notes say: ")
    plain = build_planner(m, b, None, "en")
    assert plain.fn("alice", make_ctx(last_text="Who is the tech lead?"))["provenance"] == "brief"


# ———————————————————— FakeLLM ————————————————————

def test_fake_llm_snapshot_fact_wins_and_no_index_is_unchanged():
    m, b = make_meeting("en")
    from orchestrator.fake_llm import FakeLLM
    ix = index_of(alice="The budget.q3 line says 300k but also lists many other things.")
    ctx = make_ctx(last_text="what about the budget.q3 number?")
    assert FakeLLM(m, b, ix)("alice", ctx)["provenance"] == "snapshot"               # 快照事实优先
    a, c = FakeLLM(m, b)("alice", make_ctx()), FakeLLM(m, b, ContextIndex())("alice", make_ctx())
    assert a == c and a["provenance"] == "brief" and "sources" not in a


def test_fake_llm_uses_top_hit_first_sentence_localized():
    from orchestrator.fake_llm import FakeLLM
    m, b = make_meeting("en")
    out = FakeLLM(m, b, index_of(alice=EN_NOTES))("alice", make_ctx(last_text="What is the conflict rate on mobile now?"))
    assert out["text"].startswith("My notes say: We chose a CRDT-based merge") and len(out["text"]) < 160
    assert out["provenance"] == "context" and out["sources"] == ["alice#1"] and out["confidence"] >= 0.6
    mz, bz = make_meeting("zh")
    out = FakeLLM(mz, bz, index_of(alice=ZH_NOTES))("alice", make_ctx(last_text="现在的冲突率是多少？"))
    assert out["text"].startswith("我笔记里写着：三月冲突率") and out["provenance"] == "context"
    assert len(out["text"]) < 160


# ———————————————————— 举手打分 ————————————————————

def brief_without_notes_topics() -> Brief:
    return Brief.from_dict({
        "participant_id": "eng", "agenda_item_ids": ["ag-1"], "positions": [],
        "memory_snapshot": {"topic": "t", "facts": []}, "mandate": {"can_commit": [], "must_ask": []},
        "keywords": ["roadmap"]})


def test_scorer_raises_a_hand_from_a_notes_only_match():
    from orchestrator.models import MeetingRules
    rules = MeetingRules()
    text = "What is the conflict rate on mobile after the CRDT change?"
    plain = RuleScorer({"eng": brief_without_notes_topics()}, {"eng": ["Eng"]})
    noted = RuleScorer({"eng": brief_without_notes_topics()}, {"eng": ["Eng"]}, index_of(eng=EN_NOTES))
    s0, g0 = plain.score("eng", text, [], 0.0, "ag-1")
    s1, g1 = noted.score("eng", text, [], 0.0, "ag-1")
    assert s0.total < rules.hand_threshold <= s1.total                               # 只靠笔记也能举手
    assert s1.confidence >= 0.7 > s0.confidence and s1.relevance > s0.relevance
    assert g0 == "" and g1.startswith("We chose a CRDT")                                # 要点 = 命中段落的首句
    s2, _ = noted.score("eng", "What do you think about the logo colors?", [], 0.0, "ag-1")
    s3, _ = plain.score("eng", "What do you think about the logo colors?", [], 0.0, "ag-1")
    assert s2 == s3                                                                  # 没命中：分数不变


def test_scorer_keeps_fact_gist_and_is_identical_without_notes():
    from .test_scoring import en_brief
    facts = [("sync.conflict_rate", "conflict rate is about 3 percent")]
    texts = ["How is the conflict rate on sync?", "What about the roadmap?", "Totally unrelated words here"]
    plain = RuleScorer({"eng": en_brief(keywords=["sync"], facts=facts, stance="we sync")}, {"eng": ["Eng"]})
    for ix in (None, ContextIndex(), index_of(other="Notes of another bot about sync and conflict rate.")):
        s = RuleScorer({"eng": en_brief(keywords=["sync"], facts=facts, stance="we sync")}, {"eng": ["Eng"]}, ix)
        for t in texts:
            assert s.score("eng", t, ["said"], 0.1, "ag-1") == plain.score("eng", t, ["said"], 0.1, "ag-1")
    s = RuleScorer({"eng": en_brief(keywords=["sync"], facts=facts)}, {"eng": ["Eng"]},
                   index_of(eng="Conflict rate on sync: the CRDT merge brought it down. Details follow."))
    assert s.score("eng", "How is the conflict rate on sync?", [], 0.0, "ag-1")[1] == "conflict rate is about 3 percent"


def test_scorer_zh_notes_hit_and_build_scorer_passes_index():
    ix = index_of(eng=ZH_NOTES)
    sc = build_scorer({"eng": brief_without_notes_topics()}, {"eng": ["工程"]}, context_index=ix)
    assert type(sc) is RuleScorer and sc.context_index is ix
    s, gist = sc.score("eng", "现在的冲突率是多少？", [], 0.0, "ag-1")
    assert s.confidence >= 0.7 and gist.startswith("三月冲突率")
    other = RuleScorer({"eng": brief_without_notes_topics()}, {"eng": ["工程"]}).score("eng", "现在的冲突率是多少？", [], 0.0, "ag-1")[0]
    assert s.total > other.total


# ———————————————————— 端到端：来源进事件流和纪要 ————————————————————

def test_sources_flow_into_utterance_and_minutes():
    from orchestrator.minutes import build, render_md
    notes = "## 编译缓存\n\n编译缓存命中率目前约 87%，主要靠远程缓存。命中率低于 80% 时要查缓存键。"

    async def go():
        d = await started()
        ix = index_of(eng_bot=notes)
        d.llm.context_index = ix
        d.orch.scorer.context_index = ix
        d.say(11_000, "工程分身，编译缓存命中率是多少？")
        await d.run_until(30_000)
        utts = [e.data["utterance"] for e in d.orch.log.of_type("UtteranceFinal")
                if e.data["utterance"]["speaker_id"] == "eng_bot" and not e.data["utterance"].get("interrupted")]
        ctx = [u for u in utts if u.get("provenance") == "context"]
        assert ctx and ctx[0]["sources"] == ["eng_bot#1"] and ctx[0]["text"].startswith("我笔记里写着：")
        md = render_md(build(d.orch.log))
        assert "（context: eng_bot#1）" in md
        assert any(u.sources == ["eng_bot#1"] for u in d.orch.utterances)
    run(go())


def test_minutes_without_notes_have_no_context_tags():
    from orchestrator.minutes import build, render_md

    async def go():
        d = await started()
        await d.run_until(60_000)
        return render_md(build(d.orch.log))
    md = run(go())
    assert "（snapshot）" in md or "（brief）" in md        # 来源标签照旧
    assert "context" not in md


def test_format_excerpts_respects_budget():
    from orchestrator.retrieval import Hit
    hits = [Hit(f"a#{i}", "word " * 150, 0.5) for i in range(3)]
    out = format_excerpts(hits, 1200)
    assert len(out) <= 1200 and out.startswith("[a#0] ") and "…" in out
    assert format_excerpts([], 1200) == ""
