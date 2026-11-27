from __future__ import annotations
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp_socks import ProxyConnectionError

from legisbr.core.http import HttpClient, HttpError, Response


def test_response_encoding_filename_and_json():
    response = Response(
        "url",
        200,
        {
            "content-type": "text/html; charset=iso-8859-1",
            "content-disposition": 'attachment; filename="lei.pdf"',
        },
        b"Legisla\xe7\xe3o",
    )
    assert response.text == "Legislação"
    assert response.filename == "lei.pdf"
    assert response.soup().text == "Legislação"
    assert Response("url", 200, {}, b'{"n":1}').json() == {"n": 1}


@pytest.mark.parametrize("label", ["iso-8859-1", "latin-1", "us-ascii"])
def test_latin1_labels_decode_as_windows_1252_like_browsers(label):
    # Ceará labels windows-1252 pages ISO-8859-1; \x96 is an en dash there.
    response = Response(
        "url", 200, {"content-type": f"text/html; charset={label}"}, b"Lei 1 \x96 Art."
    )
    assert response.text == "Lei 1 – Art."
    assert response.soup().text == "Lei 1 – Art."


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # RS link pages: no charset anywhere; chardet guessed windows-1250 ("Resoluçăo").
        (
            b"<html><a href='Resolu\xe7\xe3o 001.pdf'>Texto</a></html>",
            "Resolução 001.pdf",
        ),
        (
            "<html><a href='Resolução 001.pdf'>x</a></html>".encode(),
            "Resolução 001.pdf",
        ),
        (
            b'<html><head><meta charset="windows-1250"></head>'
            b"<a href='Resolu\xe7\xe3o 001.pdf'>x</a></html>",
            "Resoluçăo 001.pdf",
        ),
    ],
    ids=["undeclared-latin", "undeclared-utf8", "meta-declared"],
)
@pytest.mark.parametrize("headers", [{"content-type": "text/html"}, {}])
def test_undeclared_charset_decodes_like_browsers(body, expected, headers):
    response = Response("url", 200, headers, body)
    assert response.soup().a["href"] == expected
    assert expected in response.text


def test_json_ignores_meta_charset_inside_strings():
    """Goiás embeds whole HTML documents (with <meta charset>) in UTF-8 JSON."""
    payload = """{"conteudo": "<meta charset='windows-1252'>Resolução"}""".encode()
    response = Response("url", 200, {"content-type": "application/json"}, payload)
    assert response.json()["conteudo"].endswith("Resolução")


def test_response_charset_slices_head_to_8kb_and_avoids_message_allocations(
    monkeypatch,
):
    from bs4.dammit import EncodingDetector
    from multidict import CIMultiDict

    import legisbr.core.http as http_mod

    bom_lengths: list[int] = []
    declared_lengths: list[int] = []
    real_strip_bom = EncodingDetector.strip_byte_order_mark
    real_find_declared = EncodingDetector.find_declared_encoding

    def spy_strip_bom(data: bytes):
        bom_lengths.append(len(data))
        return real_strip_bom(data)

    def spy_find_declared(markup, *args, **kwargs):
        declared_lengths.append(len(markup))
        return real_find_declared(markup, *args, **kwargs)

    monkeypatch.setattr(EncodingDetector, "strip_byte_order_mark", spy_strip_bom)
    monkeypatch.setattr(EncodingDetector, "find_declared_encoding", spy_find_declared)

    # Normal headers must never allocate email.message.Message().
    def fail_message(*args, **kwargs):
        raise AssertionError("Message() should not be allocated for standard headers")

    monkeypatch.setattr(http_mod, "Message", fail_message)

    # Meta charset at offset ~3000 (within 8KB head, past default 2KB) on a 200KB payload.
    padding = b" " * 3000
    body = (
        b"<html><head>"
        + padding
        + b'<meta charset="windows-1252"></head><body>Resolu\xe7\xe3o'
        + b"x" * 200_000
        + b"</body></html>"
    )
    resp = Response(
        "https://example.org",
        200,
        CIMultiDict(
            {
                "Content-Type": "text/html",
                "Content-Disposition": 'attachment; filename="norma.html"',
            }
        ),
        body,
    )
    assert resp.content_type == "text/html"
    assert resp.filename == "norma.html"
    assert resp.charset == "windows-1252"
    assert "Resolução" in resp.text
    assert bom_lengths == [8192]
    assert declared_lengths == [8192]

    # BOM on a large payload also inspects only the first 8KB slice.
    bom_lengths.clear()
    bom_resp = Response(
        "https://example.org", 200, {}, b"\xef\xbb\xbf" + b"a" * 100_000
    )
    assert bom_resp.charset == "utf-8"
    assert bom_lengths == [8192]
    assert bom_resp.content_type == "text/plain"
    assert bom_resp.filename == "document"


