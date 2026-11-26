# `legisbr` — Brazilian Legislation Scraper, Archive & MCP Server

[![PyPI](https://img.shields.io/pypi/v/legisbr.svg)](https://pypi.org/project/legisbr/)
[![Python](https://img.shields.io/pypi/pyversions/legisbr.svg)](https://pypi.org/project/legisbr/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/felipeoes/legisbr/actions/workflows/ci.yml/badge.svg)](https://github.com/felipeoes/legisbr/actions/workflows/ci.yml)

High-performance, async Python library, CLI, and MCP server covering **30 official Brazilian legislation portals** — Federal legislation (Câmara dos Deputados), all **27 state jurisdictions**, CONAMA, and ICMBio.

- **Pure HTTP & Zero Browser Overhead:** Zero Playwright, zero Chromium downloads — fast, headless, container-friendly scraping directly against official portals.
- **100% Permissive MIT Core (~82 MB installed):** Pure-Rust `pdf_oxide` + `pypdf` + `html-to-markdown` + `mammoth`. Zero AGPL/GPL dependencies in core (`PyMuPDF` and `unidecode` completely removed).
- **Python 3.11–3.14:** Typed (`py.typed`), lazy PEP 562 imports (~28 ms `legisbr.sources()`, ~55 ms `legisbr list` CLI startup with zero heavy imports), zero import-time `.env` or logging side effects.
- **Stream, Search, Index & Export:** Zero-disk async streaming (`legisbr.Client`), native on-demand norm search, SQLite Schema v2 with Portuguese diacritic-insensitive FTS5, streaming ZSTD Parquet export, read-only multi-archive `ArchiveIndex`, and an optional MCP server for AI agents.

> This project queries official government portals directly and **does not scrape `leisestaduais.com.br`**.

---

## Quickstart

Requires **Python 3.11+**.

```bash
# Add to a uv project (recommended)
uv add legisbr

# Or install with uv pip / pip
uv pip install legisbr
pip install legisbr

# Install the CLI globally or run it without installing via uv
uv tool install legisbr
uvx legisbr list
```

### Optional Extras

| Extra | `uv` (project) | `pip` / `uv pip` | Purpose |
| --- | --- | --- | --- |
| `ocr` | `uv add "legisbr[ocr]"` | `pip install "legisbr[ocr]"` | Scanned PDF OCR via PydanticAI (OpenAI default) |
| `ocr-openai` | `uv add "legisbr[ocr-openai]"` | `pip install "legisbr[ocr-openai]"` | OpenAI / OpenAI-compatible OCR provider |
| `ocr-anthropic` | `uv add "legisbr[ocr-anthropic]"` | `pip install "legisbr[ocr-anthropic]"` | Anthropic Claude OCR provider |
| `ocr-google` | `uv add "legisbr[ocr-google]"` | `pip install "legisbr[ocr-google]"` | Google Gemini OCR provider |
| `ocr-bedrock` | `uv add "legisbr[ocr-bedrock]"` | `pip install "legisbr[ocr-bedrock]"` | AWS Bedrock OCR provider |
| `proxy` | `uv add "legisbr[proxy]"` | `pip install "legisbr[proxy]"` | SOCKS/HTTP proxy rotation (`aiohttp-socks`, used for Paraná) |
| `parquet` | `uv add "legisbr[parquet]"` | `pip install "legisbr[parquet]"` | Fixed-schema streaming ZSTD Parquet export (`pyarrow`) |
| `mcp` | `uv add "legisbr[mcp]"` | `pip install "legisbr[mcp]"` | Model Context Protocol server (`mcp`) for AI agents |
| `all` | `uv add "legisbr[all]"` | `pip install "legisbr[all]"` | All optional extras combined (zero GPL/AGPL/LGPL) |

For standalone CLI installs with extras, use `uv tool install "legisbr[parquet,mcp]"` (or `uv pip install "legisbr[all]"` in an existing virtual environment).

---

## Python Library API

### 1. Zero-Disk Async Streaming

`Client.scrape()` streams validated `Document` objects through a PEP 789-safe async context manager with **zero disk writes** unless a `Store` (or custom `DocumentSink`) is passed:

```python
import asyncio
import legisbr


async def main() -> None:
    async with legisbr.Client() as client:
        async with client.scrape("SP", years=2024, limit=10) as docs:
            async for doc in docs:
                print(doc.year, doc.title, len(doc.text_markdown))


asyncio.run(main())
```

### 2. Low-Memory / Headless Preset

Ideal for containers, CI runners, and small VMs (`workers <= 4`, thread-based PDF extraction, 2 MB image cache, `doc.raw` dropped when streaming without a `Store`):

```python
async with legisbr.Client(low_memory=True) as client:
    async with client.scrape("AC", years=2024, limit=5) as docs:
        async for doc in docs:
            print(doc.title)
```

### 3. On-Demand Targeted Norm Search

Search portals with native server-side filters (no full-year crawl required when `strict_native=True`), then fetch only the norms you need:

```python
async with legisbr.Client() as client:
    refs = [
        ref
        async for ref in client.search_refs(
            "Camara",
            years=2021,
            number="14133",
            strict_native=True,
        )
    ]
    if refs:
        doc = await client.fetch_ref("Camara", refs[0])
        print(doc.title, doc.document_url)
```

### 4. Local SQLite Archive, FTS5 Search & Parquet Export

Persist into SQLite Schema v2 with automatic Portuguese diacritic-insensitive FTS5 indexing (inline base64 images are automatically stripped before indexing), stream fixed-schema ZSTD Parquet, and query across multiple jurisdictions read-only with `ArchiveIndex`:

```python
from pathlib import Path
import legisbr


async def archive_and_query() -> None:
    data_dir = Path("outputs/legislation")
    store = legisbr.Store(
        data_dir / "SAO_PAULO" / "documents.db",
        source="SAO_PAULO",
        save_raw=False,  # 80-95% disk savings (skips raw PDF/HTML copies)
        fts=True,
    )

    async with legisbr.Client() as client:
        async with client.scrape("SP", years=2024, limit=25, store=store) as docs:
            async for _ in docs:
                pass

    # Fixed-schema ZSTD Parquet (HuggingFace-ready single file or per-year)
    async with store:
        await store.export(data_dir / "sp_2024.parquet", fmt="parquet", single_file=True)

    # Read-only cross-jurisdiction BM25 search (auto-falls back to immutable=1 on RO mounts)
    index = legisbr.ArchiveIndex(data_dir)
    for hit in await index.query("administracao publica", year=2024, limit=5):
        print(f"[{hit.source}] {hit.title}: {hit.snippet}")
```

---

## CLI Usage

Both `legisbr` and `python -m legisbr` expose the full CLI:

```bash
# List all 30 registered sources and capabilities (zero heavy imports)
legisbr list
legisbr list --json

# Scrape into SQLite (single scraper in-process; multiple scrapers in parallel processes)
legisbr run -s SP --year 2024 --no-raw --fts
legisbr run -s Acre Conama --years 2022:2024 --low-memory
legisbr run --all --jobs 8

# On-demand live search (add --fetch to convert matching norms to Markdown/JSON)
legisbr search -s Camara --year 2021 --number 14133 --strict-native --fetch

# Build/rebuild FTS5 indexes on existing archives and run instant local queries
legisbr index --all
legisbr query "licenciamento ambiental" --year 2024 --limit 20

# Export archives to ZSTD Parquet, JSONL, or JSON
legisbr export -s SP --format parquet --single-file --out exports/sp.parquet
legisbr export -s Acre --format jsonl --out exports/acre

# Import legacy JSON archives and inspect run performance stats
legisbr import-json -s Acre
legisbr stats

# Start the optional MCP server over your local archive directory
legisbr mcp serve --data-dir ./outputs/legislation
```

Names, codes, and aliases shown by `legisbr list` are case-insensitive. A `run` exits `1` if any listing, document fetch, or cleanup fails (all errors are recorded in SQLite `errors`).

---

## MCP Server (Claude Desktop / Claude Code / Agents)

Install with `uv add "legisbr[mcp]"`, `uv tool install "legisbr[mcp]"`, `uv pip install "legisbr[mcp]"`, or `pip install "legisbr[mcp]"` (or run directly via `uvx`) to give AI assistants instant, read-only BM25 search over local Brazilian legislation archives.

Tools exposed:
- `list_sources` — all 30 jurisdictions with local archive coverage and search capabilities.
- `search_legislation` — BM25 full-text search returning metadata + highlighted snippets (never dumps giant bodies).
- `get_legislation` — fetches full norm Markdown by `(source, doc_id)` with `max_chars` truncation safety.
- `live_search_norms` / `live_fetch_norm` — disabled by default; opt in with `--allow-live-fetch` (enforces `strict_native=True` so agents cannot accidentally trigger multi-minute crawls).

Example Claude Desktop / Claude Code MCP configuration:

```json
{
  "mcpServers": {
    "legisbr": {
      "command": "uvx",
      "args": [
        "--from",
        "legisbr[mcp]",
        "legisbr",
        "mcp",
        "serve",
        "--data-dir",
        "/path/to/outputs/legislation"
      ]
    }
  }
}
```

For remote HTTP clients, pass `--transport streamable-http --host 127.0.0.1 --port 8000`.

---

## 30-Source Coverage & Capabilities

| Code | Archive Name | Scope | Start | PDF | OCR | Native Search | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `camara` | `LEGISLACAO_FEDERAL` | Federal | 1808 | Yes | No | year, type, number, text, situation | 100 rps |
| `conama` | `CONAMA` | Environment | 1984 | Yes | Yes | year | — |
| `icmbio` | `ICMBIO` | Environment | 2016 | Yes | No | client filter | cookies disabled |
| `ac` | `ACRE` | State | 1963 | No | No | fast catalog | `insecure_tls` (missing intermediate CA) |
| `al` | `ALAGOAS` | State | 1942 | Yes | Yes | year | 5 rps |
| `am` | `AMAZONAS` | State | 1956 | No | No | year, type | 50 rps |
| `ap` | `AMAPA` | State | 1991 | No | No | year, type | `--year` filters bill filing year |
| `ba` | `BAHIA` | State | 1891 | No | No | year | 1 rps, `insecure_tls` (legacy TLS 1.2 RSA) |
| `ce` | `CEARA` | State | 1968 | No | Yes | heavy setup (no strict native) | prefetches catalog in `setup()` |
| `df` | `DISTRITO_FEDERAL` | State | 1922 | Yes | Yes | year | 100 rps, 100 workers |
| `es` | `ESPIRITO_SANTO` | State | 1958 | Yes | Yes | year | 50 rps |
| `go` | `GOIAS` | State | 1887 | Yes | Yes | year | 100 rps |
| `ma` | `MARANHAO` | State | 1906 | Yes | Yes | year, type | — |
| `mg` | `MINAS_GERAIS` | State | 1831 | Yes | Yes | year, type, number, text | — |
| `ms` | `MATO_GROSSO_DO_SUL` | State | 1979 | No | No | year, type | — |
| `mt` | `MATO_GROSSO` | State | 2017 | No | Yes | year | 5 rps |
| `pa` | `PARA` | State | 1990 | Yes | Yes | year, type | 5 rps |
| `pb` | `PARAIBA` | State | 1924 | Yes | Yes | year, type, number, text (SAPL) | 1 rps, 2 workers |
| `pe` | `PERNAMBUCO` | State | 1835 | No | No | year | — |
| `pi` | `PIAUI` | State | 1922 | Yes | Yes | year, type, number, text (SAPL) | — |
| `pr` | `PARANA` | State | 2025 | No | No | year, type, number, direct ID | 0.3 rps, 35m 403-pause, optional `[proxy]` |
| `rj` | `RIO_DE_JANEIRO` | State | 1891 | No | No | type (yearless) | 300s request timeout, 3 retries |
| `rn` | `RIO_GRANDE_DO_NORTE` | State | 1971 | Yes | Yes | year | — |
| `ro` | `RONDONIA` | State | 1981 | Yes | Yes | year, type | — |
| `rr` | `RORAIMA` | State | 1991 | Yes | Yes | year, type, number, text (SAPL) | — |
| `rs` | `RIO_GRANDE_DO_SUL` | State | 1830 | Yes | Yes | year | — |
| `sc` | `SANTA_CATARINA` | State | 1946 | No | No | year, type | — |
| `se` | `SERGIPE` | State | 1940 | Yes | Yes | year, type | — |
| `sp` | `SAO_PAULO` | State | 1835 | Yes | Yes | year, type, number, situation | — |
| `to` | `TOCANTINS` | State | 1989 | Yes | Yes | year, type | — |

---

## Performance & Compatibility

- **Rust PDF Pipeline (`pdf_oxide` + `pypdf`):** ~19.5× faster than `pymupdf4llm` on real Brazilian legislation PDFs with legal line-break restoration (`Art.`, `§`, Roman incisos). Uses a bounded `spawn` process pool (`pdf_executor="auto"`) to keep event-loop latency <50 ms, with automatic fallback to bounded `asyncio.to_thread` in restricted containers without `/dev/shm`.
- **Single-Flight `ImageCache` & `<img` Fast-Path:** Skips HTML DOM parsing on text-only laws and deduplicates concurrent coat-of-arms/header fetches in memory (bounded LRU, default 16 MiB).
- **GCRA `burst=1` Rate Limiter:** Zero-dependency lock-free slot reservation preserving strict inter-arrival spacing (critical for portals like Paraná at 0.3 rps).
- **Storage & Memory Efficiency:** `save_raw=False` / `--no-raw` cuts archive disk usage by 80–95%; `BaseScraper` deduplicates resume keys with 16-byte `blake2b` digests and tracks latency in a deterministic O(1)-memory log-bucket histogram.
- **Platform Support:** Linux, macOS, and Windows on **Python 3.11–3.14**. Note: `pdf_oxide` Linux wheels target `manylinux_2_28` / musl 1.2+, so AWS Lambda deployments should use **Python 3.12+ (Amazon Linux 2023)** rather than Amazon Linux 2 (Python 3.11).

---

## Configuration

Library code never auto-loads `.env` or mutates `os.environ`. The CLI loads `Settings.from_env(env_file=Path(".env"))` explicitly:

| Variable | Purpose | Default |
| --- | --- | --- |
| `SAVE_DIR` | Root directory for SQLite archives | `outputs/legislation` |
| `STATE_LEGISLATION_SAVE_DIR` | Optional override root for state archives | `SAVE_DIR` |
| `SPECIFIC_LEGISLATION_SAVE_DIR` | Optional override root for CONAMA / ICMBio | `SAVE_DIR` |
| `LOG_DIR` | Optional directory for per-scraper CLI log files | Empty (stderr only) |
| `LLM_MODEL` | PydanticAI model string or comma-separated fallback list (requires `legisbr[ocr-*]`) | Empty (OCR disabled) |
| `LLM_PDF_INPUT` | Send sliced PDF batches instead of 150 DPI rendered PNG pages | `false` |
| `LLM_BATCH_SIZE` | Pages per OCR request | `5` |
| `LLM_CONCURRENCY` | Maximum concurrent OCR batches per scraper process | `8` |
| `PDF_WORKERS` | Worker processes for PDF extraction (`0` forces thread mode) | `min(8, CPUs)` |
| `PROXY_FILE_PATH` | Optional newline-separated proxy list for Paraná (`legisbr[proxy]`) | Empty |
| `PROXY_ENDPOINT` | Optional proxy-list HTTP endpoint for Paraná (`legisbr[proxy]`) | Empty |

Provider credentials follow PydanticAI's standard environment variables (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `AWS_*` / `AWS_BEARER_TOKEN_BEDROCK`).

---

## Legal & Ethical Notice

- **Public Domain Status:** Official Brazilian laws, decrees, resolutions, and normative acts are excluded from copyright protection under **Lei nº 9.610/1998, art. 8º, I** (*"Não são objeto de proteção como direitos autorais: os textos de tratados ou convenções, leis, decretos, regulamentos, decisões judiciais e demais atos oficiais"*).
- **Official Portals Only:** `legisbr` fetches exclusively from official government portals with conservative per-source rate limits and never scrapes `leisestaduais.com.br` or private editorial consolidations.
- **No Legal Advice:** Extracted text reflects the official source portal at scrape time and is provided as-is for research, indexing, and engineering use; always verify authoritative texts before legal reliance.

---

## Development

```bash
git clone https://github.com/felipeoes/legisbr.git
cd legisbr
uv sync --all-extras --group dev
uv run ruff check src/legisbr tests scripts/check_proxies.py scripts/check_licenses.py
uv run ruff format --check src/legisbr tests scripts/check_proxies.py scripts/check_licenses.py
uv run pytest tests -m "not integration" -n auto --timeout=60
uv run pytest tests -m integration
uv run pre-commit run --all-files
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the 10-minute broken-scraper fix guide and PR checklist, and [SECURITY.md](SECURITY.md) for our TLS and disclosure policy.

## License

MIT — see [LICENSE](LICENSE).
