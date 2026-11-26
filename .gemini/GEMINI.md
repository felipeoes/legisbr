# `legisbr`

See [AGENTS.md](../AGENTS.md) for the authoritative repository development guide.

## Project Overview

`legisbr` (`felipeoes/legisbr`) is an async Python library, CLI, and optional MCP server covering **30 official Brazilian legislation portals** (Federal Câmara dos Deputados, all 27 state jurisdictions, CONAMA, and ICMBio).

- **Pure HTTP:** Zero Playwright or Chromium overhead.
- **Permissive Core:** `aiohttp`, `beautifulsoup4` (`lxml`), `html-to-markdown`, `mammoth`, `pdf_oxide`, `pypdf`, `pydantic`, `tenacity` (100% permissive MIT/BSD/Apache licenses).
- **Storage & Search:** Zero-disk async streaming (`legisbr.Client`), SQLite Schema v2 archives with Portuguese diacritic-insensitive FTS5, streaming ZSTD Parquet export (`legisbr[parquet]`), read-only multi-archive `ArchiveIndex`, and an optional MCP server (`legisbr[mcp]`).
- **Optional OCR & Proxies:** PydanticAI OCR (`legisbr[ocr-*]`) and SOCKS/HTTP proxy rotation (`legisbr[proxy]`).

## Commands

Python 3.11+ (tested on 3.11–3.14), managed with `uv`:

```bash
uv sync --all-extras --group dev
uv run legisbr list
uv run legisbr run -s Acre Conama --year 2024 --no-raw --fts
uv run legisbr search -s Camara --year 2021 --number 14133 --strict-native --fetch
uv run legisbr index --all
uv run legisbr query "licenciamento ambiental" --year 2024
uv run legisbr export -s Acre --format parquet --single-file --out exports/acre.parquet
uv run legisbr stats
uv run legisbr mcp serve --data-dir outputs/legislation
```

## Lint, Format & Tests

```bash
uv run ruff check --fix src/legisbr tests scripts/check_proxies.py scripts/check_licenses.py
uv run ruff format src/legisbr tests scripts/check_proxies.py scripts/check_licenses.py
uv run pytest tests -m "not integration" -n auto --timeout=60
uv run pytest tests -m integration
uv run pre-commit run --all-files
```

## Key Conventions

- Every module starts with `from __future__ import annotations` (Ruff `I002`, `target-version = "py311"`).
- Use stdlib `logging.getLogger(__name__)` everywhere; centralize environment loading in `src/legisbr/settings.py` (never auto-load `.env` at import time).
- Keep scraper constructors pure and put I/O initialization in `setup()`. Yield `DocRef` dicts from `list_type()`/`list_documents()` and build `Document` only in `fetch()`.
- Construct scrapers normally with fakes from `tests/conftest.py` (no `object.__new__`); mark live-network tests `@pytest.mark.integration`.
