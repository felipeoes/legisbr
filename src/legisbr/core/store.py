"""Transactional SQLite storage, safe FTS5 search, and streaming export."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self, TypeVar, overload

from pydantic import ValidationError

from legisbr.core.document import Document, clean_title
from legisbr.core.sink import ErrorRecord
from legisbr.registry import SourceInput, get_spec

_T = TypeVar("_T")

_SCHEMA_VERSION = "2"

_CREATE_META_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_CREATE_DOCUMENTS_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    ref_url TEXT NOT NULL,
    ref_title TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    document_url TEXT NOT NULL,
    title TEXT NOT NULL,
    year INTEGER NOT NULL,
    type TEXT NOT NULL,
    situation TEXT NOT NULL,
    summary TEXT NOT NULL,
    text_markdown TEXT NOT NULL,
    file_path TEXT,
    extra TEXT NOT NULL,
    scraped_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(ref_url, ref_title)
)
"""

_CREATE_ERRORS_SQL = """
CREATE TABLE IF NOT EXISTS errors (
    run_id INTEGER,
    at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    year INTEGER,
    type TEXT,
    situation TEXT,
    title TEXT,
    url TEXT,
    message TEXT NOT NULL
)
"""

_CREATE_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    scraper TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at TEXT,
    docs INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    llm_usage TEXT NOT NULL DEFAULT '{}',
    stats TEXT NOT NULL DEFAULT '{}'
)
"""

_CREATE_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    title,
    summary,
    text_plain,
    tokenize='unicode61 remove_diacritics 2'
)
"""

_FTS_BATCH_SIZE = 1000

# Raw files this store wrote: <year>/docs/<sha256><ext>, relative to the archive.
_OWNED_RAW_RE = re.compile(r"\d+/docs/[0-9a-f]{64}\.[a-zA-Z0-9]+")

# Inline Markdown data-URI images stripped before FTS5 indexing so base64 blobs
# never bloat the index or pollute BM25/snippets.
_DATA_URI_FAST_RE = re.compile(r"data:image/", re.IGNORECASE)
_DATA_URI_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\(\s*<?data:image/[^)\s>]+>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)",
    re.IGNORECASE,
)


def strip_data_uri_images(text: str) -> str:
    """Remove Markdown inline data-URI images before FTS5 indexing."""
    if not _DATA_URI_FAST_RE.search(text):
        return text
    cleaned = _DATA_URI_IMAGE_RE.sub(" ", text)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


@dataclass(frozen=True)
class SearchHit:
    """BM25-ranked full-text search hit without loading full document bodies."""

    id: int
    source: str
    title: str
    year: int
    type: str
    situation: str
    summary: str
    document_url: str
    snippet: str
    rank: float


def _check_fts5_available(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sqlite_compileoption_used('ENABLE_FTS5')"
    ).fetchone()
    if not row or not row[0]:
        raise RuntimeError(
            "SQLite FTS5 extension is not enabled in this Python build; "
            "full-text search is unavailable."
        )


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _row_to_dict(row: sqlite3.Row, *, default_source: str = "") -> dict[str, Any]:
    data = dict(row)
    raw_extra = data.pop("extra", "{}")
    data["extra"] = json.loads(raw_extra) if raw_extra else {}
    if not data.get("source") and default_source:
        data["source"] = default_source
    return data


def _quote_fts_terms(query: str) -> str:
    tokens = [token for token in query.split() if token]
    if not tokens:
        return ""
    return " ".join('"' + token.replace('"', '""') + '"' for token in tokens)


