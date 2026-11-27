"""Tests for Schema v2 migration, UPSERT/VACUUM stability, FTS5, Parquet, and ArchiveIndex."""

from __future__ import annotations

import builtins
import json
import os
import sqlite3
import sys
import tracemalloc
from pathlib import Path

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


def _create_v1_archive(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE documents (
                ref_url TEXT NOT NULL, ref_title TEXT NOT NULL,
                document_url TEXT NOT NULL, title TEXT NOT NULL, year INTEGER NOT NULL,
                type TEXT NOT NULL, situation TEXT NOT NULL, summary TEXT NOT NULL,
                text_markdown TEXT NOT NULL, file_path TEXT, extra TEXT NOT NULL,
                scraped_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                PRIMARY KEY (ref_url, ref_title)
            );
            CREATE INDEX documents_year ON documents(year);
            CREATE TABLE errors (
                run_id INTEGER, at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                year INTEGER, type TEXT, situation TEXT, title TEXT, url TEXT, message TEXT NOT NULL
            );
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY, scraper TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                finished_at TEXT, docs INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0, llm_usage TEXT NOT NULL DEFAULT '{}',
                stats TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        conn.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "https://example.org/list/1",
                "Lei 10",
                "https://example.org/doc/10",
                "Lei nº 10/2020",
                2020,
                "Lei",
                "Vigente",
                "Ementa antiga",
                "Art. 1º Texto preservado da migração v1 para v2 com detalhes.",
                "2020/docs/legacy.pdf",
                json.dumps({"numero": "10", "orgao": "AL"}),
                "2024-01-15T12:34:56.789Z",
            ),
        )
        conn.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "https://example.org/list/2",
                "Decreto 20",
                "https://example.org/doc/20",
                "Decreto nº 20/2021",
                2021,
                "Decreto",
                "Revogada",
                "Segundo documento",
                "Art. 1º Segundo texto preservado integralmente na tabela.",
                None,
                json.dumps({"codigo": 20}),
                "2024-02-20T00:00:00.000Z",
            ),
        )
        conn.execute(
            "INSERT INTO runs(scraper, docs, errors, stats) VALUES (?, ?, ?, ?)",
            ("ACRE", 2, 1, json.dumps({"seconds": 4.2})),
        )
        conn.execute(
            "INSERT INTO errors(run_id, year, title, message) VALUES (?, ?, ?, ?)",
            (1, 2021, "Lei quebrada", "timeout"),
        )


async def test_v1_to_v2_migration_preserves_data_and_is_idempotent(tmp_path):
    db_path = tmp_path / "documents.db"
    raw_file = tmp_path / "2020/docs/legacy.pdf"
    raw_file.parent.mkdir(parents=True)
    raw_file.write_bytes(b"%PDF-1.4 legacy")
    _create_v1_archive(db_path)

    async with Store(db_path) as store:
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

        doc1 = await store.get_document(1)
        doc2 = await store.get_document(2)
        assert doc1 is not None and doc2 is not None
        assert doc1["scraped_at"] == "2024-01-15T12:34:56.789Z"
        assert doc1["file_path"] == "2020/docs/legacy.pdf"
        assert doc1["extra"] == {"numero": "10", "orgao": "AL"}
        assert doc2["scraped_at"] == "2024-02-20T00:00:00.000Z"
        assert doc2["extra"] == {"codigo": 20}
        assert raw_file.read_bytes() == b"%PDF-1.4 legacy"

        run_row = store.connection.execute(
            "SELECT scraper, docs, stats FROM runs"
        ).fetchone()
        assert run_row["scraper"] == "ACRE" and run_row["docs"] == 2
        assert json.loads(run_row["stats"]) == {"seconds": 4.2}
        err_row = store.connection.execute(
            "SELECT title, message FROM errors"
        ).fetchone()
        assert err_row["title"] == "Lei quebrada" and err_row["message"] == "timeout"

    # Re-opening must be idempotent and keep the exact assigned integer IDs.
    async with Store(db_path) as store:
        assert await store.keys() == {
            ("https://example.org/list/1", "Lei 10"),
            ("https://example.org/list/2", "Decreto 20"),
        }
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

    index = ArchiveIndex(data_dir)
    sources = await index.sources()
    assert [s["source"] for s in sources] == ["ACRE", "RIO", "SAO_PAULO"]
    by_name = {s["source"]: s for s in sources}
    assert by_name["ACRE"]["fts_enabled"] is True and by_name["ACRE"]["documents"] == 1
    assert by_name["RIO"]["fts_enabled"] is False and by_name["RIO"]["documents"] == 1
    assert by_name["SAO_PAULO"]["documents"] == 2

    # Cross-jurisdiction query merges BM25 hits and skips unindexed RIO cleanly.
    hits = await index.query("ambiental")
    assert len(hits) == 2
    assert {h.source for h in hits} == {"ACRE", "SAO_PAULO"}
    assert hits[0].rank <= hits[1].rank

    # Source / year / doc_type filters.
    ac_only = await index.query("ambiental", sources=["acre"])
    assert len(ac_only) == 1 and ac_only[0].source == "ACRE"
    assert len(await index.query("ambiental", year=2024)) == 1
    assert len(await index.query("tributario", doc_type="Decreto")) == 1
    assert await index.query("tributario", doc_type="Lei") == []

    # get_document by source + id.
    doc = await index.get_document(
        "sao_paulo", hits[0].id if hits[0].source == "SAO_PAULO" else hits[1].id
    )
    assert doc is not None and "ambiental" in doc["text_markdown"].lower()
    assert await index.get_document("NONEXISTENT", 1) is None

    # stats() across archives.
    stats = await index.stats()
    stats_by_name = {s["source"]: s for s in stats}
    assert stats_by_name["ACRE"]["last_run"]["docs"] == 1
    assert stats_by_name["SAO_PAULO"]["by_year"] == [{"year": 2024, "documents": 2}]

    # Explicit database paths support.
    explicit = ArchiveIndex(databases=[data_dir / "ACRE" / "documents.db"])
    assert [s["source"] for s in await explicit.sources()] == ["ACRE"]

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
        ro_sources = await ro_index.sources()
        assert len(ro_sources) == 1
        assert ro_sources[0]["documents"] == 1
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            assert ro_sources[0]["immutable"] is True
        ro_hits = await ro_index.query("floresta")
        assert len(ro_hits) == 1 and ro_hits[0].source == "ACRE"
        ro_doc = await ro_index.get_document("ACRE", ro_hits[0].id)
        assert ro_doc is not None and "amazônica" in ro_doc["text_markdown"]
        ro_stats = await ro_index.stats()
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
    hits = await index.query("hidrograficas")
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
        await index.query("administracao")

    assert len(seen_calls) == 1
    uri, kwargs = seen_calls[0]
    assert uri.startswith("file:///")
    assert "mode=ro" in uri
    assert "immutable=1" not in uri
    assert kwargs.get("timeout") == 5.0
