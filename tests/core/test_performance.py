"""Performance & resource-efficiency tests: LatencyHistogram, blake2b-16 dedup, lazy startup."""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest
from test_store import document

import legisbr
from legisbr.core.http import HttpClient
from legisbr.core.scraper import BaseScraper, LatencyHistogram, _resume_digest
from legisbr.core.store import Store


def test_latency_histogram_deterministic_o1_memory_and_accuracy():
    empty = LatencyHistogram()
    assert empty.count == 0
    assert empty.min_val is None
    assert empty.max_val is None
    assert empty.quantile(0.5) is None

    hist = LatencyHistogram()
    # 10,000 evenly spaced samples from 0.001 to 10.000 s.
    samples = [(i + 1) / 1000.0 for i in range(10_000)]
    for s in samples:
        hist.observe(s)

    assert hist.count == 10_000
    assert hist.total == pytest.approx(sum(samples), rel=1e-9)
    assert hist.min_val == pytest.approx(0.001)
    assert hist.max_val == pytest.approx(10.0)
    # O(1) bucket storage regardless of sample count.
    assert len(hist.counts) == len(LatencyHistogram.BOUNDS)
    assert sum(hist.counts) == 10_000

    p50 = hist.quantile(0.5)
    p95 = hist.quantile(0.95)
    p100 = hist.quantile(1.0)
    assert p50 is not None and p95 is not None and p100 is not None
    # True p50 is 5.0, true p95 is 9.5; bucket interpolation is tightly accurate.
    assert 4.5 <= p50 <= 5.5
    assert 9.0 <= p95 <= 10.0
    assert p100 == pytest.approx(10.0)
    assert hist.min_val <= p50 <= p95 <= p100

    # Deterministic repeatability + mergeability.
    other = LatencyHistogram()
    for s in samples:
        other.observe(s)
    assert other.quantile(0.5) == p50
    assert other.quantile(0.95) == p95

    merged = LatencyHistogram()
    merged.merge(hist)
    merged.merge(other)
    assert merged.count == 20_000
    assert merged.min_val == hist.min_val
    assert merged.max_val == hist.max_val
    assert merged.quantile(0.5) == pytest.approx(p50)


def test_resume_digest_is_16_bytes_and_normalizes_title_whitespace():
    d1 = _resume_digest("https://example.org/1", "LEI  Nº\n  123/\xa02024")
    d2 = _resume_digest("https://example.org/1", "LEI Nº 123/ 2024")
    d3 = _resume_digest("https://example.org/2", "LEI Nº 123/ 2024")
    assert isinstance(d1, bytes) and len(d1) == 16
    assert d1 == d2
    assert d1 != d3


async def test_base_scraper_blake2b_dedup_on_iter_keys_and_pending():
    fetched: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    class DupScraper(BaseScraper):
        name = "DUP"
        base_url = "https://example.org"

        async def list_documents(self, year):
            # Already saved in store (with extra whitespace variant) -> skipped via iter_keys()
            yield {
                "year": year,
                "document_url": "https://example.org/saved",
                "title": "Lei   1/2024",
            }
            # Emitted twice while first fetch is still in-flight -> deduplicated via pending set
            yield {
                "year": year,
                "document_url": "https://example.org/new",
                "title": "Lei 2/2024",
            }
            yield {
                "year": year,
                "document_url": "https://example.org/new",
                "title": "Lei\n  2/2024",
            }

        async def fetch(self, ref):
            fetched.append(ref["document_url"])
            started.set()
            await release.wait()
            return document(
                title=ref["title"],
                year=ref["year"],
                document_url=ref["document_url"],
            )

    async with Store(":memory:", save_raw=False) as store:
        await store.save(
            document(title="Lei 1/2024", year=2024),
            ref_url="https://example.org/saved",
            ref_title="Lei 1/2024",
        )
        scraper = DupScraper(
            store=store, http=HttpClient(), years=range(2024, 2025), workers=4
        )
        task = asyncio.create_task(scraper.run())
        await started.wait()
        await asyncio.sleep(0.01)
        release.set()
        result = await task

    assert fetched == ["https://example.org/new"]
    assert result.docs == 1
    assert result.skipped == 2
    assert result.errors == 0


def test_lazy_package_startup_and_all_exports():
    code = (
        "import sys, time, legisbr; "
        "t = time.perf_counter(); "
        "infos = legisbr.sources(); "
        "dt = (time.perf_counter() - t) * 1000; "
        "assert len(infos) == 30; "
        "forbidden = ('aiohttp', 'bs4', 'pydantic', 'pypdfium2', 'PIL', 'pypdf'); "
        "loaded = [m for m in forbidden if m in sys.modules]; "
        "scrapers = [m for m in sys.modules if m.startswith('legisbr.scrapers')]; "
        "assert not loaded, f'Heavy modules loaded: {loaded}'; "
        "assert not scrapers, f'Scraper modules loaded: {scrapers}'; "
        "print(f'{len(infos)} sources in {dt:.2f} ms')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "30 sources" in proc.stdout

    # Verify every advertised export in __all__ resolves lazily without error.
    for name in legisbr.__all__:
        assert getattr(legisbr, name) is not None
