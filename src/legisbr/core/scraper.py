"""Bounded listing → fetch → save/stream pipeline shared by every scraper."""

from __future__ import annotations

import asyncio
import bisect
import hashlib
import inspect
import logging
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
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
from legisbr.core.pdf import PdfExecutor
from legisbr.core.query import (
    Query,
    SearchCapabilities,
    UnsupportedQueryError,
    match_type_entry,
    matches_query,
    normalize_search_text,
)
from legisbr.core.sink import DocumentSink, ErrorRecord, NullSink

logger = logging.getLogger(__name__)

DEFAULT_VALID_SITUATION = "Não consta revogação expressa"
DEFAULT_INVALID_SITUATION = "Revogada"


def _resume_digest(ref_url: str, ref_title: str) -> bytes:
    """Compact 16-byte blake2b resume key owned solely by ``BaseScraper``."""
    payload = ref_url.encode("utf-8") + b"\x00" + clean_title(ref_title).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=16).digest()


class LatencyHistogram:
    """Deterministic fixed log-bucket latency histogram (O(1) memory, zero RNG)."""

    BOUNDS: ClassVar[tuple[float, ...]] = (
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
        25.0,
        60.0,
        180.0,
        600.0,
        math.inf,
    )

    def __init__(self) -> None:
        self.counts: list[int] = [0] * len(self.BOUNDS)
        self.count: int = 0
        self.total: float = 0.0
        self.min_val: float | None = None
        self.max_val: float | None = None

    def observe(self, value: float) -> None:
        v = max(0.0, float(value))
        idx = bisect.bisect_left(self.BOUNDS, v)
        if idx >= len(self.counts):
            idx = len(self.counts) - 1
        self.counts[idx] += 1
        self.count += 1
        self.total += v
        if self.min_val is None or v < self.min_val:
            self.min_val = v
        if self.max_val is None or v > self.max_val:
            self.max_val = v

    def merge(self, other: LatencyHistogram) -> None:
        if other.count == 0:
            return
        for i, count in enumerate(other.counts):
            self.counts[i] += count
        self.count += other.count
        self.total += other.total
        if other.min_val is not None and (
            self.min_val is None or other.min_val < self.min_val
        ):
            self.min_val = other.min_val
        if other.max_val is not None and (
            self.max_val is None or other.max_val > self.max_val
        ):
            self.max_val = other.max_val

    def quantile(self, q: float) -> float | None:
        if self.count == 0:
            return None
        if q <= 0.0:
            return self.min_val
        if q >= 1.0:
            return self.max_val
        target = q * self.count
        cumulative = 0
        lower = 0.0
        for bound, bucket_count in zip(self.BOUNDS, self.counts, strict=True):
            if cumulative + bucket_count >= target and bucket_count > 0:
                upper = self.max_val if math.isinf(bound) else bound
                assert upper is not None
                low = max(lower, self.min_val if self.min_val is not None else 0.0)
                high = min(upper, self.max_val if self.max_val is not None else upper)
                if high <= low:
                    return low
                frac = (target - cumulative) / bucket_count
                return low + frac * (high - low)
            cumulative += bucket_count
            lower = bound
        return self.max_val


class _StreamDone:
    __slots__ = ()


_STREAM_DONE = _StreamDone()


class _StreamError:
    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


