"""Read-only multi-archive discovery, cross-jurisdiction FTS5 query, and stats."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self, TypeVar

from legisbr.core.store import (
    SearchHit,
    _check_fts5_available,
    _has_table,
    _row_to_dict,
    _search_connection,
)
from legisbr.registry import SourceInput, get_spec

_T = TypeVar("_T")
_MAX_FANOUT_WORKERS = 8

_IMMUTABLE_FALLBACK_CODES: frozenset[int] = frozenset(
    {
        sqlite3.SQLITE_PERM,
        sqlite3.SQLITE_READONLY,
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_CANTOPEN,
    }
)
_IMMUTABLE_FALLBACK_MESSAGES: tuple[str, ...] = (
    "readonly",
    "read-only",
    "unable to open",
    "permission denied",
    "disk i/o error",
    "i/o error",
)


def _should_fallback_immutable(exc: sqlite3.OperationalError) -> bool:
    """Allow immutable=1 only on readonly/cantopen/perm/ioerr errors, never BUSY/LOCKED."""
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return (code & 0xFF) in _IMMUTABLE_FALLBACK_CODES
    msg = str(exc).lower()
    if "locked" in msg or "busy" in msg:
        return False
    return any(token in msg for token in _IMMUTABLE_FALLBACK_MESSAGES)


def _source_name_for(path: Path) -> str:
    if path.name == "documents.db" and path.parent.name:
        return path.parent.name
    return path.stem


def _expand_source_aliases(sources: Sequence[SourceInput]) -> set[str]:
    """Expand source names, codes, and aliases via the registry for archive filtering."""
    wanted: set[str] = set()
    for raw in sources:
        token = raw.strip().lower()
        if not token:
            continue
        wanted.add(token)
        try:
            spec = get_spec(raw)
        except ValueError:
            continue
        wanted.add(spec.name.lower())
        wanted.add(spec.code.lower())
        wanted.update(spec.names)
    return wanted


def _open_readonly(path: Path) -> tuple[sqlite3.Connection, bool]:
    """Open SQLite read-only, falling back to immutable=1 on read-only WAL mounts."""
    resolved = path.resolve()
    base_uri = resolved.as_uri()
    uri_ro = f"{base_uri}?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            uri_ro, uri=True, timeout=5.0, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        # WAL databases only try to create -shm/-wal on the first read query.
        connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return connection, False
    except sqlite3.OperationalError as exc:
        if connection is not None:
            connection.close()
        if not resolved.exists() or not _should_fallback_immutable(exc):
            raise
    uri_immutable = f"{base_uri}?immutable=1"
    connection = sqlite3.connect(
        uri_immutable, uri=True, timeout=5.0, check_same_thread=False
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except BaseException:
        connection.close()
        raise
    return connection, True


def _read_meta(connection: sqlite3.Connection) -> dict[str, str]:
    if not _has_table(connection, "meta"):
        return {}
    return {
        str(row["key"]): str(row["value"])
        for row in connection.execute("SELECT key, value FROM meta")
    }


def _db_signature(resolved: Path) -> tuple[int, int, int, int] | None:
    """Return ``(mtime_ns, size, wal_mtime_ns, wal_size)`` or ``None`` if missing."""
    try:
        st = os.stat(resolved)
    except OSError:
        return None
    try:
        wal_st = os.stat(f"{resolved}-wal")
        return (st.st_mtime_ns, st.st_size, wal_st.st_mtime_ns, wal_st.st_size)
    except OSError:
        return (st.st_mtime_ns, st.st_size, 0, 0)


@dataclass
class _CachedArchive:
    """Cached read-only SQLite handle guarded by a per-archive lock."""

    signature: tuple[int, int, int, int]
    conn: sqlite3.Connection
    immutable: bool
    has_fts: bool
    has_docs: bool
    fts5_checked: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False


def _close_cached_entry(entry: _CachedArchive) -> None:
    with entry.lock:
        if not entry.closed:
            entry.closed = True
            try:
                entry.conn.close()
            except sqlite3.Error:
                pass


def _close_all_cached(
    cache: dict[Path, _CachedArchive],
    lock: threading.Lock,
) -> None:
    with lock:
        entries = list(cache.values())
        cache.clear()
    for entry in entries:
        _close_cached_entry(entry)


class ArchiveIndex:
    """Synchronous read-only index over one or more SQLite legislation archives."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        databases: Sequence[str | Path] | Mapping[str, str | Path] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self._explicit: dict[str, Path] = {}
        if databases is not None:
            if isinstance(databases, Mapping):
                for name, path in databases.items():
                    self._explicit[str(name)] = Path(path)
            else:
                for raw_path in databases:
                    path = Path(raw_path)
                    self._explicit[_source_name_for(path)] = path
        cache: dict[Path, _CachedArchive] = {}
        cache_lock = threading.Lock()
        self._conn_cache: dict[Path, _CachedArchive] = cache
        self._cache_lock: threading.Lock = cache_lock
        self._finalizer = weakref.finalize(self, _close_all_cached, cache, cache_lock)

    def close(self) -> None:
        """Close and release all cached read-only SQLite connections."""
        _close_all_cached(self._conn_cache, self._cache_lock)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def _checkout(self, db_path: Path) -> Iterator[_CachedArchive]:
        """Borrow a cached read-only connection, re-opening on external DB changes."""
        resolved = db_path.resolve()
        while True:
            sig = _db_signature(resolved)
            stale: _CachedArchive | None = None
            with self._cache_lock:
                entry = self._conn_cache.get(resolved)
                if entry is not None and (sig is None or entry.signature != sig):
                    stale = self._conn_cache.pop(resolved)
                    entry = None
            if stale is not None:
                _close_cached_entry(stale)

            if entry is None:
                conn, immutable = _open_readonly(resolved)
                try:
                    tables = {
                        str(row[0])
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='table' AND name IN ('documents', 'documents_fts')"
                        )
                    }
                    has_fts = "documents_fts" in tables
                    has_docs = "documents" in tables
                except BaseException:
                    conn.close()
                    raise
                fresh_sig = _db_signature(resolved)
                if fresh_sig is None:
                    ephemeral = _CachedArchive(
                        signature=(0, 0, 0, 0),
                        conn=conn,
                        immutable=immutable,
                        has_fts=has_fts,
                        has_docs=has_docs,
                    )
                    try:
                        yield ephemeral
                    finally:
                        _close_cached_entry(ephemeral)
                    return

                new_entry = _CachedArchive(
                    signature=fresh_sig,
                    conn=conn,
                    immutable=immutable,
                    has_fts=has_fts,
                    has_docs=has_docs,
                )
                replaced: _CachedArchive | None = None
                with self._cache_lock:
                    existing = self._conn_cache.get(resolved)
                    if existing is not None and existing.signature == fresh_sig:
                        replaced = new_entry
                        entry = existing
                    else:
                        replaced = existing
                        self._conn_cache[resolved] = new_entry
                        entry = new_entry
                if replaced is not None:
                    _close_cached_entry(replaced)

            with entry.lock:
                if entry.closed:
                    continue
                yield entry
                return

    @staticmethod
    def _map_targets(
        fn: Callable[[tuple[str, Path]], _T],
        targets: list[tuple[str, Path]],
    ) -> list[_T]:
        if len(targets) <= 1:
            return [fn(target) for target in targets]
        with ThreadPoolExecutor(
            max_workers=min(_MAX_FANOUT_WORKERS, len(targets))
        ) as pool:
            return list(pool.map(fn, targets))

    def _discover(self) -> dict[str, Path]:
        discovered: dict[str, Path] = {}
        if self.data_dir is not None and self.data_dir.exists():
            if self.data_dir.is_file():
                discovered[_source_name_for(self.data_dir)] = self.data_dir
            else:
                for db_path in sorted(self.data_dir.glob("*/documents.db")):
                    if db_path.is_file():
                        discovered[_source_name_for(db_path)] = db_path
        discovered.update(self._explicit)
        return dict(sorted(discovered.items()))

    def _resolve_source(self, source: SourceInput) -> tuple[str, Path] | None:
        archives = self._discover()
        if source in archives:
            return source, archives[source]
        by_lower = {name.lower(): (name, path) for name, path in archives.items()}
        target = source.strip().lower()
        if target in by_lower:
            return by_lower[target]
        try:
            spec = get_spec(source)
        except ValueError:
            return None
        for alias in (spec.name.lower(), spec.code.lower(), *spec.names):
            if alias in by_lower:
                return by_lower[alias]
        return None

    def _inspect_source(self, target: tuple[str, Path]) -> dict[str, Any]:
        name, db_path = target
        with self._checkout(db_path) as entry:
            conn = entry.conn
            meta = _read_meta(conn)
            if entry.has_docs:
                row = conn.execute(
                    "SELECT count(*) AS total, min(year) AS min_year, max(year) AS max_year "
                    "FROM documents"
                ).fetchone()
                total = int(row["total"]) if row else 0
                min_year = row["min_year"] if row else None
                max_year = row["max_year"] if row else None
            else:
                total = 0
                min_year = None
                max_year = None
            return {
                "source": name,
                "path": str(db_path),
                "documents": total,
                "fts_enabled": entry.has_fts,
                "schema_version": meta.get("schema_version", "2"),
                "min_year": min_year,
                "max_year": max_year,
                "immutable": entry.immutable,
            }

    def sources(self) -> list[dict[str, Any]]:
        """List discovered archives with document counts and FTS5 status."""
        targets = list(self._discover().items())
        return self._map_targets(self._inspect_source, targets)

    def query(
        self,
        text: str,
        *,
        sources: Sequence[SourceInput] | None = None,
        year: int | None = None,
        doc_type: str | None = None,
        limit: int = 20,
    ) -> list[SearchHit]:
        """Search across archives and merge BM25-ranked hits."""
        if limit <= 0 or not text.strip():
            return []
        wanted = _expand_source_aliases(sources) if sources is not None else None
        targets = [
            (name, db_path)
            for name, db_path in self._discover().items()
            if wanted is None or name.lower() in wanted
        ]
        if not targets:
            return []

        def _query_one(target: tuple[str, Path]) -> list[SearchHit]:
            name, db_path = target
            with self._checkout(db_path) as entry:
                if not entry.fts5_checked:
                    _check_fts5_available(entry.conn)
                    entry.fts5_checked = True
                if not entry.has_fts:
                    return []
                return _search_connection(
                    entry.conn,
                    text,
                    default_source=name,
                    year=year,
                    type=doc_type,
                    limit=limit,
                    check_fts=False,
                )

        hits: list[SearchHit] = []
        for part in self._map_targets(_query_one, targets):
            hits.extend(part)
        hits.sort(key=lambda hit: (hit.rank, hit.source, hit.id))
        return hits[:limit]

    def get(self, source: SourceInput, doc_id: int) -> dict[str, Any] | None:
        """Fetch a single full document by archive source (``Source``, name, code, or alias) and id."""
        resolved = self._resolve_source(source)
        if resolved is None:
            return None
        name, db_path = resolved
        with self._checkout(db_path) as entry:
            if not entry.has_docs:
                return None
            row = entry.conn.execute(
                "SELECT * FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
            return _row_to_dict(row, default_source=name) if row else None

    def _inspect_stats(self, target: tuple[str, Path]) -> dict[str, Any]:
        name, db_path = target
        with self._checkout(db_path) as entry:
            conn = entry.conn
            meta = _read_meta(conn)
            if entry.has_docs:
                row = conn.execute(
                    "SELECT count(*) AS total, min(year) AS min_year, max(year) AS max_year "
                    "FROM documents"
                ).fetchone()
                total = int(row["total"]) if row else 0
                min_year = row["min_year"] if row else None
                max_year = row["max_year"] if row else None
                by_year = [
                    {"year": int(r["year"]), "documents": int(r["documents"])}
                    for r in conn.execute(
                        "SELECT year, count(*) AS documents FROM documents "
                        "GROUP BY year ORDER BY year"
                    )
                ]
            else:
                total = 0
                min_year = None
                max_year = None
                by_year = []

            error_count = 0
            if _has_table(conn, "errors"):
                err_row = conn.execute(
                    "SELECT count(*) AS total FROM errors"
                ).fetchone()
                error_count = int(err_row["total"]) if err_row else 0

            last_run: dict[str, Any] | None = None
            if _has_table(conn, "runs"):
                run_row = conn.execute(
                    "SELECT id, scraper, started_at, finished_at, docs, errors, "
                    "llm_usage, stats FROM runs ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if run_row is not None:
                    last_run = {
                        "id": int(run_row["id"]),
                        "scraper": str(run_row["scraper"]),
                        "started_at": run_row["started_at"],
                        "finished_at": run_row["finished_at"],
                        "docs": int(run_row["docs"]),
                        "errors": int(run_row["errors"]),
                        "llm_usage": json.loads(run_row["llm_usage"] or "{}"),
                        "stats": json.loads(run_row["stats"] or "{}"),
                    }

            return {
                "source": name,
                "path": str(db_path),
                "documents": total,
                "fts_enabled": entry.has_fts,
                "schema_version": meta.get("schema_version", "2"),
                "min_year": min_year,
                "max_year": max_year,
                "errors": error_count,
                "by_year": by_year,
                "last_run": last_run,
                "immutable": entry.immutable,
            }

    def stats(self) -> list[dict[str, Any]]:
        """Return archive summary statistics without loading document bodies."""
        targets = list(self._discover().items())
        return self._map_targets(self._inspect_stats, targets)


class AsyncArchiveIndex:
    """Async read-only wrapper around ``ArchiveIndex`` executing SQLite I/O off-loop."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        databases: Sequence[str | Path] | Mapping[str, str | Path] | None = None,
    ) -> None:
        self._index = ArchiveIndex(data_dir, databases=databases)

    @property
    def data_dir(self) -> Path | None:
        return self._index.data_dir

    async def aclose(self) -> None:
        """Close and release all cached read-only SQLite connections."""
        await asyncio.to_thread(self._index.close)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def _discover(self) -> dict[str, Path]:
        return self._index._discover()

    async def sources(self) -> list[dict[str, Any]]:
        """List discovered archives with document counts and FTS5 status."""
        return await asyncio.to_thread(self._index.sources)

    async def query(
        self,
        text: str,
        *,
        sources: Sequence[SourceInput] | None = None,
        year: int | None = None,
        doc_type: str | None = None,
        limit: int = 20,
    ) -> list[SearchHit]:
        """Search across archives and merge BM25-ranked hits."""
        return await asyncio.to_thread(
            self._index.query,
            text,
            sources=sources,
            year=year,
            doc_type=doc_type,
            limit=limit,
        )

    async def get(self, source: SourceInput, doc_id: int) -> dict[str, Any] | None:
        """Fetch a single full document by archive source (``Source``, name, code, or alias) and id."""
        return await asyncio.to_thread(self._index.get, source, doc_id)

    async def stats(self) -> list[dict[str, Any]]:
        """Return archive summary statistics without loading document bodies."""
        return await asyncio.to_thread(self._index.stats)


def query(
    text: str,
    *,
    data_dir: str | Path = "outputs/legislation",
    sources: Sequence[SourceInput] | None = None,
    year: int | None = None,
    doc_type: str | None = None,
    limit: int = 20,
) -> list[SearchHit]:
    """Synchronously query local SQLite FTS5 legislation archives with BM25 ranking."""
    with ArchiveIndex(data_dir) as index:
        return index.query(
            text,
            sources=sources,
            year=year,
            doc_type=doc_type,
            limit=limit,
        )
