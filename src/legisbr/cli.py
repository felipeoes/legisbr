"""Command-line interface for scraping, searching, indexing, exporting, and serving Brazilian legislation."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from legisbr.registry import SCRAPERS, ScraperSpec, get_spec
from legisbr.settings import Settings, export_dotenv

logger = logging.getLogger(__name__)


def load_settings() -> Settings:
    """Load CLI settings from environment and optional local .env file."""
    env_file = Path(".env")
    settings = Settings.from_env(env_file=env_file)
    export_dotenv(env_file)
    return settings


def parse_years_range(raw: str) -> range:
    """Parse an inclusive ``START:END`` year range (or single year ``YYYY``)."""
    parts = [p.strip() for p in raw.split(":")]
    if len(parts) == 1:
        year = int(parts[0])
        return range(year, year + 1)
    if len(parts) == 2:
        start, end = int(parts[0]), int(parts[1])
        if start > end:
            raise ValueError(f"Invalid --years range {raw!r}: start > end")
        return range(start, end + 1)
    raise ValueError(f"Invalid --years range {raw!r}: expected START:END")


def _resolve_years(
    year: int | None, years_str: str | None, default: range | None = None
) -> range | None:
    if year is not None:
        return range(year, year + 1)
    if years_str is not None:
        return parse_years_range(years_str)
    return default


def _configure_logging(
    *, verbose: bool, log_file: Path | None = None
) -> logging.Handler | None:
    level = logging.DEBUG if verbose else logging.WARNING
    root = logging.getLogger("legisbr")
    root.setLevel(level)
    if not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    ):
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(stream)
    for handler in root.handlers:
        handler.setLevel(level)
    if log_file is None:
        return None
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(file_handler)
    return file_handler


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="legisbr", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    # list
    list_cmd = commands.add_parser("list", help="List all 30 sources and capabilities")
    list_cmd.add_argument("--json", action="store_true", help="Output JSON array")

    # run
    run = commands.add_parser("run", help="Scrape legislation into SQLite archives")
    select = run.add_mutually_exclusive_group(required=True)
    select.add_argument("-s", "--scrapers", nargs="+")
    select.add_argument("--all", action="store_true")
    year_group = run.add_mutually_exclusive_group()
    year_group.add_argument("--year", type=int, help="Scrape a single year")
    year_group.add_argument(
        "--years", type=str, help="Inclusive year range START:END (e.g. 2020:2024)"
    )
    run.add_argument("--overwrite", action="store_true")
    run.add_argument(
        "--no-raw",
        action="store_true",
        help="Skip saving raw HTML/PDF files (80-95%% disk savings)",
    )
    run.add_argument(
        "--fts",
        action="store_true",
        help="Maintain SQLite FTS5 full-text index while scraping",
    )
    run.add_argument(
        "--low-memory",
        action="store_true",
        help="Low-memory preset (workers<=4, thread PDF, 2 MB image cache)",
    )
    run.add_argument(
        "--pdf-executor",
        choices=("auto", "process", "thread"),
        default="auto",
        help="PDF extraction executor mode",
    )
    run.add_argument("-v", "--verbose", action="store_true")
    run.add_argument("--log-file", type=Path, help="Optional path for a run log file")
    run.add_argument(
        "--jobs",
        type=int,
        help="Scraper processes at once when several are selected (default: all, up to CPU count)",
    )

    # search (live on-demand)
    search = commands.add_parser(
        "search", help="Live on-demand norm search against an official portal"
    )
    search.add_argument("-s", "--source", required=True, help="Source code or alias")
    search_years = search.add_mutually_exclusive_group()
    search_years.add_argument("--year", type=int)
    search_years.add_argument("--years", type=str, help="Inclusive range START:END")
    search.add_argument("--number", type=str)
    search.add_argument("--type", type=str)
    search.add_argument("--text", type=str)
    search.add_argument("--situation", type=str)
    search.add_argument("--limit", type=int, default=10)
    search.add_argument(
        "--strict-native",
        action="store_true",
        help="Refuse client-side full scans when portal lacks native filter support",
    )
    search.add_argument(
        "--fetch",
        action="store_true",
        help="Also fetch and convert full Markdown text for matched refs",
    )
    search.add_argument("--format", choices=("markdown", "json"), default="markdown")

    # query (local FTS5)
    query = commands.add_parser(
        "query", help="Full-text BM25 search across local SQLite FTS5 archives"
    )
    query.add_argument("query", help="FTS5 search query")
    query.add_argument("--data-dir", type=Path, help="Archive root directory")
    query.add_argument("-s", "--sources", nargs="+", help="Restrict to sources")
    query.add_argument("--year", type=int)
    query.add_argument("--type", type=str, dest="doc_type")
    query.add_argument("--limit", type=int, default=20)
    query.add_argument("--json", action="store_true", help="Output JSON array")

    # index (FTS5 builder)
    index_cmd = commands.add_parser(
        "index", help="Build or rebuild SQLite FTS5 indexes on existing archives"
    )
    idx_select = index_cmd.add_mutually_exclusive_group(required=True)
    idx_select.add_argument("-s", "--scrapers", nargs="+")
    idx_select.add_argument("--all", action="store_true")
    index_cmd.add_argument("--data-dir", type=Path, help="Archive root directory")

    # export
    export = commands.add_parser(
        "export", help="Stream SQLite documents to Parquet, JSONL, or JSON"
    )
    export.add_argument("-s", "--scrapers", nargs="+", required=True)
    export.add_argument(
        "--format", choices=("parquet", "jsonl", "json"), default="jsonl"
    )
    export.add_argument(
        "--single-file",
        action="store_true",
        help=(
            "Write a single combined file (data.parquet, data.jsonl, or data.json) "
            "instead of per-year shards"
        ),
    )
    export.add_argument("--out", type=Path)

    # import-json
    migration = commands.add_parser(
        "import-json", help="Import data.json and JSON shards without deleting inputs"
    )
    migration.add_argument("-s", "--scrapers", nargs="+", required=True)

    # stats
    stats = commands.add_parser(
        "stats", help="Markdown table of each archive's last run"
    )
    stats.add_argument("-s", "--scrapers", nargs="+")

    # mcp serve
    mcp_cmd = commands.add_parser("mcp", help="Model Context Protocol server")
    mcp_sub = mcp_cmd.add_subparsers(dest="mcp_command", required=True)
    serve = mcp_sub.add_parser("serve", help="Run the legisbr MCP server")
    serve.add_argument("--data-dir", type=Path, help="Archive root directory")
    serve.add_argument(
        "--allow-live-fetch",
        action="store_true",
        help="Enable live_search_norms and live_fetch_norm tools",
    )
    serve.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    return parser.parse_args(argv)


def select_scrapers(names: list[str] | None) -> list[ScraperSpec]:
    if names is None:
        return SCRAPERS
    requested = {name.lower() for name in names}
    unknown = requested - set().union(*(spec.names for spec in SCRAPERS))
    if unknown:
        raise ValueError(f"Unknown scrapers: {', '.join(sorted(unknown))}")
    return [spec for spec in SCRAPERS if spec.names & requested]


def _database_for(
    spec: ScraperSpec, settings: Settings, *, data_dir: Path | None = None
) -> Path:
    if data_dir is not None:
        return spec.database_path(data_dir)
    return spec.database_path(
        settings.save_dir,
        state_dir=settings.state_legislation_save_dir,
        specific_dir=settings.specific_legislation_save_dir,
    )


def _format_capabilities(spec: ScraperSpec) -> str:
    caps = spec.capabilities
    if caps.fast_catalog:
        return "fast-catalog"
    if caps.heavy_setup:
        return "heavy-setup"
    flags: list[str] = []
    if caps.native_year:
        flags.append("year")
    if caps.native_type:
        flags.append("type")
    if caps.native_number:
        flags.append("num")
    if caps.native_text:
        flags.append("text")
    if caps.native_situation:
        flags.append("sit")
    if caps.direct_id:
        flags.append("id")
    return ",".join(flags) if flags else "none"


def _run_list(as_json: bool) -> int:
    current_year = datetime.now().year
    if as_json:
        import json

        payload = [
            {
                **asdict(spec.info()),
                "aliases": list(spec.aliases),
                "capabilities": asdict(spec.capabilities),
            }
            for spec in SCRAPERS
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    headers = (
        "Code",
        "Name",
        "Scope",
        "Years",
        "PDF",
        "OCR",
        "TLS",
        "Search",
        "Aliases",
    )
    print("| " + " | ".join(headers) + " |")
    print("|" + "---|" * len(headers))
    for spec in SCRAPERS:
        cells = (
            spec.code,
            spec.name,
            spec.scope,
            f"{spec.start_year}–{current_year}",
            "yes" if spec.uses_pdf else "no",
            "yes" if spec.needs_ocr else "no",
            "insecure" if spec.insecure_tls else "strict",
            _format_capabilities(spec),
            ", ".join(spec.aliases) or "–",
        )
        print("| " + " | ".join(cells) + " |")
    return 0


async def run_scraper(
    spec: ScraperSpec,
    args: argparse.Namespace,
    ocr: Any | None,
    settings: Settings | None = None,
) -> bool:
    from legisbr.core.http import HttpClient
    from legisbr.core.images import ImageCache
    from legisbr.core.pdf import shutdown_pdf_pool
    from legisbr.core.proxy import ProxyService
    from legisbr.core.store import Store

    cfg = settings or load_settings()
    name = spec.name
    try:
        low_memory = bool(getattr(args, "low_memory", False))
        workers = min(spec.workers, 4) if low_memory else spec.workers
        pdf_executor = "thread" if low_memory else getattr(args, "pdf_executor", "auto")
        pdf_workers = 1 if low_memory else cfg.pdf_workers
        cache_mb = 2 if low_memory else 16
        proxy_config = cfg.build_proxy_config() if spec.needs_proxy else None
        http = HttpClient(
            rps=spec.rps,
            max_retries=spec.max_retries,
            pause_on_403=spec.pause_on_403,
            disable_cookies=spec.disable_cookies,
            verify_ssl=not spec.insecure_tls,
            max_connections=max(100, workers),
            max_connections_per_host=max(16, workers),
            proxy_service=ProxyService(proxy_config) if proxy_config else None,
            image_cache=ImageCache(max_bytes=cache_mb * 1024 * 1024),
        )
        years = _resolve_years(
            getattr(args, "year", None),
            getattr(args, "years", None),
            default=spec.years,
        )
        assert years is not None
        store = Store(
            _database_for(spec, cfg),
            save_raw=not bool(getattr(args, "no_raw", False)),
            fts=bool(getattr(args, "fts", False)),
            source=spec.name,
            low_memory=low_memory,
        )
        scraper = spec.load_cls()(
            store=store,
            http=http,
            ocr=ocr if spec.needs_ocr else None,
            years=years,
            workers=workers,
            overwrite=bool(getattr(args, "overwrite", False)),
            pdf_executor=pdf_executor,
            pdf_workers=pdf_workers,
        )
        async with scraper:
            result = await scraper.run()
            total = sum(row["documents"] for row in await store.summary())
        print(
            f"{name}: {result.docs} saved, {result.skipped} skipped, {result.errors} errors; {total} total"
        )
        return result.errors == 0
    except Exception:
        logger.exception(f"{name} failed")
        return False
    finally:
        shutdown_pdf_pool()


async def run_in_processes(specs: list[ScraperSpec], args: argparse.Namespace) -> int:
    """One process per scraper, so CPU-bound parsing in one never stalls the others."""
    import asyncio

    jobs = args.jobs or min(len(specs), os.cpu_count() or 1)
    if jobs < 1:
        raise ValueError("--jobs must be positive")
    env = {
        **os.environ,
        "PDF_WORKERS": str(max(1, min(8, 2 * (os.cpu_count() or 1) // jobs))),
    }
    extra = [
        *(("--year", str(args.year)) if args.year is not None else ()),
        *(("--years", str(args.years)) if getattr(args, "years", None) else ()),
        *(("--overwrite",) if args.overwrite else ()),
        *(("--no-raw",) if getattr(args, "no_raw", False) else ()),
        *(("--fts",) if getattr(args, "fts", False) else ()),
        *(("--low-memory",) if getattr(args, "low_memory", False) else ()),
        *(
            ("--pdf-executor", args.pdf_executor)
            if getattr(args, "pdf_executor", "auto") != "auto"
            else ()
        ),
        *(("--verbose",) if args.verbose else ()),
        *(
            ("--log-file", str(args.log_file))
            if getattr(args, "log_file", None) is not None
            else ()
        ),
    ]
    gate = asyncio.Semaphore(jobs)

    async def child(spec: ScraperSpec) -> int:
        async with gate:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "legisbr",
                "run",
                "-s",
                spec.name,
                *extra,
                env=env,
            )
            try:
                return await process.wait()
            finally:
                if process.returncode is None:
                    process.terminate()
                    await process.wait()

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(child(spec)) for spec in specs]
    return int(any(task.result() for task in tasks))


_STATS_COLUMNS = (
    "Scraper",
    "Years",
    "Listed",
    "Docs",
    "Errors",
    "Seconds",
    "Listing s",
    "Docs/min",
    "Fetch p50 s",
    "Fetch p95 s",
    "Fetch max s",
    "HTTP req",
    "Retries",
    "MB",
    "LLM req",
)


async def print_stats(specs: list[ScraperSpec], settings: Settings) -> None:
    import json

    from legisbr.core.store import Store

    print("| " + " | ".join(_STATS_COLUMNS) + " |")
    print("|" + "---|" * len(_STATS_COLUMNS))
    for spec in specs:
        database = _database_for(spec, settings)
        if not database.exists():
            continue
        async with Store(database) as store:
            row = store.connection.execute(
                "SELECT llm_usage, stats FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            continue
        stats = json.loads(row["stats"])
        http = stats.get("http", {})
        llm = sum(
            usage.get("requests", 0) for usage in json.loads(row["llm_usage"]).values()
        )
        cells = [
            spec.name,
            stats.get("years"),
            stats.get("listed"),
            stats.get("docs"),
            stats.get("errors"),
            stats.get("seconds"),
            stats.get("listing_seconds"),
            stats.get("docs_per_minute"),
            stats.get("fetch_p50"),
            stats.get("fetch_p95"),
            stats.get("fetch_max"),
            http.get("requests"),
            http.get("retries"),
            round(http["bytes"] / 2**20, 1) if "bytes" in http else None,
            llm,
        ]
        cells[5:7] = [f"{v:.0f}" if v is not None else None for v in cells[5:7]]
        print("| " + " | ".join("–" if c is None else str(c) for c in cells) + " |")


async def _run_search(args: argparse.Namespace, cfg: Settings) -> int:
    import json

    from legisbr.client import Client

    years = _resolve_years(args.year, args.years, default=None)
    items: list[dict[str, Any]] = []
    async with Client(
        ocr=(cfg.llm_model or None) if args.fetch else None,
        proxy_file=cfg.proxy_file_path,
        proxy_endpoint=cfg.proxy_endpoint,
    ) as client:
        async for ref in client.search_refs(
            args.source,
            years=years,
            number=args.number,
            type=args.type,
            text=args.text,
            situation=args.situation,
            limit=args.limit,
            strict_native=args.strict_native,
        ):
            if args.fetch:
                doc = await client.fetch_ref(args.source, ref)
                items.append(doc.model_dump(exclude={"raw", "raw_ext"}))
            else:
                items.append(dict(ref))

    if args.format == "json":
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0

    if not items:
        print("No matching documents found.")
        return 0
    if args.fetch:
        blocks = []
        for item in items:
            header = f"# {item.get('title', '')}\n\n"
            meta = (
                f"- **Year:** {item.get('year')} | **Type:** {item.get('type')} | "
                f"**Situation:** {item.get('situation')}\n"
                f"- **URL:** {item.get('document_url')}\n\n"
            )
            blocks.append(header + meta + str(item.get("text_markdown", "")))
        print("\n\n---\n\n".join(blocks))
    else:
        for idx, item in enumerate(items, 1):
            summary = f" — {item['summary']}" if item.get("summary") else ""
            print(
                f"{idx}. **{item.get('title', '')}** ({item.get('year', '?')}, "
                f"{item.get('type', '')}){summary}\n   {item.get('document_url', '')}"
            )
    return 0


async def _run_query(args: argparse.Namespace, cfg: Settings) -> int:
    import json

    from legisbr.core.archive import ArchiveIndex

    data_dir = args.data_dir or cfg.save_dir
    resolved_sources: list[str] | None = None
    if args.sources:
        resolved_sources = []
        for raw in args.sources:
            try:
                resolved_sources.append(get_spec(raw).name)
            except ValueError:
                resolved_sources.append(raw)
    index = ArchiveIndex(data_dir)
    hits = await index.query(
        args.query,
        sources=resolved_sources,
        year=args.year,
        doc_type=args.doc_type,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps([asdict(hit) for hit in hits], ensure_ascii=False, indent=2))
        return 0
    if not hits:
        print("No matches found.")
        return 0
    for idx, hit in enumerate(hits, 1):
        print(
            f"{idx}. [{hit.source} #{hit.id}] **{hit.title}** ({hit.year}, {hit.type})\n"
            f"   {hit.document_url}\n"
            f"   > {hit.snippet}"
        )
    return 0


async def _run_index(args: argparse.Namespace, cfg: Settings) -> int:
    from legisbr.core.store import Store

    specs = select_scrapers(None if args.all else args.scrapers)
    failed = False
    for spec in specs:
        database = _database_for(spec, cfg, data_dir=args.data_dir)
        if not database.exists():
            if args.all:
                continue
            logger.error(f"{spec.name}: archive not found at {database}")
            failed = True
            continue
        try:
            async with Store(database, source=spec.name) as store:
                count = await store.rebuild_fts()
            print(f"{spec.name}: indexed {count} documents")
        except Exception:
            logger.exception(f"{spec.name}: index failed")
            failed = True
    return int(failed)


async def async_main(
    argv: list[str] | None = None, *, settings: Settings | None = None
) -> int:
    args = parse_args(argv)
    if args.command == "list":
        return _run_list(as_json=args.json)

    cfg = settings or load_settings()
    if args.command == "mcp" and args.mcp_command == "serve":
        from legisbr.mcp_server import run_mcp_server

        data_dir = args.data_dir or cfg.save_dir
        run_mcp_server(
            data_dir,
            allow_live_fetch=args.allow_live_fetch,
            transport=args.transport,
            host=args.host,
            port=args.port,
        )
        return 0

    file_handler: logging.Handler | None = None
    try:
        if args.command == "search":
            _configure_logging(verbose=False)
            return await _run_search(args, cfg)
        if args.command == "query":
            _configure_logging(verbose=False)
            return await _run_query(args, cfg)
        if args.command == "index":
            _configure_logging(verbose=False)
            return await _run_index(args, cfg)

        specs = select_scrapers(None if getattr(args, "all", False) else args.scrapers)
        log_file: Path | None = getattr(args, "log_file", None)
        if (
            log_file is None
            and args.command == "run"
            and len(specs) == 1
            and cfg.log_dir is not None
        ):
            log_file = cfg.log_dir / f"{specs[0].name}.log"
        file_handler = _configure_logging(
            verbose=getattr(args, "verbose", False), log_file=log_file
        )
        if args.command == "stats":
            await print_stats(specs, cfg)
            return 0
        if args.command == "run" and len(specs) > 1:
            return await run_in_processes(specs, args)
        if args.command == "run":
            from legisbr.core.ocr import OCR

            async with AsyncExitStack() as stack:
                ocr = None
                if cfg.llm_model and any(spec.needs_ocr for spec in specs):
                    low_memory = bool(getattr(args, "low_memory", False))
                    ocr = OCR(
                        cfg.llm_model,
                        batch_size=cfg.llm_batch_size,
                        concurrency=2 if low_memory else cfg.llm_concurrency,
                        pdf_input=cfg.llm_pdf_input,
                        dpi=120 if low_memory else 150,
                    )
                    await stack.enter_async_context(ocr.agent)
                ok = await run_scraper(specs[0], args, ocr, cfg)
            return 0 if ok else 1

        from legisbr.core.store import Store

        failed = False
        for spec in specs:
            try:
                database = _database_for(spec, cfg)
                if args.command == "export" and not database.exists():
                    raise FileNotFoundError(database)
                async with Store(database, source=spec.name) as store:
                    if args.command == "import-json":
                        count, rejected = await store.import_json(database.parent)
                        if rejected:
                            failed = True
                            print(
                                f"{spec.name}: {rejected} invalid rows rejected (see errors table)"
                            )
                    else:
                        out = args.out or database.parent / "export"
                        if args.out and len(specs) > 1:
                            out /= spec.name
                        count = await store.export(
                            out,
                            args.format,
                            single_file=bool(getattr(args, "single_file", False)),
                        )
                print(f"{spec.name}: {count} documents {args.command}")
            except Exception:
                logger.exception(f"{spec.name}: {args.command} failed")
                failed = True
        return int(failed)
    except Exception:
        logger.exception("Command failed")
        return 1
    finally:
        if file_handler is not None:
            logging.getLogger("legisbr").removeHandler(file_handler)
            file_handler.close()


def main(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    """Synchronous CLI entrypoint returning a process exit code."""
    args = parse_args(argv)
    if args.command == "list":
        return _run_list(as_json=args.json)
    if args.command == "mcp" and args.mcp_command == "serve":
        from legisbr.mcp_server import run_mcp_server

        cfg = settings or load_settings()
        data_dir = args.data_dir or cfg.save_dir
        run_mcp_server(
            data_dir,
            allow_live_fetch=args.allow_live_fetch,
            transport=args.transport,
            host=args.host,
            port=args.port,
        )
        return 0
    import asyncio

    try:
        return asyncio.run(async_main(argv, settings=settings))
    except KeyboardInterrupt:
        return 130


def entrypoint() -> None:
    """Console script target for ``legisbr`` and ``python -m legisbr``."""
    raise SystemExit(main())
