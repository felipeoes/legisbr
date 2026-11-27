# Repository development guide

## Commands

Python 3.11+ (tested on 3.11–3.14). Install with `uv sync --all-extras --group dev` (pure HTTP — no Playwright/Chromium).

```bash
uv run legisbr list
uv run legisbr run -s Acre Conama --year 2024 --no-raw --fts
uv run legisbr run --all --jobs 16
uv run legisbr search -s Camara --year 2021 --number 14133 --strict-native --fetch
uv run legisbr index --all
uv run legisbr query "licenciamento ambiental" --year 2024
uv run legisbr stats
uv run legisbr export -s Acre --format parquet --single-file --out exports/acre.parquet
uv run legisbr export -s Acre --format jsonl --out exports/acre
uv run legisbr import-json -s Acre
uv run legisbr mcp serve --data-dir outputs/legislation
uv run ruff check --fix src/legisbr tests scripts/check_proxies.py scripts/check_licenses.py
uv run ruff format src/legisbr tests scripts/check_proxies.py scripts/check_licenses.py
uv run pytest tests -m "not integration" -n auto --timeout=60
uv run pytest tests -m integration
uv run pre-commit run --all-files
```

`legisbr run` requires `-s` or `--all` and supports `--year`, `--years START:END`, `--overwrite`, `--no-raw`, `--fts`, `--low-memory`, `--pdf-executor auto|process|thread`, `-v`, `--log-file`, and `--jobs`. Several scrapers run as one child process each (`sys.executable -m legisbr run ...`; `--jobs` bounds concurrency, default all up to CPU count; each child gets scaled `PDF_WORKERS`); a single scraper runs in-process. Any scraper or cleanup failure returns exit 1. `legisbr stats` prints each archive's last run (`runs.stats`) as a Markdown table. Pre-commit excludes integration tests and enforces a 60-second per-test timeout.

## Architecture

