<div align="center">

<h1>legisbr</h1>

<p><strong>Brazilian legislation as clean Markdown, SQLite, and Parquet — in Python, CLI, or MCP.</strong></p>

<p>30 official federal, state, and environmental portals · Sync &amp; async · Zero browser dependencies.</p>

[![PyPI](https://img.shields.io/pypi/v/legisbr.svg)](https://pypi.org/project/legisbr/)
[![Python](https://img.shields.io/pypi/pyversions/legisbr.svg)](https://pypi.org/project/legisbr/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/felipeoes/legisbr/actions/workflows/ci.yml/badge.svg)](https://github.com/felipeoes/legisbr/actions/workflows/ci.yml)

[Quickstart](#quickstart) · [Python API](#python-api) · [CLI](#cli) · [MCP Server](#mcp-server) · [Sources](#sources) · [OCR](#ocr-for-scanned-pdfs)

</div>

```python
import legisbr

# Fetch a specific law directly as clean Markdown
law = legisbr.get("camara", number=14133, year=2021)
print(law.title)
print(law.text_markdown[:500])
```

## Quickstart

Requires **Python 3.11+**.

```bash
uv add legisbr              # add to a uv project (recommended)
pip install legisbr         # or install with pip
uv tool install legisbr     # install the CLI globally
uvx legisbr list            # or try the CLI without installing
```

## Why `legisbr`?

- **30 official government portals:** Federal legislation (Câmara dos Deputados), all 27 Brazilian states, CONAMA, and ICMBio — fetched directly from official portals, never scraping `leisestaduais.com.br`.
- **Sync by default, async when you scale:** Write clean synchronous scripts and notebooks with `legisbr.get()`, `legisbr.search()`, and `legisbr.Client`, or run high-throughput pipelines with `legisbr.AsyncClient`.
- **Clean Markdown from HTML, DOCX, and PDF:** Preserves legal structure (`Art.`, `§`, incisos), ruled tables, and inline images, with optional multi-provider Vision OCR for scanned PDFs.
- **Instant local search & Parquet export:** Store archives in SQLite with diacritic-insensitive Portuguese full-text search (FTS5), query across jurisdictions in milliseconds, and export directly to ZSTD Parquet.
- **Plug-and-play MCP server:** Connect Claude Desktop, Claude Code, or any MCP client to your local legislation archives with a single `uvx` command.
- **Lightweight, pure HTTP, 100% MIT core:** No headless browser runtime, fast startup, typed (`py.typed`), and permissive-only core dependencies on Linux, macOS, and Windows (Python 3.11–3.14).

## Python API

### 1. Fetch & Search Norms

Fetch a single norm directly with `legisbr.get()`, or run a metadata search with `legisbr.search()` to inspect typed `NormRef` results before downloading full text:

```python
import legisbr

# Fetch a specific law directly (raises NormNotFoundError if nothing matches)
law = legisbr.get("camara", number=14133, year=2021)
print(law.title, law.document_url)

# Metadata-only search → typed SearchResults[NormRef]
results = legisbr.search(
    "camara", year=2024, type="Lei Ordinária", text="energia", limit=5
)
for ref in results:
    print(ref.year, ref.title, ref.url)

# Fetch the top match or batch-fetch all matching norms
doc = results.one().fetch()     # use results.first for NormRef | None
docs = results.fetch()
```

Reuse connections, rate-limit pacing, and portal setup across multiple calls with `legisbr.Client`:

```python
with legisbr.Client() as client:
    law = client.get("camara", number=14133, year=2021)
    results = client.search("sp", year=2024, type="Lei", limit=5)
    docs = results.fetch()
```

### 2. Stream, Archive & Query Locally

Stream validated `Document` objects without writing to disk, or archive into SQLite, export to Parquet, and search locally:

```python
import legisbr

# Zero-disk streaming
for doc in legisbr.scrape("SP", year=2024, limit=10):
    print(doc.year, doc.title, len(doc.text_markdown))

# Archive into SQLite (save_raw=False cuts disk usage by 80-95%)
with legisbr.Client() as client:
    store = legisbr.Store.for_source(
        "SP", "outputs/legislation", save_raw=False, fts=True
    )
    result = client.run("SP", year=2024, limit=25, store=store)

# Export to Parquet (or .jsonl / .json — inferred from extension)
legisbr.export("outputs/legislation/SAO_PAULO/documents.db", "sp_2024.parquet")

# Instant diacritic-insensitive BM25 search across local archives
for hit in legisbr.query(
    "administracao publica", data_dir="outputs/legislation", year=2024, limit=5
):
    print(f"[{hit.source}] {hit.title}: {hit.snippet}")
```

For repeated local lookups (`sources()`, `query()`, `get(source, doc_id)`, `stats()`), open `with legisbr.ArchiveIndex("outputs/legislation") as index:`.

### 3. Async & High-Throughput Scraping

`AsyncClient` and `AsyncArchiveIndex` provide native async parity for async applications and concurrent pipelines:

```python
import asyncio
import legisbr


async def main() -> None:
    async with legisbr.AsyncClient() as client:
        law = await client.get("camara", number=14133, year=2021)
        results = await client.search("sp", year=2024, type="Lei", limit=5)
        docs = await client.fetch_many(results)

        async with client.scrape("AC", year=2024, limit=10) as stream:
            async for doc in stream:
                print(doc.title)


asyncio.run(main())
```

> **Small containers & CI runners:** Pass `low_memory=True` to `Client` or `AsyncClient` to cap workers (`<= 4`), use threaded PDF extraction, and shrink in-memory caches.

## OCR for Scanned PDFs

Install the `ocr` extra (`uv add "legisbr[ocr]"` or `pip install "legisbr[ocr]"`) and pass any [PydanticAI](https://ai.pydantic.dev/) model string (or comma-separated fallback list) to transcribe scanned PDFs automatically:

```python
import legisbr

with legisbr.Client(ocr="anthropic:claude-haiku-5-5") as client:
    for doc in client.scrape("PI", year=2024, limit=5):
        print(doc.title, len(doc.text_markdown))
```

Supports **OpenAI, Anthropic, Google, AWS Bedrock, Mistral, Groq, and Hugging Face** via PydanticAI using their standard provider environment variables (see [Configuration](#configuration)).

## CLI

Both `legisbr` and `python -m legisbr` expose the full CLI (source names, codes, and aliases are case-insensitive):

```bash
legisbr list                                                                  # list all 30 sources & capabilities
legisbr search -s Camara --year 2021 --number 14133 --strict-native --fetch   # live search + fetch Markdown
legisbr run -s SP --year 2024 --no-raw --fts                                  # scrape into SQLite + FTS5
legisbr query "licenciamento ambiental" --year 2024 --limit 20                # instant local BM25 search
legisbr export -s SP --format parquet --single-file --out exports/sp.parquet  # export to ZSTD Parquet
legisbr mcp serve --data-dir ./outputs/legislation                            # start MCP server for AI agents
```

Also includes `legisbr index --all` (build/rebuild FTS5 indexes), `legisbr import-json`, and `legisbr stats`.

## MCP Server

Give Claude Desktop, Claude Code, or any MCP-compatible AI agent instant, read-only BM25 search over local Brazilian legislation archives:

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

Tools exposed:

- `list_sources` — all 30 jurisdictions with local archive coverage and search capabilities.
- `search_legislation` — BM25 full-text search returning metadata and highlighted snippets.
- `get_legislation` — full norm Markdown by `(source, doc_id)` with `max_chars` truncation safety.
- `--allow-live-fetch` *(opt-in)* — enables `live_search_norms` and `live_fetch_norm` against official portals (`strict_native=True`, official-domain allowlist).

For remote HTTP clients, pass `--transport streamable-http --host 127.0.0.1 --port 8000`.

## Optional Extras

| Extra | `uv` (project) | `pip` / `uv pip` | Purpose |
| --- | --- | --- | --- |
| `ocr` | `uv add "legisbr[ocr]"` | `pip install "legisbr[ocr]"` | Scanned PDF OCR via PydanticAI (OpenAI, Anthropic, Google, Bedrock, Mistral, Groq, Hugging Face) |
| `proxy` | `uv add "legisbr[proxy]"` | `pip install "legisbr[proxy]"` | SOCKS/HTTP proxy rotation (`aiohttp-socks`, used for Paraná) |
| `parquet` | `uv add "legisbr[parquet]"` | `pip install "legisbr[parquet]"` | Fixed-schema streaming ZSTD Parquet export (`pyarrow`) |
| `mcp` | `uv add "legisbr[mcp]"` | `pip install "legisbr[mcp]"` | Model Context Protocol server (`mcp`) for AI agents |
| `all` | `uv add "legisbr[all]"` | `pip install "legisbr[all]"` | All optional extras combined (zero GPL/AGPL/LGPL) |

## Sources

**30 Official Portals:** 1 Federal · 27 States · 2 Environmental — *official portals only, never `leisestaduais.com.br`.*

<details>
<summary>View all 30 sources &amp; native search capabilities</summary>

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

</details>

## Configuration

Library code never auto-loads `.env` or mutates `os.environ`. The CLI loads `Settings.from_env(env_file=Path(".env"))` explicitly.

<details>
<summary>Environment variables (.env)</summary>

| Variable | Purpose | Default |
| --- | --- | --- |
| `SAVE_DIR` | Root directory for SQLite archives | `outputs/legislation` |
| `STATE_LEGISLATION_SAVE_DIR` | Optional override root for state archives | `SAVE_DIR` |
| `SPECIFIC_LEGISLATION_SAVE_DIR` | Optional override root for CONAMA / ICMBio | `SAVE_DIR` |
| `LOG_DIR` | Optional directory for per-scraper CLI log files | Empty (stderr only) |
| `LLM_MODEL` | PydanticAI model string or comma-separated fallback list (requires `legisbr[ocr]`) | Empty (OCR disabled) |
| `LLM_PDF_INPUT` | Send sliced PDF batches instead of 150 DPI rendered PNG pages | `false` |
| `LLM_BATCH_SIZE` | Pages per OCR request | `5` |
| `LLM_CONCURRENCY` | Maximum concurrent OCR batches per scraper process | `8` |
| `PDF_WORKERS` | Worker processes for PDF extraction (`0` forces thread mode) | `min(8, CPUs)` |
| `PROXY_FILE_PATH` | Optional newline-separated proxy list for Paraná (`legisbr[proxy]`) | Empty |
| `PROXY_ENDPOINT` | Optional proxy-list HTTP endpoint for Paraná (`legisbr[proxy]`) | Empty |

Provider credentials follow PydanticAI's standard environment variables (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `AWS_*` / `AWS_BEARER_TOKEN_BEDROCK`, `MISTRAL_API_KEY`, `GROQ_API_KEY`, `HF_TOKEN`).

</details>

## Legal Notice, Contributing & License

- **Public domain:** Official Brazilian laws, decrees, resolutions, and normative acts are excluded from copyright protection under **Lei nº 9.610/1998, art. 8º, I**.
- **Official portals only:** `legisbr` queries official government portals with conservative per-source rate limits and never scrapes `leisestaduais.com.br`.
- **No legal advice:** Extracted text reflects the official portal at scrape time and is provided as-is for research, indexing, and engineering; always verify authoritative texts before legal reliance.

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, the 10-minute broken-scraper fix guide, and the PR checklist.

Licensed under the [MIT License](LICENSE).
