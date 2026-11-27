"""Tests for SQLite schema initialization, UPSERT/VACUUM stability, FTS5, Parquet, and ArchiveIndex."""

from __future__ import annotations

import builtins
import json
import os
import sqlite3
import sys
import tracemalloc

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from legisbr.core.archive import ArchiveIndex
from legisbr.core.document import Document
from legisbr.core.store import SearchHit, Store, strip_data_uri_images


def make_doc(**kwargs) -> Document:
    defaults = {
        "year": 2024,
        "title": "Lei Ordinária nº 1/2024",
        "type": "Lei",
        "situation": "Vigente",
        "summary": "Dispõe sobre normas administrativas.",
        "document_url": "https://example.org/lei/1",
        "text_markdown": (
            "Art. 1º Esta lei estabelece as disposições gerais sobre a "
            "Constituição estadual e a administração pública."
        ),
    }
    defaults.update(kwargs)
    return Document(**defaults)


async def test_fresh_store_initializes_schema_meta_and_indexes(tmp_path):
    db_path = tmp_path / "documents.db"
    async with Store(db_path, source="ACRE") as store:
        meta = dict(store.connection.execute("SELECT key, value FROM meta").fetchall())
        assert meta["schema_version"] == "2"
        assert meta["fts_enabled"] == "0"

        cols = {
            row[1] for row in store.connection.execute("PRAGMA table_info(documents)")
        }
        assert {"id", "source", "ref_url", "ref_title", "scraped_at"} <= cols
        indexes = {
            row[1] for row in store.connection.execute("PRAGMA index_list(documents)")
        }
        assert "documents_year" in indexes
        assert "documents_type" in indexes

        await store.save(
            make_doc(title="Lei nº 10/2020", year=2020),
            ref_url="https://example.org/list/1",
            ref_title="Lei 10",
        )
        doc1 = await store.get_document(1)
        assert doc1 is not None and doc1["source"] == "ACRE"

    # Re-opening must be idempotent and keep the assigned integer ID.
    async with Store(db_path) as store:
        assert await store.keys() == {("https://example.org/list/1", "Lei 10")}
        reopened = await store.get_document(1)
        assert reopened is not None and reopened["title"] == "Lei nº 10/2020"


async def test_upsert_keeps_stable_id_across_updates_vacuum_and_fts_has_zero_stale_hits(
    tmp_path,
):
    db_path = tmp_path / "documents.db"
    async with Store(db_path, fts=True, source="AC") as store:
        await store.save(
            make_doc(
                title="Lei nº 100/2024",
                text_markdown=(
                    "Art. 1º Regulamenta a piscicultura amazônica com regras "
                    "claras para cooperativas locais."
                ),
            ),
            ref_url="https://example.org/list/100",
            ref_title="Lei 100",
        )
        await store.save(
            make_doc(
                title="Lei nº 200/2024",
                document_url="https://example.org/lei/200",
                text_markdown=(
                    "Art. 1º Institui a política estadual de mobilidade urbana "
                    "e transporte coletivo."
                ),
            ),
            ref_url="https://example.org/list/200",
            ref_title="Lei 200",
        )
        first_id = store.connection.execute(
            "SELECT id FROM documents WHERE ref_title='Lei 100'"
        ).fetchone()[0]
        second_id = store.connection.execute(
            "SELECT id FROM documents WHERE ref_title='Lei 200'"
        ).fetchone()[0]

        # Delete a row to create a gap before VACUUM, and verify id stays stable.
        with store.connection:
            store.connection.execute("DELETE FROM documents WHERE id = ?", (first_id,))
            store.connection.execute(
                "DELETE FROM documents_fts WHERE rowid = ?", (first_id,)
            )
        store.connection.execute("VACUUM")
        after_vacuum_id = store.connection.execute(
            "SELECT id FROM documents WHERE ref_title='Lei 200'"
        ).fetchone()[0]
        assert after_vacuum_id == second_id

        # Replace Lei 200 with completely different content: id must stay identical
        # and old terms must vanish completely from FTS5.
        await store.save(
            make_doc(
                title="Lei nº 200/2024",
                document_url="https://example.org/lei/200",
                text_markdown=(
                    "Art. 1º Dispõe exclusivamente sobre reflorestamento de "
                    "nascentes e proteção de biomas."
                ),
            ),
            ref_url="https://example.org/list/200",
            ref_title="Lei 200",
        )
        after_update_id = store.connection.execute(
            "SELECT id FROM documents WHERE ref_title='Lei 200'"
        ).fetchone()[0]
        assert after_update_id == second_id

        assert await store.search("mobilidade") == []
        new_hits = await store.search("reflorestamento")
        assert len(new_hits) == 1
        assert new_hits[0].id == second_id
        assert new_hits[0].source == "AC"

    # Reopen with default fts=False: meta.fts_enabled='1' must still maintain FTS!
    async with Store(db_path) as store:
        await store.save(
            make_doc(
                title="Lei nº 200/2024",
                document_url="https://example.org/lei/200",
                text_markdown=(
                    "Art. 1º Disciplina a telemedicina pública em hospitais "
                    "regionais do interior."
                ),
            ),
            ref_url="https://example.org/list/200",
            ref_title="Lei 200",
        )
        assert await store.search("reflorestamento") == []
        hits = await store.search("telemedicina")
        assert len(hits) == 1 and hits[0].id == second_id
        assert hits[0].source == "AC"