- `src/legisbr/__init__.py`: PEP 562 lazy top-level exports (`Client`, `Store`, `ArchiveIndex`, `SearchHit`, `Document`, `OCR`, `Query`, `SearchCapabilities`, `UnsupportedQueryError`, `ErrorRecord`, `DocumentSink`, `NullSink`, `Settings`, `scrape`, `search_refs`, `sources`, `SourceInfo`) + `logging.NullHandler()`. `import legisbr; legisbr.sources()` must stay <30 ms with zero heavy imports (`aiohttp`, `bs4`, `pydantic`, `pypdfium2`, `pdfplumber`, `PIL`, `pypdf`).
- `src/legisbr/registry.py`: Lazy `ScraperSpec` entries (`_state()` for state scrapers) for all 30 sources storing string `cls_path`/`cls_name`, codes, aliases, start years, rates, workers, OCR/proxy settings, `insecure_tls` (`ac` and `ba` only), and `SearchCapabilities`.
- `src/legisbr/settings.py`: Frozen `Settings` dataclass loaded explicitly via `Settings.from_env(env_file=...)` using a stdlib `.env` parser (never mutates `os.environ` or runs at import time).
- `src/legisbr/client.py`: Public async `Client` facade managing per-source `HttpClient` pools (enforcing `spec.rps` unless `force_rps=True`, `verify_ssl=not spec.insecure_tls`), cached `setup()` scrapers, `low_memory` preset, `list_refs()`, `search_refs()`, `fetch_ref()`, and PEP 789-safe `async with client.scrape(...)`.
- `src/legisbr/cli.py` & `src/legisbr/__main__.py`: Stdlib-only top-level CLI (`legisbr` / `python -m legisbr`) covering `list`, `run`, `search`, `query`, `index`, `export`, `import-json`, `stats`, and `mcp serve`.
- `src/legisbr/mcp_server.py`: Optional MCP server (`legisbr[mcp]`, `mcp>=1.14`, supporting both `mcp 2.x` `MCPServer` and `mcp 1.x` `FastMCP`) exposing `list_sources`, `search_legislation`, `get_legislation`, and opt-in (`--allow-live-fetch`) `live_search_norms` (`strict_native=True`) + SSRF-guarded `live_fetch_norm` (opaque `_ref_id` handles, portal-host allowlist, `heavy_setup` rejection, server-scoped `Client`).
- `src/legisbr/core/document.py`: Pydantic `Document` (`DocRef` alias); required metadata and validated Markdown, excluded raw bytes, extra source fields allowed. No dictionary accessor shims.
- `src/legisbr/core/sink.py`: Frozen `ErrorRecord`, `DocumentSink` protocol, and zero-disk stateless `NullSink` (invokes `on_error` or logs via stdlib `logging`).
- `src/legisbr/core/store.py`: SQLite Schema v2 per archive (`meta`, `documents` with stable `id INTEGER PRIMARY KEY`, `source`, `UNIQUE(ref_url, ref_title)`, `ON CONFLICT DO UPDATE ... RETURNING id`, automatic idempotent v1 → v2 migration, `save_raw`, `low_memory` PRAGMAs, batched `iter_keys()`, `errors`, `runs`). Safe Python-maintained FTS5 (`unicode61 remove_diacritics 2`, zero triggers) over `strip_data_uri_images()` text with `ENABLE_FTS5` detection, `rebuild_fts()`, BM25 `search()` returning `SearchHit` snippets without loading full bodies, `get_document()`, and `get_by_url()`. Streaming export supports JSON, JSONL, and fixed-schema ZSTD Parquet (`extra_json`, lazy `pyarrow`). Raw files are content-addressed, atomically replaced, and stored relative to the archive directory.
- `src/legisbr/core/archive.py`: Read-only multi-archive `ArchiveIndex` discovering `<data_dir>/*/documents.db` and explicit DB paths, opening `file:...?mode=ro` with automatic probe + fallback to `file:...?immutable=1` on read-only/permission errors only (re-raising `SQLITE_BUSY`/`SQLITE_LOCKED`).
- `src/legisbr/core/query.py`: Stdlib-only `Query`, `SearchCapabilities`, `UnsupportedQueryError`, and diacritic-insensitive matching helpers.
- `src/legisbr/core/scraper.py`: `BaseScraper` implements bounded queue workers, optional `store: DocumentSink | None` (default `NullSink`), single-source 16-byte `blake2b` resume/pending deduplication, deterministic O(1)-memory `LatencyHistogram` stats, PEP 789-safe background-task `stream()`, `search_refs()`, and `AsyncExitStack` resource cleanup.
- `src/legisbr/core/sapl.py`: Shared SAPL logic for Paraíba, Piauí, and Roraima (lazy `assuntonorma` loading + native `/api/norma/normajuridica/` search).
- `src/legisbr/core/http.py`: `HttpClient.request()` returns a fully buffered `Response` (`content`, `text`, `json()`, `soup()`, `filename`, `content_type`). Zero-dependency GCRA `burst=1` `RateLimiter` (lock-free while sleeping). Default `verify_ssl=True`; `DEFAULT@SECLEVEL=1` only when `verify_ssl=False` (`ac`, `ba`). Lazy-imports `aiohttp_socks` (`legisbr[proxy]`). Browser-like charset resolution (header → BOM → HTML `<meta>` → UTF-8 → windows-1252). `pause_on_403` waits out site IP blocks (Paraná).
- `src/legisbr/core/images.py`: Precompiled `<img` regex fast-path + strict magic-byte MIME validation + single-flight waiter-counted LRU `ImageCache` (default 16 MiB, spacer sentinels <100 B, never caches failures).
- `src/legisbr/core/pdf.py`: Permissive `pypdfium2` + `pypdf` + selective `pdfplumber` + `pillow` PDF engine: visual reading-order extraction, ruled-table GFM conversion, watermark filtering, 1-bit CCITT & selective per-page OCR routing, citation-safe legal line-break restoration, scan detection (`is_pdf_scanned`), 1-based page slicing (`slice_pdf_pages`), 150 DPI PNG rendering (`render_pdf_batch`), and bounded `spawn` `PdfRunner` with automatic fallback to `asyncio.to_thread` when `/dev/shm` is unavailable.
- `src/legisbr/core/convert.py`: HTML/DOCX/PDF conversion helpers delegating PDF work to `legisbr.core.pdf`; lazy-imports `mammoth` on `.docx`; legacy OLE2 `.doc` fails explicitly; `sniff_extension` trusts content over filenames.
- `src/legisbr/core/ocr.py`: Lazy PydanticAI `OCR` (`legisbr[ocr]`), configurable `dpi=150`, batch semaphore, serialized local PDF rendering, per-model/per-run usage, all-or-nothing document conversion on batch failure.
- `src/legisbr/core/proxy.py`: Round-robin proxy rotation for `needs_proxy` specs (Paraná), per-proxy rate limiter and verified TLS.
- `src/legisbr/scrapers/states/<uf>.py`, `federal/camara.py`, `environment/{conama,icmbio}.py`: Source-specific extraction (zero Playwright, zero `unidecode`, zero `fitz`).