def test_response_header_parsing_edge_cases():
    # RFC 2231 encoded filename* falls back cleanly to Message.
    r1 = Response(
        "u",
        200,
        {
            "Content-Type": 'TEXT/HTML; foo=bar; CHARSET="ISO-8859-1"',
            "Content-Disposition": "attachment; filename*=UTF-8''lei%20n%C2%BA%201.pdf",
        },
        b"Lei \x96 1",
    )
    assert r1.content_type == "text/html"
    assert r1.charset == "cp1252"
    assert r1.text == "Lei – 1"
    assert r1.filename == "lei nº 1.pdf"

    # Invalid/empty content-type defaults to text/plain like email.message.Message.
    r2 = Response("u", 200, {"Content-Type": "invalid"}, b"ok")
    assert r2.content_type == "text/plain"
    r3 = Response("u", 200, {"Content-Type": ""}, b"ok")
    assert r3.content_type == "text/plain"


@pytest.mark.parametrize("status", [400, 404, 503])
async def test_http_errors_raise_after_retry_budget(status):
    http = HttpClient(max_retries=1)
    response = MagicMock(status=status, reason="unavailable", url="url", headers={})
    response.read = AsyncMock(return_value=b"body")
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.request.return_value = context
    http._ensure_session = AsyncMock(return_value=session)
    with pytest.raises(HttpError) as exc:
        await http.request("url")
    assert exc.value.status == status
    context.__aexit__.assert_awaited_once()
    accepted = await http.request("url", ok_statuses=(status,))
    assert accepted.content == b"body"


async def test_body_read_is_inside_retry_and_response_is_released(monkeypatch):
    from tenacity import wait_none

    import legisbr.core.http as module

    monkeypatch.setattr(module, "wait_exponential", lambda **kw: wait_none())
    http = HttpClient(rps=1000, max_retries=2)
    response = MagicMock(status=200, url="url", headers={})
    response.read = AsyncMock(
        side_effect=[aiohttp.ClientPayloadError("truncated"), b"complete"]
    )
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.request.return_value = context
    http._ensure_session = AsyncMock(return_value=session)
    assert (await http.request("url")).content == b"complete"
    assert context.__aexit__.await_count == 2
    assert http.stats == {"requests": 2, "retries": 1, "bytes": 8}


async def test_block_pause_holds_every_request_until_it_ends(monkeypatch):
    """Paraná answers 403 to a client that went too fast and blocks it for ~30 min."""
    from tenacity import wait_none

    import legisbr.core.http as module

    monkeypatch.setattr(module, "wait_exponential", lambda **kw: wait_none())
    # 10 rps: the second request is still waiting on the limiter when the 403 lands.
    http = HttpClient(rps=10, max_retries=2, pause_on_403=0.3)
    sent: list[float] = []

    def respond(method, url, **kwargs):
        sent.append(asyncio.get_running_loop().time())
        status = 403 if len(sent) == 1 else 200
        response = MagicMock(status=status, reason="blocked", url=url, headers={})
        response.read = AsyncMock(return_value=b"ok")

        async def arrive() -> MagicMock:
            await asyncio.sleep(0.05 if status == 403 else 0)
            return response

        context = MagicMock()
        context.__aenter__ = AsyncMock(side_effect=arrive)
        context.__aexit__ = AsyncMock(return_value=False)
        return context

    session = MagicMock()
    session.request.side_effect = respond
    http._ensure_session = AsyncMock(return_value=session)

    await asyncio.gather(http.request("blocked"), http.request("other"))
    assert len(sent) == 3
    assert min(sent[1:]) - sent[0] >= 0.3


def _proxy_service(tmp_path, *proxies: str):
    from legisbr.core.proxy import ProxyService

    path = tmp_path / "proxies.txt"
    path.write_text("\n".join(proxies))
    return ProxyService({"file_path": str(path)})


