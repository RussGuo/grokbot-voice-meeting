import asyncio

from orchestrator.brief_compact import BRIEF_LIMIT, compact_brief, compact_snapshot
from orchestrator.bots import load_example
from orchestrator.chunker import SentenceChunker, pump_segments


def test_chunker_splits_on_sentence_punct():
    ch = SentenceChunker()
    out = ch.feed("好的。冲突率约 3%！要不要先做评估？") + ch.flush()
    assert out == ["好的。", "冲突率约 3%！", "要不要先做评估？"]


def test_chunker_cuts_long_run_over_20_chars():
    ch = SentenceChunker(max_chars=20)
    text = "这是一段没有句号但是很长很长很长的话，后面还有一些内容继续说下去不停"
    out = ch.feed(text) + ch.flush()
    assert "".join(out) == text
    assert all(len(s) <= 21 for s in out[:-1]) and len(out) >= 2
    assert out[0].endswith("，")                  # 优先在逗号处切


def test_chunker_semicolon_and_streaming_pieces():
    ch = SentenceChunker()
    segs = []
    for p in ["先灰", "度 1%", "；再看", "留存。"]:
        segs += ch.feed(p)
    assert segs == ["先灰度 1%；", "再看留存。"] and ch.flush() == []


def test_pump_segments_carries_token_and_stops_when_invalid():
    sent = []
    valid = {"ok": True}

    def send(seg):
        sent.append(seg)
        if seg.seq == 1:
            valid["ok"] = False                   # 第二段发出后令牌失效（例如被用户打断）

    pieces = ["第一句。", "第二句。", "第三句。", "第四句。"]
    r = asyncio.run(pump_segments(pieces, "tok-7", lambda t: valid["ok"] and t == "tok-7", send))
    assert [s.text for s in sent] == ["第一句。", "第二句。"]
    assert all(s.token_id == "tok-7" for s in sent) and r["stopped"] and r["sent"] == 2
    assert r["first_segment_ms"] is not None


def test_pump_segments_async_source_and_flush_final():
    async def src():
        for p in ["没有句号的", "尾巴"]:
            yield p
    sent = []
    r = asyncio.run(pump_segments(src(), "t", lambda t: True, sent.append))
    assert [s.text for s in sent] == ["没有句号的尾巴"] and sent[-1].final and not r["stopped"]


def test_fake_voice_segments_dropped_after_interrupt():
    from orchestrator.driver import SimDriver

    async def go():
        d = SimDriver()
        await d.start()
        tok = d.orch.token.id
        d.say(1_000, "停", dur_ms=600)
        await d.run_until(20_000)
        segs_after = [e for e in d.orch.log if e.type == "OutputDropped" and e.data.get("token_id") == tok]
        assert segs_after == []                   # 打断后语音层不再发旧令牌的段
        assert d.orch.metrics.get("segments")     # 打断前已收到分段
    asyncio.run(go())


def test_compact_brief_under_limit_keeps_stance_numbers_mandate():
    _, briefs = load_example()
    for b in briefs.values():
        text, cut, full = compact_brief(b.to_dict())
        assert len(text) <= BRIEF_LIMIT
        assert "【立场】" in text and "【授权】" in text and "【关键数字（会前快照）】" in text
        assert b.positions[0].stance in text
        assert all(m in text for m in b.mandate.must_ask)
        assert "source" not in text and "as_of" not in text


def test_compact_brief_truncates_by_priority_and_flags():
    _, briefs = load_example()
    d = briefs["eng_bot"].to_dict()
    d["memory_snapshot"]["facts"] += [{"key": f"extra.{i}", "value": "示例很长的事实" * 10, "source": "x", "as_of": "y"}
                                     for i in range(20)]
    d["memory_snapshot"]["recent_decisions"] += ["一条很长的决策" * 20]
    text, cut, full = compact_brief(d)
    assert cut and full > BRIEF_LIMIT and len(text) <= BRIEF_LIMIT
    assert "【立场】" in text and "【授权】" in text       # 高优先级保住
    assert "近期决策" not in text                          # 低优先级先丢
    assert "冲突率约 3%" in text                           # 带数字的事实优先保留


def test_compact_snapshot_numbers_first():
    _, briefs = load_example()
    t, cut = compact_snapshot(briefs["pm_bot"].memory_snapshot.to_dict())
    assert "四成" in t or "2 个百分点" in t and not cut


def test_truncation_logged_as_event():
    from orchestrator.driver import SimDriver

    async def go():
        d = SimDriver()
        d.orch.briefs["eng_bot"].memory_snapshot.facts *= 40
        d.orch.briefs["eng_bot"].memory_snapshot.recent_decisions += ["很长的决策" * 50]
        ctx = d.orch.assemble_chat_ctx("eng_bot", "answer", {})
        assert len(ctx.brief_text) <= BRIEF_LIMIT
        ev = d.orch.log.of_type("ContextTruncated")
        assert ev and ev[0].data["participant_id"] == "eng_bot" and ev[0].data["part"] == "brief"
    asyncio.run(go())
