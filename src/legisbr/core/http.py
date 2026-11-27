"""Buffered HTTP responses with rate limiting and retries."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from dataclasses import dataclass
from email.message import Message
from typing import Any

import aiohttp
from bs4 import BeautifulSoup
from bs4.dammit import EncodingDetector
from multidict import CIMultiDict
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from legisbr.core.images import ImageCache
from legisbr.core.proxy import ProxyService

logger = logging.getLogger(__name__)

# How long a proxy that failed to connect or answer sits out before another try.
_PROXY_REST = 5 * 60
_PROXY_INSTALL_HINT = "Install proxy support with: pip install 'legisbr[proxy]'"


def _load_proxy_module() -> Any:
    try:
        import aiohttp_socks
    except ImportError as exc:
        raise ImportError(_PROXY_INSTALL_HINT) from exc
    return aiohttp_socks


class RateLimiter:
    """Strict burst=1 GCRA rate limiter (lock-free while sleeping, cancellation-safe)."""

    def __init__(self, requests_per_second: float) -> None:
        if requests_per_second <= 0:
            raise ValueError("rps must be positive")
        self.requests_per_second = requests_per_second
        self._interval = 1.0 / requests_per_second
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval
        delay = slot - now
        if delay > 0:
            await asyncio.sleep(delay)


class HttpError(Exception):
    def __init__(self, url: str, status: int | None, reason: str) -> None:
        self.url, self.status, self.reason = (url, status, reason)
        super().__init__(f"{url}: HTTP {status}: {reason}")


class _RetryableError(HttpError):
    pass


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    headers: dict[str, str]
    content: bytes

    def _message(self) -> Message:
        message = Message()
        for key, value in self.headers.items():
            message[key] = value
        return message

    @property
    def charset(self) -> str:
        charset = (
            self._message().get_content_charset()
            or EncodingDetector.strip_byte_order_mark(self.content)[1]
            # JSON may embed whole HTML documents; their <meta> doesn't describe the JSON.
            or EncodingDetector.find_declared_encoding(
                self.content, is_html="json" not in self.content_type
            )
        )
        if not charset:
            # Like browsers: UTF-8 when valid, else windows-1252. bs4's chardet guess
            # varies per page (RS link pages came out windows-1250).
            try:
                self.content.decode("utf-8")
                charset = "utf-8"
            except UnicodeDecodeError:
                charset = "cp1252"
        charset = charset.lower()
        # Browsers (WHATWG) decode these labels as windows-1252; Ceará relies on it.
        if charset in {"iso-8859-1", "latin-1", "latin1", "us-ascii", "ascii"}:
            return "cp1252"
        return charset

    @property
    def text(self) -> str:
        charset = self.charset
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def soup(self) -> BeautifulSoup:
        return BeautifulSoup(self.content, "lxml", from_encoding=self.charset)

    @property
    def content_type(self) -> str:
        return self._message().get_content_type()

    @property
    def filename(self) -> str:
        return self._message().get_filename() or "document"


class HttpClient:
    def __init__(
        self,
        rps: float = 10,
        max_retries: int = 6,
        proxy_service: ProxyService | None = None,
        verify_ssl: bool = True,
        disable_cookies: bool = False,
        proxy_timeout: int = 15,
        pause_on_403: float = 0,
        max_connections: int = 100,
        max_connections_per_host: int = 32,
        ttl_dns_cache: int = 300,
        image_cache: ImageCache | None = None,
    ) -> None:
        if max_retries < 1:
            raise ValueError("max_retries must be positive")
        self.max_retries = max_retries
        self.verify_ssl = verify_ssl
        self.limiter = RateLimiter(rps)
        # Sites limit each IP, so every proxy gets its own budget.
        self._proxy_limiters: dict[str, RateLimiter] = {}
        # For sites whose 403 blocks the whole client for a while: every direct request
        # waits this many seconds instead of failing against the block. A blocked proxy
        # rests that long instead.
        self.pause_on_403 = pause_on_403
        self._resume_at = 0.0
        self.proxy_service = proxy_service
        self._ssl_ctx = ssl.create_default_context()
        if not verify_ssl:
            self._ssl_ctx.check_hostname = False
            self._ssl_ctx.verify_mode = ssl.CERT_NONE
            # legislabahia.ba.gov.br only offers TLS 1.2 RSA key exchange, which
            # OpenSSL's default security level rejects.
            self._ssl_ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        self.headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        }
        self._disable_cookies = disable_cookies
        self._proxy_timeout = proxy_timeout
        self._max_connections = max_connections
        self._max_connections_per_host = max_connections_per_host
        self._ttl_dns_cache = ttl_dns_cache
        self.image_cache = image_cache if image_cache is not None else ImageCache()
        self._default_session: aiohttp.ClientSession | None = None
        self._proxy_sessions: dict[str, aiohttp.ClientSession] = {}
        self._inflight_proxies: dict[str, int] = {}
        self._session_lock = asyncio.Lock()
        self.stats = {"requests": 0, "retries": 0, "bytes": 0}

    async def _prune_retired_proxies(self, current_proxy: str | None = None) -> None:
        """Close and drop cached sessions/limiters for proxies rotated out of ProxyService."""
        if not self.proxy_service or not (self._proxy_sessions or self._proxy_limiters):
            return
        if not getattr(self.proxy_service, "_loaded", True):
            return
        raw_proxies = getattr(self.proxy_service, "proxies", None)
        if raw_proxies is None:
            return
        active = {p if "://" in p else f"http://{p}" for p in raw_proxies}
        if current_proxy:
            active.add(current_proxy)
        in_flight = {p for p, count in self._inflight_proxies.items() if count > 0}
        keep = active | in_flight

        for stale in [p for p in self._proxy_limiters if p not in keep]:
            self._proxy_limiters.pop(stale, None)

        retired_sessions: list[aiohttp.ClientSession] = []
        async with self._session_lock:
            for stale in [p for p in self._proxy_sessions if p not in keep]:
                session = self._proxy_sessions.pop(stale, None)
                if session is not None and not session.closed:
                    retired_sessions.append(session)
        if retired_sessions:
            await asyncio.gather(
                *(s.close() for s in retired_sessions), return_exceptions=True
            )

    async def _ensure_session(self, proxy: str | None = None) -> aiohttp.ClientSession:
        async with self._session_lock:
            session = (
                self._proxy_sessions.get(proxy) if proxy else self._default_session
            )
            if session is None or session.closed:
                if proxy:
                    # Proxies are untrusted: always verify, so none can alter a page.
                    proxy_mod = _load_proxy_module()
                    connector = proxy_mod.ProxyConnector.from_url(
                        proxy, ssl=ssl.create_default_context()
                    )
                else:
                    connector = aiohttp.TCPConnector(
                        ssl=self._ssl_ctx,
                        limit=self._max_connections,
                        limit_per_host=self._max_connections_per_host,
                        ttl_dns_cache=self._ttl_dns_cache,
                    )
                session = aiohttp.ClientSession(
                    headers=self.headers,
                    connector=connector,
                    cookie_jar=aiohttp.DummyCookieJar()
                    if self._disable_cookies
                    else None,
                )
                if proxy:
                    self._proxy_sessions[proxy] = session
                else:
                    self._default_session = session
            return session

    async def request(
        self,
        url: str,
        method: str = "GET",
        *,
        ok_statuses: tuple[int, ...] = (),
        timeout: float = 120,
        use_proxy: bool = True,
        **kwargs: Any,
    ) -> Response:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=2, min=5, max=60),
            retry=retry_if_exception_type(_RetryableError),
            reraise=True,
        ):
            with attempt:
                self.stats["requests"] += 1
                self.stats["retries"] += attempt.retry_state.attempt_number > 1
                proxy = (
                    await self.proxy_service.get_proxy()
                    if self.proxy_service and use_proxy
                    else None
                )
                if self.proxy_service and use_proxy:
                    await self._prune_retired_proxies(proxy)
                retry_errors: tuple[type[BaseException], ...] = (
                    aiohttp.ClientError,
                    asyncio.IncompleteReadError,
                    TimeoutError,
                    ConnectionError,
                )
                if proxy:
                    proxy_mod = _load_proxy_module()
                    retry_errors += (
                        proxy_mod.ProxyError,
                        proxy_mod.ProxyConnectionError,
                        proxy_mod.ProxyTimeoutError,
                    )
                    if proxy not in self._proxy_limiters:
                        self._proxy_limiters[proxy] = RateLimiter(
                            self.limiter.requests_per_second
                        )
                    await self._proxy_limiters[proxy].acquire()
                else:
                    await self.limiter.acquire()
                    # Checked after the limiter: a block may begin while this waits.
                    while (pause := self._resume_at - time.monotonic()) > 0:
                        await asyncio.sleep(pause)
                        await self.limiter.acquire()
                session = await self._ensure_session(proxy)
                if proxy:
                    self._inflight_proxies[proxy] = (
                        self._inflight_proxies.get(proxy, 0) + 1
                    )
                try:
                    request_timeout = aiohttp.ClientTimeout(
                        total=timeout, connect=self._proxy_timeout if proxy else None
                    )
                    async with session.request(
                        method, url, timeout=request_timeout, **kwargs
                    ) as response:
                        if (
                            response.status >= 400
                            and response.status not in ok_statuses
                        ):
                            if response.status == 403 and self.pause_on_403:
                                if proxy:
                                    self.proxy_service.suspend(
                                        proxy, self.pause_on_403, "HTTP 403"
                                    )
                                    raise _RetryableError(url, 403, "proxy blocked")
                                logger.warning(
                                    f"HTTP 403 from {url}; pausing requests for {self.pause_on_403:.0f}s"
                                )
                                self._resume_at = time.monotonic() + self.pause_on_403
                            error_cls = (
                                _RetryableError
                                if response.status
                                in {403, 408, 429, 500, 502, 503, 504}
                                else HttpError
                            )
                            raise error_cls(
                                url,
                                response.status,
                                response.reason or "Request failed",
                            )
                        content = await response.read()
                        self.stats["bytes"] += len(content)
                        return Response(
                            str(response.url),
                            response.status,
                            CIMultiDict(response.headers),
                            content,
                        )
                except retry_errors as exc:
                    if proxy:
                        self.proxy_service.suspend(
                            proxy, _PROXY_REST, type(exc).__name__
                        )
                    raise _RetryableError(url, None, str(exc)) from exc
                finally:
                    if proxy:
                        remaining = self._inflight_proxies.get(proxy, 1) - 1
                        if remaining <= 0:
                            self._inflight_proxies.pop(proxy, None)
                        else:
                            self._inflight_proxies[proxy] = remaining
        raise AssertionError("Retry loop did not execute")

    async def close(self) -> None:
        sessions = list(self._proxy_sessions.values())
        if self._default_session is not None:
            sessions.append(self._default_session)
        results = await asyncio.gather(
            *(s.close() for s in sessions), return_exceptions=True
        )
        self._proxy_sessions.clear()
        self._proxy_limiters.clear()
        self._inflight_proxies.clear()
        self._default_session = None
        if self.proxy_service:
            await self.proxy_service.cleanup()
        failures = [r for r in results if isinstance(r, Exception)]
        if failures:
            raise ExceptionGroup("HTTP cleanup failed", failures)
