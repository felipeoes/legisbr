"""Read-only multi-archive discovery, cross-jurisdiction FTS5 query, and stats."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote

from legisbr.core.store import (
    SearchHit,
    _check_fts5_available,
    _has_table,
    _row_to_dict,
    _search_connection,
)


def _source_name_for(path: Path) -> str:
    if path.name == "documents.db" and path.parent.name:
        return path.parent.name
    return path.stem


def _open_readonly(path: Path) -> tuple[sqlite3.Connection, bool]:
    """Open SQLite read-only, falling back to immutable=1 on read-only WAL mounts."""
    resolved = path.resolve()
    encoded = quote(resolved.as_posix(), safe="/:")
    uri_ro = f"file:{encoded}?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri_ro, uri=True, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        # WAL databases only try to create -shm/-wal on the first read query.
        connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return connection, False
    except sqlite3.OperationalError:
        if connection is not None:
            connection.close()
        if not resolved.exists():
            raise
    uri_immutable = f"file:{encoded}?immutable=1"
    connection = sqlite3.connect(uri_immutable, uri=True, check_same_thread=False)
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


class ArchiveIndex:
    """Read-only index over one or more SQLite legislation archives."""

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

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

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

    def _resolve_source(self, source: str) -> tuple[str, Path] | None:
        archives = self._discover()
        if source in archives:
            return source, archives[source]
        target = source.lower()
        for name, path in archives.items():
            if name.lower() == target:
                return name, path
        return None

    async def sources(self) -> list[dict[str, Any]]:
        """List discovered archives with document counts and FTS5 status."""

        def collect() -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            for name, db_path in self._discover().items():
                conn, immutable = _open_readonly(db_path)
                try:
                    meta = _read_meta(conn)
                    has_docs = _has_table(conn, "documents")
                    if has_docs:
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
                    fts_ready = _has_table(conn, "documents_fts")
                    results.append(
                        {
                            "source": name,
                            "path": str(db_path),
                            "documents": total,
                            "fts_enabled": fts_ready,
                            "schema_version": meta.get("schema_version", "1"),
                            "min_year": min_year,
                            "max_year": max_year,
                            "immutable": immutable,
                        }
                    )
                finally:
                    conn.close()
            return results

        return await asyncio.to_thread(collect)

    async def query(
        self,
        text: str,
        *,
        sources: Sequence[str] | None = None,
        year: int | None = None,
        doc_type: str | None = None,
        limit: int = 20,
    ) -> list[SearchHit]:
        """Search across archives and merge BM25-ranked hits."""
        if limit <= 0 or not text.strip():
            return []
        wanted = {s.lower() for s in sources} if sources is not None else None

        def run_query() -> list[SearchHit]:
            hits: list[SearchHit] = []
            for name, db_path in self._discover().items():
                if wanted is not None and name.lower() not in wanted:
                    continue
                conn, _ = _open_readonly(db_path)
                try:
                    _check_fts5_available(conn)
                    if not _has_table(conn, "documents_fts"):
                        continue
                    hits.extend(
                        _search_connection(
                            conn,
                            text,
                            default_source=name,
                            year=year,
                            type=doc_type,
                            limit=limit,
                        )
                    )
                finally:
                    conn.close()
            hits.sort(key=lambda hit: (hit.rank, hit.source, hit.id))
            return hits[:limit]

        return await asyncio.to_thread(run_query)

    async def get_document(self, source: str, doc_id: int) -> dict[str, Any] | None:
        """Fetch a single full document by archive source name and integer id."""

        def fetch() -> dict[str, Any] | None:
            resolved = self._resolve_source(source)
            if resolved is None:
                return None
            name, db_path = resolved
            conn, _ = _open_readonly(db_path)
            try:
                if not _has_table(conn, "documents"):
                    return None
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(documents)")
                }
                id_col = "id" if "id" in columns else "rowid"
                row = conn.execute(
                    f"SELECT * FROM documents WHERE {id_col} = ?",
                    (doc_id,),
                ).fetchone()
                return _row_to_dict(row, default_source=name) if row else None
            finally:
                conn.close()

        return await asyncio.to_thread(fetch)

    async def stats(self) -> list[dict[str, Any]]:
        """Return archive summary statistics without loading document bodies."""

        def collect() -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            for name, db_path in self._discover().items():
                conn, immutable = _open_readonly(db_path)
                try:
                    meta = _read_meta(conn)
                    has_docs = _has_table(conn, "documents")
                    if has_docs:
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
                        run_cols = {
                            r[1] for r in conn.execute("PRAGMA table_info(runs)")
                        }
                        stats_col = "stats" if "stats" in run_cols else "'{}' AS stats"
                        run_row = conn.execute(
                            f"SELECT id, scraper, started_at, finished_at, docs, errors, "
                            f"llm_usage, {stats_col} FROM runs ORDER BY id DESC LIMIT 1"
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

                    results.append(
                        {
                            "source": name,
                            "path": str(db_path),
                            "documents": total,
                            "fts_enabled": _has_table(conn, "documents_fts"),
                            "schema_version": meta.get("schema_version", "1"),
                            "min_year": min_year,
                            "max_year": max_year,
                            "errors": error_count,
                            "by_year": by_year,
                            "last_run": last_run,
                            "immutable": immutable,
                        }
                    )
                finally:
                    conn.close()
            return results

        return await asyncio.to_thread(collect)
