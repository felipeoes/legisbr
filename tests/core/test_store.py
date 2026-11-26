from __future__ import annotations
import json
import logging
import sqlite3
import tracemalloc

import pytest

from legisbr.core.document import Document
from legisbr.core.sink import DocumentSink, ErrorRecord, NullSink
from legisbr.core.store import Store


def document(**kwargs) -> Document:
    return Document(
        **{
            "year": 2024,
            "title": "Lei 1",
            "type": "Lei",
            "situation": "Vigente",
            "document_url": "https://example.org/1",
            "text_markdown": "Art. 1º Esta lei estabelece as disposições para a administração pública.",
            **kwargs,
        }
    )


async def test_long_titles_and_stable_listing_identity(tmp_path):
    async with Store(tmp_path / "documents.db") as store:
        for suffix in ("one", "two"):
            await store.save(
                document(title="a" * 300 + suffix, raw=suffix.encode(), raw_ext=".pdf"),
                ref_url="listing",
                ref_title=suffix,
            )
        assert await store.keys() == {("listing", "one"), ("listing", "two")}
        assert len(list((tmp_path / "2024/docs").glob("*.pdf"))) == 2
        for fmt in ("json", "jsonl"):
            assert await store.export(tmp_path / "export", fmt) == 2
            text = (tmp_path / f"export/2024/data.{fmt}").read_text()
            rows = (
                json.loads(text)
                if fmt == "json"
                else [json.loads(line) for line in text.splitlines()]
            )
            assert {row["document_url"] for row in rows} == {"https://example.org/1"}
            assert all("raw" not in row for row in rows)


async def test_underscore_listing_fields_are_not_stored(tmp_path):
    async with Store(tmp_path / "documents.db") as store:
        await store.save(document(_pdf_link="https://example.org/1.pdf", number="1"))
        await store.export(tmp_path / "export")
        row = json.loads((tmp_path / "export/2024/data.jsonl").read_text())
        assert row["number"] == "1" and "_pdf_link" not in row