def test_strip_data_uri_images_helper():
    raw = (
        "Art. 1º Texto inicial.\n"
        "![Brasão](data:image/png;base64,iVBORw0KGgoAAAANSUNIQUEBASE64TOKEN==)\n"
        "![Foto externa](https://example.org/foto.png)\n"
        "Art. 2º Texto final."
    )
    stripped = strip_data_uri_images(raw)
    assert "UNIQUEBASE64TOKEN" not in stripped
    assert "data:image/" not in stripped
    assert "![Foto externa](https://example.org/foto.png)" in stripped
    assert "Art. 1º Texto inicial." in stripped
    assert "Art. 2º Texto final." in stripped

    plain = "Art. 1º Sem imagens embutidas."
    assert strip_data_uri_images(plain) is plain


async def test_fts5_strips_base64_images_matches_diacritics_filters_and_rebuild(
    tmp_path,
):
    db_path = tmp_path / "documents.db"
    # Populate without FTS first, then run rebuild_fts().
    async with Store(db_path, source="SP") as store:
        await store.save(
            make_doc(
                year=2023,
                title="Emenda à Constituição Estadual nº 50/2023",
                type="Emenda Constitucional",
                situation="Vigente",
                summary="Altera dispositivos sobre educação e saúde.",
                document_url="https://example.org/sp/50",
                text_markdown=(
                    "Art. 1º Fica assegurada a proteção à Constituição e à saúde.\n\n"
                    "![Brasão](data:image/png;base64,XYZSECRETBASE64BLOB987654321)\n\n"
                    "Art. 2º A administração pública observará a transparência."
                ),
            ),
            ref_url="https://example.org/sp/50",
            ref_title="Emenda 50",
        )
        await store.save(
            make_doc(
                year=2024,
                title="Decreto nº 123/2024",
                type="Decreto",
                situation="Revogada",
                summary="Regulamenta ações de meio-ambiente.",
                document_url="https://example.org/sp/123",
                text_markdown=(
                    "Art. 1º Estabelece diretrizes sobre meio-ambiente e "
                    "licenciamento florestal no território estadual."
                ),
            ),
            ref_url="https://example.org/sp/123",
            ref_title="Decreto 123",
        )

        with pytest.raises(RuntimeError, match="FTS5 index is not built"):
            await store.search("constituicao")

        indexed = await store.rebuild_fts()
        assert indexed == 2
        meta = dict(store.connection.execute("SELECT key, value FROM meta").fetchall())
        assert meta["fts_enabled"] == "1"

        # Base64 data URI tokens must NOT be indexed.
        assert await store.search("XYZSECRETBASE64BLOB987654321") == []
        assert await store.search("base64") == []

        # Portuguese diacritic-insensitive search: "constituicao" -> "Constituição".
        hits = await store.search("constituicao")
        assert len(hits) == 1
        hit = hits[0]
        assert isinstance(hit, SearchHit)
        assert hit.source == "SP"
        assert hit.year == 2023
        assert hit.type == "Emenda Constitucional"
        assert "[" in hit.snippet and "]" in hit.snippet
        assert "XYZSECRETBASE64BLOB" not in hit.snippet

        # "saude" matches "saúde", "administracao" matches "administração".
        assert len(await store.search("saude")) == 1
        assert len(await store.search("administracao")) == 1

        # Filters work cleanly.
        assert await store.search("constituicao", year=2024) == []
        assert len(await store.search("constituicao", year=2023)) == 1
        assert len(await store.search("meio-ambiente", type="Decreto")) == 1
        assert await store.search("meio-ambiente", situation="Vigente") == []
        assert len(await store.search("meio-ambiente", situation="Revogada")) == 1

        # Natural query with punctuation/slashes falls back safely to quoted FTS terms.
        punct_hits = await store.search("Decreto nº 123/2024")
        assert len(punct_hits) == 1 and punct_hits[0].year == 2024