def _session(status: int = 200, error: Exception | None = None) -> MagicMock:
    response = MagicMock(status=status, reason="r", url="url", headers={})
    response.read = AsyncMock(return_value=b"ok")
    context = MagicMock()
    context.__aenter__ = AsyncMock(side_effect=error, return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.request.return_value = context
    return session


async def test_proxies_rotate_and_failed_ones_rest_before_returning(tmp_path):
    service = _proxy_service(tmp_path, "http://a:1", "socks5://b:2", "c:3")
    assert [await service.get_proxy() for _ in range(4)] == [
        "http://a:1",
        "socks5://b:2",
        "http://c:3",
        "http://a:1",
    ]
    service.suspend("socks5://b:2", 0.1, "timeout")
    assert {await service.get_proxy() for _ in range(4)} == {"http://a:1", "http://c:3"}
    service.suspend("http://a:1", 60, "HTTP 403")
    service.suspend("http://c:3", 60, "HTTP 403")
    # With every proxy resting, requests go direct.
    assert await service.get_proxy() is None
    # Free proxies flap: a failed one comes back once its rest ends.
    await asyncio.sleep(0.15)
    assert await service.get_proxy() == "socks5://b:2"


async def test_each_proxy_has_its_own_rate_limit(tmp_path):
    """Paraná limits each IP; N proxies at 0.3 rps each fetch N times faster."""
    http = HttpClient(
        rps=1, proxy_service=_proxy_service(tmp_path, "http://a:1", "http://b:2")
    )
    http._ensure_session = AsyncMock(return_value=_session())
    loop = asyncio.get_running_loop()
    start = loop.time()
    await asyncio.gather(http.request("u1"), http.request("u2"))
    assert loop.time() - start < 0.5
    assert [c.args[0] for c in http._ensure_session.await_args_list] == [
        "http://a:1",
        "http://b:2",
    ]


@pytest.mark.parametrize(
    "failing",
    [
        _session(status=403),
        _session(error=ProxyConnectionError(111, "refused")),
        # Seen live: a proxy that hangs up mid SOCKS handshake.
        _session(error=asyncio.IncompleteReadError(b"", 2)),
    ],
    ids=["blocked", "dead", "hung-up"],
)
async def test_failed_proxy_rests_and_the_retry_uses_the_next(
    tmp_path, monkeypatch, failing
):
    from tenacity import wait_none

    import legisbr.core.http as module

    monkeypatch.setattr(module, "wait_exponential", lambda **kw: wait_none())
    service = _proxy_service(tmp_path, "http://a:1", "http://b:2")
    http = HttpClient(rps=1000, max_retries=2, pause_on_403=600, proxy_service=service)
    sessions = {"http://a:1": failing, "http://b:2": _session(), None: _session()}
    http._ensure_session = AsyncMock(side_effect=lambda proxy: sessions[proxy])
    assert (await http.request("url")).content == b"ok"
    assert {await service.get_proxy() for _ in range(3)} == {"http://b:2"}
    # A 403 rests the proxy through the site's block; other failures more briefly.
    assert service._resting["http://a:1"] - time.monotonic() > 100
    # A proxy's block is not the client's: direct requests carry on without a pause.
    await asyncio.wait_for(http.request("url", use_proxy=False), 1)


async def test_direct_requests_skip_the_proxies(tmp_path):
    http = HttpClient(rps=1000, proxy_service=_proxy_service(tmp_path, "http://a:1"))
    http._ensure_session = AsyncMock(return_value=_session())
    await http.request("url", use_proxy=False)
    assert http._ensure_session.await_args.args == (None,)


async def test_proxied_connections_verify_certificates(monkeypatch):
    """Free proxies are untrusted; without verification one could rewrite documents."""
    import ssl

    import aiohttp_socks

    from_url = MagicMock(side_effect=lambda url, **kw: aiohttp.TCPConnector())
    monkeypatch.setattr(aiohttp_socks.ProxyConnector, "from_url", from_url)
    http = HttpClient(verify_ssl=False)
    await http._ensure_session("socks5://a:1")
    assert from_url.call_args.kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
    await http.close()


def test_http_client_verifies_ssl_by_default_and_honors_insecure_opt_in():
    import ssl

    secure = HttpClient()
    assert secure.verify_ssl is True
    assert secure._ssl_ctx.check_hostname is True
    assert secure._ssl_ctx.verify_mode == ssl.CERT_REQUIRED

    insecure = HttpClient(verify_ssl=False)
    assert insecure.verify_ssl is False
    assert insecure._ssl_ctx.check_hostname is False
    assert insecure._ssl_ctx.verify_mode == ssl.CERT_NONE


async def test_http_client_configures_tcp_connector_limits(monkeypatch):
    captured: dict = {}

    class SpyConnector(aiohttp.TCPConnector):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(aiohttp, "TCPConnector", SpyConnector)
    http = HttpClient(
        max_connections=64, max_connections_per_host=24, ttl_dns_cache=120
    )
    await http._ensure_session(None)
    assert captured["limit"] == 64
    assert captured["limit_per_host"] == 24
    assert captured["ttl_dns_cache"] == 120
    assert "enable_cleanup_closed" not in captured
    await http.close()


async def test_missing_aiohttp_socks_raises_proxy_install_hint(tmp_path, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "aiohttp_socks", None)
    service = _proxy_service(tmp_path, "socks5://a:1")
    http = HttpClient(rps=1000, max_retries=1, proxy_service=service)
    with pytest.raises(ImportError, match="legisbr\\[proxy\\]"):
        await http.request("https://example.org")


async def test_gcra_rate_limiter_enforces_burst_one_spacing_at_0_3_rps(monkeypatch):
    """Paraná requires strict burst=1 spacing at 0.3 rps (~3.333 s per slot)."""
    import legisbr.core.http as module
    from legisbr.core.http import RateLimiter

    clock = 100.0
    sleeps: list[float] = []

    def fake_monotonic() -> float:
        return clock

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)

    limiter = RateLimiter(0.3)
    await asyncio.gather(*(limiter.acquire() for _ in range(4)))
    # First request fires immediately (0 sleep); subsequent ones wait 1/0.3, 2/0.3, 3/0.3.
    expected = [1 / 0.3, 2 / 0.3, 3 / 0.3]
    assert len(sleeps) == 3
    for actual, exp in zip(sleeps, expected, strict=True):
        assert actual == pytest.approx(exp, rel=1e-6)