@dataclass
class RunResult:
    docs: int = 0
    errors: int = 0
    skipped: int = 0
    listed: int = 0
    seconds: float = 0.0
    listing_seconds: float = 0.0
    fetch_histogram: LatencyHistogram = field(
        default_factory=LatencyHistogram, repr=False
    )

    def stats(self) -> dict:
        """Performance summary stored with the run (see `legisbr stats`)."""
        p50 = self.fetch_histogram.quantile(0.5)
        p95 = self.fetch_histogram.quantile(0.95)
        pmax = self.fetch_histogram.max_val
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
            "fetch_p50": round(p50, 2) if p50 is not None else None,
            "fetch_p95": round(p95, 2) if p95 is not None else None,
            "fetch_max": round(pmax, 2) if pmax is not None else None,
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
        store: DocumentSink | None = None,
        http: HttpClient,
        years: range,
        ocr: OCR | None = None,
        workers: int = 16,
        overwrite: bool = False,
        image_cache: ImageCache | None = None,
        on_error: Callable[[ErrorRecord], Awaitable[None] | None] | None = None,
        pdf_executor: PdfExecutor = "auto",
        pdf_workers: int | None = None,
    ) -> None:
        if workers < 1:
            raise ValueError("workers must be positive")
        self.store: DocumentSink = (
            store if store is not None else NullSink(on_error=on_error)
        )
        self.http, self.ocr = (http, ocr)
        self.image_cache = (
            image_cache
            if image_cache is not None
            else getattr(http, "image_cache", None) or ImageCache()
        )
        self.years, self.workers, self.overwrite = (years, workers, overwrite)
        self.pdf_executor: PdfExecutor = pdf_executor
        self.pdf_workers = pdf_workers
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
        record = ErrorRecord(
            message=message,
            run_id=self._run_id,
            year=ref.get("year"),
            type=str(ref.get("type", "") or ""),
            situation=str(ref.get("situation", "") or ""),
            title=str(ref.get("title", "") or ""),
            url=str(ref.get("document_url", "") or ""),
        )
        await self.store.record_error(record)
        logger.error(f"{self.name}: {ref.get('title', 'listing')}: {message}")

    async def _load_existing_keys(self) -> set[bytes]:
        if self.overwrite:
            return set()
        keys: set[bytes] = set()
        async for ref_url, ref_title in self.store.iter_keys():
            keys.add(_resume_digest(ref_url, ref_title))
        return keys

    def _run_stats(self) -> dict[str, Any]:
        return {
            **self._result.stats(),
            "years": f"{self.years.start}-{self.years.stop - 1}",
            "workers": self.workers,
            "http": dict(self.http.stats),
        }

    async def run(self) -> RunResult:
        self._result = RunResult()
        started = time.perf_counter()
        self._run_id = await self.store.start_run(self.name)
        scope = f"{self.name}:{self._run_id}"
        token = usage_scope.set(scope)
        try:
            await self.setup()
            # Overwrite re-scrapes everything; each save replaces the old row.
            await self._scrape(await self._load_existing_keys())
        except Exception as exc:
            await self.error({}, exc)
        finally:
            self._result.seconds = time.perf_counter() - started
            try:
                await self.store.finish_run(
                    self._run_id,
                    self._result.docs,
                    self._result.errors,
                    self.ocr.usage_for(scope) if self.ocr else {},
                    self._run_stats(),
                )
            finally:
                usage_scope.reset(token)
        return self._result

    @asynccontextmanager
    async def stream(
        self,
        *,
        types: Sequence[str] | None = None,
        limit: int | None = None,
        drop_raw: bool = False,
        run_setup: bool = True,
    ) -> AsyncIterator[AsyncIterator[Document]]:
        """PEP 789-safe bounded async streaming pipeline with deterministic cleanup."""
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative or None")
        self._result = RunResult()
        started = time.perf_counter()
        self._run_id = await self.store.start_run(self.name)
        scope = f"{self.name}:{self._run_id}"
        token = usage_scope.set(scope)
        output: asyncio.Queue[
            tuple[Document, str, str, float] | _StreamDone | _StreamError
        ] = asyncio.Queue(maxsize=max(1, self.workers * 2))
        keys: set[bytes] = set()

        async def _background() -> None:
            try:
                if limit == 0:
                    await output.put(_STREAM_DONE)
                    return
                if run_setup:
                    await self.setup()
                if not self.overwrite:
                    async for ref_url, ref_title in self.store.iter_keys():
                        keys.add(_resume_digest(ref_url, ref_title))
                await self._scrape(keys, types=types, output=output)
                await output.put(_STREAM_DONE)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                await output.put(_StreamError(exc))

        bg_task = asyncio.create_task(_background())
        save_to_store = not isinstance(self.store, NullSink)

        async def _iter_docs() -> AsyncIterator[Document]:
            yielded = 0
            while True:
                item = await output.get()
                if isinstance(item, _StreamDone):
                    return
                if isinstance(item, _StreamError):
                    raise item.exc
                doc, ref_url, ref_title, elapsed = item
                if save_to_store:
                    await self.store.save(doc, ref_url=ref_url, ref_title=ref_title)
                elif drop_raw:
                    doc.raw = None
                self._result.docs += 1
                self._result.fetch_histogram.observe(elapsed)
                yield doc
                yielded += 1
                if limit is not None and yielded >= limit:
                    return

        try:
            yield _iter_docs()
        finally:
            if not bg_task.done():
                bg_task.cancel()
            try:
                await bg_task
            except asyncio.CancelledError:
                pass
            self._result.seconds = time.perf_counter() - started
            try:
                await self.store.finish_run(
                    self._run_id,
                    self._result.docs,
                    self._result.errors,
                    self.ocr.usage_for(scope) if self.ocr else {},
                    self._run_stats(),
                )
            finally:
                usage_scope.reset(token)

    async def _iter_year_refs(
        self, year: int | None, types: Sequence[str] | None
    ) -> AsyncIterator[DocRef]:
        if types is None:
            async for ref in self.list_documents(year):
                yield ref
            return
        wanted = {normalize_search_text(t) for t in types}
        uses_list_type = (
            type(self).list_documents is BaseScraper.list_documents
            and isinstance(self.types, dict)
            and bool(self.types)
        )
        if uses_list_type:
            for name, value in self.types.items():
                if normalize_search_text(name) not in wanted:
                    continue
                try:
                    async for ref in self.list_type(year, name, value):
                        yield ref
                except Exception as exc:
                    await self.error({"year": year, "type": name}, exc)
            return
        async for ref in self.list_documents(year):
            if normalize_search_text(str(ref.get("type") or "")) in wanted:
                yield ref

    async def _scrape(
        self,
        keys: set[bytes],
        *,
        types: Sequence[str] | None = None,
        output: asyncio.Queue[
            tuple[Document, str, str, float] | _StreamDone | _StreamError
        ]
        | None = None,
    ) -> None:
        queue: asyncio.Queue[DocRef | None] = asyncio.Queue(maxsize=self.workers * 2)
        pending: set[bytes] = set()
        started = time.perf_counter()

        async def produce() -> None:
            # One queue for all years, so workers never wait at a year boundary.
            for year in [None] if self.yearless else self.years:
                try:
                    async for ref in self._iter_year_refs(year, types):
                        ref = dict(ref)
                        ref.setdefault("year", year)
                        if ref["year"] is None:
                            await self.error(ref, ValueError("Listing row has no year"))
                            continue
                        if self.yearless and ref["year"] not in self.years:
                            continue
                        ref["title"] = clean_title(ref["title"])
                        digest = _resume_digest(ref["document_url"], ref["title"])
                        if digest in keys or digest in pending:
                            self._result.skipped += 1
                            continue
                        pending.add(digest)
                        self._result.listed += 1
                        await queue.put(ref)
                except Exception as exc:
                    await self.error({"year": year}, exc)
            self._result.listing_seconds = time.perf_counter() - started
            for _ in range(self.workers):
                await queue.put(None)

        async def consume() -> None:
            while (ref := (await queue.get())) is not None:
                ref_url, ref_title = ref["document_url"], ref["title"]
                digest = _resume_digest(ref_url, ref_title)
                fetch_started = time.perf_counter()
                try:
                    doc = await self.fetch(dict(ref))
                    if doc is None:
                        raise ValueError("Fetch produced no document")
                    elapsed = time.perf_counter() - fetch_started
                    if output is not None:
                        await output.put((doc, ref_url, ref_title, elapsed))
                        keys.add(digest)
                    else:
                        await self.store.save(doc, ref_url=ref_url, ref_title=ref_title)
                        self._result.docs += 1
                        self._result.fetch_histogram.observe(elapsed)
                        keys.add(digest)
                        del doc
                except Exception as exc:
                    await self.error(ref, exc)
                finally:
                    pending.discard(digest)
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
            pdf_executor=self.pdf_executor,
            pdf_workers=self.pdf_workers,
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