async def test_fts5_search_does_not_load_full_bodies(tmp_path):
    async with Store(tmp_path / "documents.db", fts=True) as store:
        padding = "norma estadual vigente " * 40000  # ~920 KB per doc
        for i in range(12):
            await store.save(
                make_doc(
                    title=f"Lei nº {i}/2024",
                    document_url=f"https://example.org/{i}",
                    text_markdown=f"Art. 1º Termo único constitucionalidade. {padding}",
                )
            )
        tracemalloc.start()
        hits = await store.search("constitucionalidade", limit=10)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert len(hits) == 10
        assert peak < 1024 * 1024


async def test_fts5_unavailable_raises_clear_runtime_error(monkeypatch, tmp_path):
    from legisbr.core import store as store_mod

    def fake_check(_conn):
        raise RuntimeError(
            "SQLite FTS5 extension is not enabled in this Python build; "
            "full-text search is unavailable."
        )

    monkeypatch.setattr(store_mod, "_check_fts5_available", fake_check)
    with pytest.raises(RuntimeError, match="FTS5"):
        async with Store(tmp_path / "documents.db", fts=True):
            pass


async def test_parquet_export_fixed_schema_zstd_heterogeneous_extra(tmp_path):
    db_path = tmp_path / "documents.db"
    async with Store(db_path, source="MG") as store:
        docs = [
            (
                make_doc(
                    year=2023,
                    title="Lei 1/2023",
                    document_url="https://example.org/2023/1",
                    raw=b"pdf-1",
                    raw_ext=".pdf",
                    numero=1,
                    tags=["fiscal", "orcamento"],
                    nullable_field=None,
                ),
                "list/2023/1",
                "Lei 1/2023",
            ),
            (
                make_doc(
                    year=2023,
                    title="Lei 2/2023",
                    document_url="https://example.org/2023/2",
                    nested={"autor": "Executivo", "processo": 42},
                    nullable_field="now-a-string",
                ),
                "list/2023/2",
                "Lei 2/2023",
            ),
            (
                make_doc(
                    year=2024,
                    title="Decreto 3/2024",
                    type="Decreto",
                    document_url="https://example.org/2024/3",
                    portal_id="abc-999",
                    active=True,
                ),
                "list/2024/3",
                "Decreto 3/2024",
            ),
            (
                make_doc(
                    year=2024,
                    title="Decreto 4/2024",
                    type="Decreto",
                    document_url="https://example.org/2024/4",
                ),
                "list/2024/4",
                "Decreto 4/2024",
            ),
        ]
        for doc, ref_url, ref_title in docs:
            await store.save(doc, ref_url=ref_url, ref_title=ref_title)

        # 1. Per-year Parquet export with small batch_size=1 to force multi-batch streaming.
        per_year_dir = tmp_path / "parquet_years"
        count = await store.export(
            per_year_dir, fmt="parquet", single_file=False, batch_size=1
        )
        assert count == 4

        t2023 = pq.read_table(per_year_dir / "2023/data.parquet")
        t2024 = pq.read_table(per_year_dir / "2024/data.parquet")
        assert t2023.num_rows == 2
        assert t2024.num_rows == 2
        assert t2023.schema == t2024.schema

        expected_types = {
            "id": pa.int64(),
            "source": pa.string(),
            "ref_url": pa.string(),
            "ref_title": pa.string(),
            "document_url": pa.string(),
            "title": pa.string(),
            "year": pa.int32(),
            "type": pa.string(),
            "situation": pa.string(),
            "summary": pa.string(),
            "text_markdown": pa.string(),
            "file_path": pa.string(),
            "extra_json": pa.string(),
        }
        assert {field.name: field.type for field in t2023.schema} == expected_types

        # Verify ZSTD compression.
        pf = pq.ParquetFile(per_year_dir / "2023/data.parquet")
        assert pf.metadata.row_group(0).column(0).compression == "ZSTD"

        # 2. single_file=True into a directory -> data.parquet
        single_dir = tmp_path / "parquet_single"
        assert (
            await store.export(
                single_dir, fmt="parquet", single_file=True, batch_size=2
            )
            == 4
        )
        table = pq.read_table(single_dir / "data.parquet")
        assert table.num_rows == 4
        rows = table.to_pylist()
        assert [r["year"] for r in rows] == [2023, 2023, 2024, 2024]
        assert all(r["source"] == "MG" for r in rows)
        assert rows[0]["file_path"] is not None and rows[0]["file_path"].endswith(
            ".pdf"
        )
        assert rows[1]["file_path"] is None
        assert json.loads(rows[0]["extra_json"]) == {
            "numero": 1,
            "tags": ["fiscal", "orcamento"],
            "nullable_field": None,
        }
        assert json.loads(rows[1]["extra_json"]) == {
            "nested": {"autor": "Executivo", "processo": 42},
            "nullable_field": "now-a-string",
        }
        assert json.loads(rows[2]["extra_json"]) == {
            "portal_id": "abc-999",
            "active": True,
        }
        assert json.loads(rows[3]["extra_json"]) == {}

        # 3. single_file=True with explicit .parquet path
        explicit_file = tmp_path / "custom" / "corpus.parquet"
        assert (
            await store.export(
                explicit_file, fmt="parquet", single_file=True, batch_size=2
            )
            == 4
        )
        assert pq.read_table(explicit_file).num_rows == 4