def _search_connection(
    connection: sqlite3.Connection,
    query: str,
    *,
    default_source: str = "",
    year: int | None = None,
    type: str | None = None,
    situation: str | None = None,
    limit: int = 20,
) -> list[SearchHit]:
    if limit <= 0 or not query.strip():
        return []
    _check_fts5_available(connection)
    if not _has_table(connection, "documents_fts"):
        raise RuntimeError(
            "FTS5 index is not built for this archive; call rebuild_fts() or open Store with fts=True."
        )

    clauses = ["documents_fts MATCH ?"]
    filter_params: list[Any] = []
    if year is not None:
        clauses.append("d.year = ?")
        filter_params.append(year)
    if type is not None:
        clauses.append("d.type = ?")
        filter_params.append(type)
    if situation is not None:
        clauses.append("d.situation = ?")
        filter_params.append(situation)

    sql = (
        "SELECT "
        "d.id, d.source, d.title, d.year, d.type, d.situation, "
        "d.summary, d.document_url, "
        "snippet(documents_fts, -1, '[', ']', '...', 24) AS snippet, "
        "bm25(documents_fts, 5.0, 2.0, 1.0) AS rank "
        "FROM documents_fts "
        "JOIN documents AS d ON d.id = documents_fts.rowid "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY rank ASC, d.id ASC "
        "LIMIT ?"
    )

    try:
        rows = connection.execute(sql, (query, *filter_params, limit)).fetchall()
    except sqlite3.OperationalError:
        escaped = _quote_fts_terms(query)
        if not escaped or escaped == query:
            raise
        rows = connection.execute(sql, (escaped, *filter_params, limit)).fetchall()

    return [
        SearchHit(
            id=int(row["id"]),
            source=str(row["source"] or default_source),
            title=str(row["title"]),
            year=int(row["year"]),
            type=str(row["type"]),
            situation=str(row["situation"]),
            summary=str(row["summary"]),
            document_url=str(row["document_url"]),
            snippet=str(row["snippet"] or ""),
            rank=float(row["rank"]),
        )
        for row in rows
    ]


