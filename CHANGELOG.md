# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - TBD

### Added
- Installable `legisbr` package (`src/legisbr/` layout, `hatchling` build backend, `py.typed`, PEP 562 lazy top-level exports, and `legisbr` / `python -m legisbr` CLI).
- Full **Python 3.11–3.14** support across all 30 Brazilian legislation scrapers (27 states, Câmara dos Deputados, CONAMA, and ICMBio).
- Zero-disk streaming pipeline (`DocumentSink`, `NullSink`, typed `ErrorRecord`, PEP 789-safe async streaming) and high-level `legisbr.Client`.
- On-demand targeted norm search (`Query`, `SearchCapabilities`, `search_refs`) with native server-side filters on high-value portals.
- SQLite **Schema v2** (`id INTEGER PRIMARY KEY`, `meta` table, automatic idempotent v1 → v2 migration, `UPSERT` stability across updates & `VACUUM`, `--no-raw` / `save_raw=False`, low-memory PRAGMAs, batched `iter_keys()`).
- Safe Python-maintained SQLite **FTS5** full-text search over image-stripped text (`unicode61 remove_diacritics 2`, BM25 ranking, snippets, `rebuild_fts()`).
- Fixed-schema streaming **ZSTD Parquet** export (`legisbr[parquet]`).
- Read-only multi-archive `ArchiveIndex` with automatic `immutable=1` fallback for read-only WAL mounts.
- Optional **MCP server** (`legisbr[mcp]`) for querying local legislation archives from AI agents.
- Zero-dependency GCRA `burst=1` rate limiter, single-flight memory-bounded `ImageCache` with `<img` fast-path, and per-source TLS configuration (`verify_ssl=True` default).
- GitHub Actions CI (Python 3.11–3.14, lowest-direct resolution, wheel/sdist & permissive license audit), OIDC PyPI Trusted Publishing release workflow, and weekly 30-source health matrix.

### Changed
- Replaced AGPL `pymupdf`/`pymupdf4llm` with permissively licensed `pypdfium2` + `pypdf` + `pdfplumber` + `pillow`, and replaced GPL `unidecode` with stdlib `unicodedata`.
- Migrated library logging from `loguru` to stdlib `logging` with `NullHandler()` and zero import-time side effects.
- Isolated optional integrations behind extras: `[ocr]`, `[proxy]`, `[parquet]`, `[mcp]`, and `[all]`.

### Removed
- Removed Playwright / Chromium dependency (`BrowserPool`) and `pyrate-limiter`, reducing core install footprint by >85%.
- Removed root `main.py` in favor of `legisbr` / `python -m legisbr`.