async def test_failed_insert_preserves_existing_row_and_file(tmp_path):
    async with Store(tmp_path / "documents.db") as store:
        await store.save(document(raw=b"old", raw_ext=".pdf"))
        store.connection.execute(
            "CREATE TRIGGER fail BEFORE INSERT ON documents BEGIN SELECT RAISE(ABORT, 'crash'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            await store.save(document(raw=b"new", raw_ext=".pdf"))
        row = store.connection.execute("SELECT file_path FROM documents").fetchone()
        assert (tmp_path / row[0]).read_bytes() == b"old"
        assert len(await store.keys()) == 1


async def test_summary_does_not_load_bodies(tmp_path):
    async with Store(tmp_path / "documents.db") as store:
        body = "x" * (1024 * 1024)
        for i in range(100):
            await store.save(document(title=str(i), text_markdown=body))
        tracemalloc.start()
        summary = await store.summary()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert summary[0]["documents"] == 100
        assert peak < 1024 * 1024


async def test_import_fails_loudly_without_deleting_inputs(tmp_path):
    source = tmp_path / "2024/data.json"
    source.parent.mkdir()
    source.write_text("[broken")
    async with Store(tmp_path / "documents.db") as store:
        with pytest.raises(json.JSONDecodeError):
            await store.import_json(tmp_path)
        assert source.read_text() == "[broken"
        source.write_text(json.dumps([document(file_path="old.pdf").model_dump()]))
        assert await store.import_json(tmp_path) == (1, 0)
        assert (
            store.connection.execute("SELECT file_path FROM documents").fetchone()[0]
            == "old.pdf"
        )


async def test_export_import_round_trip_keeps_listing_keys(tmp_path):
    async with Store(tmp_path / "a/documents.db") as store:
        await store.save(document(), ref_url="listing", ref_title="Listing title")
        await store.export(tmp_path / "archive", "json")
    async with Store(tmp_path / "b/documents.db") as store:
        assert await store.import_json(tmp_path / "archive") == (1, 0)
        assert await store.keys() == {("listing", "Listing title")}
        extra = store.connection.execute("SELECT extra FROM documents").fetchone()[0]
        assert json.loads(extra) == {}


async def test_import_reads_legacy_envelope_and_records_invalid_rows(tmp_path):
    source = tmp_path / "2024/data.json"
    source.parent.mkdir()
    rows = [document().model_dump(), {**document(title="Lei 2").model_dump()}]
    rows[1]["situation"] = ""
    source.write_text(json.dumps({"summary": {}, "documents": rows}))
    async with Store(tmp_path / "documents.db") as store:
        assert await store.import_json(tmp_path) == (1, 1)
        assert len(await store.keys()) == 1
        title, message = store.connection.execute(
            "SELECT title, message FROM errors"
        ).fetchone()
        assert title == "Lei 2" and "situation" in message


async def test_error_runs(tmp_path):
    async with Store(tmp_path / "documents.db") as store:
        run = await store.start_run("TEST")
        await store.record_error(run_id=run, message="failed", year=2024)
        await store.finish_run(run, 1, 1, {"test": {"requests": 1}})
        assert store.connection.execute("SELECT errors FROM runs").fetchone()[0] == 1


@pytest.mark.parametrize(
    "field", ["title", "type", "situation", "document_url", "text_markdown"]
)
def test_document_validates_required_text(field):
    with pytest.raises(ValueError):
        document(**{field: " "})


async def test_cancelled_context_entry_closes_open_connection():
    import asyncio

    store = Store(":memory:")
    original_open = store.open

    async def interrupted_open():
        await original_open()
        raise asyncio.CancelledError

    store.open = interrupted_open
    with pytest.raises(asyncio.CancelledError):
        async with store:
            pass
    assert store._connection is None


def test_document_normalizes_title_whitespace_and_null_summary():
    doc = document(title="Ato\n      n° 4.307/2012", summary=None)
    assert doc.title == "Ato n° 4.307/2012" and doc.summary == ""


async def test_import_normalizes_ref_title_like_listing_keys(tmp_path):
    (tmp_path / "2024").mkdir()
    row = document().model_dump() | {"ref_url": "listing", "ref_title": "Lei\n   1"}
    (tmp_path / "2024/data.json").write_text(json.dumps([row]))
    async with Store(tmp_path / "documents.db") as store:
        assert await store.import_json(tmp_path) == (1, 0)
        assert await store.keys() == {("listing", "Lei 1")}


def _raw_files(root):
    return sorted(p.read_bytes() for p in root.glob("*/docs/*") if p.is_file())


async def test_raw_file_path_is_relative_so_archives_can_move(tmp_path):
    async with Store(tmp_path / "documents.db") as store:
        await store.save(document(raw=b"pdf", raw_ext=".pdf"))
        (path,) = store.connection.execute("SELECT file_path FROM documents").fetchone()
    assert path.startswith("2024/docs/") and path.endswith(".pdf")
    assert (tmp_path / path).read_bytes() == b"pdf"


async def test_replacing_a_document_removes_its_old_raw_file(tmp_path):
    async with Store(tmp_path / "documents.db") as store:
        await store.save(document(raw=b"old", raw_ext=".pdf"))
        await store.save(document(raw=b"new", raw_ext=".html", year=2025))
        await store.save(document(raw=b"other", raw_ext=".pdf"), ref_title="Lei 2")
    assert _raw_files(tmp_path) == [b"new", b"other"]


async def test_failed_insert_removes_the_new_orphan_raw_file(tmp_path):
    async with Store(tmp_path / "documents.db") as store:
        await store.save(document(raw=b"old", raw_ext=".pdf"))
        store.connection.execute(
            "CREATE TRIGGER fail BEFORE INSERT ON documents BEGIN SELECT RAISE(ABORT, 'crash'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            await store.save(document(raw=b"new", raw_ext=".pdf"))
    assert _raw_files(tmp_path) == [b"old"]


async def test_replace_never_deletes_files_the_store_did_not_write(tmp_path):
    foreign = tmp_path / "2024" / "legacy.pdf"
    foreign.parent.mkdir()
    foreign.write_bytes(b"legacy")
    async with Store(tmp_path / "documents.db") as store:
        await store.save(document(), file_path=str(foreign))
        await store.save(document(raw=b"new", raw_ext=".pdf"))
    assert foreign.read_bytes() == b"legacy"


async def test_runs_keep_stats_and_old_archives_gain_the_column(tmp_path):
    db = tmp_path / "documents.db"
    with sqlite3.connect(db) as old:
        old.execute(
            "CREATE TABLE runs (id INTEGER PRIMARY KEY, scraper TEXT NOT NULL, started_at TEXT, finished_at TEXT, docs INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0, llm_usage TEXT NOT NULL DEFAULT '{}')"
        )
    async with Store(db) as store:
        run = await store.start_run("TEST")
        await store.finish_run(run, 1, 0, {}, {"seconds": 2.5})
        (stats,) = store.connection.execute("SELECT stats FROM runs").fetchone()
    assert json.loads(stats) == {"seconds": 2.5}


async def test_memory_store_save_raw_false_and_true():
    async with Store(":memory:", save_raw=True) as strict_store:
        with pytest.raises(ValueError, match="filesystem store"):
            await strict_store.save(document(raw=b"pdf-bytes", raw_ext=".pdf"))

    async with Store(":memory:", save_raw=False) as mem_store:
        await mem_store.save(document(raw=b"pdf-bytes", raw_ext=".pdf"))
        assert await mem_store.keys() == {("https://example.org/1", "Lei 1")}
        doc = await mem_store.get_by_url("https://example.org/1")
        assert doc is not None and doc["file_path"] is None


async def test_filesystem_store_save_raw_false_writes_no_raw_files(tmp_path):
    async with Store(tmp_path / "documents.db", save_raw=False) as store:
        await store.save(document(raw=b"pdf-bytes", raw_ext=".pdf"))
        doc = await store.get_document(1)
        assert doc is not None and doc["file_path"] is None
    assert _raw_files(tmp_path) == []


async def test_low_memory_and_default_cache_pragmas(tmp_path):
    async with Store(tmp_path / "default.db") as store:
        (cache_size,) = store.connection.execute("PRAGMA cache_size").fetchone()
        assert cache_size == -16000
    async with Store(tmp_path / "low.db", low_memory=True) as store:
        (cache_size,) = store.connection.execute("PRAGMA cache_size").fetchone()
        assert cache_size == -4000


async def test_iter_keys_streams_in_batches_and_filters_year(tmp_path):
    async with Store(tmp_path / "documents.db") as store:
        for i in range(7):
            year = 2023 if i < 3 else 2024
            await store.save(
                document(
                    year=year,
                    title=f"Lei {i}",
                    document_url=f"https://example.org/{i}",
                )
            )
        all_keys = [key async for key in store.iter_keys(batch_size=2)]
        assert len(all_keys) == 7
        assert set(all_keys) == await store.keys()

        keys_2023 = [key async for key in store.iter_keys(year=2023, batch_size=2)]
        assert set(keys_2023) == await store.keys(year=2023)
        assert len(keys_2023) == 3


async def test_document_sink_protocol_error_record_and_null_sink(caplog):
    store = Store(":memory:", save_raw=False)
    null_sink = NullSink()
    assert isinstance(store, DocumentSink)
    assert isinstance(null_sink, DocumentSink)

    async with store:
        run_id = await store.start_run("TEST")
        err = ErrorRecord(
            message="boom",
            run_id=run_id,
            year=2024,
            type="Lei",
            situation="Vigente",
            title="Lei 1",
            url="https://example.org/1",
        )
        await store.record_error(err)
        row = store.connection.execute(
            "SELECT run_id, year, type, situation, title, url, message FROM errors"
        ).fetchone()
        assert dict(row) == {
            "run_id": run_id,
            "year": 2024,
            "type": "Lei",
            "situation": "Vigente",
            "title": "Lei 1",
            "url": "https://example.org/1",
            "message": "boom",
        }

    # NullSink logs when no on_error callback is given.
    with caplog.at_level(logging.ERROR, logger="legisbr.core.sink"):
        async with null_sink:
            run_id = await null_sink.start_run("TEST")
            assert run_id == 0
            assert [k async for k in null_sink.iter_keys()] == []
            await null_sink.save(document())
            await null_sink.record_error(
                ErrorRecord(message="logged err", title="Lei 9", url="https://x.test/9")
            )
            await null_sink.finish_run(run_id, 1, 1, {})
    assert "logged err" in caplog.text

    # NullSink supports both sync and async on_error callbacks.
    sync_seen: list[ErrorRecord] = []
    async_seen: list[ErrorRecord] = []

    async def on_err_async(record: ErrorRecord) -> None:
        async_seen.append(record)

    sync_sink = NullSink(on_error=sync_seen.append)
    async_sink = NullSink(on_error=on_err_async)
    sample_err = ErrorRecord(message="cb err")
    await sync_sink.record_error(sample_err)
    await async_sink.record_error(sample_err)
    assert sync_seen == [sample_err]
    assert async_seen == [sample_err]
