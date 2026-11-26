"""Tests for the optional legisbr MCP server tools and live-fetch guardrails."""

from __future__ import annotations

import builtins
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from legisbr.core.document import Document
from legisbr.core.store import Store
from legisbr.mcp_server import create_mcp_server, run_mcp_server


def _make_doc(**kwargs: Any) -> Document:
    defaults: dict[str, Any] = {
        "year": 2024,
        "title": "Lei Ordinária nº 1/2024",
        "type": "Lei",
        "situation": "Vigente",
        "summary": "Dispõe sobre organização administrativa.",
        "document_url": "https://example.org/lei/1",
        "text_markdown": (
            "Art. 1º Esta lei estabelece diretrizes sobre a Constituição "
            "estadual e a administração pública."
        ),
    }
    defaults.update(kwargs)
    return Document(**defaults)


def _extract_tool_result(result: Any) -> Any:
    """Normalize return payloads across mcp 2.x CallToolResult and mcp 1.x tuples."""
    if hasattr(result, "structured_content") and result.structured_content is not None:
        sc = result.structured_content
        if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
            return sc["result"]
        return sc
    if isinstance(result, tuple) and len(result) == 2:
        _, structured = result
        if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
            return structured["result"]
        return structured
    blocks = getattr(result, "content", result)
    if not blocks:
        return None
    if len(blocks) == 1 and hasattr(blocks[0], "text"):
        return json.loads(blocks[0].text)
    return [json.loads(block.text) for block in blocks if hasattr(block, "text")]


async def _build_archive(data_dir: Path) -> None:
    async with Store(data_dir / "ACRE" / "documents.db", fts=True, source="ACRE") as ac:
        await ac.save(
            _make_doc(
                year=2023,
                title="Lei da Constituição Estadual do Acre",
                type="Lei",
                document_url="https://ac.gov.br/lei/10",
                text_markdown=(
                    "Art. 1º Protege a Constituição e a floresta amazônica "
                    "com transparência administrativa."
                ),
            )
        )
    async with Store(
        data_dir / "SAO_PAULO" / "documents.db", fts=True, source="SAO_PAULO"
    ) as sp:
        await sp.save(
            _make_doc(
                year=2024,
                title="Lei de Mobilidade Paulista",
                type="Lei",
                document_url="https://sp.gov.br/lei/20",
                text_markdown="Art. 1º " + ("transporte público metropolitano " * 300),
            )
        )
        await sp.save(
            _make_doc(
                year=2024,
                title="Decreto Ambiental Paulista",
                type="Decreto",
                document_url="https://sp.gov.br/dec/21",
                text_markdown=(
                    "Art. 1º Estabelece regras de preservação ambiental e "
                    "recursos hídricos."
                ),
            )
        )


