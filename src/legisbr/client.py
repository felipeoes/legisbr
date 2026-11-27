"""Public sync ``Client``, async ``AsyncClient``, and top-level search/scrape helpers."""

from __future__ import annotations

import asyncio
import inspect
import logging
import weakref
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Self, TypeVar

from legisbr._sync import DocumentStream, _BridgeStream, _LoopThread
from legisbr.core.document import DocRef, Document
from legisbr.core.http import HttpClient
from legisbr.core.images import ImageCache
from legisbr.core.ocr import OCR
from legisbr.core.pdf import PdfExecutor
from legisbr.core.proxy import ProxyService
from legisbr.core.query import (
    NormNotFoundError,
    NormRef,
    Query,
    SearchResults,
    UnsupportedQueryError,
    normalize_search_text,
    normalize_year_filter,
    rank_best_ref,
)
from legisbr.core.scraper import BaseScraper, RunResult
from legisbr.core.sink import DocumentSink, ErrorRecord, NullSink
from legisbr.registry import ScraperSpec, SourceInfo, get_spec
from legisbr.registry import sources as registry_sources

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _normalize_years(years: int | range | None, default: range) -> range:
    if years is None:
        return default
    if isinstance(years, int):
        return range(years, years + 1)
    return years


class AsyncClient:
    """Native async library client managing per-source HTTP pools, OCR, and scrapers."""

    def __init__(
        self,
        *,
        ocr: OCR | str | None = None,
        proxy_file: str | Path | None = None,
        proxy_endpoint: str | None = None,
        workers: int | None = None,
        rps: float | None = None,
        force_rps: bool = False,
        low_memory: bool = False,
        verify_ssl: bool | None = None,
        pdf_executor: PdfExecutor = "auto",
        pdf_workers: int | None = None,
        image_cache_mb: int = 16,
        on_error: Callable[[ErrorRecord], Awaitable[None] | None] | None = None,
    ) -> None:
        if workers is not None and workers < 1:
            raise ValueError("workers must be positive")
        if rps is not None and rps <= 0:
            raise ValueError("rps must be positive")
        if image_cache_mb < 1:
            raise ValueError("image_cache_mb must be positive")

        self._init_kwargs: dict[str, Any] = {
            "ocr": ocr,
            "proxy_file": proxy_file,
            "proxy_endpoint": proxy_endpoint,
            "workers": workers,
            "rps": rps,
            "force_rps": force_rps,
            "low_memory": low_memory,
            "verify_ssl": verify_ssl,
            "pdf_executor": pdf_executor,
            "pdf_workers": pdf_workers,
            "image_cache_mb": image_cache_mb,
            "on_error": on_error,
        }

        self.low_memory = low_memory
        self.proxy_file = Path(proxy_file) if proxy_file is not None else None
        self.proxy_endpoint = proxy_endpoint
        self.rps = rps
        self.force_rps = force_rps
        self.verify_ssl = verify_ssl
        self.on_error = on_error

        if low_memory:
            self.workers: int | None = min(workers, 4) if workers is not None else 4
            self.pdf_executor: PdfExecutor = "thread"
            self.pdf_workers: int | None = 1
            self.image_cache_mb: int = min(image_cache_mb, 2)
            self._ocr_concurrency = 2
            self._ocr_dpi = 120
        else:
            self.workers = workers
            self.pdf_executor = pdf_executor
            self.pdf_workers = pdf_workers
            self.image_cache_mb = image_cache_mb
            self._ocr_concurrency = 8
            self._ocr_dpi = 150

        if isinstance(ocr, str):
            self._ocr_model: str = ocr.strip()
            self._ocr: OCR | None = None
        else:
            self._ocr_model = ""
            self._ocr = ocr

        self._entered = False
        self._closed = False
        self._stack = AsyncExitStack()
        self._ocr_lock = asyncio.Lock()
        self._http_clients: dict[str, HttpClient] = {}
        self._scrapers: dict[str, BaseScraper] = {}
        self._setup_done: set[str] = set()
        self._setup_locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> Self:
        self._closed = False
        self._entered = True
        if self._ocr is not None:
            await self._stack.enter_async_context(self._ocr.agent)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Close all per-source HTTP clients and managed async resources."""
        clients = list(self._http_clients.values())
        self._http_clients.clear()
        self._scrapers.clear()
        self._setup_done.clear()
        self._setup_locks.clear()
        errors: list[Exception] = []
        if clients:
            results = await asyncio.gather(
                *(client.close() for client in clients), return_exceptions=True
            )
            errors.extend(r for r in results if isinstance(r, Exception))
        try:
            await self._stack.aclose()
        except Exception as exc:
            errors.append(exc)
        self._entered = False
        self._closed = True
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("AsyncClient cleanup failed", errors)

    def sources(self) -> list[SourceInfo]:
        """Return lightweight metadata for all 30 registered legislation sources."""
        return registry_sources()

    def _effective_workers(self, spec: ScraperSpec) -> int:
        count = self.workers if self.workers is not None else spec.workers
        return min(count, 4) if self.low_memory else count

    def _effective_rps(self, spec: ScraperSpec) -> float:
        if self.rps is None:
            return spec.rps
        if self.force_rps:
            return self.rps
        return min(self.rps, spec.rps)

    def _proxy_config(self) -> dict[str, str] | None:
        if not (self.proxy_file or self.proxy_endpoint):
            return None
        cfg: dict[str, str] = {}
        if self.proxy_file is not None:
            cfg["file_path"] = str(self.proxy_file)
        if self.proxy_endpoint:
            cfg["endpoint"] = self.proxy_endpoint
        return cfg

    def _get_http_client(self, spec: ScraperSpec) -> HttpClient:
        http = self._http_clients.get(spec.code)
        if http is not None:
            return http
        proxy_cfg = self._proxy_config() if spec.needs_proxy else None
        workers = self._effective_workers(spec)
        verify_ssl = (
            (not spec.insecure_tls) if self.verify_ssl is None else self.verify_ssl
        )
        http = HttpClient(
            rps=self._effective_rps(spec),
            max_retries=spec.max_retries,
            proxy_service=ProxyService(proxy_cfg) if proxy_cfg else None,
            verify_ssl=verify_ssl,
            disable_cookies=spec.disable_cookies,
            pause_on_403=spec.pause_on_403,
            max_connections=max(100, workers),
            max_connections_per_host=max(16, workers),
            image_cache=ImageCache(max_bytes=self.image_cache_mb * 1024 * 1024),
        )
        self._http_clients[spec.code] = http
        return http

    async def _ensure_ocr(self, spec: ScraperSpec) -> OCR | None:
        if not spec.needs_ocr:
            return None
        if self._ocr is None and self._ocr_model:
            async with self._ocr_lock:
                if self._ocr is None and self._ocr_model:
                    ocr = OCR(
                        models=self._ocr_model,
                        concurrency=self._ocr_concurrency,
                        dpi=self._ocr_dpi,
                    )
                    if self._entered:
                        await self._stack.enter_async_context(ocr.agent)
                    self._ocr = ocr
        return self._ocr

    def _build_scraper(
        self,
        spec: ScraperSpec,
        *,
        years: range,
        store: DocumentSink | None = None,
        overwrite: bool = False,
        ocr: OCR | None = None,
    ) -> BaseScraper:
        http = self._get_http_client(spec)
        return spec.load_cls()(
            store=store if store is not None else NullSink(on_error=self.on_error),
            http=http,
            years=years,
            ocr=ocr,
            workers=self._effective_workers(spec),
            overwrite=overwrite,
            image_cache=http.image_cache,
            on_error=self.on_error,
            pdf_executor=self.pdf_executor,
            pdf_workers=self.pdf_workers,
        )

    async def _get_scraper(
        self,
        spec: ScraperSpec,
        *,
        ensure_setup: bool = True,
        need_ocr: bool = False,
    ) -> BaseScraper:
        scraper = self._scrapers.get(spec.code)
        if scraper is None:
            ocr = await self._ensure_ocr(spec) if need_ocr else None
            scraper = self._build_scraper(spec, years=spec.years, ocr=ocr)
            self._scrapers[spec.code] = scraper
        elif need_ocr and scraper.ocr is None:
            scraper.ocr = await self._ensure_ocr(spec)
        if ensure_setup and spec.code not in self._setup_done:
            lock = self._setup_locks.setdefault(spec.code, asyncio.Lock())
            async with lock:
                if spec.code not in self._setup_done:
                    await scraper.setup()
                    self._setup_done.add(spec.code)
        return scraper

    def _bind_ref(self, source: str, raw: Mapping[str, Any]) -> NormRef:
        client_ref = weakref.ref(self)
        fallback_kwargs = dict(self._init_kwargs)

        async def _async_fetch(ref: NormRef) -> Document:
            live = client_ref()
            if live is not None and not live._closed:
                return await live.fetch(ref)
            async with AsyncClient(**fallback_kwargs) as temp:
                return await temp.fetch(ref)

        def _sync_fetch(ref: NormRef) -> Document:
            with Client(**fallback_kwargs) as temp:
                return temp.fetch(ref)

        return NormRef.from_raw(
            source,
            raw,
            sync_fetch=_sync_fetch,
            async_fetch=_async_fetch,
        )

    def _bind_results(
        self, source: str, query: Query, items: Sequence[NormRef]
    ) -> SearchResults:
        client_ref = weakref.ref(self)
        fallback_kwargs = dict(self._init_kwargs)

        async def _async_fetch_many(
            refs: Sequence[NormRef], concurrency: int | None
        ) -> list[Document]:
            live = client_ref()
            if live is not None and not live._closed:
                return await live.fetch_many(refs, concurrency=concurrency)
            async with AsyncClient(**fallback_kwargs) as temp:
                return await temp.fetch_many(refs, concurrency=concurrency)

        def _sync_fetch_many(
            refs: Sequence[NormRef], concurrency: int | None
        ) -> list[Document]:
            with Client(**fallback_kwargs) as temp:
                return temp.fetch_many(refs, concurrency=concurrency)

        return SearchResults(
            source=source,
            query=query,
            items=tuple(items),
            _sync_fetch_many=_sync_fetch_many,
            _async_fetch_many=_async_fetch_many,
        )

    async def _dispatch_search_error(self, ref: DocRef, exc: Exception) -> None:
        if self.on_error is None:
            logger.warning("Search failed for %s: %s", ref, exc)
            return
        record = ErrorRecord(
            message=str(exc),
            year=ref.get("year"),
            type=str(ref.get("type", "") or ""),
            situation=str(ref.get("situation", "") or ""),
            title=str(ref.get("title", "") or ""),
            url=str(ref.get("document_url", "") or ""),
        )
        outcome = self.on_error(record)
        if inspect.isawaitable(outcome):
            await outcome

    async def list_refs(
        self,
        source: str,
        *,
        year: int | None = None,
        years: int | range | None = None,
        types: Sequence[str] | None = None,
    ) -> AsyncIterator[NormRef]:
        """Stream typed document references from *source* without downloading full texts."""
        effective_years = normalize_year_filter(year, years)
        spec = get_spec(source)
        scraper = await self._get_scraper(spec, ensure_setup=True)
        target_years = _normalize_years(effective_years, spec.years)
        wanted_types = {normalize_search_text(t) for t in types} if types else None
        uses_list_type = (
            type(scraper).list_documents is BaseScraper.list_documents
            and isinstance(scraper.types, dict)
            and bool(scraper.types)
        )

        for cur_year in [None] if scraper.yearless else target_years:
            if uses_list_type:
                for type_name, type_value in scraper.types.items():
                    if (
                        wanted_types is not None
                        and normalize_search_text(type_name) not in wanted_types
                    ):
                        continue
                    try:
                        async for raw in scraper.list_type(
                            cur_year, type_name, type_value
                        ):
                            ref = dict(raw)
                            if cur_year is not None:
                                ref.setdefault("year", cur_year)
                            if scraper.yearless and ref.get("year") not in target_years:
                                continue
                            yield self._bind_ref(spec.code, ref)
                    except Exception as exc:
                        await self._dispatch_search_error(
                            {"year": cur_year, "type": type_name}, exc
                        )
            else:
                try:
                    async for raw in scraper.list_documents(cur_year):
                        ref = dict(raw)
                        if cur_year is not None:
                            ref.setdefault("year", cur_year)
                        if scraper.yearless and ref.get("year") not in target_years:
                            continue
                        if (
                            wanted_types is not None
                            and normalize_search_text(str(ref.get("type") or ""))
                            not in wanted_types
                        ):
                            continue
                        yield self._bind_ref(spec.code, ref)
                except Exception as exc:
                    await self._dispatch_search_error({"year": cur_year}, exc)

    async def search_refs(
        self,
        source: str,
        *,
        year: int | None = None,
        years: int | range | None = None,
        number: str | int | None = None,
        type: str | None = None,
        text: str | None = None,
        situation: str | None = None,
        limit: int | None = 20,
        strict_native: bool = False,
    ) -> AsyncIterator[NormRef]:
        """Stream typed ``NormRef`` objects matching a query (for large/unbounded searches)."""
        effective_years = normalize_year_filter(year, years)
        spec = get_spec(source)
        query = Query(
            years=effective_years,
            number=number,
            type=type,
            text=text,
            situation=situation,
            limit=limit,
        )
        if strict_native and not spec.capabilities.supports_query(query):
            raise UnsupportedQueryError(
                f"{spec.name} does not natively support {query!r} "
                f"(capabilities={spec.capabilities!r})"
            )
        # Native search_refs() overrides (Camara, SAPL, SP, MG, PR) query portals
        # directly without setup() (e.g. skipping SAPL's assuntonorma preload).
        # Scrapers using BaseScraper.search_refs() delegate to list_documents() /
        # list_type(), so session setup() (e.g. DF's ASP.NET cookie) must still run.
        needs_setup = spec.load_cls().search_refs is BaseScraper.search_refs
        scraper = await self._get_scraper(spec, ensure_setup=needs_setup)
        async for ref in scraper.search_refs(
            query,
            strict_native=strict_native,
            on_error=self._dispatch_search_error if self.on_error is not None else None,
        ):
            yield self._bind_ref(spec.code, ref)

    async def search(
        self,
        source: str,
        *,
        year: int | None = None,
        years: int | range | None = None,
        number: str | int | None = None,
        type: str | None = None,
        text: str | None = None,
        situation: str | None = None,
        limit: int | None = 20,
        strict_native: bool = False,
    ) -> SearchResults:
        """Search *source* and return typed ``SearchResults[NormRef]`` bound to this client."""
        effective_years = normalize_year_filter(year, years)
        spec = get_spec(source)
        query = Query(
            years=effective_years,
            number=number,
            type=type,
            text=text,
            situation=situation,
            limit=limit,
        )
        items = [
            ref
            async for ref in self.search_refs(
                source,
                years=effective_years,
                number=query.number,
                type=type,
                text=text,
                situation=situation,
                limit=limit,
                strict_native=strict_native,
            )
        ]
        return self._bind_results(spec.code, query, items)

    async def get(
        self,
        source: str,
        *,
        year: int | None = None,
        years: int | range | None = None,
        number: str | int | None = None,
        type: str | None = None,
        text: str | None = None,
        situation: str | None = None,
        strict_native: bool = False,
    ) -> Document:
        """Search *source*, pick the best matching norm, fetch and return its ``Document``."""
        effective_years = normalize_year_filter(year, years)
        probe_query = Query(
            years=effective_years,
            number=number,
            type=type,
            text=text,
            situation=situation,
            limit=10 if number is not None else 1,
        )
        search_limit = 10 if probe_query.number is not None else 1
        results = await self.search(
            source,
            years=effective_years,
            number=probe_query.number,
            type=type,
            text=text,
            situation=situation,
            limit=search_limit,
            strict_native=strict_native,
        )
        if not results:
            raise NormNotFoundError(
                f"No matching norm found for source={source!r} with {probe_query!r}"
            )
        best = rank_best_ref(results, probe_query)
        return await self.fetch(best)

    async def fetch(self, ref: NormRef) -> Document:
        """Fetch and convert a typed ``NormRef`` using ``ref.source``."""
        return await self.fetch_ref(ref.source, ref)

    async def fetch_ref(self, source: str, ref: DocRef | NormRef) -> Document:
        """Fetch and convert a single document reference, reusing cached scraper setup."""
        spec = get_spec(source)
        scraper = await self._get_scraper(
            spec,
            ensure_setup=not spec.capabilities.heavy_setup,
            need_ocr=True,
        )
        payload = ref.raw_payload() if isinstance(ref, NormRef) else dict(ref)
        if "document_url" not in payload and "url" in payload:
            payload["document_url"] = payload["url"]
        doc = await scraper.fetch(payload)
        if doc is None:
            raise ValueError("Fetch produced no document")
        if self.low_memory:
            doc.raw = None
        return doc

    async def fetch_many(
        self,
        refs: Sequence[NormRef],
        *,
        concurrency: int | None = None,
    ) -> list[Document]:
        """Concurrently fetch multiple ``NormRef`` objects preserving input order."""
        if concurrency is not None and concurrency < 1:
            raise ValueError("concurrency must be positive")
        if not refs:
            return []

        global_sem = asyncio.Semaphore(concurrency) if concurrency is not None else None
        source_sems: dict[str, asyncio.Semaphore] = {}

        def _source_sem(source: str) -> asyncio.Semaphore:
            spec = get_spec(source)
            sem = source_sems.get(spec.code)
            if sem is None:
                cap = self._effective_workers(spec)
                if concurrency is not None:
                    cap = min(cap, concurrency)
                sem = asyncio.Semaphore(max(1, cap))
                source_sems[spec.code] = sem
            return sem

        docs: list[Document | None] = [None] * len(refs)

        async def _fetch_one(idx: int, ref: NormRef) -> None:
            s_sem = _source_sem(ref.source)
            if global_sem is not None:
                async with global_sem:
                    async with s_sem:
                        docs[idx] = await self.fetch(ref)
            else:
                async with s_sem:
                    docs[idx] = await self.fetch(ref)

        try:
            async with asyncio.TaskGroup() as group:
                for idx, ref in enumerate(refs):
                    group.create_task(_fetch_one(idx, ref))
        except ExceptionGroup as eg:
            if len(eg.exceptions) == 1:
                raise eg.exceptions[0] from None
            raise

        return [doc for doc in docs if doc is not None]

    @asynccontextmanager
    async def scrape(
        self,
        source: str,
        *,
        year: int | None = None,
        years: int | range | None = None,
        types: Sequence[str] | None = None,
        limit: int | None = None,
        store: DocumentSink | None = None,
        overwrite: bool = False,
    ) -> AsyncIterator[AsyncIterator[Document]]:
        """Stream validated ``Document`` objects with zero disk writes unless *store* is passed."""
        effective_years = normalize_year_filter(year, years)
        spec = get_spec(source)
        target_years = _normalize_years(effective_years, spec.years)
        ocr = await self._ensure_ocr(spec)
        scraper = self._build_scraper(
            spec,
            years=target_years,
            store=store,
            overwrite=overwrite,
            ocr=ocr,
        )
        # If the caller passed an unopened sink (e.g. Store(path)), manage its lifecycle;
        # if already open (Store._connection is not None), leave ownership to the caller.
        owns_store = store is not None and getattr(store, "_connection", None) is None
        drop_raw = self.low_memory and store is None
        async with AsyncExitStack() as stack:
            if owns_store:
                assert store is not None
                await stack.enter_async_context(store)
            async with scraper.stream(
                types=types,
                limit=limit,
                drop_raw=drop_raw,
                run_setup=True,
            ) as docs:
                yield docs

    async def run(
        self,
        source: str,
        *,
        year: int | None = None,
        years: int | range | None = None,
        types: Sequence[str] | None = None,
        limit: int | None = None,
        store: DocumentSink | None = None,
        overwrite: bool = False,
    ) -> RunResult:
        """Run a scrape to completion (persisting into *store* if provided) and return ``RunResult``."""
        effective_years = normalize_year_filter(year, years)
        spec = get_spec(source)
        target_years = _normalize_years(effective_years, spec.years)
        ocr = await self._ensure_ocr(spec)
        scraper = self._build_scraper(
            spec,
            years=target_years,
            store=store,
            overwrite=overwrite,
            ocr=ocr,
        )
        owns_store = store is not None and getattr(store, "_connection", None) is None
        drop_raw = self.low_memory and store is None
        async with AsyncExitStack() as stack:
            if owns_store:
                assert store is not None
                await stack.enter_async_context(store)
            if types is None and limit is None:
                return await scraper.run()
            async with scraper.stream(
                types=types,
                limit=limit,
                drop_raw=drop_raw,
                run_setup=True,
            ) as docs:
                async for _ in docs:
                    pass
            return scraper._result


def _close_sync_client_resources(
    async_client: AsyncClient, loop_thread: _LoopThread
) -> None:
    try:
        if loop_thread.is_running:
            try:
                loop_thread.submit(async_client.close())
            except RuntimeError:
                pass
    finally:
        loop_thread.close()


class Client:
    """First-class synchronous library client backed by a Jupyter-safe private event loop."""

    def __init__(
        self,
        *,
        ocr: OCR | str | None = None,
        proxy_file: str | Path | None = None,
        proxy_endpoint: str | None = None,
        workers: int | None = None,
        rps: float | None = None,
        force_rps: bool = False,
        low_memory: bool = False,
        verify_ssl: bool | None = None,
        pdf_executor: PdfExecutor = "auto",
        pdf_workers: int | None = None,
        image_cache_mb: int = 16,
        on_error: Callable[[ErrorRecord], Awaitable[None] | None] | None = None,
    ) -> None:
        self._async_client = AsyncClient(
            ocr=ocr,
            proxy_file=proxy_file,
            proxy_endpoint=proxy_endpoint,
            workers=workers,
            rps=rps,
            force_rps=force_rps,
            low_memory=low_memory,
            verify_ssl=verify_ssl,
            pdf_executor=pdf_executor,
            pdf_workers=pdf_workers,
            image_cache_mb=image_cache_mb,
            on_error=on_error,
        )
        self._init_kwargs = dict(self._async_client._init_kwargs)
        self._loop_thread = _LoopThread()
        self._closed = False
        self._finalizer = weakref.finalize(
            self,
            _close_sync_client_resources,
            self._async_client,
            self._loop_thread,
        )

    @property
    def workers(self) -> int | None:
        return self._async_client.workers

    @property
    def rps(self) -> float | None:
        return self._async_client.rps

    @property
    def force_rps(self) -> bool:
        return self._async_client.force_rps

    @property
    def low_memory(self) -> bool:
        return self._async_client.low_memory

    @property
    def verify_ssl(self) -> bool | None:
        return self._async_client.verify_ssl

    @property
    def pdf_executor(self) -> PdfExecutor:
        return self._async_client.pdf_executor

    @property
    def pdf_workers(self) -> int | None:
        return self._async_client.pdf_workers

    @property
    def image_cache_mb(self) -> int:
        return self._async_client.image_cache_mb

    @property
    def proxy_file(self) -> Path | None:
        return self._async_client.proxy_file

    @property
    def proxy_endpoint(self) -> str | None:
        return self._async_client.proxy_endpoint

    @property
    def on_error(self) -> Callable[[ErrorRecord], Awaitable[None] | None] | None:
        return self._async_client.on_error

    def __enter__(self) -> Self:
        if self._closed:
            raise RuntimeError("Client is closed")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close all underlying HTTP pools, async resources, and the private event loop."""
        if self._closed:
            return
        self._closed = True
        self._finalizer()

    async def _ensure_async_entered(self) -> None:
        if not self._async_client._entered:
            await self._async_client.__aenter__()

    def _run_coro(self, coro_fn: Callable[[], Awaitable[_T]]) -> _T:
        if self._closed:
            raise RuntimeError("Client is closed")

        async def _wrapped() -> _T:
            await self._ensure_async_entered()
            return await coro_fn()

        return self._loop_thread.submit(_wrapped())

    def _rebind_ref(self, ref: NormRef) -> NormRef:
        client_ref = weakref.ref(self)
        fallback_kwargs = dict(self._init_kwargs)

        def _sync_fetch(target: NormRef) -> Document:
            live = client_ref()
            if live is not None and not live._closed:
                return live.fetch(target)
            with Client(**fallback_kwargs) as temp:
                return temp.fetch(target)

        async def _async_fetch(target: NormRef) -> Document:
            # Async callers run on their own event loop; never share _loop_thread's aiohttp pool.
            async with AsyncClient(**fallback_kwargs) as temp:
                return await temp.fetch(target)

        return replace(ref, _sync_fetch=_sync_fetch, _async_fetch=_async_fetch)

    def _rebind_results(self, results: SearchResults) -> SearchResults:
        client_ref = weakref.ref(self)
        fallback_kwargs = dict(self._init_kwargs)
        rebound_items = tuple(self._rebind_ref(ref) for ref in results.items)

        def _sync_fetch_many(
            refs: Sequence[NormRef], concurrency: int | None
        ) -> list[Document]:
            live = client_ref()
            if live is not None and not live._closed:
                return live.fetch_many(refs, concurrency=concurrency)
            with Client(**fallback_kwargs) as temp:
                return temp.fetch_many(refs, concurrency=concurrency)

        async def _async_fetch_many(
            refs: Sequence[NormRef], concurrency: int | None
        ) -> list[Document]:
            async with AsyncClient(**fallback_kwargs) as temp:
                return await temp.fetch_many(refs, concurrency=concurrency)

        return SearchResults(
            source=results.source,
            query=results.query,
            items=rebound_items,
            _sync_fetch_many=_sync_fetch_many,
            _async_fetch_many=_async_fetch_many,
        )

    def sources(self) -> list[SourceInfo]:
        """Return lightweight metadata for all 30 registered legislation sources."""
        return registry_sources()

    def get(
        self,
        source: str,
        *,
        year: int | None = None,
        years: int | range | None = None,
        number: str | int | None = None,
        type: str | None = None,
        text: str | None = None,
        situation: str | None = None,
        strict_native: bool = False,
    ) -> Document:
        """Search *source*, pick the best matching norm, fetch and return its ``Document``."""
        return self._run_coro(
            lambda: self._async_client.get(
                source,
                year=year,
                years=years,
                number=number,
                type=type,
                text=text,
                situation=situation,
                strict_native=strict_native,
            )
        )

    def search(
        self,
        source: str,
        *,
        year: int | None = None,
        years: int | range | None = None,
        number: str | int | None = None,
        type: str | None = None,
        text: str | None = None,
        situation: str | None = None,
        limit: int | None = 20,
        strict_native: bool = False,
    ) -> SearchResults:
        """Search *source* and return typed ``SearchResults[NormRef]`` bound to this client."""
        results = self._run_coro(
            lambda: self._async_client.search(
                source,
                year=year,
                years=years,
                number=number,
                type=type,
                text=text,
                situation=situation,
                limit=limit,
                strict_native=strict_native,
            )
        )
        return self._rebind_results(results)

    def fetch(self, ref: NormRef) -> Document:
        """Fetch and convert a typed ``NormRef`` using ``ref.source``."""
        return self._run_coro(lambda: self._async_client.fetch(ref))

    def fetch_ref(self, source: str, ref: DocRef | NormRef) -> Document:
        """Fetch and convert a single document reference, reusing cached scraper setup."""
        return self._run_coro(lambda: self._async_client.fetch_ref(source, ref))

    def fetch_many(
        self,
        refs: Sequence[NormRef],
        *,
        concurrency: int | None = None,
    ) -> list[Document]:
        """Concurrently fetch multiple ``NormRef`` objects preserving input order."""
        return self._run_coro(
            lambda: self._async_client.fetch_many(refs, concurrency=concurrency)
        )

    def list_refs(
        self,
        source: str,
        *,
        year: int | None = None,
        years: int | range | None = None,
        types: Sequence[str] | None = None,
    ) -> Iterator[NormRef]:
        """Stream typed ``NormRef`` objects from *source* without downloading full texts."""
        if self._closed:
            raise RuntimeError("Client is closed")
        effective_years = normalize_year_filter(year, years)
        spec = get_spec(source)
        maxsize = max(16, self._async_client._effective_workers(spec) * 2)

        async def _rebound_gen() -> AsyncIterator[NormRef]:
            async for ref in self._async_client.list_refs(
                source, years=effective_years, types=types
            ):
                yield self._rebind_ref(ref)

        return _BridgeStream(
            self._loop_thread,
            lambda: aclosing(_rebound_gen()),
            maxsize=maxsize,
            before_enter=self._ensure_async_entered,
        )

    def _open_scrape_stream(
        self,
        source: str,
        *,
        year: int | None = None,
        years: int | range | None = None,
        types: Sequence[str] | None = None,
        limit: int | None = None,
        store: DocumentSink | None = None,
        overwrite: bool = False,
        owns_client: bool = False,
    ) -> DocumentStream:
        if self._closed:
            raise RuntimeError("Client is closed")
        effective_years = normalize_year_filter(year, years)
        spec = get_spec(source)
        maxsize = max(16, self._async_client._effective_workers(spec) * 2)
        on_close = self.close if owns_client else None
        return DocumentStream(
            self._loop_thread,
            lambda: self._async_client.scrape(
                source,
                years=effective_years,
                types=types,
                limit=limit,
                store=store,
                overwrite=overwrite,
            ),
            maxsize=maxsize,
            before_enter=self._ensure_async_entered,
            on_close=on_close,
        )

    def scrape(
        self,
        source: str,
        *,
        year: int | None = None,
        years: int | range | None = None,
        types: Sequence[str] | None = None,
        limit: int | None = None,
        store: DocumentSink | None = None,
        overwrite: bool = False,
    ) -> DocumentStream:
        """Stream validated ``Document`` objects with backpressure and deterministic cleanup."""
        return self._open_scrape_stream(
            source,
            year=year,
            years=years,
            types=types,
            limit=limit,
            store=store,
            overwrite=overwrite,
            owns_client=False,
        )

    def run(
        self,
        source: str,
        *,
        year: int | None = None,
        years: int | range | None = None,
        types: Sequence[str] | None = None,
        limit: int | None = None,
        store: DocumentSink | None = None,
        overwrite: bool = False,
    ) -> RunResult:
        """Run a scrape to completion (persisting into *store* if provided) and return ``RunResult``."""
        return self._run_coro(
            lambda: self._async_client.run(
                source,
                year=year,
                years=years,
                types=types,
                limit=limit,
                store=store,
                overwrite=overwrite,
            )
        )