async def test_parquet_export_missing_pyarrow_gives_install_hint(monkeypatch, tmp_path):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "pyarrow" or name.startswith("pyarrow."):
            raise ImportError("No module named 'pyarrow'")
        return real_import(name, globals, locals, fromlist, level)

    for mod in [
        m for m in list(sys.modules) if m == "pyarrow" or m.startswith("pyarrow.")
    ]:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    async with Store(tmp_path / "documents.db") as store:
        await store.save(make_doc())
        with pytest.raises(ImportError, match=r"pip install 'legisbr\[parquet\]'"):
            await store.export(tmp_path / "out", fmt="parquet")


async def test_archive_index_multi_source_query_stats_and_readonly_immutable_fallback(
    tmp_path,
):
    data_dir = tmp_path / "archives"

    # Build 3 archives: AC (indexed), SP (indexed), RJ (unindexed).
    async with Store(data_dir / "ACRE" / "documents.db", fts=True, source="ACRE") as ac:
        run_id = await ac.start_run("ACRE")
        await ac.save(
            make_doc(
                year=2023,
                title="Lei Estadual de Proteção Ambiental",
                type="Lei",
                document_url="https://ac.gov.br/lei/1",
                text_markdown=(
                    "Art. 1º Institui o fundo estadual de proteção ambiental "
                    "e conservação da floresta amazônica."
                ),
            )
        )
        await ac.finish_run(run_id, 1, 0, {}, {"seconds": 1.5})

    async with Store(
        data_dir / "SAO_PAULO" / "documents.db", fts=True, source="SAO_PAULO"
    ) as sp:
        await sp.save(
            make_doc(
                year=2024,
                title="Lei Ambiental Paulista",
                type="Lei",
                document_url="https://sp.gov.br/lei/2",
                text_markdown=(
                    "Art. 1º Política ambiental paulista sobre proteção ambiental, "
                    "saneamento ambiental e licenciamento ambiental."
                ),
            )
        )
        await sp.save(
            make_doc(
                year=2024,
                title="Decreto Tributário Paulista",
                type="Decreto",
                document_url="https://sp.gov.br/dec/3",
                text_markdown=(
                    "Art. 1º Regulamenta alíquotas de imposto estadual sobre "
                    "operações de circulação de mercadorias."
                ),
            )
        )

    async with Store(data_dir / "RIO" / "documents.db", fts=False, source="RIO") as rj:
        await rj.save(
            make_doc(
                year=2022,
                title="Lei do Rio sem FTS",
                document_url="https://rj.gov.br/lei/1",
            )
        )

    with ArchiveIndex(data_dir) as index:
        sources = index.sources()
        assert [s["source"] for s in sources] == ["ACRE", "RIO", "SAO_PAULO"]
        by_name = {s["source"]: s for s in sources}
        assert (
            by_name["ACRE"]["fts_enabled"] is True and by_name["ACRE"]["documents"] == 1
        )
        assert (
            by_name["RIO"]["fts_enabled"] is False and by_name["RIO"]["documents"] == 1
        )
        assert by_name["SAO_PAULO"]["documents"] == 2

        # Cross-jurisdiction query merges BM25 hits and skips unindexed RIO cleanly.
        hits = index.query("ambiental")
        assert len(hits) == 2
        assert {h.source for h in hits} == {"ACRE", "SAO_PAULO"}
        assert hits[0].rank <= hits[1].rank

        # Source / alias / year / doc_type filters.
        ac_only = index.query("ambiental", sources=["acre"])
        assert len(ac_only) == 1 and ac_only[0].source == "ACRE"
        sp_alias = index.query("ambiental", sources=["SP"])
        assert len(sp_alias) == 1 and sp_alias[0].source == "SAO_PAULO"
        assert len(index.query("ambiental", year=2024)) == 1
        assert len(index.query("tributario", doc_type="Decreto")) == 1
        assert index.query("tributario", doc_type="Lei") == []

        # get() by source (canonical or alias) + id.
        sp_id = hits[0].id if hits[0].source == "SAO_PAULO" else hits[1].id
        doc = index.get("sao_paulo", sp_id)
        assert doc is not None and "ambiental" in doc["text_markdown"].lower()
        doc_by_alias = index.get("SP", sp_id)
        assert doc_by_alias is not None and doc_by_alias["id"] == sp_id
        assert index.get("NONEXISTENT", 1) is None

        # stats() across archives.
        stats = index.stats()
        stats_by_name = {s["source"]: s for s in stats}
        assert stats_by_name["ACRE"]["last_run"]["docs"] == 1
        assert stats_by_name["SAO_PAULO"]["by_year"] == [{"year": 2024, "documents": 2}]

    # Explicit database paths support.
    explicit = ArchiveIndex(databases=[data_dir / "ACRE" / "documents.db"])
    assert [s["source"] for s in explicit.sources()] == ["ACRE"]

    # Simulate a read-only mount (chmod 0555 dir + 0444 files) on a clean WAL archive:
    # mode=ro fails trying to create/write -shm, and ArchiveIndex falls back to immutable=1.
    ro_dir = tmp_path / "ro_mount" / "ACRE"
    ro_db = ro_dir / "documents.db"
    async with Store(ro_db, fts=True, source="ACRE") as ro_store:
        await ro_store.save(
            make_doc(
                year=2023,
                title="Lei Estadual de Proteção Ambiental",
                type="Lei",
                document_url="https://ac.gov.br/lei/1",
                text_markdown=(
                    "Art. 1º Institui o fundo estadual de proteção ambiental "
                    "e conservação da floresta amazônica."
                ),
            )
        )

    for child in ro_dir.iterdir():
        os.chmod(child, 0o444)
    os.chmod(ro_dir, 0o555)
    try:
        ro_index = ArchiveIndex(ro_dir.parent)
        ro_sources = ro_index.sources()
        assert len(ro_sources) == 1
        assert ro_sources[0]["documents"] == 1
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            assert ro_sources[0]["immutable"] is True
        ro_hits = ro_index.query("floresta")
        assert len(ro_hits) == 1 and ro_hits[0].source == "ACRE"
        ro_doc = ro_index.get("ACRE", ro_hits[0].id)
        assert ro_doc is not None and "amazônica" in ro_doc["text_markdown"]
        ro_stats = ro_index.stats()
        assert ro_stats[0]["documents"] == 1
    finally:
        os.chmod(ro_dir, 0o755)
        for child in ro_dir.iterdir():
            os.chmod(child, 0o644)