class _FakeClient:
    def __init__(self) -> None:
        self.search_calls: list[dict[str, Any]] = []
        self.fetch_calls: list[tuple[str, dict[str, Any]]] = []
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> _FakeClient:
        self.entered = True
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.exited = True

    async def search_refs(
        self,
        source: str,
        *,
        years: int | range | None = None,
        number: str | None = None,
        type: str | None = None,
        text: str | None = None,
        situation: str | None = None,
        limit: int | None = 20,
        strict_native: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        self.search_calls.append(
            {
                "source": source,
                "years": years,
                "number": number,
                "type": type,
                "text": text,
                "situation": situation,
                "limit": limit,
                "strict_native": strict_native,
            }
        )
        for idx in range(1, 30):
            yield {
                "title": f"Lei nº {number or idx}/{years or 2024}",
                "year": years or 2024,
                "type": type or "Lei",
                "situation": "Vigente",
                "document_url": f"https://live.example.org/{source}/{idx}",
            }

    async def fetch_ref(self, source: str, ref: dict[str, Any]) -> Document:
        self.fetch_calls.append((source, dict(ref)))
        return _make_doc(
            year=int(ref.get("year", 2024)),
            title=str(ref.get("title", "Lei Live")),
            document_url=str(ref.get("document_url", "https://live.example.org/1")),
            text_markdown="Art. 1º " + ("Texto normativo oficial completo. " * 20),
            raw=b"%PDF-1.4 secret-raw-bytes",
            raw_ext=".pdf",
        )


async def test_mcp_server_registers_all_five_tools_and_lists_sources(tmp_path):
    await _build_archive(tmp_path)
    server = create_mcp_server(tmp_path)

    tools = await server.list_tools()
    assert {tool.name for tool in tools} == {
        "list_sources",
        "search_legislation",
        "get_legislation",
        "live_search_norms",
        "live_fetch_norm",
    }

    sources = _extract_tool_result(await server.call_tool("list_sources", {}))
    assert len(sources) == 30
    by_code = {item["code"]: item for item in sources}
    assert by_code["ac"]["name"] == "ACRE"
    assert by_code["ac"]["archived_documents"] == 1
    assert by_code["ac"]["fts_enabled"] is True
    assert by_code["sp"]["archived_documents"] == 2
    assert by_code["sp"]["min_year"] == 2024
    assert by_code["mg"]["archived_documents"] == 0
    assert by_code["camara"]["capabilities"]["native_text"] is True


async def test_mcp_search_legislation_and_get_legislation_truncation(tmp_path):
    await _build_archive(tmp_path)
    server = create_mcp_server(tmp_path)

    # Diacritic-insensitive BM25 search with snippets and NO full body dump.
    hits = _extract_tool_result(
        await server.call_tool("search_legislation", {"query": "constituicao"})
    )
    assert len(hits) == 1
    hit = hits[0]
    assert hit["source"] == "ACRE"
    assert "[" in hit["snippet"] and "]" in hit["snippet"]
    assert "text_markdown" not in hit

    # Source alias ("sp" -> SAO_PAULO) and doc_type filter work cleanly.
    sp_hits = _extract_tool_result(
        await server.call_tool(
            "search_legislation",
            {
                "query": "hidricos",
                "sources": ["sp"],
                "year": 2024,
                "doc_type": "Decreto",
                "limit": 999,
            },
        )
    )
    assert len(sp_hits) == 1
    assert sp_hits[0]["source"] == "SAO_PAULO"

    # Full get_legislation without truncation.
    full_doc = _extract_tool_result(
        await server.call_tool("get_legislation", {"source": "ac", "doc_id": hit["id"]})
    )
    assert full_doc is not None
    assert full_doc["truncated"] is False
    assert "Constituição" in full_doc["text_markdown"]
    assert full_doc["total_chars"] == len(full_doc["text_markdown"])

    # Truncation enforced when document exceeds max_chars.
    long_hits = _extract_tool_result(
        await server.call_tool(
            "search_legislation", {"query": "metropolitano", "sources": ["SP"]}
        )
    )
    assert len(long_hits) == 1
    truncated_doc = _extract_tool_result(
        await server.call_tool(
            "get_legislation",
            {"source": "SAO_PAULO", "doc_id": long_hits[0]["id"], "max_chars": 120},
        )
    )
    assert truncated_doc is not None
    assert truncated_doc["truncated"] is True
    assert len(truncated_doc["text_markdown"]) == 120
    assert truncated_doc["total_chars"] > 120

    # Missing document returns None.
    missing = _extract_tool_result(
        await server.call_tool("get_legislation", {"source": "ACRE", "doc_id": 9999})
    )
    assert missing is None


async def test_mcp_live_tools_disabled_by_default_and_strict_native_when_enabled(
    tmp_path,
):
    await _build_archive(tmp_path)
    fake_client = _FakeClient()

    # Disabled by default: both live tools must refuse execution.
    ro_server = create_mcp_server(
        tmp_path, allow_live_fetch=False, client_factory=lambda: fake_client
    )
    for tool_name, args in (
        ("live_search_norms", {"source": "sp", "year": 2024}),
        (
            "live_fetch_norm",
            {"source": "sp", "ref": {"document_url": "https://example.org/1"}},
        ),
    ):
        with pytest.raises(Exception) as exc_info:
            await ro_server.call_tool(tool_name, args)
        cause = exc_info.value.__cause__ or exc_info.value
        assert isinstance(cause, PermissionError) or "--allow-live-fetch" in str(
            exc_info.value
        )
    assert fake_client.search_calls == []
    assert fake_client.fetch_calls == []

    # Enabled: live_search_norms enforces strict_native=True and caps limit <= 20.
    live_server = create_mcp_server(
        tmp_path, allow_live_fetch=True, client_factory=lambda: fake_client
    )
    refs = _extract_tool_result(
        await live_server.call_tool(
            "live_search_norms",
            {
                "source": "camara",
                "year": 2024,
                "number": "123",
                "doc_type": "Lei",
                "text": "educacao",
                "limit": 100,
            },
        )
    )
    assert len(refs) == 20
    assert len(fake_client.search_calls) == 1
    assert fake_client.search_calls[0] == {
        "source": "camara",
        "years": 2024,
        "number": "123",
        "type": "Lei",
        "text": "educacao",
        "situation": None,
        "limit": 20,
        "strict_native": True,
    }
    assert fake_client.entered and fake_client.exited

    # live_fetch_norm returns model_dump excluding raw and enforces max_chars.
    fetched = _extract_tool_result(
        await live_server.call_tool(
            "live_fetch_norm",
            {"source": "camara", "ref": refs[0], "max_chars": 80},
        )
    )
    assert len(fake_client.fetch_calls) == 1
    assert "raw" not in fetched and "raw_ext" not in fetched
    assert fetched["truncated"] is True
    assert len(fetched["text_markdown"]) == 80
    assert fetched["total_chars"] > 80


def test_mcp_missing_sdk_raises_clear_install_hint(monkeypatch, tmp_path):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "mcp" or name.startswith("mcp."):
            raise ImportError("No module named 'mcp'")
        return real_import(name, globals, locals, fromlist, level)

    for mod in [m for m in list(sys.modules) if m == "mcp" or m.startswith("mcp.")]:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ImportError, match=r"pip install 'legisbr\[mcp\]'"):
        create_mcp_server(tmp_path)


def test_run_mcp_server_dispatches_transports(monkeypatch, tmp_path):
    from legisbr import mcp_server as mod

    calls: list[tuple[str, dict[str, Any]]] = []

    class _DummyServer:
        def run(self, transport: str = "stdio", **kwargs: Any) -> None:
            calls.append((transport, kwargs))

    monkeypatch.setattr(mod, "_load_mcp_server_class", lambda: (_DummyServer, 2))
    monkeypatch.setattr(mod, "create_mcp_server", lambda *a, **kw: _DummyServer())

    run_mcp_server(tmp_path, transport="stdio")
    run_mcp_server(
        tmp_path,
        allow_live_fetch=True,
        transport="streamable-http",
        host="0.0.0.0",
        port=9090,
    )
    assert calls == [
        ("stdio", {}),
        ("streamable-http", {"host": "0.0.0.0", "port": 9090}),
    ]

    with pytest.raises(ValueError, match="transport must be"):
        run_mcp_server(tmp_path, transport="invalid")  # type: ignore[arg-type]
