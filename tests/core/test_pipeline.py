from __future__ import annotations
import asyncio
from unittest.mock import AsyncMock

from test_store import document

from legisbr.core.http import HttpClient, HttpError
from legisbr.core.scraper import BaseScraper
from legisbr.core.store import Store


class Scraper(BaseScraper):
    name = "TEST"
    base_url = "https://example.org"
    active = 0
    peak = 0

    async def list_documents(self, year):
        if year == 2023:
            raise HttpError(self.base_url, 500, "listing failed")
        for i in range(30):
            yield {"year": year, "document_url": f"listing/{i}", "title": str(i)}

    async def fetch(self, ref):
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        return document(title=ref["title"], year=1989)


async def test_bounded_workers_year_isolation_and_global_resume():
    async with Store(":memory:") as store:
        scraper = Scraper(
            store=store, http=HttpClient(), years=range(2023, 2025), workers=3
        )
        result = await scraper.run()
        assert result.docs == 30 and result.errors == 1
        assert scraper.peak <= 3
        assert len(await store.keys(1989)) == 30
        scraper.fetch = AsyncMock(side_effect=AssertionError("already saved"))
        result = await scraper.run()
        assert result.docs == 0 and result.skipped == 30 and result.errors == 1
        scraper.fetch.assert_not_called()


async def test_cleanup_attempts_every_resource():
    http = HttpClient()
    http.close = AsyncMock(side_effect=RuntimeError("close failed"))
    store = Store(":memory:")
    scraper = Scraper(store=store, http=http, years=range(2024, 2025))
    import pytest

    with pytest.raises(RuntimeError, match="close failed"):
        async with scraper:
            pass
    assert store._connection is None


class TypedScraper(BaseScraper):
    name = "TYPED"
    base_url = "https://example.org"
    types = {"Lei": "lei", "Decreto": "decreto"}

    async def list_type(self, year, name, value):
        if name == "Lei":
            raise HttpError(self.base_url, 500, "type failed")
        yield {"year": year, "type": name, "document_url": value, "title": name}

    async def fetch(self, ref):
        return document(title=ref["title"], year=ref["year"])


async def test_failed_type_does_not_stop_later_types():
    async with Store(":memory:") as store:
        scraper = TypedScraper(store=store, http=HttpClient(), years=range(2024, 2025))
        result = await scraper.run()
        assert result.docs == 1 and result.errors == 1
        assert (
            store.connection.execute("SELECT type FROM errors").fetchone()[0] == "Lei"
        )


async def test_failed_page_is_recorded_and_later_pages_still_listed():
    async with Store(":memory:") as store:
        scraper = Scraper(
            store=store, http=HttpClient(), years=range(2024, 2025), workers=2
        )

        async def page(number):
            if number == 1:
                raise HttpError("page", 503, "failed")
            return [{"document_url": f"page/{number}", "title": str(number)}]

        refs = [ref async for ref in scraper.paginate(page, 3)]
        assert [ref["title"] for ref in refs] == ["2", "3"]
        assert (
            store.connection.execute("SELECT count(*) FROM errors").fetchone()[0] == 1
        )


async def test_paginate_sliding_window_does_not_block_on_slow_page_1():
    async with Store(":memory:") as store:
        scraper = Scraper(
            store=store, http=HttpClient(), years=range(2024, 2025), workers=2
        )
        release_page_1 = asyncio.Event()
        page_3_started = asyncio.Event()
        active = peak = 0

        async def fetch_page(number: int):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                if number == 1:
                    # Must not deadlock: page 3 (workers + 1) must start while page 1 is blocked!
                    await asyncio.wait_for(release_page_1.wait(), timeout=2.0)
                elif number == 3:
                    page_3_started.set()
                    release_page_1.set()
                else:
                    await asyncio.sleep(0.005)
                return [{"document_url": f"page/{number}", "title": str(number)}]
            finally:
                active -= 1

        refs = await asyncio.wait_for(
            _collect_async(scraper.paginate(fetch_page, 5)), 3.0
        )
        assert page_3_started.is_set()
        assert peak == 2
        assert [ref["title"] for ref in refs] == ["1", "2", "3", "4", "5"]

        # Empty page range yields nothing.
        assert [ref async for ref in scraper.paginate(fetch_page, 0)] == []


async def _collect_async(ait):
    return [item async for item in ait]


