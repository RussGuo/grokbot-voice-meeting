import asyncio
import json

from orchestrator.models import PrincipalQuery
from orchestrator.principal import FilePrincipal


def test_file_principal_delivers_when_the_real_bot_replies(tmp_path):
    delivered = []

    def schedule(delay, ev):
        delivered.append((delay, ev))

    client = FilePrincipal(schedule, tmp_path, poll_s=0.05)
    q = PrincipalQuery(id="pq-1", asked_by="pm", principal_bot_id="pm", question="预算呢？",
                       agenda_item_id="ag-1", context_event_seq=1, asked_at=0, ttl_ms=5_000)

    async def go():
        await client.ask(q)
        assert (tmp_path / "outbox" / "pq-1.json").is_file()
        (tmp_path / "inbox").mkdir()
        (tmp_path / "inbox" / "pq-1.json").write_text(json.dumps({"text": "两周。"}), encoding="utf-8")
        for _ in range(40):
            if delivered:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("answer was not delivered")

    asyncio.run(go())
    assert delivered[0][1]["text"] == "两周。"
    assert delivered[0][1]["query_id"] == "pq-1"