async def test_archive_index_immutable_fallback_on_operational_error(
    monkeypatch, tmp_path
):
    from legisbr.core import archive as archive_mod

    db_path = tmp_path / "ACRE" / "documents.db"
    async with Store(db_path, fts=True, source="ACRE") as store:
        await store.save(
            make_doc(
                title="Lei de Recursos Hídricos",
                text_markdown="Art. 1º Protege bacias hidrográficas e mananciais.",
            )
        )

    seen_uris: list[str] = []
    real_connect = sqlite3.connect

    def fake_connect(database, *args, **kwargs):
        seen_uris.append(str(database))
        if isinstance(database, str) and "mode=ro" in database:
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(archive_mod.sqlite3, "connect", fake_connect)

    index = ArchiveIndex(databases={"ACRE": db_path})
    hits = index.query("hidrograficas")
    assert len(hits) == 1 and hits[0].source == "ACRE"
    assert any("mode=ro" in uri for uri in seen_uris)
    assert any("immutable=1" in uri for uri in seen_uris)


@pytest.mark.parametrize(
    ("error_msg", "error_code"),
    [
        ("database is locked", sqlite3.SQLITE_BUSY),
        ("database table is locked", sqlite3.SQLITE_LOCKED),
        ("database is locked", None),
    ],
    ids=["sqlite-busy", "sqlite-locked", "busy-message-only"],
)
async def test_archive_index_busy_or_locked_never_falls_back_to_immutable(
    monkeypatch, tmp_path, error_msg, error_code
):
    from legisbr.core import archive as archive_mod

    db_path = tmp_path / "ACRE" / "documents.db"
    async with Store(db_path, fts=True, source="ACRE") as store:
        await store.save(make_doc(title="Lei Concorrente"))

    seen_calls: list[tuple[str, dict]] = []

    def busy_connect(database, *args, **kwargs):
        seen_calls.append((str(database), dict(kwargs)))
        exc = sqlite3.OperationalError(error_msg)
        if error_code is not None:
            exc.sqlite_errorcode = error_code  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(archive_mod.sqlite3, "connect", busy_connect)

    index = ArchiveIndex(databases={"ACRE": db_path})
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        index.query("administracao")

    assert len(seen_calls) == 1
    uri, kwargs = seen_calls[0]
    assert uri.startswith("file:///")
    assert "mode=ro" in uri
    assert "immutable=1" not in uri
    assert kwargs.get("timeout") == 5.0


