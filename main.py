"""Run scrapers and import/export their SQLite archives."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from legisbr.core.http import HttpClient
from legisbr.core.ocr import OCR
from legisbr.core.proxy import ProxyService
from legisbr.core.store import Store
from legisbr.registry import SCRAPERS, ScraperSpec
from legisbr.settings import Settings

logger = logging.getLogger(__name__)


def load_settings() -> Settings:
    """Load CLI settings from environment and optional local .env file."""
    return Settings.from_env(env_file=Path(".env"))


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
    logger.setLevel(level)
    if not logger.handlers:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(stream)
    for handler in logger.handlers:
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
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List scraper names and aliases")
    run = commands.add_parser("run", help="Scrape legislation")
    select = run.add_mutually_exclusive_group(required=True)
    select.add_argument("-s", "--scrapers", nargs="+")
    select.add_argument("--all", action="store_true")
    run.add_argument("--year", type=int)
    run.add_argument("--overwrite", action="store_true")
    run.add_argument("-v", "--verbose", action="store_true")
    run.add_argument("--log-file", type=Path, help="Optional path for a run log file")
    run.add_argument(
        "--jobs",
        type=int,
        help="Scraper processes at once when several are selected (default: all, up to CPU count)",
    )
    stats = commands.add_parser(
        "stats", help="Markdown table of each archive's last run"
    )
    stats.add_argument("-s", "--scrapers", nargs="+")
    export = commands.add_parser("export", help="Stream SQLite documents to JSON/JSONL")
    export.add_argument("-s", "--scrapers", nargs="+", required=True)
    export.add_argument("--format", choices=("jsonl", "json"), default="jsonl")
    export.add_argument("--out", type=Path)
    migration = commands.add_parser(
        "import-json", help="Import data.json and JSON shards without deleting inputs"
    )
    migration.add_argument("-s", "--scrapers", nargs="+", required=True)
    return parser.parse_args(argv)


def select_scrapers(names: list[str] | None) -> list[ScraperSpec]:
    if names is None:
        return SCRAPERS
    requested = {name.lower() for name in names}
    unknown = requested - set().union(*(spec.names for spec in SCRAPERS))
    if unknown:
        raise ValueError(f"Unknown scrapers: {', '.join(sorted(unknown))}")
    return [spec for spec in SCRAPERS if spec.names & requested]


def _database_for(spec: ScraperSpec, settings: Settings) -> Path:
    return spec.database_path(
        settings.save_dir,
        state_dir=settings.state_legislation_save_dir,
        specific_dir=settings.specific_legislation_save_dir,
    )


async def run_scraper(
    spec: ScraperSpec,
    args: argparse.Namespace,
    ocr: OCR | None,
    settings: Settings | None = None,
) -> bool:
    cfg = settings or load_settings()
    name = spec.name
    try:
        proxy_config = cfg.build_proxy_config() if spec.needs_proxy else None
        http = HttpClient(
            rps=spec.rps,
            max_retries=spec.max_retries,
            pause_on_403=spec.pause_on_403,
            disable_cookies=spec.disable_cookies,
            proxy_service=ProxyService(proxy_config) if proxy_config else None,
        )
        years = range(args.year, args.year + 1) if args.year is not None else spec.years
        scraper = spec.load_cls()(
            store=Store(_database_for(spec, cfg)),
            http=http,
            ocr=ocr if spec.needs_ocr else None,
            years=years,
            workers=spec.workers,
            overwrite=args.overwrite,
        )
        async with scraper:
            result = await scraper.run()
            total = sum(row["documents"] for row in await scraper.store.summary())
        print(
            f"{name}: {result.docs} saved, {result.skipped} skipped, {result.errors} errors; {total} total"
        )
        return result.errors == 0
    except Exception:
        logger.exception(f"{name} failed")
        return False


async def run_in_processes(specs: list[ScraperSpec], args: argparse.Namespace) -> int:
    """One process per scraper, so CPU-bound parsing in one never stalls the others."""
    jobs = args.jobs or min(len(specs), os.cpu_count() or 1)
    if jobs < 1:
        raise ValueError("--jobs must be positive")
    # LLM_CONCURRENCY applies per child: few sources need much OCR, and an even
    # split left the one that does with a single batch.
    env = {
        **os.environ,
        # PDF pools are 2x oversubscribed: most scrapers wait on the network.
        "PDF_WORKERS": str(max(1, min(8, 2 * (os.cpu_count() or 1) // jobs))),
    }
    extra = [
        *(("--year", str(args.year)) if args.year is not None else ()),
        *(("--overwrite",) if args.overwrite else ()),
        *(("--verbose",) if args.verbose else ()),
    ]
    gate = asyncio.Semaphore(jobs)

    async def child(spec: ScraperSpec) -> int:
        async with gate:
            process = await asyncio.create_subprocess_exec(
                sys.executable, __file__, "run", "-s", spec.name, *extra, env=env
            )
            try:
                return await process.wait()
            finally:
                if process.returncode is None:  # cancelled: don't leave orphans
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


async def main(
    argv: list[str] | None = None, *, settings: Settings | None = None
) -> int:
    args = parse_args(argv)
    if args.command == "list":
        for spec in SCRAPERS:
            print(
                f"{spec.cls_name.removesuffix('Scraper')} [{', '.join(spec.aliases)}] {spec.years.start}–{spec.years.stop - 1}"
            )
        return 0
    cfg = settings or load_settings()
    file_handler: logging.Handler | None = None
    try:
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
            async with AsyncExitStack() as stack:
                ocr = None
                if cfg.llm_model and any(spec.needs_ocr for spec in specs):
                    ocr = OCR(
                        cfg.llm_model,
                        batch_size=cfg.llm_batch_size,
                        concurrency=cfg.llm_concurrency,
                        pdf_input=cfg.llm_pdf_input,
                    )
                    await stack.enter_async_context(ocr.agent)
                ok = await run_scraper(specs[0], args, ocr, cfg)
            return 0 if ok else 1
        failed = False
        for spec in specs:
            try:
                database = _database_for(spec, cfg)
                if args.command == "export" and not database.exists():
                    raise FileNotFoundError(database)
                async with Store(database) as store:
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
                        count = await store.export(out, args.format)
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


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