class Store:
    def __init__(
        self,
        path: str | Path,
        *,
        save_raw: bool = True,
        fts: bool = False,
        source: str = "",
        low_memory: bool = False,
    ) -> None:
        self.path = str(path)
        self.root = Path(path).parent
        self.save_raw = save_raw
        self.fts = fts
        self.source = source
        self.low_memory = low_memory
        self._fts_active = False
        self._lock = asyncio.Lock()
        self._connection: sqlite3.Connection | None = None

    @classmethod
    def for_source(
        cls,
        source: SourceInput,
        data_dir: str | Path = "outputs/legislation",
        *,
        save_raw: bool = True,
        fts: bool = False,
        low_memory: bool = False,
    ) -> Store:
        """Build a ``Store`` for *source* (``Source`` enum, code, name, or alias) under *data_dir*."""
        spec = get_spec(source)
        return cls(
            spec.database_path(Path(data_dir)),
            save_raw=save_raw,
            fts=fts,
            source=spec.name,
            low_memory=low_memory,
        )

    async def _call(self, operation: Callable[..., _T], *args: Any) -> _T:
        async with self._lock:
            task = asyncio.create_task(asyncio.to_thread(operation, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Finish the SQLite call before another task can use the connection.
                await task
                raise

    @staticmethod
    def _rebuild_fts_sync(connection: sqlite3.Connection) -> int:
        _check_fts5_available(connection)
        indexed = 0
        with connection:
            connection.execute("DROP TABLE IF EXISTS documents_fts")
            connection.execute(_CREATE_FTS_SQL)
            reader = connection.execute(
                "SELECT id, title, summary, text_markdown FROM documents ORDER BY id"
            )
            while True:
                rows = reader.fetchmany(_FTS_BATCH_SIZE)
                if not rows:
                    break
                batch = [
                    (
                        int(row["id"]),
                        str(row["title"]),
                        str(row["summary"]),
                        strip_data_uri_images(str(row["text_markdown"])),
                    )
                    for row in rows
                ]
                connection.executemany(
                    "INSERT INTO documents_fts(rowid, title, summary, text_plain) "
                    "VALUES (?, ?, ?, ?)",
                    batch,
                )
                indexed += len(batch)
            connection.execute(
                "INSERT INTO meta(key, value) VALUES ('fts_enabled', '1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'"
            )
        return indexed

    def _open(self) -> None:
        if self._connection is not None:
            return
        if self.path != ":memory:":
            self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, check_same_thread=False)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA temp_store=MEMORY")
            cache_kb = -4000 if self.low_memory else -16000
            connection.execute(f"PRAGMA cache_size={cache_kb}")

            with connection:
                connection.execute(_CREATE_META_SQL)
                connection.execute(_CREATE_DOCUMENTS_SQL)
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS documents_year ON documents(year)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS documents_type ON documents(type)"
                )
                connection.execute(_CREATE_ERRORS_SQL)
                connection.execute(_CREATE_RUNS_SQL)
                connection.execute(
                    "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (_SCHEMA_VERSION,),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('fts_enabled', '0')"
                )

            fts_row = connection.execute(
                "SELECT value FROM meta WHERE key='fts_enabled'"
            ).fetchone()
            fts_enabled_in_db = bool(fts_row and fts_row[0] == "1")
            if self.fts or fts_enabled_in_db:
                _check_fts5_available(connection)
                fts_exists = _has_table(connection, "documents_fts")
                if not fts_enabled_in_db or not fts_exists:
                    self._rebuild_fts_sync(connection)
                self._fts_active = True
            else:
                self._fts_active = False
        except BaseException:
            connection.close()
            raise
        self._connection = connection

    async def open(self) -> None:
        await self._call(self._open)

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Store is not open")
        return self._connection

    async def close(self) -> None:
        def close() -> None:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
                self._fts_active = False

        await self._call(close)

    async def __aenter__(self) -> Self:
        try:
            await self.open()
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def _upsert_fts(
        self, doc_id: int, title: str, summary: str, text_markdown: str
    ) -> None:
        text_plain = strip_data_uri_images(text_markdown)
        self.connection.execute(
            "DELETE FROM documents_fts WHERE rowid = ?",
            (doc_id,),
        )
        self.connection.execute(
            "INSERT INTO documents_fts(rowid, title, summary, text_plain) "
            "VALUES (?, ?, ?, ?)",
            (doc_id, title, summary, text_plain),
        )

    def _insert(
        self,
        doc: Document,
        ref_url: str,
        ref_title: str,
        file_path: str | None,
        source: str | None = None,
    ) -> str | None:
        """Write the row (and optional raw file / FTS entry); returns stored file_path."""
        if doc.raw is not None and self.save_raw:
            if self.path == ":memory:":
                raise ValueError("Raw files require a filesystem store")
            file_path = self._raw_path(doc, ref_url, ref_title)
            target = self.root / file_path
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".tmp")
            try:
                temporary.write_bytes(doc.raw)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        data = doc.model_dump()
        # Keys starting with "_" are listing-only helpers, never archive data.
        extra = {
            key: value
            for key, value in data.items()
            if key not in Document.model_fields and not key.startswith("_")
        }
        extra.pop("file_path", None)
        doc_source = source if source is not None else self.source
        row = self.connection.execute(
            """
            INSERT INTO documents (
                ref_url, ref_title, source, document_url, title, year,
                type, situation, summary, text_markdown, file_path, extra
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ref_url, ref_title) DO UPDATE SET
                source = CASE
                    WHEN excluded.source != '' THEN excluded.source
                    ELSE documents.source
                END,
                document_url = excluded.document_url,
                title = excluded.title,
                year = excluded.year,
                type = excluded.type,
                situation = excluded.situation,
                summary = excluded.summary,
                text_markdown = excluded.text_markdown,
                file_path = excluded.file_path,
                extra = excluded.extra,
                scraped_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            RETURNING id
            """,
            (
                ref_url,
                ref_title,
                doc_source,
                doc.document_url,
                doc.title,
                doc.year,
                doc.type,
                doc.situation,
                doc.summary,
                doc.text_markdown,
                file_path,
                json.dumps(extra, ensure_ascii=False),
            ),
        ).fetchone()
        if self._fts_active and row is not None:
            self._upsert_fts(int(row[0]), doc.title, doc.summary, doc.text_markdown)
        return file_path

    @staticmethod
    def _raw_path(doc: Document, ref_url: str, ref_title: str) -> str:
        ext = doc.raw_ext or ".bin"
        if not re.fullmatch(r"\.[a-zA-Z0-9]+", ext):
            raise ValueError(f"Invalid raw extension: {ext!r}")
        identity = json.dumps([ref_url, ref_title], ensure_ascii=False).encode()
        # Content-addressed, so a failed transaction never overwrites the old file.
        assert doc.raw is not None
        digest = hashlib.sha256(identity + b"\x00" + doc.raw).hexdigest()
        return f"{doc.year}/docs/{digest}{ext}"

    def _owned_raw(self, file_path: str | None) -> Path | None:
        """The raw file behind file_path, only if this store wrote it."""
        if not file_path:
            return None
        return self.root / file_path if _OWNED_RAW_RE.fullmatch(file_path) else None

    def _save(
        self, doc: Document, ref_url: str, ref_title: str, file_path: str | None
    ) -> None:
        if doc.raw is not None and self.save_raw and self.path == ":memory:":
            raise ValueError("Raw files require a filesystem store")
        row = self.connection.execute(
            "SELECT file_path FROM documents WHERE ref_url=? AND ref_title=?",
            (ref_url, ref_title),
        ).fetchone()
        old = self._owned_raw(row[0]) if row else None
        # Validates raw_ext before anything is written.
        new = (
            self.root / self._raw_path(doc, ref_url, ref_title)
            if doc.raw is not None and self.save_raw
            else None
        )
        try:
            with self.connection:
                self._insert(doc, ref_url, ref_title, file_path)
        except BaseException:
            # Content-addressed: a path other than the old one is a fresh orphan.
            if new is not None and new != old and self.path != ":memory:":
                new.unlink(missing_ok=True)
            raise
        if old is not None and old != new:
            old.unlink(missing_ok=True)

    async def save(
        self,
        doc: Document,
        *,
        ref_url: str | None = None,
        ref_title: str | None = None,
        file_path: str | None = None,
    ) -> None:
        await self._call(
            self._save,
            doc,
            ref_url or doc.document_url,
            ref_title or doc.title,
            file_path,
        )

    async def iter_keys(
        self, year: int | None = None, *, batch_size: int = 5000
    ) -> AsyncIterator[tuple[str, str]]:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        last_id: int | None = None
        while True:

            def fetch_batch(after_id: int | None) -> list[tuple[int, str, str]]:
                if year is None:
                    if after_id is None:
                        sql = (
                            "SELECT id, ref_url, ref_title FROM documents "
                            "ORDER BY id LIMIT ?"
                        )
                        params: tuple[Any, ...] = (batch_size,)
                    else:
                        sql = (
                            "SELECT id, ref_url, ref_title FROM documents "
                            "WHERE id > ? ORDER BY id LIMIT ?"
                        )
                        params = (after_id, batch_size)
                else:
                    if after_id is None:
                        sql = (
                            "SELECT id, ref_url, ref_title FROM documents "
                            "WHERE year = ? ORDER BY id LIMIT ?"
                        )
                        params = (year, batch_size)
                    else:
                        sql = (
                            "SELECT id, ref_url, ref_title FROM documents "
                            "WHERE year = ? AND id > ? ORDER BY id LIMIT ?"
                        )
                        params = (year, after_id, batch_size)
                return [
                    (int(row[0]), str(row[1]), str(row[2]))
                    for row in self.connection.execute(sql, params)
                ]

            batch = await self._call(fetch_batch, last_id)
            if not batch:
                break
            last_id = batch[-1][0]
            for _, ref_url, ref_title in batch:
                yield (ref_url, ref_title)
            if len(batch) < batch_size:
                break

    async def keys(self, year: int | None = None) -> set[tuple[str, str]]:
        def select() -> set[tuple[str, str]]:
            sql = "SELECT ref_url, ref_title FROM documents"
            rows = self.connection.execute(
                sql if year is None else sql + " WHERE year=?",
                () if year is None else (year,),
            )
            return {(row[0], row[1]) for row in rows}

        return await self._call(select)

    async def start_run(self, scraper: str) -> int:
        def start() -> int:
            with self.connection:
                cursor = self.connection.execute(
                    "INSERT INTO runs(scraper) VALUES (?)", (scraper,)
                )
                assert cursor.lastrowid is not None
                return int(cursor.lastrowid)

        return await self._call(start)

    async def finish_run(
        self,
        run_id: int,
        docs: int,
        errors: int,
        usage: dict[str, Any],
        stats: dict[str, Any] | None = None,
    ) -> None:
        def finish() -> None:
            with self.connection:
                self.connection.execute(
                    "UPDATE runs SET finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                    "docs=?, errors=?, llm_usage=?, stats=? WHERE id=?",
                    (docs, errors, json.dumps(usage), json.dumps(stats or {}), run_id),
                )

        await self._call(finish)

    @overload
    async def record_error(self, error: ErrorRecord) -> None: ...

    @overload
    async def record_error(
        self,
        *,
        message: str,
        run_id: int | None = None,
        year: int | None = None,
        type: str = "",
        situation: str = "",
        title: str = "",
        url: str = "",
    ) -> None: ...

    async def record_error(
        self,
        error: ErrorRecord | None = None,
        *,
        message: str | None = None,
        run_id: int | None = None,
        year: int | None = None,
        type: str = "",
        situation: str = "",
        title: str = "",
        url: str = "",
    ) -> None:
        if error is None:
            if message is None:
                raise TypeError("record_error requires an ErrorRecord or message=...")
            record = ErrorRecord(
                message=message,
                run_id=run_id,
                year=year,
                type=type,
                situation=situation,
                title=title,
                url=url,
            )
        else:
            record = error

        def insert() -> None:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO errors(run_id, year, type, situation, title, url, message) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.run_id,
                        record.year,
                        record.type,
                        record.situation,
                        record.title,
                        record.url,
                        record.message,
                    ),
                )

        await self._call(insert)

    async def rebuild_fts(self) -> int:
        """Drop and rebuild the image-stripped FTS5 index in one transaction."""

        def rebuild() -> int:
            count = self._rebuild_fts_sync(self.connection)
            self._fts_active = True
            return count

        return await self._call(rebuild)

    async def search(
        self,
        query: str,
        *,
        year: int | None = None,
        type: str | None = None,
        situation: str | None = None,
        limit: int = 20,
    ) -> list[SearchHit]:
        """Search documents with BM25 ranking and snippets without loading full bodies."""
        return await self._call(
            lambda: _search_connection(
                self.connection,
                query,
                default_source=self.source,
                year=year,
                type=type,
                situation=situation,
                limit=limit,
            )
        )

    async def get_document(self, doc_id: int) -> dict[str, Any] | None:
        """Fetch a single document row by stable integer id."""

        def select() -> dict[str, Any] | None:
            row = self.connection.execute(
                "SELECT * FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
            return _row_to_dict(row, default_source=self.source) if row else None

        return await self._call(select)

    async def get_by_url(self, document_url: str) -> dict[str, Any] | None:
        """Fetch the first document matching document_url."""

        def select() -> dict[str, Any] | None:
            row = self.connection.execute(
                "SELECT * FROM documents WHERE document_url = ? ORDER BY id ASC LIMIT 1",
                (document_url,),
            ).fetchone()
            return _row_to_dict(row, default_source=self.source) if row else None

        return await self._call(select)

    async def summary(self) -> list[dict[str, Any]]:
        return await self._call(
            lambda: [
                dict(row)
                for row in self.connection.execute(
                    "SELECT year, type, situation, count(*) AS documents "
                    "FROM documents GROUP BY year, type, situation "
                    "ORDER BY year, type, situation"
                )
            ]
        )

    def _export_json_target(
        self, target: Path, fmt: str, sql: str, params: tuple[Any, ...]
    ) -> int:
        return _export_json_target(
            self.connection, target, fmt, sql, params, default_source=self.source
        )

    def _export_parquet(
        self,
        out_dir: Path,
        *,
        single_file: bool,
        year: int | None = None,
        batch_size: int = 2000,
    ) -> int:
        return _export_parquet_connection(
            self.connection,
            out_dir,
            default_source=self.source,
            single_file=single_file,
            year=year,
            batch_size=batch_size,
        )

    async def export(
        self,
        out_dir: str | Path,
        fmt: str = "jsonl",
        *,
        single_file: bool = False,
        year: int | None = None,
        batch_size: int = 2000,
    ) -> int:
        if fmt not in {"json", "jsonl", "parquet"}:
            raise ValueError("Export format must be json, jsonl, or parquet")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        out_path = Path(out_dir)

        if fmt == "parquet":
            return await self._call(
                lambda: _export_parquet_connection(
                    self.connection,
                    out_path,
                    default_source=self.source,
                    single_file=single_file,
                    year=year,
                    batch_size=batch_size,
                )
            )

        return await self._call(
            lambda: _export_json_connection(
                self.connection,
                out_path,
                fmt,
                default_source=self.source,
                single_file=single_file,
                year=year,
            )
        )

    async def import_json(self, directory: str | Path) -> tuple[int, int]:
        """Import JSON archives; returns (imported, rejected). Rejected rows go to errors."""

        def import_files() -> tuple[int, int]:
            imported = rejected = 0
            root = Path(directory)
            paths = sorted(root.glob("[0-9]*/data.json")) + sorted(
                root.glob("[0-9]*/shards/*.json")
            )
            for path in paths:
                rows = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(rows, list):
                    raise ValueError(f"Expected a JSON array of documents in {path}")
                with self.connection:
                    for row in rows:
                        row = dict(row)
                        ref_url = row.pop("ref_url", None)
                        ref_title = row.pop("ref_title", None)
                        row_source = row.pop("source", None)
                        try:
                            doc = Document.model_validate(row)
                        except ValidationError as exc:
                            rejected += 1
                            self.connection.execute(
                                "INSERT INTO errors(year, type, situation, title, url, message) "
                                "VALUES (?, ?, ?, ?, ?, ?)",
                                (
                                    row.get("year"),
                                    row.get("type"),
                                    row.get("situation"),
                                    row.get("title"),
                                    row.get("document_url"),
                                    f"import {path}: {exc}",
                                ),
                            )
                            continue
                        self._insert(
                            doc,
                            ref_url or doc.document_url,
                            clean_title(ref_title) if ref_title else doc.title,
                            row.get("file_path"),
                            source=(
                                str(row_source) if row_source is not None else None
                            ),
                        )
                        imported += 1
            return imported, rejected

        return await self._call(import_files)


_EXPORT_SUFFIX_TO_FMT: dict[str, str] = {
    ".parquet": "parquet",
    ".jsonl": "jsonl",
    ".json": "json",
}


def _export_json_target(
    connection: sqlite3.Connection,
    target: Path,
    fmt: str,
    sql: str,
    params: tuple[Any, ...],
    *,
    default_source: str = "",
) -> int:
    count = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            if fmt == "json":
                output.write("[\n")
            first = True
            for row in connection.execute(sql, params):
                data = dict(row)
                extra = json.loads(data.pop("extra"))
                if not data.get("source"):
                    if default_source:
                        data["source"] = default_source
                    else:
                        data.pop("source", None)
                if fmt == "json" and not first:
                    output.write(",\n")
                output.write(json.dumps({**extra, **data}, ensure_ascii=False))
                if fmt == "jsonl":
                    output.write("\n")
                first = False
                count += 1
            if fmt == "json":
                output.write("\n]\n")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return count


def _export_json_connection(
    connection: sqlite3.Connection,
    out_path: Path,
    fmt: str,
    *,
    default_source: str = "",
    single_file: bool = False,
    year: int | None = None,
) -> int:
    select_cols = (
        "ref_url, ref_title, source, document_url, title, year, "
        "type, situation, summary, text_markdown, file_path, extra"
    )
    if single_file:
        target = (
            out_path
            if out_path.suffix.lower() == f".{fmt}"
            else out_path / f"data.{fmt}"
        )
        if year is None:
            sql = (
                f"SELECT {select_cols} FROM documents ORDER BY year, ref_url, ref_title"
            )
            params: tuple[Any, ...] = ()
        else:
            sql = (
                f"SELECT {select_cols} FROM documents "
                "WHERE year=? ORDER BY ref_url, ref_title"
            )
            params = (year,)
        return _export_json_target(
            connection, target, fmt, sql, params, default_source=default_source
        )

    count = 0
    if year is None:
        years = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT year FROM documents ORDER BY year"
            )
        ]
    else:
        years = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT year FROM documents WHERE year=? ORDER BY year",
                (year,),
            )
        ]
    for yr in years:
        target = out_path / str(yr) / f"data.{fmt}"
        count += _export_json_target(
            connection,
            target,
            fmt,
            f"SELECT {select_cols} FROM documents WHERE year=? ORDER BY ref_url, ref_title",
            (yr,),
            default_source=default_source,
        )
    return count


