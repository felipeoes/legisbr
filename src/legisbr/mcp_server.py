"""Optional Model Context Protocol (MCP) server for querying Brazilian legislation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from legisbr.core.archive import ArchiveIndex
from legisbr.registry import get_spec
from legisbr.registry import sources as registry_sources

_LIVE_FETCH_DISABLED_MSG = (
    "Live fetching is disabled; start the server with --allow-live-fetch"
)
_MCP_INSTALL_HINT = "Install MCP support with: pip install 'legisbr[mcp]'"

_MAX_SEARCH_LIMIT = 50
_MAX_LIVE_SEARCH_LIMIT = 20


def _load_mcp_server_class() -> tuple[type[Any], int]:
    """Lazy-import MCP server class supporting both mcp 2.x and mcp 1.x."""
    try:
        from mcp.server.mcpserver import MCPServer

        return MCPServer, 2
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP

        return FastMCP, 1
    except ImportError as exc:
        raise ImportError(_MCP_INSTALL_HINT) from exc


def _default_client_factory() -> Any:
    from legisbr.client import Client

    return Client()


def _truncate_markdown(doc: dict[str, Any], *, max_chars: int | None) -> dict[str, Any]:
    result = dict(doc)
    text = str(result.get("text_markdown", ""))
    total_chars = len(text)
    if max_chars is not None and max_chars >= 0 and total_chars > max_chars:
        result["text_markdown"] = text[:max_chars]
        result["truncated"] = True
    else:
        result["truncated"] = False
    result["total_chars"] = total_chars
    return result


def _resolve_archive_source(source: str, available: dict[str, str]) -> str:
    """Resolve canonical archive folder name from code, alias, or exact name."""
    lowered = source.strip().lower()
    if lowered in available:
        return available[lowered]
    try:
        spec = get_spec(source)
    except ValueError:
        return source
    for alias in spec.names:
        if alias in available:
            return available[alias]
    return source


def create_mcp_server(
    data_dir: str | Path,
    *,
    allow_live_fetch: bool = False,
    client_factory: Callable[[], Any] | None = None,
) -> Any:
    """Build a configured MCP server exposing local archive and optional live tools."""
    server_cls, major = _load_mcp_server_class()
    index = ArchiveIndex(data_dir)
    make_client = client_factory or _default_client_factory

    instructions = (
        "Query archived Brazilian federal, state, and environmental legislation "
        "stored in local SQLite FTS5 databases. Use list_sources to inspect "
        "jurisdictions and archive coverage, search_legislation for BM25 full-text "
        "search with snippets, and get_legislation to fetch a specific norm."
    )
    if major >= 2:
        server = server_cls("legisbr", instructions=instructions)
    else:
        server = server_cls("legisbr", instructions=instructions)

    async def _available_archive_map() -> dict[str, str]:
        return {
            entry["source"].lower(): entry["source"] for entry in await index.sources()
        }

    @server.tool()
    async def list_sources() -> list[dict[str, Any]]:
        """List all 30 registered Brazilian legislation sources with local archive counts."""
        archived_entries = await index.sources()
        archived_by_key: dict[str, dict[str, Any]] = {}
        for entry in archived_entries:
            key = entry["source"].lower()
            archived_by_key[key] = entry
            try:
                spec = get_spec(entry["source"])
            except ValueError:
                continue
            archived_by_key[spec.name.lower()] = entry

        results: list[dict[str, Any]] = []
        for info in registry_sources():
            matched = archived_by_key.get(info.name.lower()) or archived_by_key.get(
                info.code.lower()
            )
            results.append(
                {
                    "code": info.code,
                    "name": info.name,
                    "aliases": list(info.aliases),
                    "scope": info.scope,
                    "start_year": info.start_year,
                    "uses_pdf": info.uses_pdf,
                    "capabilities": asdict(info.capabilities),
                    "archived_documents": matched["documents"] if matched else 0,
                    "fts_enabled": matched["fts_enabled"] if matched else False,
                    "min_year": matched["min_year"] if matched else None,
                    "max_year": matched["max_year"] if matched else None,
                }
            )
        return results

    @server.tool()
    async def search_legislation(
        query: str,
        sources: list[str] | None = None,
        year: int | None = None,
        doc_type: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """BM25 full-text search across local legislation archives (metadata + snippets only)."""
        capped_limit = max(1, min(int(limit), _MAX_SEARCH_LIMIT))
        resolved_sources: list[str] | None = None
        if sources is not None:
            available = await _available_archive_map()
            resolved_sources = [
                _resolve_archive_source(src, available) for src in sources
            ]
        hits = await index.query(
            query,
            sources=resolved_sources,
            year=year,
            doc_type=doc_type,
            limit=capped_limit,
        )
        return [asdict(hit) for hit in hits]

    @server.tool()
    async def get_legislation(
        source: str,
        doc_id: int,
        max_chars: int = 50000,
    ) -> dict[str, Any] | None:
        """Retrieve a full archived norm by source and integer id, truncating safely at max_chars."""
        available = await _available_archive_map()
        resolved = _resolve_archive_source(source, available)
        doc = await index.get_document(resolved, int(doc_id))
        if doc is None:
            return None
        return _truncate_markdown(doc, max_chars=max_chars)

    @server.tool()
    async def live_search_norms(
        source: str,
        year: int | None = None,
        number: str | None = None,
        doc_type: str | None = None,
        text: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Query an official legislation portal live using strict native server-side filters."""
        if not allow_live_fetch:
            raise PermissionError(_LIVE_FETCH_DISABLED_MSG)
        capped_limit = max(1, min(int(limit), _MAX_LIVE_SEARCH_LIMIT))
        refs: list[dict[str, Any]] = []
        async with make_client() as client:
            async for ref in client.search_refs(
                source,
                years=year,
                number=number,
                type=doc_type,
                text=text,
                limit=capped_limit,
                strict_native=True,
            ):
                refs.append(dict(ref))
                if len(refs) >= capped_limit:
                    break
        return refs

    @server.tool()
    async def live_fetch_norm(
        source: str,
        ref: dict[str, Any],
        max_chars: int = 50000,
    ) -> dict[str, Any]:
        """Fetch and convert a single norm reference live from an official legislation portal."""
        if not allow_live_fetch:
            raise PermissionError(_LIVE_FETCH_DISABLED_MSG)
        async with make_client() as client:
            doc = await client.fetch_ref(source, dict(ref))
        if hasattr(doc, "model_dump"):
            payload = doc.model_dump(exclude={"raw", "raw_ext"})
        else:
            payload = {
                k: v for k, v in dict(doc).items() if k not in {"raw", "raw_ext"}
            }
        return _truncate_markdown(payload, max_chars=max_chars)

    return server


def run_mcp_server(
    data_dir: str | Path,
    *,
    allow_live_fetch: bool = False,
    transport: Literal["stdio", "streamable-http"] = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Create and run the `legisbr` MCP server on stdio or streamable-http."""
    if transport not in {"stdio", "streamable-http"}:
        raise ValueError("transport must be 'stdio' or 'streamable-http'")
    _, major = _load_mcp_server_class()
    server = create_mcp_server(data_dir, allow_live_fetch=allow_live_fetch)
    if transport == "stdio":
        server.run(transport="stdio")
        return
    if major >= 2:
        server.run(transport="streamable-http", host=host, port=port)
        return
    if hasattr(server, "settings"):
        server.settings.host = host
        server.settings.port = port
    server.run(transport="streamable-http")
