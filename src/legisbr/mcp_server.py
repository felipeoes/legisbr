"""Optional Model Context Protocol (MCP) server for querying Brazilian legislation."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import secrets
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

from legisbr.core.archive import ArchiveIndex
from legisbr.registry import ScraperSpec, get_spec
from legisbr.registry import sources as registry_sources

_LIVE_FETCH_DISABLED_MSG = (
    "Live fetching is disabled; start the server with --allow-live-fetch"
)
_MCP_INSTALL_HINT = "Install MCP support with: pip install 'legisbr[mcp]'"

_MAX_SEARCH_LIMIT = 50
_MAX_LIVE_SEARCH_LIMIT = 20
_MAX_CHARS = 200_000
_MAX_CACHED_REFS = 512

_URL_KEY_TOKENS: tuple[str, ...] = ("url", "link", "href", "uri", "src", "endpoint")
_URI_SCHEME_RE = re.compile(
    r"^(?:https?|file|ftp|ftps|gopher|data|javascript|dict|ldap|ldaps|sftp|tftp|ssh|ws|wss):",
    re.IGNORECASE,
)

# Official portal domain suffixes per registered source code (verified against each
# scraper's base_url and live document links, preventing SSRF and cross-domain hijacks).
_SOURCE_ALLOWED_DOMAINS: dict[str, tuple[str, ...]] = {
    "camara": ("camara.leg.br",),
    "conama": ("conama.mma.gov.br",),
    "icmbio": (
        "in.gov.br",
        "icmbio.gov.br",
        "app.powerbi.com",
        "wabi-brazil-south-api.analysis.windows.net",
    ),
    "ac": ("legis.ac.gov.br",),
    "al": ("sefaz.al.gov.br",),
    "am": ("imprensaoficial.am.gov.br",),
    "ap": ("al.ap.leg.br",),
    "ba": ("legislabahia.ba.gov.br",),
    "ce": ("al.ce.gov.br",),
    "df": ("sinj.df.gov.br",),
    "es": ("al.es.gov.br",),
    "go": ("casacivil.go.gov.br",),
    "ma": ("al.ma.leg.br",),
    "ms": ("aacpdappls.net.ms.gov.br",),
    "mt": ("al.mt.gov.br",),
    "mg": ("almg.gov.br",),
    "pa": ("alepa.pa.gov.br",),
    "pb": ("al.pb.leg.br",),
    "pr": ("legislacao.pr.gov.br",),
    "pe": ("alepe.pe.gov.br",),
    "pi": ("al.pi.leg.br",),
    "rj": ("alerj.rj.gov.br",),
    "rn": ("al.rn.leg.br",),
    "rs": ("al.rs.gov.br",),
    "ro": ("casacivil.ro.gov.br",),
    "rr": ("al.rr.leg.br",),
    "sc": ("alesc.sc.gov.br",),
    "sp": ("al.sp.gov.br", "imprensaoficial.com.br"),
    "se": ("legislacao.se.gov.br",),
    "to": ("al.to.leg.br",),
}


def _allowed_domains_for_source(spec: ScraperSpec) -> tuple[str, ...]:
    """Return allowed official portal domain suffixes for *spec*."""
    domains = _SOURCE_ALLOWED_DOMAINS.get(spec.code)
    if domains:
        return domains
    base_url = getattr(spec.load_cls(), "base_url", "")
    host = (urlparse(base_url).hostname or "").rstrip(".").lower()
    if not host:
        raise ValueError(
            f"No allowed official domain configured for source {spec.code!r}"
        )
    return (host,)


def _validate_live_url(
    raw_url: str,
    *,
    spec: ScraperSpec,
    allowed_domains: tuple[str, ...],
) -> None:
    """Reject non-HTTP(S), private/local/metadata IPs, and off-domain URLs."""
    if not raw_url or raw_url != raw_url.strip():
        raise ValueError("Invalid URL in ref")
    if "\\" in raw_url or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw_url):
        raise ValueError("Invalid control characters or backslash in ref URL")
    if raw_url.startswith("//"):
        raise ValueError("Protocol-relative URLs are not allowed in ref")

    parsed = urlparse(raw_url)
    if not parsed.scheme:
        if parsed.netloc:
            raise ValueError("URL in ref is missing scheme")
        base_url = getattr(spec.load_cls(), "base_url", "")
        if not base_url:
            raise ValueError("Relative URL in ref cannot be resolved for this source")
        resolved = urljoin(base_url, raw_url)
        parsed = urlparse(resolved)

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"Disallowed URL scheme in ref: {scheme!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL userinfo credentials are not allowed in ref")

    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise ValueError("URL in ref is missing hostname")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError(f"Local/internal hostname is not allowed in ref: {host!r}")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
            or not ip.is_global
        ):
            raise ValueError(
                f"Private, loopback, or link-local IP address is not allowed in ref: {host!r}"
            )
        raise ValueError(f"IP literal host is not allowed in ref: {host!r}")

    if not any(
        host == domain or host.endswith(f".{domain}") for domain in allowed_domains
    ):
        raise ValueError(
            f"URL host {host!r} is not an allowed official portal domain for "
            f"{spec.name} ({spec.code})"
        )


def _validate_ref_urls(
    value: Any,
    *,
    spec: ScraperSpec,
    allowed_domains: tuple[str, ...],
    is_url_key: bool = False,
) -> None:
    """Recursively validate every URL field inside a live-fetch *ref* payload."""
    if isinstance(value, dict):
        for key, item in value.items():
            key_lower = str(key).lower()
            child_is_url = any(tok in key_lower for tok in _URL_KEY_TOKENS)
            _validate_ref_urls(
                item,
                spec=spec,
                allowed_domains=allowed_domains,
                is_url_key=child_is_url,
            )
        return
    if isinstance(value, list | tuple):
        for item in value:
            _validate_ref_urls(
                item,
                spec=spec,
                allowed_domains=allowed_domains,
                is_url_key=is_url_key,
            )
        return
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return
        if (
            is_url_key
            or "://" in stripped
            or stripped.startswith("//")
            or _URI_SCHEME_RE.match(stripped)
        ):
            _validate_live_url(value, spec=spec, allowed_domains=allowed_domains)


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


def _clamp_max_chars(max_chars: int) -> int:
    return max(1, min(int(max_chars), _MAX_CHARS))


def _truncate_markdown(doc: dict[str, Any], *, max_chars: int | None) -> dict[str, Any]:
    result = dict(doc)
    text = str(result.get("text_markdown", ""))
    total_chars = len(text)
    if max_chars is not None:
        capped = _clamp_max_chars(max_chars)
        if total_chars > capped:
            result["text_markdown"] = text[:capped]
            result["truncated"] = True
        else:
            result["truncated"] = False
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
    server_cls, _ = _load_mcp_server_class()
    index = ArchiveIndex(data_dir)
    make_client = client_factory or _default_client_factory

    shared_client: Any | None = None
    client_lock = asyncio.Lock()
    ref_cache: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()

    async def _get_live_client() -> Any:
        nonlocal shared_client
        if shared_client is not None:
            return shared_client
        async with client_lock:
            if shared_client is None:
                client = make_client()
                await client.__aenter__()
                shared_client = client
            return shared_client

    async def _close_live_client() -> None:
        nonlocal shared_client
        async with client_lock:
            client = shared_client
            shared_client = None
        if client is not None:
            await client.__aexit__(None, None, None)

    @asynccontextmanager
    async def _lifespan(_server: Any) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await _close_live_client()

    instructions = (
        "Query archived Brazilian federal, state, and environmental legislation "
        "stored in local SQLite FTS5 databases. Use list_sources to inspect "
        "jurisdictions and archive coverage, search_legislation for BM25 full-text "
        "search with snippets, and get_legislation to fetch a specific norm."
    )
    server = server_cls("legisbr", instructions=instructions, lifespan=_lifespan)
    # Expose explicit async cleanup hook for callers/tests invoking tools outside run().
    server.aclose = _close_live_client

    def _available_archive_map() -> dict[str, str]:
        return {name.lower(): name for name in index._discover()}

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
            available = _available_archive_map()
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
        capped_chars = _clamp_max_chars(max_chars)
        available = _available_archive_map()
        resolved = _resolve_archive_source(source, available)
        doc = await index.get_document(resolved, int(doc_id))
        if doc is None:
            return None
        return _truncate_markdown(doc, max_chars=capped_chars)

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
        spec = get_spec(source)
        capped_limit = max(1, min(int(limit), _MAX_LIVE_SEARCH_LIMIT))
        refs: list[dict[str, Any]] = []
        client = await _get_live_client()
        async for ref in client.search_refs(
            source,
            years=year,
            number=number,
            type=doc_type,
            text=text,
            limit=capped_limit,
            strict_native=True,
        ):
            stored = dict(ref)
            token = secrets.token_urlsafe(16)
            ref_cache[token] = (spec.code, stored)
            while len(ref_cache) > _MAX_CACHED_REFS:
                ref_cache.popitem(last=False)
            refs.append({**stored, "_ref_id": token})
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
        spec = get_spec(source)
        if spec.capabilities.heavy_setup:
            raise ValueError(
                f"Live fetching is not supported for {spec.name} ({spec.code}) "
                "because it requires heavy full-catalog setup"
            )
        capped_chars = _clamp_max_chars(max_chars)
        ref_payload = dict(ref)
        ref_id = ref_payload.pop("_ref_id", None)
        if ref_id is None and set(ref_payload.keys()) == {"ref_id"}:
            ref_id = ref_payload.pop("ref_id", None)
        if ref_id is not None and not ref_payload:
            cached = ref_cache.get(str(ref_id))
            if cached is None or cached[0] != spec.code:
                raise ValueError(
                    f"Unknown or expired ref handle for source {spec.name} ({spec.code})"
                )
            ref_cache.move_to_end(str(ref_id))
            ref_payload = dict(cached[1])

        allowed_domains = _allowed_domains_for_source(spec)
        _validate_ref_urls(ref_payload, spec=spec, allowed_domains=allowed_domains)

        client = await _get_live_client()
        doc = await client.fetch_ref(source, ref_payload)
        if hasattr(doc, "model_dump"):
            payload = doc.model_dump(exclude={"raw", "raw_ext"})
        else:
            payload = {
                k: v for k, v in dict(doc).items() if k not in {"raw", "raw_ext"}
            }
        return _truncate_markdown(payload, max_chars=capped_chars)

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
