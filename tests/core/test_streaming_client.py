"""Tests for PEP 789-safe BaseScraper.stream() and the public legisbr.Client facade."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_store import document

import legisbr
from legisbr.client import Client
from legisbr.core.http import HttpClient, Response
from legisbr.core.query import SearchCapabilities, UnsupportedQueryError
from legisbr.core.scraper import BaseScraper
from legisbr.core.sink import ErrorRecord, NullSink
from legisbr.core.store import Store
from legisbr.registry import ScraperSpec, get_spec


def _make_fake_spec(
    scraper_cls: type[BaseScraper],
    *,
    code: str = "fake",
    name: str = "FAKE",
    rps: float = 10,
    workers: int = 8,
    insecure_tls: bool = False,
    needs_ocr: bool = False,
    capabilities: SearchCapabilities = SearchCapabilities(native_year=True),
) -> ScraperSpec:
    return ScraperSpec(
        name=name,
        cls_name=scraper_cls.__name__,
        cls_path=f"{scraper_cls.__module__}:{scraper_cls.__name__}",
        code=code,
        scope="state",
        aliases=(code,),
        start_year=2024,
        rps=rps,
        workers=workers,
        insecure_tls=insecure_tls,
        needs_ocr=needs_ocr,
        capabilities=capabilities,
        _cls=scraper_cls,
    )


class _StreamingDummyScraper(BaseScraper):
    name = "STREAM_DUMMY"
    base_url = "https://example.org"
    types = {"Lei": "lei", "Decreto": "dec"}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.setup_calls = 0
        self.fetch_calls = 0

    async def setup(self) -> None:
        self.setup_calls += 1

    async def list_type(self, year, name, value):
        for i in range(10):
            yield {
                "year": year,
                "type": name,
                "title": f"{name} nº {i}/{year}",
                "summary": f"Ementa {i}",
                "document_url": f"https://example.org/{value}/{year}/{i}",
            }

    async def fetch(self, ref):
        self.fetch_calls += 1
        await asyncio.sleep(0)
        return document(
            title=ref["title"],
            year=ref["year"],
            type=ref["type"],
            document_url=ref["document_url"],
            raw=b"<html>raw body</html>",
            raw_ext=".html",
        )


async def test_stream_zero_disk_and_early_break_cancels_background_tasks():
    started_fetches = 0
    cancelled_fetches = 0

    class SlowInfiniteScraper(BaseScraper):
        name = "SLOW"
        base_url = "https://example.org"

        async def list_documents(self, year):
            for i in range(100):
                yield {
                    "year": year,
                    "type": "Lei",
                    "title": f"Lei {i}",
                    "document_url": f"https://example.org/{i}",
                }
                await asyncio.sleep(0)

        async def fetch(self, ref):
            nonlocal started_fetches, cancelled_fetches
            started_fetches += 1
            try:
                await asyncio.sleep(0.01)
                return document(title=ref["title"], year=ref["year"])
            except asyncio.CancelledError:
                cancelled_fetches += 1
                raise

    before_tasks = set(asyncio.all_tasks())
    scraper = SlowInfiniteScraper(http=HttpClient(), years=range(2024, 2025), workers=4)
    assert isinstance(scraper.store, NullSink)

    seen = []
    async with scraper.stream() as docs:
        async for doc in docs:
            seen.append(doc.title)
            if len(seen) == 3:
                break

    after_tasks = set(asyncio.all_tasks()) - before_tasks
    assert len(seen) == 3
    assert not after_tasks, f"Leaked background tasks: {after_tasks}"
    assert started_fetches >= 3


async def test_stream_propagates_caller_cancellation_and_pipeline_exceptions():
    class BlockingScraper(BaseScraper):
        name = "BLOCK"
        base_url = "https://example.org"

        async def list_documents(self, year):
            await asyncio.sleep(10)
            if False:
                yield {}

        async def fetch(self, ref):
            return document()

    before_tasks = set(asyncio.all_tasks())

    async def run_stream():
        scraper = BlockingScraper(http=HttpClient(), years=range(2024, 2025), workers=2)
        async with scraper.stream() as docs:
            async for _ in docs:
                pass

    task = asyncio.create_task(run_stream())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (set(asyncio.all_tasks()) - before_tasks)

    # Fatal setup() exception propagates cleanly out of stream()
    class BrokenSetupScraper(BaseScraper):
        name = "BROKEN_SETUP"
        base_url = "https://example.org"

        async def setup(self) -> None:
            raise RuntimeError("setup exploded")

        async def list_documents(self, year):
            if False:
                yield {}

    scraper = BrokenSetupScraper(http=HttpClient(), years=range(2024, 2025), workers=2)
    with pytest.raises(RuntimeError, match="setup exploded"):
        async with scraper.stream() as docs:
            async for _ in docs:
                pass


async def test_stream_limit_and_store_persistence(tmp_path: Path):
    db = tmp_path / "test" / "documents.db"
    async with Store(db, save_raw=False) as store:
        scraper = _StreamingDummyScraper(
            store=store, http=HttpClient(), years=range(2024, 2025), workers=3
        )
        got = []
        async with scraper.stream(types=["Lei"], limit=4) as docs:
            async for doc in docs:
                got.append(doc)
        assert len(got) == 4
        assert all(doc.type == "Lei" for doc in got)
        assert len(await store.keys()) == 4

        # Zero limit yields nothing immediately.
        async with scraper.stream(limit=0) as docs:
            assert [doc async for doc in docs] == []


async def test_client_scrape_null_sink_on_error_and_low_memory(
    tmp_path: Path, monkeypatch
):
    class FlakyScraper(_StreamingDummyScraper):
        async def fetch(self, ref):
            if ref["title"].endswith("0/2024"):
                raise ValueError("broken norm")
            return await super().fetch(ref)

    fake_spec = _make_fake_spec(FlakyScraper, code="flaky", name="FLAKY", workers=16)
    monkeypatch.setattr("legisbr.client.get_spec", lambda name: fake_spec)

    errors: list[ErrorRecord] = []

    async def on_error(err: ErrorRecord) -> None:
        errors.append(err)

    missing_log = tmp_path / "no_logs_created"
    monkeypatch.setenv("LOG_DIR", str(missing_log))
    before_env = dict(os.environ)

    async with Client(low_memory=True, on_error=on_error) as client:
        assert client.workers == 4
        assert client.pdf_executor == "thread"
        assert client.pdf_workers == 1
        assert client.image_cache_mb == 2
        assert client._ocr_concurrency == 2
        assert client._ocr_dpi == 120

        # Zero-disk streaming with low_memory=True drops doc.raw
        docs_out = []
        async with client.scrape("flaky", years=2024, types=["Lei"], limit=3) as docs:
            async for doc in docs:
                docs_out.append(doc)
        assert len(docs_out) == 3
        assert all(doc.raw is None for doc in docs_out)
        assert len(errors) == 1
        assert isinstance(errors[0], ErrorRecord)
        assert "broken norm" in errors[0].message

        # With Store(save_raw=True), Store.save_raw is the single source of truth (raw preserved).
        db = tmp_path / "saved" / "documents.db"
        store = Store(db, save_raw=True)
        async with client.scrape(
            "flaky", years=2024, types=["Lei"], limit=2, store=store
        ) as docs:
            saved_docs = [doc async for doc in docs]
        assert len(saved_docs) == 2
        assert all(doc.raw is not None for doc in saved_docs)
        # Store was unopened when passed -> Client closed it after scrape().
        assert store._connection is None

    assert not missing_log.exists()
    assert dict(os.environ) == before_env


async def test_client_per_source_http_isolation_rps_clamping_and_tls():
    async with Client(rps=50) as client:
        pr = get_spec("pr")  # rps=0.3, insecure_tls=False
        ba = get_spec("ba")  # rps=1, insecure_tls=True
        ac = get_spec("ac")  # rps=10, insecure_tls=True
        sp = get_spec("sp")  # rps=10, insecure_tls=False

        http_pr = client._get_http_client(pr)
        http_ba = client._get_http_client(ba)
        http_ac = client._get_http_client(ac)
        http_sp = client._get_http_client(sp)

        # Distinct HttpClient per source.
        assert len({id(http_pr), id(http_ba), id(http_ac), id(http_sp)}) == 4
        # rps=50 is clamped to spec.rps when force_rps=False.
        assert http_pr.limiter.requests_per_second == pytest.approx(0.3)
        assert http_ba.limiter.requests_per_second == pytest.approx(1.0)
        assert http_sp.limiter.requests_per_second == pytest.approx(10.0)
        # Per-spec insecure_tls wired cleanly.
        assert http_ac.verify_ssl is False
        assert http_ba.verify_ssl is False
        assert http_sp.verify_ssl is True
        assert http_pr.verify_ssl is True

    # force_rps=True and explicit verify_ssl=True override defaults.
    async with Client(rps=50, force_rps=True, verify_ssl=True) as client:
        http_ba = client._get_http_client(get_spec("ba"))
        assert http_ba.limiter.requests_per_second == pytest.approx(50.0)
        assert http_ba.verify_ssl is True


async def test_client_caches_setup_across_fetch_ref_list_refs_and_search_refs(
    monkeypatch,
):
    setup_counter = 0

    class StatefulScraper(_StreamingDummyScraper):
        capabilities = SearchCapabilities(
            native_year=True, native_type=True, native_number=True
        )

        async def setup(self) -> None:
            nonlocal setup_counter
            setup_counter += 1
            await super().setup()

    fake_spec = _make_fake_spec(
        StatefulScraper,
        code="stateful",
        name="STATEFUL",
        capabilities=StatefulScraper.capabilities,
    )
    monkeypatch.setattr("legisbr.client.get_spec", lambda name: fake_spec)

    async with Client() as client:
        refs = [
            ref async for ref in client.list_refs("stateful", years=2024, types=["Lei"])
        ]
        assert len(refs) == 10
        assert setup_counter == 1

        # Repeated fetch_ref calls reuse the cached set-up scraper (setup NOT re-run!).
        d1 = await client.fetch_ref("stateful", refs[0])
        d2 = await client.fetch_ref("stateful", refs[1])
        d3 = await client.fetch_ref("stateful", refs[2])
        assert [d1.title, d2.title, d3.title] == [
            "Lei nº 0/2024",
            "Lei nº 1/2024",
            "Lei nº 2/2024",
        ]
        assert setup_counter == 1

        # search_refs delegates cleanly with limit
        found = [
            ref
            async for ref in client.search_refs(
                "stateful", years=2024, type="Lei", number="5", limit=2
            )
        ]
        assert len(found) == 1 and found[0]["title"] == "Lei nº 5/2024"


async def test_client_search_refs_strict_native_rejects_ce_without_setup(monkeypatch):
    setup_spy = AsyncMock()
    monkeypatch.setattr("legisbr.scrapers.states.ce.CearaAleceScraper.setup", setup_spy)
    async with Client() as client:
        with pytest.raises(UnsupportedQueryError):
            [
                ref
                async for ref in client.search_refs(
                    "ce", years=2024, strict_native=True
                )
            ]
    setup_spy.assert_not_awaited()


async def test_top_level_convenience_scrape_and_search_refs(monkeypatch):
    fake_spec = _make_fake_spec(_StreamingDummyScraper, code="conv", name="CONV")
    monkeypatch.setattr("legisbr.client.get_spec", lambda name: fake_spec)

    async with legisbr.scrape("conv", years=2024, types=["Decreto"], limit=2) as docs:
        got = [doc async for doc in docs]
    assert len(got) == 2 and all(d.type == "Decreto" for d in got)

    refs = [
        ref
        async for ref in legisbr.search_refs("conv", years=2024, type="Lei", limit=3)
    ]
    assert len(refs) == 3
    assert all(r["type"] == "Lei" for r in refs)


async def test_client_closes_per_source_http_clients():
    client = Client()
    async with client:
        http_sp = client._get_http_client(get_spec("sp"))
        http_sp.close = AsyncMock()
    http_sp.close.assert_awaited_once()
    assert not client._http_clients
    assert len(client.sources()) == 30


async def test_scraper_forwards_pdf_executor_and_workers_to_bytes_to_markdown(
    monkeypatch,
):
    captured: dict = {}

    async def fake_bytes_to_markdown(
        data,
        content_type,
        url,
        http,
        ocr=None,
        *,
        pdf_executor="auto",
        pdf_workers=None,
    ):
        captured["pdf_executor"] = pdf_executor
        captured["pdf_workers"] = pdf_workers
        return "# Lei\n\n" + "Art. 1º Texto da norma estadual. " * 4

    monkeypatch.setattr(
        "legisbr.core.scraper.bytes_to_markdown", fake_bytes_to_markdown
    )
    http = HttpClient()
    http.request = AsyncMock(
        return_value=Response(
            "https://example.org/a.pdf",
            200,
            {"Content-Type": "application/pdf"},
            b"%PDF-1.4\n...",
        )
    )
    scraper = _StreamingDummyScraper(
        http=http,
        years=range(2024, 2025),
        pdf_executor="thread",
        pdf_workers=1,
    )
    await scraper.download("https://example.org/a.pdf")
    assert captured == {"pdf_executor": "thread", "pdf_workers": 1}


async def test_client_defers_ocr_construction_until_fetch_or_scrape(monkeypatch):
    created_ocr: list[dict] = []

    class FakeAgent:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    class FakeOCR:
        def __init__(self, models, **kwargs):
            created_ocr.append({"models": models, **kwargs})
            self.agent = FakeAgent()

        def usage_for(self, scope):
            return {}

    fake_spec = _make_fake_spec(
        _StreamingDummyScraper,
        code="ocrsrc",
        name="OCRSRC",
        needs_ocr=True,
        capabilities=SearchCapabilities(native_year=True, native_type=True),
    )
    monkeypatch.setattr("legisbr.client.get_spec", lambda name: fake_spec)
    monkeypatch.setattr("legisbr.client.OCR", FakeOCR)

    async with Client(ocr="anthropic:claude-haiku-5-5") as client:
        # Metadata-only list_refs() and search_refs() must NEVER construct OCR.
        refs = [
            ref async for ref in client.list_refs("ocrsrc", years=2024, types=["Lei"])
        ]
        assert len(refs) == 10
        assert created_ocr == []
        assert client._scrapers["ocrsrc"].ocr is None

        found = [
            ref
            async for ref in client.search_refs(
                "ocrsrc", years=2024, type="Lei", limit=2
            )
        ]
        assert len(found) == 2
        assert created_ocr == []
        assert client._scrapers["ocrsrc"].ocr is None

        # Only fetch_ref() triggers OCR construction and attaches it to cached scraper.
        doc1 = await client.fetch_ref("ocrsrc", found[0])
        assert doc1.title == found[0]["title"]
        assert len(created_ocr) == 1
        assert created_ocr[0]["models"] == "anthropic:claude-haiku-5-5"
        assert isinstance(client._scrapers["ocrsrc"].ocr, FakeOCR)

        # Subsequent fetch_ref() reuses the same OCR instance.
        await client.fetch_ref("ocrsrc", found[1])
        assert len(created_ocr) == 1

    # Fresh Client calling scrape() constructs OCR once.
    created_ocr.clear()
    async with Client(ocr="anthropic:claude-haiku-5-5") as client:
        async with client.scrape("ocrsrc", years=2024, types=["Lei"], limit=1) as docs:
            got = [doc async for doc in docs]
        assert len(got) == 1
        assert len(created_ocr) == 1