async def test_archive_index_cache_invalidation_on_external_modification_and_close(
    tmp_path,
):
    import gc

    db_path = tmp_path / "ACRE" / "documents.db"
    async with Store(db_path, fts=False, source="ACRE") as store:
        await store.save(
            make_doc(
                title="Lei Inicial",
                document_url="https://ac.gov.br/lei/1",
                text_markdown=(
                    "Art. 1º Norma estadual inicial sem índice textual de busca "
                    "construído na base de dados."
                ),
            )
        )

    index = ArchiveIndex(tmp_path)
    resolved = db_path.resolve()
    # First access caches the connection with has_fts=False.
    assert index.query("florestal") == []
    assert resolved in index._conn_cache
    first_entry = index._conn_cache[resolved]
    first_conn = first_entry.conn
    assert first_entry.has_fts is False

    # Repeated query reuses the exact same cached connection.
    assert index.query("florestal") == []
    assert index._conn_cache[resolved] is first_entry

    # External writer adds a document and builds FTS5 -> invalidates cached entry!
    async with Store(db_path, fts=True, source="ACRE") as store:
        await store.save(
            make_doc(
                title="Lei Florestal Nova",
                document_url="https://ac.gov.br/lei/2",
                text_markdown=(
                    "Art. 1º Estabelece diretrizes de proteção florestal e manejo "
                    "sustentável no território estadual."
                ),
            )
        )

    hits = index.query("florestal")
    assert len(hits) == 1
    assert hits[0].title == "Lei Florestal Nova"
    second_entry = index._conn_cache[resolved]
    assert second_entry is not first_entry
    assert second_entry.conn is not first_conn
    assert second_entry.has_fts is True
    # Old invalidated connection must already be closed.
    with pytest.raises(sqlite3.ProgrammingError):
        first_conn.execute("SELECT 1")

    # Explicit close() releases and closes all cached connections cleanly.
    index.close()
    assert index._conn_cache == {}
    with pytest.raises(sqlite3.ProgrammingError):
        second_entry.conn.execute("SELECT 1")

    # weakref.finalize cleans up even when close() is not called explicitly.
    temp_index = ArchiveIndex(tmp_path)
    assert len(temp_index.query("florestal")) == 1
    leaked_conn = temp_index._conn_cache[resolved].conn
    del temp_index
    gc.collect()
    with pytest.raises(sqlite3.ProgrammingError):
        leaked_conn.execute("SELECT 1")