def _export_parquet_connection(
    connection: sqlite3.Connection,
    out_dir: Path,
    *,
    default_source: str = "",
    single_file: bool = False,
    year: int | None = None,
    batch_size: int = 2000,
) -> int:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ImportError(
            "Install Parquet support with: pip install 'legisbr[parquet]'"
        ) from exc

    schema = pa.schema(
        [
            pa.field("id", pa.int64(), nullable=False),
            pa.field("source", pa.string(), nullable=False),
            pa.field("ref_url", pa.string(), nullable=False),
            pa.field("ref_title", pa.string(), nullable=False),
            pa.field("document_url", pa.string(), nullable=False),
            pa.field("title", pa.string(), nullable=False),
            pa.field("year", pa.int32(), nullable=False),
            pa.field("type", pa.string(), nullable=False),
            pa.field("situation", pa.string(), nullable=False),
            pa.field("summary", pa.string(), nullable=False),
            pa.field("text_markdown", pa.string(), nullable=False),
            pa.field("file_path", pa.string(), nullable=True),
            pa.field("extra_json", pa.string(), nullable=False),
        ]
    )

    select_cols = (
        "id, source, ref_url, ref_title, document_url, title, year, "
        "type, situation, summary, text_markdown, file_path, extra"
    )

    def write_query(target: Path, sql: str, params: tuple[Any, ...]) -> int:
        written = 0
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        try:
            with pq.ParquetWriter(temporary, schema, compression="zstd") as writer:
                cursor = connection.execute(sql, params)
                while True:
                    rows = cursor.fetchmany(batch_size)
                    if not rows:
                        break
                    columns = {
                        "id": [int(r["id"]) for r in rows],
                        "source": [str(r["source"] or default_source) for r in rows],
                        "ref_url": [str(r["ref_url"]) for r in rows],
                        "ref_title": [str(r["ref_title"]) for r in rows],
                        "document_url": [str(r["document_url"]) for r in rows],
                        "title": [str(r["title"]) for r in rows],
                        "year": [int(r["year"]) for r in rows],
                        "type": [str(r["type"]) for r in rows],
                        "situation": [str(r["situation"]) for r in rows],
                        "summary": [str(r["summary"] or "") for r in rows],
                        "text_markdown": [str(r["text_markdown"]) for r in rows],
                        "file_path": [
                            None if r["file_path"] is None else str(r["file_path"])
                            for r in rows
                        ],
                        "extra_json": [str(r["extra"] or "{}") for r in rows],
                    }
                    batch = pa.RecordBatch.from_pydict(columns, schema=schema)
                    writer.write_batch(batch)
                    written += len(rows)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return written

    if single_file:
        target = (
            out_dir
            if out_dir.suffix.lower() == ".parquet"
            else out_dir / "data.parquet"
        )
        if year is None:
            sql = (
                f"SELECT {select_cols} FROM documents ORDER BY year, ref_url, ref_title"
            )
            params: tuple[Any, ...] = ()
        else:
            sql = (
                f"SELECT {select_cols} FROM documents "
                "WHERE year=? ORDER BY ref_url, ref_title"
            )
            params = (year,)
        return write_query(target, sql, params)

    total = 0
    if year is None:
        years = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT year FROM documents ORDER BY year"
            )
        ]
    else:
        years = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT year FROM documents WHERE year=? ORDER BY year",
                (year,),
            )
        ]
    for yr in years:
        target = out_dir / str(yr) / "data.parquet"
        total += write_query(
            target,
            f"SELECT {select_cols} FROM documents WHERE year=? ORDER BY ref_url, ref_title",
            (yr,),
        )
    return total