async def test_paginate_early_stop_cancels_in_flight_tasks_cleanly():
    async with Store(":memory:") as store:
        scraper = Scraper(
            store=store, http=HttpClient(), years=range(2024, 2025), workers=2
        )
        page_2_cancelled = asyncio.Event()
        page_2_started = asyncio.Event()

        async def fetch_page(number: int):
            if number == 1:
                await asyncio.wait_for(page_2_started.wait(), timeout=2.0)
                return [{"document_url": "page/1", "title": "1"}]
            if number == 2:
                page_2_started.set()
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    page_2_cancelled.set()
                    raise
            if number == 3:
                raise RuntimeError("page 3 failed before break")
            return [{"document_url": f"page/{number}", "title": str(number)}]

        before_tasks = set(asyncio.all_tasks())
        got = []
        gen = scraper.paginate(fetch_page, 6)
        try:
            async for ref in gen:
                got.append(ref["title"])
                break
        finally:
            await gen.aclose()

        assert got == ["1"]
        assert page_2_cancelled.is_set()
        assert not (set(asyncio.all_tasks()) - before_tasks)


async def test_overwrite_rescrapes_without_deleting_rows():
    async with Store(":memory:") as store:
        scraper = Scraper(store=store, http=HttpClient(), years=range(2024, 2025))

        async def fetch(ref):
            return document(title=ref["title"], year=2024)

        scraper.fetch = fetch
        await scraper.run()
        scraper.overwrite = True
        scraper.fetch = AsyncMock(side_effect=RuntimeError("site down"))
        result = await scraper.run()
        assert scraper.fetch.await_count == 30 and result.errors == 30
        assert len(await store.keys()) == 30


async def test_workers_move_to_next_year_while_one_document_is_slow():
    released = asyncio.Event()

    class SlowScraper(Scraper):
        async def list_documents(self, year):
            yield {"year": year, "document_url": f"doc/{year}", "title": str(year)}

        async def fetch(self, ref):
            if ref["year"] == 2023:
                await released.wait()
            released.set()
            return document(title=ref["title"], year=ref["year"])

    async with Store(":memory:") as store:
        scraper = SlowScraper(
            store=store, http=HttpClient(), years=range(2023, 2025), workers=2
        )
        result = await asyncio.wait_for(scraper.run(), 2)
        assert result.docs == 2


async def test_yearless_row_without_year_is_an_error():
    class YearlessScraper(Scraper):
        yearless = True

        async def list_documents(self, year):
            yield {"year": None, "document_url": "doc/none", "title": "no year"}
            yield {"year": 2024, "document_url": "doc/2024", "title": "2024"}

    async with Store(":memory:") as store:
        scraper = YearlessScraper(
            store=store, http=HttpClient(), years=range(2024, 2025)
        )
        result = await scraper.run()
        assert result.docs == 1 and result.errors == 1


async def test_listing_titles_are_whitespace_normalized_for_keys_and_documents():
    class SpacedScraper(Scraper):
        async def list_documents(self, year):
            yield {
                "year": year,
                "document_url": "doc/1",
                "title": "LEI N.º\n  15.215,\xa0\xa0 DE 05.09.12",
            }

        async def fetch(self, ref):
            return document(title=ref["title"], year=ref["year"])

    async with Store(":memory:") as store:
        scraper = SpacedScraper(store=store, http=HttpClient(), years=range(2024, 2025))
        assert (await scraper.run()).docs == 1
        assert await store.keys() == {("doc/1", "LEI N.º 15.215, DE 05.09.12")}


async def test_recorded_error_keeps_the_chained_cause():
    class CauseScraper(Scraper):
        async def list_documents(self, year):
            yield {"year": year, "document_url": "doc/1", "title": "1"}

        async def fetch(self, ref):
            try:
                raise TimeoutError("model timed out")
            except TimeoutError as exc:
                raise ValueError("PDF OCR failed") from exc

    async with Store(":memory:") as store:
        await CauseScraper(
            store=store, http=HttpClient(), years=range(2024, 2025)
        ).run()
        (message,) = store.connection.execute("SELECT message FROM errors").fetchone()
        assert message == "PDF OCR failed (caused by TimeoutError: model timed out)"


async def test_run_records_performance_stats():
    import json

    class Flaky(Scraper):
        async def fetch(self, ref):
            if ref["title"] == "0":
                raise ValueError("broken")
            return await super().fetch(ref)

    async with Store(":memory:") as store:
        scraper = Flaky(
            store=store, http=HttpClient(), years=range(2024, 2025), workers=3
        )
        result = await scraper.run()
        (raw,) = store.connection.execute("SELECT stats FROM runs").fetchone()
    stats = json.loads(raw)
    assert result.listed == 30 and stats["listed"] == 30
    assert (stats["docs"], stats["errors"], stats["workers"]) == (29, 1, 3)
    assert 0 <= stats["listing_seconds"] <= stats["seconds"]
    assert stats["fetch_p50"] <= stats["fetch_p95"] <= stats["fetch_max"]
    assert stats["http"] == {"requests": 0, "retries": 0, "bytes": 0}
