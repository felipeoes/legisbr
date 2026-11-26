"""Bounded listing → fetch → save pipeline shared by every scraper."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import urljoin

from legisbr.core.convert import (
    bytes_to_markdown,
    detect_extension,
    html_to_markdown,
    sniff_extension,
)
from legisbr.core.document import DocRef, Document, clean_title
from legisbr.core.http import HttpClient
from legisbr.core.images import ImageCache
from legisbr.core.ocr import OCR, usage_scope
from legisbr.core.query import (
    Query,
    SearchCapabilities,
    UnsupportedQueryError,
    match_type_entry,
    matches_query,
)
from legisbr.core.store import Store

logger = logging.getLogger(__name__)

DEFAULT_VALID_SITUATION = "Não consta revogação expressa"
DEFAULT_INVALID_SITUATION = "Revogada"


@dataclass
class RunResult:
    docs: int = 0
    errors: int = 0
    skipped: int = 0
    listed: int = 0
    seconds: float = 0.0
    listing_seconds: float = 0.0
    fetch_seconds: list[float] = field(default_factory=list, repr=False)

    def stats(self) -> dict:
        """Performance summary stored with the run (see `main.py stats`)."""
        times = sorted(self.fetch_seconds)

        def quantile(q: float) -> float | None:
            return (
                round(times[min(len(times) - 1, int(len(times) * q))], 2)
                if times
                else None
            )

        return {
            "seconds": round(self.seconds, 1),
            "listing_seconds": round(self.listing_seconds, 1),
            "listed": self.listed,
            "docs": self.docs,
            "errors": self.errors,
            "skipped": self.skipped,
            "docs_per_minute": round(self.docs * 60 / self.seconds, 1)
            if self.seconds
            else 0.0,
            "fetch_p50": quantile(0.5),
            "fetch_p95": quantile(0.95),
            "fetch_max": quantile(1.0),
        }


class BaseScraper:
    name: ClassVar[str]
    base_url: ClassVar[str]
    types: ClassVar[dict | list] = {}
    situations: ClassVar[dict | list] = {}
    yearless: ClassVar[bool] = False
    capabilities: ClassVar[SearchCapabilities] = SearchCapabilities()

    def __init__(
        self,
        *,
        store: Store,
        http: HttpClient,
        years: range,
        ocr: OCR | None = None,
        workers: int = 16,
        overwrite: bool = False,
        image_cache: ImageCache | None = None,
    ) -> None:
        if workers < 1:
            raise ValueError("workers must be positive")
        self.store, self.http, self.ocr = (store, http, ocr)
        self.image_cache = (
            image_cache
            if image_cache is not None
            else getattr(http, "image_cache", None) or ImageCache()
        )
        self.years, self.workers, self.overwrite = (years, workers, overwrite)
        self._stack = AsyncExitStack()
        self._run_id: int | None = None
        self._result = RunResult()

    async def __aenter__(self) -> BaseScraper:
        try:
            self._stack.push_async_callback(self.http.close)
            await self._stack.enter_async_context(self.store)
            return self
        except BaseException:
            await self._stack.aclose()
            raise

    async def __aexit__(self, *exc: Any) -> None:
        await self._stack.aclose()

    async def setup(self) -> None:
        pass

    async def list_documents(self, year: int | None) -> AsyncIterator[DocRef]:
        """List each type separately so one failing type doesn't stop the others."""
        for name, value in self.types.items():
            try:
                async for ref in self.list_type(year, name, value):
                    yield ref
            except Exception as exc:
                await self.error({"year": year, "type": name}, exc)

    async def list_type(
        self, year: int | None, name: str, value: Any
    ) -> AsyncIterator[DocRef]:
        raise NotImplementedError
        yield

    async def fetch(self, ref: DocRef) -> Document | None:
        """None is recorded as an error; skip unwanted refs while listing instead."""
        raise NotImplementedError

    async def _report_search_error(
        self,
        ref: DocRef,
        exc: Exception,
        on_error: Callable[[DocRef, Exception], Awaitable[None] | None] | None,
    ) -> None:
        """Route on-demand search errors to *on_error* or logger without touching ``self.store``."""
        if on_error is not None:
            outcome = on_error(ref, exc)
            if inspect.isawaitable(outcome):
                await outcome
            return
        logger.warning(f"{self.name}: search failed for {ref}: {exc}")

    async def search_refs(
        self,
        query: Query,
        *,
        strict_native: bool = False,
        on_error: Callable[[DocRef, Exception], Awaitable[None] | None] | None = None,
    ) -> AsyncIterator[DocRef]:
        """Stream refs matching *query* with early exit on ``query.limit``."""
        if strict_native and not self.capabilities.supports_query(query):
            raise UnsupportedQueryError(
                f"{self.name} does not natively support {query!r} "
                f"(capabilities={self.capabilities!r})"
            )
        if query.limit == 0:
            return
        years: list[int | None] = (
            [None] if self.yearless else list(query.year_range(self.years))
        )
        uses_list_type = (
            type(self).list_documents is BaseScraper.list_documents
            and isinstance(self.types, dict)
            and bool(self.types)
        )
        selected_types: list[tuple[str, Any]] | None = None
        if uses_list_type:
            if query.type is not None:
                matched = match_type_entry(self.types, query.type)
                if matched is None:
                    return
                selected_types = [matched]
            else:
                selected_types = list(self.types.items())

        yielded = 0
        for year in years:
            if selected_types is not None:
                for type_name, type_value in selected_types:
                    try:
                        async for raw_ref in self.list_type(
                            year, type_name, type_value
                        ):
                            ref = dict(raw_ref)
                            if year is not None:
                                ref.setdefault("year", year)
                            if isinstance(ref.get("title"), str):
                                ref["title"] = clean_title(ref["title"])
                            if not matches_query(ref, query):
                                continue
                            yield ref
                            yielded += 1
                            if query.limit is not None and yielded >= query.limit:
                                return
                    except Exception as exc:
                        await self._report_search_error(
                            {"year": year, "type": type_name}, exc, on_error
                        )
            else:
                try:
                    async for raw_ref in self.list_documents(year):
                        ref = dict(raw_ref)
                        if year is not None:
                            ref.setdefault("year", year)
                        if isinstance(ref.get("title"), str):
                            ref["title"] = clean_title(ref["title"])
                        if not matches_query(ref, query):
                            continue
                        yield ref
                        yielded += 1
                        if query.limit is not None and yielded >= query.limit:
                            return
                except Exception as exc:
                    await self._report_search_error({"year": year}, exc, on_error)

    async def error(self, ref: DocRef, exc: Exception) -> None:
        self._result.errors += 1
        message = str(exc)
        if exc.__cause__ is not None:
            message += f" (caused by {type(exc.__cause__).__name__}: {exc.__cause__})"
        await self.store.record_error(
            run_id=self._run_id,
            message=message,
            year=ref.get("year"),
            type=ref.get("type", ""),
            situation=ref.get("situation", ""),
            title=ref.get("title", ""),
            url=ref.get("document_url", ""),
        )
        logger.error(f"{self.name}: {ref.get('title', 'listing')}: {message}")

    async def run(self) -> RunResult:
        self._result = RunResult()
        started = time.perf_counter()
        self._run_id = await self.store.start_run(self.name)
        scope = f"{self.name}:{self._run_id}"
        token = usage_scope.set(scope)
        try:
            await self.setup()
            # Overwrite re-scrapes everything; each save replaces the old row.
            await self._scrape(set() if self.overwrite else await self.store.keys())
        except Exception as exc:
            await self.error({}, exc)
        finally:
            self._result.seconds = time.perf_counter() - started
            stats = {
                **self._result.stats(),
                "years": f"{self.years.start}-{self.years.stop - 1}",
                "workers": self.workers,
                "http": dict(self.http.stats),
            }
            try:
                await self.store.finish_run(
                    self._run_id,
                    self._result.docs,
                    self._result.errors,
                    self.ocr.usage_for(scope) if self.ocr else {},
                    stats,
                )
            finally:
                usage_scope.reset(token)
        return self._result

    async def _scrape(self, keys: set[tuple[str, str]]) -> None:
        queue: asyncio.Queue[DocRef | None] = asyncio.Queue(maxsize=self.workers * 2)
        pending: set[tuple[str, str]] = set()
        started = time.perf_counter()

        async def produce() -> None:
            # One queue for all years, so workers never wait at a year boundary.
            for year in [None] if self.yearless else self.years:
                try:
                    async for ref in self.list_documents(year):
                        ref = dict(ref)
                        ref.setdefault("year", year)
                        if ref["year"] is None:
                            await self.error(ref, ValueError("Listing row has no year"))
                            continue
                        if self.yearless and ref["year"] not in self.years:
                            continue
                        ref["title"] = clean_title(ref["title"])
                        key = (ref["document_url"], ref["title"])
                        if key in keys or key in pending:
                            self._result.skipped += 1
                            continue
                        pending.add(key)
                        self._result.listed += 1
                        await queue.put(ref)
                except Exception as exc:
                    await self.error({"year": year}, exc)
            self._result.listing_seconds = time.perf_counter() - started
            for _ in range(self.workers):
                await queue.put(None)

        async def consume() -> None:
            while (ref := (await queue.get())) is not None:
                key = (ref["document_url"], ref["title"])
                fetch_started = time.perf_counter()
                try:
                    doc = await self.fetch(dict(ref))
                    if doc is None:
                        raise ValueError("Fetch produced no document")
                    await self.store.save(doc, ref_url=key[0], ref_title=key[1])
                    self._result.docs += 1
                    self._result.fetch_seconds.append(
                        time.perf_counter() - fetch_started
                    )
                    keys.add(key)
                    del doc
                except Exception as exc:
                    await self.error(ref, exc)
                finally:
                    pending.discard(key)
                    queue.task_done()

        async with asyncio.TaskGroup() as group:
            group.create_task(produce())
            for _ in range(self.workers):
                group.create_task(consume())

    async def paginate(
        self,
        fetch_page: Callable[[int], Awaitable[list[DocRef]]],
        total_pages: int,
        start: int = 1,
    ) -> AsyncIterator[DocRef]:
        """Fetch pages in bounded batches; a failed page is recorded and skipped."""
        for first in range(start, total_pages + 1, self.workers):
            tasks = [
                asyncio.create_task(fetch_page(page))
                for page in range(first, min(first + self.workers, total_pages + 1))
            ]
            try:
                pages = await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            for page, refs in enumerate(pages, first):
                if isinstance(refs, Exception):
                    await self.error({"title": f"listing page {page}"}, refs)
                    continue
                if isinstance(refs, BaseException):
                    raise refs
                for ref in refs:
                    yield ref

    async def download(self, url: str) -> tuple[str, bytes, str]:
        response = await self.http.request(url)
        sniffed = sniff_extension(response.content)
        declared = detect_extension(response.content_type, response.filename)
        if (declared == ".pdf" or url.lower().endswith(".pdf")) and sniffed != ".pdf":
            raise ValueError(f"Expected PDF from {url}")
        ext = sniffed or declared
        markdown = await bytes_to_markdown(
            response.content,
            response.headers.get("Content-Type", response.content_type),
            url,
            self.http,
            self.ocr,
        )
        return (markdown, response.content, ext)

    async def markdown(self, *, html: str, base_url: str | None = None) -> str:
        async def fetch_image(url: str) -> bytes:
            return (await self.http.request(url)).content

        return await html_to_markdown(
            html, base_url or self.base_url, fetch_image, cache=self.image_cache
        )

    async def fetch_pdf(self, ref: DocRef) -> Document:
        url = urljoin(self.base_url, ref.get("_pdf_link") or ref["document_url"])
        markdown, raw, ext = await self.download(url)
        return Document(
            **{
                **ref,
                "document_url": url,
                "text_markdown": markdown,
                "raw": raw,
                "raw_ext": ext,
            }
        )

    async def fetch_html(
        self, ref: DocRef, html: str, url: str, raw: bytes, raw_ext: str = ".html"
    ) -> Document:
        """Build a Document from page HTML and raw HTTP body bytes."""
        return Document(
            **{
                **ref,
                "document_url": url,
                "text_markdown": await self.markdown(html=html, base_url=url),
                "raw": raw,
                "raw_ext": raw_ext,
            }
        )