async def test_archive_index_parallel_multi_archive_bm25_parity_and_thread_safety(
    tmp_path,
):
    from concurrent.futures import ThreadPoolExecutor

    data_dir = tmp_path / "multi"
    # Build 12 archives (> max_workers=8) to exercise parallel ThreadPoolExecutor fan-out.
    for i in range(12):
        name = f"ESTADO_{i:02d}"
        async with Store(data_dir / name / "documents.db", fts=True, source=name) as s:
            for j in range(3):
                repeats = "ambiental " * ((i + j) % 5 + 1)
                await s.save(
                    make_doc(
                        year=2020 + (i % 4),
                        title=f"Lei {i}-{j} Ambiental",
                        document_url=f"https://example.org/{i}/{j}",
                        text_markdown=(
                            f"Art. 1º Estabelece a política pública {repeats}"
                            f"aplicável no território do estado {i}."
                        ),
                    )
                )

    with ArchiveIndex(data_dir) as parallel_idx:
        parallel_hits = parallel_idx.query("ambiental", limit=15)
        parallel_sources = parallel_idx.sources()
        parallel_stats = parallel_idx.stats()
        assert len(parallel_idx._conn_cache) == 12

        # Exact BM25 ranking parity against single-archive sequential queries.
        sequential_hits: list[SearchHit] = []
        for i in range(12):
            name = f"ESTADO_{i:02d}"
            with ArchiveIndex(databases={name: data_dir / name / "documents.db"}) as si:
                sequential_hits.extend(si.query("ambiental", limit=15))
        sequential_hits.sort(key=lambda h: (h.rank, h.source, h.id))
        expected = sequential_hits[:15]

        assert parallel_hits == expected
        assert [s["source"] for s in parallel_sources] == [
            f"ESTADO_{i:02d}" for i in range(12)
        ]
        assert [s["source"] for s in parallel_stats] == [
            f"ESTADO_{i:02d}" for i in range(12)
        ]
        assert all(s["documents"] == 3 for s in parallel_sources)

        # Concurrent multi-threaded callers sharing the same ArchiveIndex must stay safe & exact.
        with ThreadPoolExecutor(max_workers=6) as callers:
            futures = [
                callers.submit(parallel_idx.query, "ambiental", limit=15)
                for _ in range(12)
            ]
            for fut in futures:
                assert fut.result() == expected

    # Exiting context manager closed all 12 cached connections.
    assert parallel_idx._conn_cache == {}
