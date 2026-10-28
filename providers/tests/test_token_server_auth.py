"""web/token_server.py public mode: Bearer header, cookie, legacy ?k=, and the key never reaching the access log."""
import importlib.util
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KEY = "s3cret-key-for-tests"


def _load():
    spec = importlib.util.spec_from_file_location("token_server_auth_under_test", ROOT / "web" / "token_server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def test_key_via_header_cookie_and_legacy_query(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    ts = _load()
    monkeypatch.setattr(ts, "ACCESS_KEY", KEY)
    async with TestClient(TestServer(ts.make_app())) as c:
        assert (await c.get("/healthz")).status == 200                      # always open
        assert (await c.get("/")).status == 403
        assert (await c.get("/", headers={"Authorization": "Bearer nope"})).status == 403
        assert (await c.get("/", headers={"Authorization": f"Basic {KEY}"})).status == 403
        r = await c.get("/", headers={"Authorization": f"Bearer {KEY}"})
        assert r.status == 200 and "mk" not in r.cookies                    # header clients need no cookie
        assert (await c.get("/", headers={"authorization": f"bearer {KEY}"})).status == 200
        c.session.cookie_jar.clear()
        r = await c.get(f"/?k={KEY}")                                       # legacy link still works...
        assert r.status == 200 and "mk" in r.cookies                        # ...and is exchanged for a cookie
        assert r.cookies["mk"]["httponly"]
        assert (await c.get("/api/replay")).status == 404                   # replay stays closed in public mode
        c.session.cookie_jar.update_cookies({"mk": "stale"})
        assert (await c.get("/", headers={"Authorization": f"Bearer {KEY}"})).status == 200   # bad cookie does not mask a good header


def test_access_log_never_contains_the_query_string(caplog):
    from aiohttp.test_utils import make_mocked_request
    from aiohttp.web_log import AccessLogger
    from aiohttp.web import Response
    ts = _load()
    log = logging.getLogger("token_server_auth_test")
    lg = ts.PathOnlyAccessLogger(log, AccessLogger.LOG_FORMAT)
    req = make_mocked_request("GET", f"/api/token?room=r1&k={KEY}")
    with caplog.at_level(logging.INFO, logger=log.name):
        lg.log(req, Response(text="ok"), 0.01)
    assert "/api/token" in caplog.text and KEY not in caplog.text and "k=" not in caplog.text