def export_archive(
    source_or_db: Store | str | Path,
    out: str | Path,
    *,
    fmt: str | None = None,
    single_file: bool | None = None,
    year: int | None = None,
    batch_size: int = 2000,
) -> Path:
    """Synchronously export a SQLite legislation archive to Parquet, JSONL, or JSON.

    Automatically infers *fmt* from ``.parquet`` / ``.jsonl`` / ``.json`` suffixes on *out*
    and sets ``single_file=True`` when *out* has a known file extension.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    out_path = Path(out)
    inferred_fmt = _EXPORT_SUFFIX_TO_FMT.get(out_path.suffix.lower())
    if fmt is None:
        resolved_fmt = inferred_fmt or "jsonl"
    else:
        resolved_fmt = fmt.lower()
        if resolved_fmt not in {"json", "jsonl", "parquet"}:
            raise ValueError("Export format must be json, jsonl, or parquet")

    resolved_single = (inferred_fmt is not None) if single_file is None else single_file
    if resolved_single:
        result_path = (
            out_path
            if out_path.suffix.lower() == f".{resolved_fmt}"
            else out_path / f"data.{resolved_fmt}"
        )
    else:
        result_path = out_path

    borrowed_conn: sqlite3.Connection | None = None
    owned_conn: sqlite3.Connection | None = None
    default_source = ""

    if isinstance(source_or_db, Store):
        default_source = source_or_db.source
        if source_or_db._connection is not None:
            borrowed_conn = source_or_db._connection
        else:
            if source_or_db.path == ":memory:":
                raise RuntimeError("In-memory Store must be open to export")
            db_path = Path(source_or_db.path)
            if not db_path.exists():
                raise FileNotFoundError(db_path)
            from legisbr.core.archive import _open_readonly

            owned_conn, _ = _open_readonly(db_path)
    else:
        candidate = Path(source_or_db)
        if candidate.is_dir() and (candidate / "documents.db").is_file():
            db_path = candidate / "documents.db"
        elif candidate.is_file():
            db_path = candidate
        elif isinstance(source_or_db, str):
            from legisbr.registry import get_spec

            try:
                spec = get_spec(source_or_db)
            except ValueError as exc:
                raise FileNotFoundError(candidate) from exc
            db_path = spec.database_path(Path("outputs/legislation"))
            default_source = spec.name
            if not db_path.is_file():
                raise FileNotFoundError(db_path)
        else:
            raise FileNotFoundError(candidate)

        if (
            not default_source
            and db_path.name == "documents.db"
            and db_path.parent.name
        ):
            default_source = db_path.parent.name
        from legisbr.core.archive import _open_readonly

        owned_conn, _ = _open_readonly(db_path)

    conn = borrowed_conn if borrowed_conn is not None else owned_conn
    assert conn is not None
    try:
        if resolved_fmt == "parquet":
            _export_parquet_connection(
                conn,
                out_path,
                default_source=default_source,
                single_file=resolved_single,
                year=year,
                batch_size=batch_size,
            )
        else:
            _export_json_connection(
                conn,
                out_path,
                resolved_fmt,
                default_source=default_source,
                single_file=resolved_single,
                year=year,
            )
    finally:
        if owned_conn is not None:
            owned_conn.close()

    return result_path
