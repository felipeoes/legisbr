"""Comprehensive tests for ergonomic search (NormRef/SearchResults/get) and first-class sync API."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from test_store import document

import legisbr
from legisbr.client import AsyncClient, Client
from legisbr.core.archive import ArchiveIndex, AsyncArchiveIndex
from legisbr.core.query import (
    NormNotFoundError,
    NormRef,
    Query,
    SearchCapabilities,
    SearchResults,
    normalize_year_filter,
    rank_best_ref,
)
from legisbr.core.scraper import BaseScraper, RunResult
from legisbr.core.store import SearchHit, Store
from legisbr.registry import ScraperSpec, Source


def _make_fake_spec(
    scraper_cls: type[BaseScraper],
    *,
    code: str = "fake",
    name: str = "FAKE",
    workers: int = 4,
    capabilities: SearchCapabilities = SearchCapabilities(
        native_year=True, native_type=True, native_number=True, native_text=True
    ),
) -> ScraperSpec:
    return ScraperSpec(
        name=name,
        cls_name=scraper_cls.__name__,
        cls_path=f"{scraper_cls.__module__}:{scraper_cls.__name__}",
        code=code,
        scope="state",
        aliases=(code,),
        start_year=2021,
        workers=workers,
        capabilities=capabilities,
        _cls=scraper_cls,
    )


def test_query_number_coercion_year_filter_and_rank_best_ref():
    assert Query(number=14133).number == "14133"
    assert Query(number="  14.133  ").number == "14.133"
    assert Query(number="   ").number is None
    with pytest.raises(TypeError):
        Query(number=True)

    assert normalize_year_filter(2024, None) == 2024
    assert normalize_year_filter(None, range(2020, 2025)) == range(2020, 2025)
    assert normalize_year_filter(None, None) is None
    with pytest.raises(ValueError, match="not both"):
        normalize_year_filter(2024, range(2020, 2025))
    with pytest.raises(TypeError):
        normalize_year_filter(True, None)  # type: ignore[arg-type]

    # Exact primary number in title beats a decree merely citing 14.133.
    decree_citing = NormRef.from_raw(
        Source.FEDERAL,
        {
            "year": 2021,
            "type": "Decreto",
            "title": "Decreto nº 10.818, de 27 de setembro de 2021 - Regulamenta a Lei nº 14.133/2021",
            "document_url": "https://www2.camara.leg.br/dec-10818",
        },
    )
    exact_law = NormRef.from_raw(
        Source.FEDERAL,
        {
            "year": 2021,
            "type": "Lei Ordinária",
            "title": "Lei nº 14.133, de 1º de abril de 2021",
            "document_url": "https://www2.camara.leg.br/lei-14133",
        },
    )
    assert rank_best_ref([decree_citing, exact_law], Query(number=14133)) is exact_law

    # Explicit `number` field exact match beats non-matching explicit `number`.
    explicit_other = NormRef.from_raw(
        Source.SP,
        {
            "year": 2021,
            "number": "10818",
            "title": "Decreto nº 10.818 sobre Lei 14.133",
            "document_url": "https://www.al.sp.gov.br/norma/1",
        },
    )
    explicit_exact = NormRef.from_raw(
        Source.SP,
        {
            "year": 2021,
            "number": "14133/2021",
            "title": "Lei nº 14.133/2021",
            "document_url": "https://www.al.sp.gov.br/norma/2",
        },
    )
    assert (
        rank_best_ref([explicit_other, explicit_exact], Query(number=14133))
        is explicit_exact
    )

    # Empty refs raises NormNotFoundError (subclass of LookupError).
    with pytest.raises(NormNotFoundError):
        rank_best_ref([], Query(number=14133))
    assert issubclass(NormNotFoundError, LookupError)


def test_norm_ref_and_search_results_ergonomics():
    raw = {
        "year": "2021",
        "type": "Lei Ordinária",
        "title": "Lei nº 14.133/2021",
        "summary": "Nova Lei de Licitações.",
        "situation": "Vigente",
        "numero": "14133",
        "document_url": "https://www2.camara.leg.br/lei-14133",
        "id": 999,
        "_internal": "hidden",
    }
    ref = NormRef.from_raw(Source.FEDERAL, raw)
    assert ref.source == Source.FEDERAL == "federal"
    assert ref.year == 2021
    assert ref.number == "14133"
    assert ref.url == "https://www2.camara.leg.br/lei-14133"
    assert ref.extra == {"id": 999}
    assert "_internal" not in ref.extra
    assert ref.raw_payload()["_internal"] == "hidden"

    as_dict = ref.to_dict()
    assert as_dict["source"] == "federal"
    assert as_dict["url"] == "https://www2.camara.leg.br/lei-14133"
    assert as_dict["number"] == "14133"
    assert as_dict["extra"] == {"id": 999}
    assert "_internal" not in as_dict

    q = Query(years=2021, number=14133)
    results = SearchResults(source=Source.FEDERAL, query=q, items=(ref,))
    assert len(results) == 1
    assert bool(results) is True
    assert results.first is ref
    assert results.one() is ref
    assert results[0] is ref
    assert list(results) == [ref]
    sliced = results[:1]
    assert isinstance(sliced, SearchResults) and list(sliced) == [ref]
    assert results.to_dicts() == [as_dict]

    empty = SearchResults(source=Source.FEDERAL, query=q, items=())
    assert len(empty) == 0
    assert bool(empty) is False
    assert empty.first is None
    assert empty.fetch() == []
    with pytest.raises(NormNotFoundError):
        empty.one()


def test_sync_get_search_bound_and_standalone_fetch_and_client_reuse(monkeypatch):
    setup_calls = 0
    fetch_order: list[str] = []

    class RankedPortalScraper(BaseScraper):
        name = "RANKED"
        base_url = "https://example.org"
        capabilities = SearchCapabilities(
            native_year=True, native_type=True, native_number=True, native_text=True
        )

        async def setup(self) -> None:
            nonlocal setup_calls
            setup_calls += 1

        async def list_documents(self, year):
            # Intentionally emit a citing decree BEFORE the exact Lei nº 14.133!
            yield {
                "year": year,
                "type": "Decreto",
                "title": "Decreto nº 10.818/2021 - Regulamenta Lei nº 14.133/2021",
                "summary": "Regulamenta itens da 14.133.",
                "situation": "Vigente",
                "document_url": "https://example.org/dec/10818",
                "_secret_token": "tok-dec",
            }
            yield {
                "year": year,
                "type": "Lei Ordinária",
                "title": "Lei nº 14.133/2021",
                "summary": "Lei de Licitações e Contratos.",
                "situation": "Vigente",
                "document_url": "https://example.org/lei/14133",
                "_secret_token": "tok-lei",
            }
            yield {
                "year": year,
                "type": "Lei Ordinária",
                "title": "Lei nº 14.134/2021",
                "summary": "Nova Lei do Gás.",
                "situation": "Vigente",
                "document_url": "https://example.org/lei/14134",
                "_secret_token": "tok-gas",
            }

        async def fetch(self, ref):
            # Vary latency to confirm fetch_many preserves input order despite completion order.
            delay = 0.03 if "10818" in ref["title"] else 0.005
            await asyncio.sleep(delay)
            fetch_order.append(ref["title"])
            assert ref.get("_secret_token") in {"tok-dec", "tok-lei", "tok-gas"}
            return document(
                year=ref["year"],
                type=ref["type"],
                title=ref["title"],
                summary=ref.get("summary", ""),
                document_url=ref["document_url"],
            )

    fake_spec = _make_fake_spec(RankedPortalScraper, code="ranked", name="RANKED")
    monkeypatch.setattr("legisbr.client.get_spec", lambda name: fake_spec)

    # 1. Top-level legisbr.get() ranks exact number match first (Lei 14.133 beats Decreto 10.818).
    law = legisbr.get("ranked", number=14133, year=2021)
    assert law.title == "Lei nº 14.133/2021"
    assert law.year == 2021

    # Missing match raises NormNotFoundError.
    with pytest.raises(NormNotFoundError):
        legisbr.get("ranked", number=999999, year=2021)

    # Mutually exclusive year= and years= raises ValueError.
    with pytest.raises(ValueError, match="not both"):
        legisbr.get("ranked", year=2021, years=range(2020, 2022))

    # 2. Standalone legisbr.search() closes its temp client, yet ref.fetch() and results.fetch()
    # transparently open a short-lived Client and preserve input order!
    results = legisbr.search("ranked", year=2021, limit=5)
    assert isinstance(results, SearchResults)
    assert len(results) == 3
    assert all(isinstance(r, NormRef) for r in results)

    single = results.one().fetch()
    assert single.title == results[0].title

    all_docs = results.fetch(concurrency=3)
    assert [d.title for d in all_docs] == [r.title for r in results]

    # 3. Inside `with Client() as client:`, ref.fetch() and results.fetch() reuse live client
    # and cached scraper setup (setup_calls increases by exactly 1!).
    setup_calls = 0
    with Client() as client:
        bound_results = client.search("ranked", year=2021, limit=5)
        assert setup_calls == 1
        d0 = bound_results[0].fetch()
        d_all = bound_results.fetch()
        assert setup_calls == 1
        assert d0.title == bound_results[0].title
        assert [d.title for d in d_all] == [r.title for r in bound_results]

        # list_refs streams typed NormRef bound to the live client too.
        listed = list(client.list_refs("ranked", year=2021))
        assert len(listed) == 3
        assert all(isinstance(r, NormRef) for r in listed)
        assert listed[1].fetch().title == "Lei nº 14.133/2021"
        assert setup_calls == 1

    # Even AFTER the Client closes, previously obtained NormRef/SearchResults still work!
    after_close_doc = bound_results[1].fetch()
    assert after_close_doc.title == "Lei nº 14.133/2021"
    assert len(bound_results.fetch()) == 3
    assert setup_calls == 3  # 1 inside `with`, +1 temp client for ref, +1 for results

    # Using a closed Client directly raises RuntimeError.
    with pytest.raises(RuntimeError, match="closed"):
        client.get("ranked", year=2021)


async def test_sync_client_works_inside_running_asyncio_loop_like_jupyter(monkeypatch):
    """Calling sync legisbr.get/search/scrape/Client inside an active loop must never raise RuntimeError."""

    class LoopTestScraper(BaseScraper):
        name = "LOOPTEST"
        base_url = "https://example.org"

        async def list_documents(self, year):
            for i in range(3):
                yield {
                    "year": year,
                    "type": "Lei",
                    "title": f"Lei nº {i + 1}/{year}",
                    "document_url": f"https://example.org/{year}/{i + 1}",
                }

        async def fetch(self, ref):
            return document(
                year=ref["year"],
                type=ref["type"],
                title=ref["title"],
                document_url=ref["document_url"],
            )

    fake_spec = _make_fake_spec(LoopTestScraper, code="looptest", name="LOOPTEST")
    monkeypatch.setattr("legisbr.client.get_spec", lambda name: fake_spec)

    # Confirm we truly are inside a running event loop!
    assert asyncio.get_running_loop().is_running()
    threads_before = {t.ident for t in threading.enumerate()}

    # Sync Client + top-level helpers run cleanly without nested-loop RuntimeError.
    law = legisbr.get("looptest", year=2024, number=2)
    assert law.title == "Lei nº 2/2024"

    res = legisbr.search("looptest", year=2024, limit=3)
    assert len(res) == 3
    docs_sync = res.fetch()
    assert [d.title for d in docs_sync] == [
        "Lei nº 1/2024",
        "Lei nº 2/2024",
        "Lei nº 3/2024",
    ]
    # Even `await res.afetch()` and `await res[0].afetch()` work from async code!
    docs_async = await res.afetch()
    assert [d.title for d in docs_async] == [d.title for d in docs_sync]
    assert (await res[0].afetch()).title == "Lei nº 1/2024"

    streamed = list(legisbr.scrape("looptest", year=2024, limit=2))
    assert len(streamed) == 2

    # All temporary sync threads exited cleanly (zero leaked legisbr-sync threads).
    leaked = [
        t
        for t in threading.enumerate()
        if t.ident not in threads_before and t.name.startswith("legisbr-sync")
    ]
    assert not leaked


def test_sync_client_lazy_thread_zero_threads_for_sources():
    threads_before = {t.ident for t in threading.enumerate()}
    with Client() as client:
        infos = client.sources()
        assert len(infos) == 30
        assert not client._loop_thread.is_running
    leaked = [
        t
        for t in threading.enumerate()
        if t.ident not in threads_before and t.name.startswith("legisbr-sync")
    ]
    assert not leaked


def test_document_stream_backpressure_early_break_and_client_run_store(
    tmp_path: Path, monkeypatch
):
    produced_count = 0
    cancelled = threading.Event()

    class BackpressureScraper(BaseScraper):
        name = "BP"
        base_url = "https://example.org"

        async def list_documents(self, year):
            for i in range(80):
                yield {
                    "year": year,
                    "type": "Lei",
                    "title": f"Lei nº {i}/{year}",
                    "document_url": f"https://example.org/{year}/{i}",
                }

        async def fetch(self, ref):
            nonlocal produced_count
            try:
                produced_count += 1
                await asyncio.sleep(0.002)
                return document(
                    year=ref["year"],
                    type=ref["type"],
                    title=ref["title"],
                    document_url=ref["document_url"],
                )
            except asyncio.CancelledError:
                cancelled.set()
                raise

    fake_spec = _make_fake_spec(BackpressureScraper, code="bp", name="BP", workers=1)
    monkeypatch.setattr("legisbr.client.get_spec", lambda name: fake_spec)

    threads_before = {t.ident for t in threading.enumerate()}

    # 1. Backpressure + early break with context manager cleans up owned temporary Client.
    with legisbr.scrape("bp", year=2024, workers=1) as stream:
        first = next(stream)
        assert first.year == 2024
        # Give producer a brief moment to fill the bounded buffers (DocumentStream maxsize=16
        # + BaseScraper output queue=2 + 1 in flight -> well below all 80 docs!).
        time.sleep(0.12)
        assert produced_count < 40

    leaked = [
        t
        for t in threading.enumerate()
        if t.ident not in threads_before and t.name.startswith("legisbr-sync")
    ]
    assert not leaked

    # 2. Natural exhaustion via plain `for doc in legisbr.scrape(...)` closes owned Client immediately.
    got = list(legisbr.scrape("bp", year=2024, limit=5))
    assert len(got) == 5
    leaked_after = [
        t
        for t in threading.enumerate()
        if t.ident not in threads_before and t.name.startswith("legisbr-sync")
    ]
    assert not leaked_after

    # 3. Client.run(..., store=Store.for_source(...)) persists cleanly without dummy loop!
    # Patch get_spec in legisbr.registry? Store.for_source uses real registry ("SP").
    store = Store.for_source("SP", tmp_path, save_raw=False, fts=True)
    assert Path(store.path) == tmp_path / "SAO_PAULO" / "documents.db"
    assert store.source == "SAO_PAULO"

    with Client(workers=2) as client:
        result = client.run("bp", year=2024, limit=4, store=store)
    assert isinstance(result, RunResult)
    assert result.docs == 4
    assert result.errors == 0
    # Unopened Store was opened and cleanly closed by client.run().
    assert store._connection is None

    # Local query via sync ArchiveIndex and top-level legisbr.query() with alias "SP"!
    hits = legisbr.query("administracao", data_dir=tmp_path, sources=["SP"], limit=10)
    assert len(hits) == 4
    assert all(isinstance(h, SearchHit) and h.source == "SAO_PAULO" for h in hits)

    with ArchiveIndex(tmp_path) as index:
        full_doc = index.get("SP", hits[0].id)
        assert full_doc is not None
        assert full_doc["source"] == "SAO_PAULO"


async def test_store_for_source_export_archive_and_sync_async_archive_index(
    tmp_path: Path,
):
    data_dir = tmp_path / "legislation"
    sp_store = Store.for_source("SP", data_dir, save_raw=False, fts=True)
    assert Path(sp_store.path) == data_dir / "SAO_PAULO" / "documents.db"
    assert sp_store.source == "SAO_PAULO"

    async with sp_store:
        await sp_store.save(
            document(
                year=2023,
                title="Lei Paulista nº 1/2023",
                type="Lei",
                document_url="https://www.al.sp.gov.br/norma/1",
                text_markdown="Art. 1º Política de saneamento básico e administração estadual.",
            )
        )
        await sp_store.save(
            document(
                year=2024,
                title="Decreto Paulista nº 2/2024",
                type="Decreto",
                document_url="https://www.al.sp.gov.br/norma/2",
                text_markdown="Art. 1º Programa de mobilidade metropolitana paulista.",
            )
        )

    ac_store = Store.for_source("AC", data_dir, save_raw=False, fts=True)
    async with ac_store:
        await ac_store.save(
            document(
                year=2024,
                title="Lei Acreana nº 5/2024",
                type="Lei",
                document_url="https://legis.ac.gov.br/norma/5",
                text_markdown="Art. 1º Conservação florestal e mobilidade fluvial.",
            )
        )

    # 1. Sync ArchiveIndex resolves Source enum and string aliases (Source.SP, "ac", "SaoPaulo").
    with ArchiveIndex(data_dir) as idx:
        sp_hits = idx.query("mobilidade", sources=[Source.SP])
        assert len(sp_hits) == 1 and sp_hits[0].source == "SAO_PAULO"

        ac_hits = idx.query("mobilidade", sources=[Source.AC])
        assert len(ac_hits) == 1 and ac_hits[0].source == "ACRE"

        got_sp = idx.get(Source.SP, sp_hits[0].id)
        assert got_sp is not None and got_sp["title"] == "Decreto Paulista nº 2/2024"

        got_alias = idx.get("SaoPaulo", sp_hits[0].id)
        assert got_alias is not None and got_alias["id"] == sp_hits[0].id

    # 2. AsyncArchiveIndex parity with Source enum and aclose() cleanup.
    async with AsyncArchiveIndex(data_dir) as aidx:
        ahits = await aidx.query("mobilidade", sources=[Source.SP])
        assert len(ahits) == 1 and ahits[0].source == "SAO_PAULO"
        adoc = await aidx.get(Source.SP, ahits[0].id)
        assert adoc is not None and adoc["title"] == "Decreto Paulista nº 2/2024"
        asources = await aidx.sources()
        assert {s["source"] for s in asources} == {"ACRE", "SAO_PAULO"}
        astats = await aidx.stats()
        assert len(astats) == 2
        assert len(aidx._index._conn_cache) == 2
    assert aidx._index._conn_cache == {}

    aidx2 = AsyncArchiveIndex(data_dir)
    assert len(await aidx2.sources()) == 2
    assert len(aidx2._index._conn_cache) == 2
    await aidx2.aclose()
    assert aidx2._index._conn_cache == {}

    # 3. Top-level sync legisbr.query() with Source enum and strings.
    top_hits = legisbr.query("mobilidade", data_dir=data_dir, year=2024)
    assert len(top_hits) == 2
    enum_hits = legisbr.query(
        "mobilidade", data_dir=data_dir, sources=[Source.SP, Source.AC], year=2024
    )
    assert len(enum_hits) == 2
    sp_only_hits = legisbr.query(
        "mobilidade", data_dir=data_dir, sources=[Source.SP], year=2024
    )
    assert len(sp_only_hits) == 1 and sp_only_hits[0].source == "SAO_PAULO"

    # Store.for_source accepts Source enum directly (including Source.FEDERAL).
    fed_store = Store.for_source(Source.FEDERAL, data_dir, save_raw=False)
    assert Path(fed_store.path) == data_dir / "LEGISLACAO_FEDERAL" / "documents.db"
    assert fed_store.source == "LEGISLACAO_FEDERAL"
    assert (
        Path(Store.for_source(Source.SP, data_dir, save_raw=False).path)
        == data_dir / "SAO_PAULO" / "documents.db"
    )

    # 4. Top-level sync legisbr.export() auto-infers format & single_file from suffix + year filter!
    pq_out = legisbr.export(
        data_dir / "SAO_PAULO" / "documents.db", tmp_path / "sp_all.parquet"
    )
    assert pq_out == tmp_path / "sp_all.parquet"
    assert pq.read_table(pq_out).num_rows == 2

    pq_2024 = legisbr.export(
        data_dir / "SAO_PAULO" / "documents.db",
        tmp_path / "sp_2024.parquet",
        year=2024,
    )
    table_2024 = pq.read_table(pq_2024)
    assert table_2024.num_rows == 1
    assert table_2024.column("year").to_pylist() == [2024]

    jsonl_out = legisbr.export(sp_store, tmp_path / "sp.jsonl")
    assert jsonl_out == tmp_path / "sp.jsonl"
    lines = [
        json.loads(line)
        for line in jsonl_out.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(lines) == 2

    json_out = legisbr.export(
        data_dir / "SAO_PAULO", tmp_path / "sp_2023.json", year=2023
    )
    assert json_out == tmp_path / "sp_2023.json"
    rows = json.loads(json_out.read_text(encoding="utf-8"))
    assert len(rows) == 1 and rows[0]["year"] == 2023

    # Directory export (no known extension -> single_file=False, per-year shards).
    dir_out = legisbr.export(
        data_dir / "SAO_PAULO" / "documents.db", tmp_path / "shards", fmt="jsonl"
    )
    assert dir_out == tmp_path / "shards"
    assert (dir_out / "2023" / "data.jsonl").is_file()
    assert (dir_out / "2024" / "data.jsonl").is_file()


async def test_async_client_run_with_preopened_and_unopened_store(
    tmp_path: Path, monkeypatch
):
    class SimpleScraper(BaseScraper):
        name = "SIMPLE"
        base_url = "https://example.org"

        async def list_documents(self, year):
            for i in range(3):
                yield {
                    "year": year,
                    "type": "Lei",
                    "title": f"Lei {i}/{year}",
                    "document_url": f"https://example.org/{year}/{i}",
                }

        async def fetch(self, ref):
            return document(
                year=ref["year"],
                type=ref["type"],
                title=ref["title"],
                document_url=ref["document_url"],
            )

    fake_spec = _make_fake_spec(SimpleScraper, code="simple", name="SIMPLE")
    monkeypatch.setattr("legisbr.client.get_spec", lambda name: fake_spec)

    # Unopened store -> AsyncClient.run opens and closes it.
    unopened = Store(tmp_path / "unopened.db", save_raw=False)
    async with AsyncClient() as client:
        res = await client.run("simple", year=2024, store=unopened)
    assert res.docs == 3
    assert unopened._connection is None

    # Pre-opened store -> AsyncClient.run leaves it open for caller.
    async with Store(tmp_path / "opened.db", save_raw=False) as opened:
        async with AsyncClient() as client:
            res2 = await client.run("simple", year=2024, limit=2, store=opened)
        assert res2.docs == 2
        assert opened._connection is not None
        assert len(await opened.keys()) == 2


def test_cold_startup_zero_heavy_imports_zero_threads_and_all_exports():
    code = (
        "import sys, threading, time, legisbr; "
        "t = time.perf_counter(); "
        "assert legisbr.Source.PI == 'pi'; "
        "assert legisbr.Source.FEDERAL == 'federal'; "
        "infos = legisbr.sources(); "
        "dt = (time.perf_counter() - t) * 1000; "
        "assert len(infos) == 30; "
        "assert threading.active_count() == 1, f'Threads spawned at import: {threading.enumerate()}'; "
        "forbidden = ('aiohttp', 'bs4', 'pydantic', 'pypdfium2', 'PIL', 'pypdf', 'pydantic_ai', 'pyarrow', 'mcp'); "
        "loaded = [m for m in forbidden if m in sys.modules]; "
        "assert not loaded, f'Heavy modules loaded: {loaded}'; "
        "print(f'OK {dt:.2f}ms')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "OK " in proc.stdout

    # Every public symbol in __all__ resolves cleanly.
    for name in legisbr.__all__:
        assert hasattr(legisbr, name), f"Missing export {name!r}"


async def test_source_enum_end_to_end_across_client_and_async_client(
    tmp_path: Path, monkeypatch
):
    class FederalFakeScraper(BaseScraper):
        name = "LEGISLACAO_FEDERAL"
        base_url = "https://www.camara.leg.br/legislacao/"
        capabilities = SearchCapabilities(
            native_year=True, native_type=True, native_number=True, native_text=True
        )

        async def list_documents(self, year):
            yield {
                "year": year,
                "type": "Lei Ordinária",
                "title": f"Lei nº 14.133/{year}",
                "document_url": f"https://www2.camara.leg.br/legin/fed/lei/{year}/lei-14133.html",
            }

        async def fetch(self, ref):
            return document(
                year=ref["year"],
                type=ref["type"],
                title=ref["title"],
                document_url=ref["document_url"],
            )

    fed_spec = ScraperSpec(
        name="LEGISLACAO_FEDERAL",
        cls_name="FederalFakeScraper",
        cls_path=f"{FederalFakeScraper.__module__}:FederalFakeScraper",
        code=Source.FEDERAL,
        scope="federal",
        aliases=("Camara",),
        start_year=1808,
        capabilities=FederalFakeScraper.capabilities,
        _cls=FederalFakeScraper,
    )
    monkeypatch.setattr(
        "legisbr.client.get_spec",
        lambda name: (
            fed_spec
            if str(name).strip().lower() in {"federal", "camara", "legislacao_federal"}
            else (_ for _ in ()).throw(ValueError(f"Unexpected source {name!r}"))
        ),
    )

    # AsyncClient accepts Source.FEDERAL and returns NormRef/SearchResults with code=Source.FEDERAL.
    async with AsyncClient() as aclient:
        ares = await aclient.search(Source.FEDERAL, year=2021, number=14133)
        assert ares.source is Source.FEDERAL
        assert ares.source == "federal"
        assert ares.one().source is Source.FEDERAL
        adoc = await aclient.get(Source.FEDERAL, year=2021, number=14133)
        assert adoc.title == "Lei nº 14.133/2021"
        alisted = [ref async for ref in aclient.list_refs(Source.FEDERAL, year=2021)]
        assert len(alisted) == 1 and alisted[0].source is Source.FEDERAL
        async with aclient.scrape(Source.FEDERAL, year=2021, limit=1) as stream:
            ascraped = [d async for d in stream]
        assert len(ascraped) == 1

    # Sync Client + top-level helpers accept Source.FEDERAL, "federal", and "camara" interchangeably.
    store = Store.for_source(Source.FEDERAL, tmp_path, save_raw=False, fts=True)
    with Client() as client:
        sres = client.search(Source.FEDERAL, year=2021, number=14133)
        assert sres.source is Source.FEDERAL
        assert sres.one().source is Source.FEDERAL
        assert (
            client.get(Source.FEDERAL, year=2021, number=14133).title
            == "Lei nº 14.133/2021"
        )
        assert (
            client.get("federal", year=2021, number=14133).title == "Lei nº 14.133/2021"
        )
        assert (
            client.get("camara", year=2021, number=14133).title == "Lei nº 14.133/2021"
        )
        run_res = client.run(Source.FEDERAL, year=2021, limit=1, store=store)
        assert run_res.docs == 1

    assert (
        legisbr.get(Source.FEDERAL, year=2021, number=14133).title
        == "Lei nº 14.133/2021"
    )
    assert len(list(legisbr.scrape(Source.FEDERAL, year=2021, limit=1))) == 1
    hits = legisbr.query("administracao", data_dir=tmp_path, sources=[Source.FEDERAL])
    assert len(hits) == 1 and hits[0].source == "LEGISLACAO_FEDERAL"


def test_document_stream_error_propagation_and_cleanup(monkeypatch):
    class FailingSetupScraper(BaseScraper):
        name = "FAIL_SETUP"
        base_url = "https://example.org"

        async def setup(self) -> None:
            raise RuntimeError("portal unreachable")

        async def list_documents(self, year):
            if False:
                yield {}

    fake_spec = _make_fake_spec(
        FailingSetupScraper, code="failsetup", name="FAIL_SETUP"
    )
    monkeypatch.setattr("legisbr.client.get_spec", lambda name: fake_spec)

    threads_before = {t.ident for t in threading.enumerate()}
    with pytest.raises(RuntimeError, match="portal unreachable"):
        list(legisbr.scrape("failsetup", year=2024))

    leaked = [
        t
        for t in threading.enumerate()
        if t.ident not in threads_before and t.name.startswith("legisbr-sync")
    ]
    assert not leaked


async def test_async_client_fetch_many_preserves_order_and_respects_concurrency(
    monkeypatch,
):
    in_flight = 0
    max_in_flight = 0

    class SlowOrderScraper(BaseScraper):
        name = "SLOW_ORDER"
        base_url = "https://example.org"

        async def list_documents(self, year):
            for i in range(6):
                yield {
                    "year": year,
                    "type": "Lei",
                    "title": f"Lei nº {i}/{year}",
                    "document_url": f"https://example.org/{year}/{i}",
                }

        async def fetch(self, ref):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                # Reverse latency so later items finish sooner if unconstrained.
                idx = int(ref["document_url"].rsplit("/", 1)[-1])
                await asyncio.sleep(0.01 * (6 - idx))
                return document(
                    year=ref["year"],
                    type=ref["type"],
                    title=ref["title"],
                    document_url=ref["document_url"],
                )
            finally:
                in_flight -= 1

    fake_spec = _make_fake_spec(
        SlowOrderScraper, code="sloworder", name="SLOW_ORDER", workers=8
    )
    monkeypatch.setattr("legisbr.client.get_spec", lambda name: fake_spec)

    async with AsyncClient() as client:
        results = await client.search("sloworder", year=2024, limit=6)
        docs = await client.fetch_many(results, concurrency=2)
        assert [d.title for d in docs] == [r.title for r in results]
        assert max_in_flight <= 2
        assert max_in_flight >= 1