## Scraper contract

Constructors only set attributes; accept optional `store`, `http`, `years`, `ocr`, `workers`, `overwrite`, `image_cache`, `on_error`, `pdf_executor`, and `pdf_workers`. Implement:

```python
async def list_type(self, year: int | None, name: str, value: Any) -> AsyncIterator[DocRef]: ...  # once per `types` item
async def list_documents(self, year: int | None) -> AsyncIterator[DocRef]: ...  # override instead when listing isn't per type
async def fetch(self, ref: DocRef) -> Document: ...
```

Listing refs contain stable `document_url` and `title`, plus year/type/situation and source-specific fields needed by fetch. Build `Document` only in `fetch()`. The base persists results and records typed `ErrorRecord`s; a `None` fetch result is an error, so skip unwanted refs while listing. Ref keys starting with `_` (`_pdf_link`, `_html_link`) are listing-only and never stored. Use `setup()` for initialization requiring I/O. Declare class `capabilities` matching `registry.py`'s `ScraperSpec.capabilities`, override `search_refs()` when a portal supports native server-side filtering, and avoid overriding `list_documents()` when declaring `native_type=True` so `list_type()` narrowing applies. Keep session/JSF/ASP.NET pagination sequential.

## Style and tests

- Annotate all signatures, use built-in generics and `X | Y`, and format with Ruff (`target-version = "py311"`).
- Every module MUST start with `from __future__ import annotations` (enforced by Ruff `I002` for Python 3.11 compatibility).
- Use stdlib `logging.getLogger(__name__)` everywhere (never `loguru`, never add handlers or write log files at import time).
- No inline lint suppressions without a concrete reason. Centralize environment settings in `src/legisbr/settings.py`.
- Keep core dependencies 100% permissive (verified by `scripts/check_licenses.py --mode core`) and extras restricted to permissive + weak-copyleft `MPL-2.0` allowlist (`--mode extras`).
- Do not swallow errors or implement scraper retry loops; reject malformed listing responses visibly.
- Preserve images: keep `embed_images=True` for PDF conversion, inline HTML images before `html-to-markdown`, and retain base64 Markdown images during cleaning (strip them only inside FTS5 indexing). Use BeautifulSoup's `lxml` parser.
- Tests construct scrapers normally with fakes (`tests/conftest.py`); no `object.__new__`. Async pytest mode is automatic. Mark every live-network test `integration`. Avoid running full archives during unit tests.

## Configuration and data

See `.env.example` and `README.md`. `LLM_MODEL` accepts PydanticAI model strings or a comma-separated fallback list; `LLM_PDF_INPUT`, `LLM_BATCH_SIZE`, and `LLM_CONCURRENCY` control OCR; `PDF_WORKERS` sizes the PDF process pool. Standard provider credentials include `OPENAI_API_KEY`, `OPENAI_BASE_URL`, AWS settings, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `MISTRAL_API_KEY`, `GROQ_API_KEY`, and `HF_TOKEN`.

Archives live at `<save_dir>/<NAME>/documents.db` (Schema v2). `import-json` reads `YEAR/data.json` (a list or legacy `{"summary", "documents"}`) and shards without deleting inputs, preserves raw file paths, and records invalid rows in `errors` (exit 1). Exports keep `ref_url`/`ref_title` so re-imports resume correctly; Parquet exports use a fixed Arrow schema with `extra_json`. `--overwrite` re-scrapes without deleting anything; `UPSERT` keeps `documents.id` and FTS5 synchronized. Summary and search queries must never load full `text_markdown` bodies.
