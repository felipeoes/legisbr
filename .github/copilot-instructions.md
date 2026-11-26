# Copilot Instructions (`legisbr`)

See [AGENTS.md](../AGENTS.md) for the authoritative repository development guide.

## Build, Run & Test

Python 3.11+ (tested on 3.11–3.14), managed with `uv`. Pure HTTP — no Playwright or Chromium required.

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

## Architecture

- `src/legisbr/__init__.py`: PEP 562 lazy public exports (`Client`, `Store`, `ArchiveIndex`, `SearchHit`, `Document`, `OCR`, `Query`, `SearchCapabilities`, `UnsupportedQueryError`, `ErrorRecord`, `DocumentSink`, `NullSink`, `Settings`, `scrape`, `search_refs`, `sources`, `SourceInfo`) with `logging.NullHandler()` and fast (<30 ms) zero-heavy-import startup.
- `src/legisbr/registry.py`: Lazy `ScraperSpec` entries for all 30 official sources (27 states, Câmara dos Deputados, CONAMA, ICMBio) with codes, aliases, start years, rates, workers, `insecure_tls` (`ac` and `ba` only), and `SearchCapabilities`.
- `src/legisbr/client.py`: Async `Client` facade managing per-source `HttpClient` pools, cached `setup()` scrapers, `low_memory` preset, `list_refs()`, `search_refs()`, `fetch_ref()`, and PEP 789-safe `async with client.scrape(...)`.
- `src/legisbr/cli.py` & `src/legisbr/__main__.py`: Stdlib-only CLI (`legisbr` / `python -m legisbr`).
- `src/legisbr/mcp_server.py`: Optional MCP server (`legisbr[mcp]`) for read-only archive queries and opt-in `--allow-live-fetch`.
- `src/legisbr/core/`:
  - `document.py`: Pydantic `Document` (`DocRef` alias) with validated Markdown.
  - `sink.py`: `ErrorRecord`, `DocumentSink` protocol, and stateless `NullSink`.
  - `store.py` & `archive.py`: SQLite Schema v2 per archive with Portuguese diacritic-insensitive FTS5, streaming JSON/JSONL/ZSTD Parquet export, and read-only multi-archive `ArchiveIndex`.
  - `scraper.py` & `sapl.py`: `BaseScraper` bounded queue workers, resume deduplication, stats, streaming, and shared SAPL API support (`pb`, `pi`, `rr`).
  - `http.py` & `proxy.py`: `HttpClient`, zero-dependency GCRA `RateLimiter`, verified TLS by default, and optional proxy rotation (`legisbr[proxy]`).
  - `pdf.py`, `convert.py`, `images.py`, `ocr.py`: Permissive `pdf_oxide` + `pypdf` PDF pipeline, HTML/DOCX conversion, bounded single-flight `ImageCache`, and optional PydanticAI `OCR` (`legisbr[ocr-*]`).
- `src/legisbr/scrapers/`: `states/<uf>.py`, `federal/camara.py`, and `environment/{conama,icmbio}.py`.

## Scraper Contract & Conventions

- Keep scraper constructors pure (attribute assignment only); do I/O initialization in `async def setup()`.
- Implement `list_type(year, name, value)` (or override `list_documents(year)`) yielding `DocRef` dicts with stable `document_url` and `title`, and build `Document` only inside `fetch(ref)`.
- Every Python module must begin with `from __future__ import annotations` (enforced by Ruff `I002`, `target-version = "py311"`).
- Use stdlib `logging.getLogger(__name__)` everywhere; never auto-load `.env` or mutate `os.environ` at import time (centralize env config in `src/legisbr/settings.py`).
- Keep core dependencies 100% permissive (`scripts/check_licenses.py --mode core`) and optional extras free of GPL/AGPL/LGPL (`--mode extras`).
- Never swallow errors or add custom scraper retry loops; preserve embedded images (`embed_images=True`, inline HTML images before Markdown conversion) and use BeautifulSoup's `lxml` parser.
- Write offline unit tests using fakes from `tests/conftest.py` (no `object.__new__`); mark any live-network tests with `@pytest.mark.integration`.
