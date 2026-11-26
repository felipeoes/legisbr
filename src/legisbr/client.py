"""Public async Client facade and convenience streaming/search helpers."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, Self

from legisbr.core.document import DocRef, Document
from legisbr.core.http import HttpClient
from legisbr.core.images import ImageCache
from legisbr.core.ocr import OCR
from legisbr.core.pdf import PdfExecutor
from legisbr.core.proxy import ProxyService
from legisbr.core.query import Query, UnsupportedQueryError, normalize_search_text
from legisbr.core.scraper import BaseScraper
from legisbr.core.sink import DocumentSink, ErrorRecord, NullSink
from legisbr.registry import ScraperSpec, SourceInfo, get_spec
from legisbr.registry import sources as registry_sources

logger = logging.getLogger(__name__)


def _normalize_years(years: int | range | None, default: range) -> range:
    if years is None:
        return default
    if isinstance(years, int):
        return range(years, years + 1)
    return years


class Client:
    """Ergonomic async library client managing per-source HTTP pools, OCR, and scrapers."""

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
        self._stack = AsyncExitStack()
        self._http_clients: dict[str, HttpClient] = {}
        self._scrapers: dict[str, BaseScraper] = {}
        self._setup_done: set[str] = set()
        self._setup_locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> Self:
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
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("Client cleanup failed", errors)

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
        self, spec: ScraperSpec, *, ensure_setup: bool = True
    ) -> BaseScraper:
        scraper = self._scrapers.get(spec.code)
        if scraper is None:
            ocr = await self._ensure_ocr(spec)
            scraper = self._build_scraper(spec, years=spec.years, ocr=ocr)
            self._scrapers[spec.code] = scraper
        if ensure_setup and spec.code not in self._setup_done:
            lock = self._setup_locks.setdefault(spec.code, asyncio.Lock())
            async with lock:
                if spec.code not in self._setup_done:
                    await scraper.setup()
                    self._setup_done.add(spec.code)
        return scraper

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
        years: int | range | None = None,
        types: Sequence[str] | None = None,
    ) -> AsyncIterator[DocRef]:
        """Stream document references from *source* without downloading full texts."""
        spec = get_spec(source)
        scraper = await self._get_scraper(spec, ensure_setup=True)
        target_years = _normalize_years(years, spec.years)
        wanted_types = {normalize_search_text(t) for t in types} if types else None
        uses_list_type = (
            type(scraper).list_documents is BaseScraper.list_documents
            and isinstance(scraper.types, dict)
            and bool(scraper.types)
        )

        for year in [None] if scraper.yearless else target_years:
            if uses_list_type:
                for type_name, type_value in scraper.types.items():
                    if (
                        wanted_types is not None
                        and normalize_search_text(type_name) not in wanted_types
                    ):
                        continue
                    try:
                        async for raw in scraper.list_type(year, type_name, type_value):
                            ref = dict(raw)
                            if year is not None:
                                ref.setdefault("year", year)
                            if scraper.yearless and ref.get("year") not in target_years:
                                continue
                            yield ref
                    except Exception as exc:
                        await self._dispatch_search_error(
                            {"year": year, "type": type_name}, exc
                        )
            else:
                try:
                    async for raw in scraper.list_documents(year):
                        ref = dict(raw)
                        if year is not None:
                            ref.setdefault("year", year)
                        if scraper.yearless and ref.get("year") not in target_years:
                            continue
                        if (
                            wanted_types is not None
                            and normalize_search_text(str(ref.get("type") or ""))
                            not in wanted_types
                        ):
                            continue
                        yield ref
                except Exception as exc:
                    await self._dispatch_search_error({"year": year}, exc)

    async def search_refs(
        self,
        source: str,
        *,
        years: int | range | None = None,
        number: str | None = None,
        type: str | None = None,
        text: str | None = None,
        situation: str | None = None,
        limit: int | None = 20,
        strict_native: bool = False,
    ) -> AsyncIterator[DocRef]:
        """Search *source* for matching document references."""
        spec = get_spec(source)
        query = Query(
            years=years,
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
        # Native / fast-catalog searches skip heavy setup() (e.g. SAPL assuntonorma).
        needs_setup = not spec.capabilities.supports_query(query)
        scraper = await self._get_scraper(spec, ensure_setup=needs_setup)
        async for ref in scraper.search_refs(
            query,
            strict_native=strict_native,
            on_error=self._dispatch_search_error if self.on_error is not None else None,
        ):
            yield ref

    async def fetch_ref(self, source: str, ref: DocRef) -> Document:
        """Fetch and convert a single document reference, reusing cached scraper setup."""
        spec = get_spec(source)
        scraper = await self._get_scraper(spec, ensure_setup=True)
        doc = await scraper.fetch(dict(ref))
        if doc is None:
            raise ValueError("Fetch produced no document")
        if self.low_memory:
            doc.raw = None
        return doc

    @asynccontextmanager
    async def scrape(
        self,
        source: str,
        *,
        years: int | range | None = None,
        types: Sequence[str] | None = None,
        limit: int | None = None,
        store: DocumentSink | None = None,
        overwrite: bool = False,
    ) -> AsyncIterator[AsyncIterator[Document]]:
        """Stream validated ``Document`` objects with zero disk writes unless *store* is passed."""
        spec = get_spec(source)
        target_years = _normalize_years(years, spec.years)
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


@asynccontextmanager
async def scrape(
    source: str,
    *,
    years: int | range | None = None,
    types: Sequence[str] | None = None,
    limit: int | None = None,
    store: DocumentSink | None = None,
    overwrite: bool = False,
    **client_kwargs: Any,
) -> AsyncIterator[AsyncIterator[Document]]:
    """Convenience wrapper opening a temporary ``Client`` to stream documents."""
    async with Client(**client_kwargs) as client:
        async with client.scrape(
            source,
            years=years,
            types=types,
            limit=limit,
            store=store,
            overwrite=overwrite,
        ) as docs:
            yield docs


async def search_refs(
    source: str,
    *,
    years: int | range | None = None,
    number: str | None = None,
    type: str | None = None,
    text: str | None = None,
    situation: str | None = None,
    limit: int | None = 20,
    strict_native: bool = False,
    **client_kwargs: Any,
) -> AsyncIterator[DocRef]:
    """Convenience wrapper opening a temporary ``Client`` to search document references."""
    async with Client(**client_kwargs) as client:
        async for ref in client.search_refs(
            source,
            years=years,
            number=number,
            type=type,
            text=text,
            situation=situation,
            limit=limit,
            strict_native=strict_native,
        ):
            yield ref