def get(
    source: str,
    *,
    year: int | None = None,
    years: int | range | None = None,
    number: str | int | None = None,
    type: str | None = None,
    text: str | None = None,
    situation: str | None = None,
    strict_native: bool = False,
    **client_kwargs: Any,
) -> Document:
    """Synchronously search *source*, fetch the best matching norm, and return its ``Document``."""
    with Client(**client_kwargs) as client:
        return client.get(
            source,
            year=year,
            years=years,
            number=number,
            type=type,
            text=text,
            situation=situation,
            strict_native=strict_native,
        )


def search(
    source: str,
    *,
    year: int | None = None,
    years: int | range | None = None,
    number: str | int | None = None,
    type: str | None = None,
    text: str | None = None,
    situation: str | None = None,
    limit: int | None = 20,
    strict_native: bool = False,
    **client_kwargs: Any,
) -> SearchResults:
    """Synchronously search *source* and return typed ``SearchResults[NormRef]``."""
    with Client(**client_kwargs) as client:
        return client.search(
            source,
            year=year,
            years=years,
            number=number,
            type=type,
            text=text,
            situation=situation,
            limit=limit,
            strict_native=strict_native,
        )


def scrape(
    source: str,
    *,
    year: int | None = None,
    years: int | range | None = None,
    types: Sequence[str] | None = None,
    limit: int | None = None,
    store: DocumentSink | None = None,
    overwrite: bool = False,
    **client_kwargs: Any,
) -> DocumentStream:
    """Synchronously stream validated ``Document`` objects from *source*."""
    # Validate eagerly so invalid args fail before allocating a Client or thread.
    effective_years = normalize_year_filter(year, years)
    get_spec(source)
    client = Client(**client_kwargs)
    try:
        return client._open_scrape_stream(
            source,
            years=effective_years,
            types=types,
            limit=limit,
            store=store,
            overwrite=overwrite,
            owns_client=True,
        )
    except BaseException:
        client.close()
        raise