async def test_gcra_rate_limiter_enforces_burst_one_spacing_at_100_rps():
    from legisbr.core.http import RateLimiter

    limiter = RateLimiter(100)
    stamps: list[float] = []
    for _ in range(6):
        await limiter.acquire()
        stamps.append(time.monotonic())
    gaps = [stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1)]
    assert stamps[-1] - stamps[0] >= 0.045
    assert all(gap >= 0.007 for gap in gaps)


async def test_gcra_rate_limiter_does_not_hold_lock_while_sleeping():
    from legisbr.core.http import RateLimiter

    limiter = RateLimiter(2)
    await limiter.acquire()
    waiter = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0.02)
    assert not limiter._lock.locked()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not limiter._lock.locked()


async def test_rotated_proxies_prune_retired_sessions_and_limiters(tmp_path):
    from legisbr.core.proxy import ProxyService

    path = tmp_path / "proxies.txt"
    path.write_text("http://a:1\nhttp://b:2\n", encoding="utf-8")
    service = ProxyService({"file_path": str(path), "ttl": 0})
    http = HttpClient(rps=1000, proxy_service=service)

    fake_sessions: dict[str, MagicMock] = {}

    async def make_session(proxy: str | None = None) -> MagicMock:
        assert proxy is not None
        if proxy not in fake_sessions:
            sess = _session()
            sess.closed = False
            sess.close = AsyncMock()
            fake_sessions[proxy] = sess
            http._proxy_sessions[proxy] = sess
        return fake_sessions[proxy]

    http._ensure_session = AsyncMock(side_effect=make_session)

    await http.request("https://example.org/1")
    await http.request("https://example.org/2")
    assert set(http._proxy_sessions) == {"http://a:1", "http://b:2"}
    assert set(http._proxy_limiters) == {"http://a:1", "http://b:2"}

    # Rotate out a:1 and b:2, replacing with c:3.
    path.write_text("http://c:3\n", encoding="utf-8")
    await http.request("https://example.org/3")

    assert set(http._proxy_sessions) == {"http://c:3"}
    assert set(http._proxy_limiters) == {"http://c:3"}
    fake_sessions["http://a:1"].close.assert_awaited_once()
    fake_sessions["http://b:2"].close.assert_awaited_once()
    fake_sessions["http://c:3"].close.assert_not_awaited()
    await http.close()
