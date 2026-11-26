# Contributing to `legisbr`

Thank you for helping keep Brazilian federal, state, and environmental legislation open, fast, and reliable!

## Development Setup

Requirements: **Python 3.11+** and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/felipeoes/legisbr.git
cd legisbr
uv sync --all-extras --group dev
uv run pytest tests -m "not integration" -n auto
```

If you prefer installing editable into an existing virtual environment:

```bash
uv pip install -e ".[all]"   # or: pip install -e ".[all]"
```

## Fix a Broken State Scraper in 10 Minutes

Government portals periodically tweak their HTML markup or JSON endpoints. Most fixes take just a few minutes:

1. **Locate the scraper** under `src/legisbr/scrapers/` (`states/<uf>.py`, `federal/camara.py`, or `environment/{conama,icmbio}.py`).
2. **Capture a minimal fixture** (the failing HTML fragment, JSON payload, or small PDF) directly from the official portal.
3. **Add an offline unit test** in `tests/scrapers/` using `make_scraper` and `make_mock_response` / `make_mock_json_response` from `tests/conftest.py`. Never hit live networks inside default unit tests (mark live smoke tests with `@pytest.mark.integration`).
4. **Update the parser** in the scraper module and run your targeted test:
   ```bash
   uv run pytest tests/scrapers/state/test_acre_scraper.py -v
   ```
5. **Run lint, formatting, and the offline suite:**
   ```bash
   uv run ruff check src/legisbr tests
   uv run ruff format --check src/legisbr tests
   uv run pytest tests -m "not integration" -n auto --timeout=60
   ```

## Adding a New Jurisdiction or Source

1. Subclass `BaseScraper` (from `legisbr.core.scraper`) in `src/legisbr/scrapers/`.
   - Keep constructors pure (attribute assignment only; do network setup in `async def setup()`).
   - Implement `list_type()` or `list_documents()` yielding `DocRef` dicts with stable `document_url`, `title`, `year`, `type`, and `situation`.
   - Build `Document` only inside `fetch()`. Prefix listing-only helper keys with `_` (e.g., `_pdf_link`).
   - Do not write custom retry loops or swallow exceptions — let `HttpClient` retries and `BaseScraper` error recording handle failures visibly.
   - Preserve images (`embed_images=True` for PDFs; inline HTML images before Markdown conversion).
2. Register a lazy `ScraperSpec` in `src/legisbr/registry.py` (string module path + class name so `legisbr list` never imports heavy modules eagerly).
3. Add unit tests covering listing pagination, metadata extraction, fetch conversion, error propagation, and resume behavior.

## Scraping Etiquette & Policy

- **Official sources only:** Never scrape third-party aggregators such as `leisestaduais.com.br`. Every scraper must target the official legislative assembly, government portal, or public agency directly.
- **Respect portal rate limits:** Set conservative `rps` on `ScraperSpec` (the built-in GCRA limiter enforces strict `burst=1` spacing so portals like Paraná are not flooded).
- **Secure TLS by default:** `HttpClient` enforces `verify_ssl=True`. Set `insecure_tls=True` on a `ScraperSpec` **only** when you have verified that the official government portal serves a broken certificate chain or legacy cipher.
- **Permissive licensing:** Core dependencies must remain strictly permissive (MIT, BSD, Apache-2.0, ISC, PSF-2.0, Zlib). Optional extras must contain zero GPL/AGPL/LGPL dependencies. Run `python scripts/check_licenses.py` to verify.

## Pull Request Checklist

- [ ] All modules include `from __future__ import annotations` (enforced by Ruff `I002`) and support Python 3.11+.
- [ ] `uv run ruff check src/legisbr tests` and `uv run ruff format --check src/legisbr tests` pass cleanly.
- [ ] `uv run pytest tests -m "not integration" -n auto --timeout=60` passes with no skipped regressions.
- [ ] New/updated parsers include offline regression tests with minimal fixtures (no secrets, proxy lists, or personal data).
