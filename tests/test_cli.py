"""Unit tests for the ``legisbr`` CLI (list, run, search, query, index, export, import-json, stats, mcp)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_store import document

from legisbr import cli
from legisbr.core.scraper import BaseScraper
from legisbr.core.store import Store
from legisbr.registry import SCRAPERS, ScraperSpec
from legisbr.settings import Settings


def test_selection_aliases_and_unknown_names():
    assert cli.select_scrapers(["PB"])[0].name == "PARAIBA"
    assert len(cli.select_scrapers(["Conama", "CONAMASCRAPER"])) == 1
    with pytest.raises(ValueError, match="Unknown"):
        cli.select_scrapers(["misspelled"])
    assert len(SCRAPERS) == 30


def test_registry_years_and_bahia_rate():
    bahia = cli.select_scrapers(["BA"])[0]
    assert bahia.rps == 1  # the server returns 500s above 1 request per second
    assert bahia.years.start == 1891 and bahia.years.stop == datetime.now().year + 1


def test_parana_stays_under_its_block_and_lists_without_session_state():
    parana = cli.select_scrapers(["PR"])[0]
    assert parana.rps * 60 < 20
    assert parana.pause_on_403 >= 30 * 60
    assert parana.disable_cookies
    assert parana.workers >= 20


def test_parse_years_range():
    assert cli.parse_years_range("2020:2024") == range(2020, 2025)
    assert cli.parse_years_range("2024") == range(2024, 2025)
    with pytest.raises(ValueError, match="start > end"):
        cli.parse_years_range("2025:2020")
    with pytest.raises(ValueError, match="START:END"):
        cli.parse_years_range("2020:2022:2024")


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["run"],
        ["run", "--all", "-s", "AC"],
        ["run", "-s", "AC", "--year", "2024", "--years", "2020:2024"],
    ],
)
def test_cli_requires_valid_selection_and_flags(argv):
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(argv)
    assert exc.value.code == 2


def test_list_markdown_and_json_and_lazy_imports(capsys):
    assert cli.main(["list"]) == 0
    md_out = capsys.readouterr().out
    assert md_out.startswith("| Code | Name |")
    assert "| camara | LEGISLACAO_FEDERAL |" in md_out
    assert "| ac | ACRE |" in md_out

    assert cli.main(["list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 30
    codes = {row["code"] for row in payload}
    assert {"camara", "conama", "icmbio", "sp", "ac", "ba", "pr"} <= codes

    # Fresh subprocess verifies `legisbr list` never imports heavy libraries.
    code = (
        "import sys, time; from legisbr.cli import main; "
        "t = time.perf_counter(); rc = main(['list']); dt = (time.perf_counter() - t) * 1000; "
        "assert rc == 0; "
        "forbidden = ('aiohttp', 'bs4', 'pydantic', 'pdf_oxide', 'pypdf', 'pyarrow', 'mcp'); "
        "loaded = [m for m in forbidden if m in sys.modules]; "
        "assert not loaded, f'Heavy modules imported: {loaded}'; "
        "print(f'OK {dt:.1f}ms')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], check=True, capture_output=True, text=True
    )
    assert "OK " in proc.stdout

    # python -m legisbr list works end-to-end.
    mod_proc = subprocess.run(
        [sys.executable, "-m", "legisbr", "list", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert len(json.loads(mod_proc.stdout)) == 30


@pytest.mark.parametrize("failed", [False, True])
async def test_exit_code_reflects_scraper_failure(monkeypatch, failed):
    monkeypatch.setattr(cli, "run_scraper", AsyncMock(return_value=not failed))
    assert await cli.async_main(
        ["run", "-s", "AC"], settings=Settings(llm_model="")
    ) == int(failed)


async def test_run_flags_no_raw_fts_low_memory_and_years_range(tmp_path, monkeypatch):
    observed: dict = {}

    class SpyScraper(BaseScraper):
        name = "SPY"
        base_url = "https://example.org"

        async def list_documents(self, year):
            yield {
                "year": year,
                "type": "Lei",
                "title": f"Lei 1/{year}",
                "document_url": f"https://example.org/{year}/1",
            }

        async def fetch(self, ref):
            observed["years"] = self.years
            observed["workers"] = self.workers
            observed["pdf_executor"] = self.pdf_executor
            observed["pdf_workers"] = self.pdf_workers
            observed["save_raw"] = self.store.save_raw
            observed["fts"] = self.store.fts
            observed["low_memory"] = self.store.low_memory
            return document(
                title=ref["title"],
                year=ref["year"],
                raw=b"<html>body</html>",
                raw_ext=".html",
            )

    spec = ScraperSpec(
        name="SPY",
        cls_name="SpyScraper",
        cls_path="tests.test_cli:SpyScraper",
        code="spy",
        scope="state",
        aliases=("spy",),
        start_year=2020,
        workers=16,
        _cls=SpyScraper,
    )
    monkeypatch.setattr(cli, "SCRAPERS", [spec])
    cfg = Settings(llm_model="", save_dir=tmp_path)
    rc = await cli.async_main(
        [
            "run",
            "-s",
            "spy",
            "--years",
            "2023:2024",
            "--no-raw",
            "--fts",
            "--low-memory",
            "--pdf-executor",
            "process",
        ],
        settings=cfg,
    )
    assert rc == 0
    assert observed == {
        "years": range(2023, 2025),
        "workers": 4,
        "pdf_executor": "thread",  # low-memory forces thread mode
        "pdf_workers": 1,
        "save_raw": False,
        "fts": True,
        "low_memory": True,
    }
    db = spec.database_path(tmp_path)
    async with Store(db) as store:
        assert len(await store.keys()) == 2
        hits = await store.search("administracao")
        assert len(hits) == 2
    # --no-raw skipped writing any raw HTML files on disk
    assert not list((db.parent).rglob("*.html"))


async def test_cleanup_failure_sets_exit_one(tmp_path, monkeypatch):
    class BrokenCleanup(BaseScraper):
        name = "BROKEN"
        base_url = "https://example.org"

        async def list_documents(self, year):
            if False:
                yield {}

        async def __aexit__(self, *exc):
            await super().__aexit__(*exc)
            raise RuntimeError("cleanup failed")

    spec = ScraperSpec(
        name="BROKEN",
        cls_name="BrokenCleanup",
        cls_path="tests.test_cli:BrokenCleanup",
        code="bad",
        scope="state",
        aliases=("bad",),
        start_year=2024,
        _cls=BrokenCleanup,
    )
    monkeypatch.setattr(cli, "SCRAPERS", [spec])
    cfg = Settings(llm_model="", save_dir=tmp_path, log_dir=tmp_path / "logs")
    assert await cli.async_main(["run", "-s", "bad"], settings=cfg) == 1
    assert (tmp_path / "logs" / "BROKEN.log").exists()


async def test_import_and_export_jsonl_and_parquet_single_file(tmp_path, monkeypatch):
    import pyarrow.parquet as pq

    spec = SCRAPERS[3]
    cfg = Settings(save_dir=tmp_path)
    database = spec.database_path(tmp_path)
    source = database.parent / "2024/data.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps([document().model_dump()]))
    monkeypatch.setattr(cli, "SCRAPERS", [spec])

    assert await cli.async_main(["import-json", "-s", "AC"], settings=cfg) == 0

    out_jsonl = tmp_path / "export_jsonl"
    assert (
        await cli.async_main(
            ["export", "-s", "AC", "--out", str(out_jsonl)], settings=cfg
        )
        == 0
    )
    assert (
        json.loads((out_jsonl / "2024/data.jsonl").read_text())["title"]
        == document().title
    )

    out_pq = tmp_path / "export_pq"
    assert (
        await cli.async_main(
            [
                "export",
                "-s",
                "AC",
                "--format",
                "parquet",
                "--single-file",
                "--out",
                str(out_pq),
            ],
            settings=cfg,
        )
        == 0
    )
    table = pq.read_table(out_pq / "data.parquet")
    assert table.num_rows == 1
    assert table.column("title").to_pylist() == [document().title]

    assert source.exists()
    source.write_text(json.dumps([{**document().model_dump(), "situation": ""}]))
    assert await cli.async_main(["import-json", "-s", "AC"], settings=cfg) == 1


class FakeProcess:
    def __init__(self, code: int | None) -> None:
        self.code, self.returncode = code, None
        self.stopped = asyncio.Event()

    async def wait(self) -> int:
        if self.code is None:
            await self.stopped.wait()
            return -15
        self.returncode = self.code
        return self.code

    def terminate(self) -> None:
        self.returncode = -15
        self.stopped.set()


def _three_specs():
    return [
        replace(SCRAPERS[3], needs_ocr=True),
        replace(SCRAPERS[4], needs_ocr=True),
        replace(SCRAPERS[5], needs_ocr=False),
    ]


async def test_several_scrapers_run_in_child_processes_via_module(
    tmp_path, monkeypatch
):
    specs = _three_specs()
    calls = []

    async def spawn(*argv, env):
        calls.append((argv, env))
        return FakeProcess(1 if specs[1].name in argv else 0)

    monkeypatch.setattr(cli, "SCRAPERS", specs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    cfg = Settings(save_dir=tmp_path, llm_concurrency=8)
    code = await cli.async_main(
        [
            "run",
            "--all",
            "--year",
            "2012",
            "--overwrite",
            "--no-raw",
            "--fts",
            "--low-memory",
            "--pdf-executor",
            "thread",
        ],
        settings=cfg,
    )
    assert code == 1
    assert sorted(argv[argv.index("-s") + 1] for argv, _ in calls) == sorted(
        spec.name for spec in specs
    )
    for argv, env in calls:
        assert argv[:4] == (sys.executable, "-m", "legisbr", "run")
        assert "--no-raw" in argv and "--fts" in argv and "--low-memory" in argv
        assert "--pdf-executor" in argv and "thread" in argv
        assert env.get("LLM_CONCURRENCY") == cli.os.environ.get("LLM_CONCURRENCY")
        assert int(env["PDF_WORKERS"]) >= 1


async def test_cancelled_runner_terminates_its_children(tmp_path, monkeypatch):
    processes = []

    async def spawn(*argv, env):
        processes.append(FakeProcess(None))
        return processes[-1]

    monkeypatch.setattr(cli, "SCRAPERS", _three_specs())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    cfg = Settings(save_dir=tmp_path)
    task = asyncio.create_task(
        cli.async_main(["run", "--all", "--jobs", "2"], settings=cfg)
    )
    while len(processes) < 2:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(processes) == 2
    assert all(process.returncode == -15 for process in processes)


async def test_stats_command_prints_latest_run_per_archive(
    tmp_path, monkeypatch, capsys
):
    spec = SCRAPERS[3]
    cfg = Settings(save_dir=tmp_path)
    async with Store(spec.database_path(tmp_path)) as store:
        old = await store.start_run(spec.name)
        await store.finish_run(old, 1, 0, {}, {"seconds": 1.0, "docs": 1})
        run = await store.start_run(spec.name)
        stats = {
            "seconds": 90.0,
            "listed": 166,
            "docs": 166,
            "docs_per_minute": 110.7,
            "fetch_p50": 0.4,
            "http": {"requests": 400, "retries": 2, "bytes": 5_000_000},
        }
        await store.finish_run(run, 166, 0, {"m": {"requests": 3}}, stats)
    monkeypatch.setattr(cli, "SCRAPERS", [spec])
    assert await cli.async_main(["stats"], settings=cfg) == 0
    out = capsys.readouterr().out
    assert out.startswith("| Scraper |")
    row = next(line for line in out.splitlines() if spec.name in line)
    assert (
        "| 166 |" in row and "| 90 |" in row and "| 110.7 |" in row and "| 4.8 |" in row
    )


async def test_index_and_query_commands_over_real_fts5_archive(tmp_path, capsys):
    sp_db = tmp_path / "SAO_PAULO" / "documents.db"
    async with Store(sp_db, save_raw=False, fts=False, source="SAO_PAULO") as store:
        await store.save(
            document(
                title="Lei Estadual nº 17.890/2024",
                year=2024,
                summary="Dispõe sobre licenciamento ambiental simplificado.",
                text_markdown="Art. 1º Fica instituída a política de licenciamento ambiental e saneamento básico estadual.",
                document_url="https://www.al.sp.gov.br/norma/17890",
            )
        )

    cfg = Settings(save_dir=tmp_path)
    assert (
        await cli.async_main(
            ["index", "-s", "SP", "--data-dir", str(tmp_path)], settings=cfg
        )
        == 0
    )
    assert "SAO_PAULO: indexed 1 documents" in capsys.readouterr().out

    # Query via alias `-s SP` and Portuguese diacritic-insensitive query
    assert (
        await cli.async_main(
            [
                "query",
                "saneamento basico",
                "--data-dir",
                str(tmp_path),
                "-s",
                "SP",
                "--json",
            ],
            settings=cfg,
        )
        == 0
    )
    hits = json.loads(capsys.readouterr().out)
    assert len(hits) == 1
    assert hits[0]["source"] == "SAO_PAULO"
    assert "17.890" in hits[0]["title"]

    # Markdown output path
    assert (
        await cli.async_main(
            ["query", "licenciamento", "--data-dir", str(tmp_path)], settings=cfg
        )
        == 0
    )
    assert "SAO_PAULO" in capsys.readouterr().out


async def test_search_command_with_and_without_fetch(monkeypatch, capsys):
    fake_ref = {
        "year": 2021,
        "type": "Lei Ordinária",
        "title": "Lei nº 14.133/2021",
        "summary": "Lei de Licitações e Contratos Administrativos.",
        "situation": "Não consta revogação expressa",
        "document_url": "https://www2.camara.leg.br/legin/fed/lei/2021/lei-14133.html",
    }
    fake_doc = document(
        year=2021,
        type="Lei Ordinária",
        title="Lei nº 14.133/2021",
        summary="Lei de Licitações.",
        text_markdown="# Lei nº 14.133/2021\n\nArt. 1º Esta Lei estabelece normas gerais de licitação e contratação.",
        document_url=fake_ref["document_url"],
    )

    created_clients: list[FakeClient] = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created_clients.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def search_refs(self, source, **kwargs):
            yield fake_ref

        async def fetch_ref(self, source, ref):
            return fake_doc

    monkeypatch.setattr("legisbr.client.Client", FakeClient)
    cfg = Settings(llm_model="anthropic:claude-haiku-5-5")

    assert (
        await cli.async_main(
            [
                "search",
                "-s",
                "Camara",
                "--year",
                "2021",
                "--number",
                "14133",
                "--strict-native",
                "--format",
                "json",
            ],
            settings=cfg,
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1 and out[0]["title"] == "Lei nº 14.133/2021"
    # Metadata-only search never passes an OCR model to Client.
    assert created_clients[-1].kwargs["ocr"] is None

    assert (
        await cli.async_main(
            ["search", "-s", "Camara", "--year", "2021", "--fetch"], settings=cfg
        )
        == 0
    )
    md = capsys.readouterr().out
    assert "# Lei nº 14.133/2021" in md
    assert "normas gerais de licitação" in md
    assert created_clients[-1].kwargs["ocr"] == "anthropic:claude-haiku-5-5"


def test_load_settings_exports_dotenv_provider_keys_and_respects_save_dir(
    tmp_path, monkeypatch
):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "LLM_MODEL=anthropic:claude-haiku-5-5",
                "ANTHROPIC_API_KEY=sk-ant-from-dotenv",
                "OPENAI_API_KEY=sk-openai-from-dotenv",
                f"STATE_LEGISLATION_SAVE_DIR={tmp_path / 'dotenv_states'}",
                f"SPECIFIC_LEGISLATION_SAVE_DIR={tmp_path / 'dotenv_specific'}",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    for key in (
        "LLM_MODEL",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "SAVE_DIR",
        "STATE_LEGISLATION_SAVE_DIR",
        "SPECIFIC_LEGISLATION_SAVE_DIR",
    ):
        monkeypatch.setenv(key, "")
        cli.os.environ.pop(key, None)

    override_dir = tmp_path / "cli_override"
    monkeypatch.setenv("SAVE_DIR", str(override_dir))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-from-shell")

    cfg = cli.load_settings()

    assert cfg.llm_model == "anthropic:claude-haiku-5-5"
    assert cfg.save_dir == override_dir
    assert cfg.state_legislation_save_dir is None
    assert cfg.specific_legislation_save_dir is None
    # Provider key from .env exported for pydantic_ai; existing shell key preserved.
    assert cli.os.environ["ANTHROPIC_API_KEY"] == "sk-ant-from-dotenv"
    assert cli.os.environ["OPENAI_API_KEY"] == "sk-openai-from-shell"
    # Scoped dirs from .env did NOT leak into os.environ when SAVE_DIR was explicitly set.
    assert "STATE_LEGISLATION_SAVE_DIR" not in cli.os.environ
    assert "SPECIFIC_LEGISLATION_SAVE_DIR" not in cli.os.environ
    # Both run (`_database_for`) and query (`cfg.save_dir`) resolve inside override_dir.
    conama = cli.select_scrapers(["CONAMA"])[0]
    assert cli._database_for(conama, cfg) == override_dir / "CONAMA" / "documents.db"


def test_mcp_serve_command_wires_to_run_mcp_server(tmp_path, monkeypatch):
    spy = MagicMock()
    monkeypatch.setattr("legisbr.mcp_server.run_mcp_server", spy)
    cfg = Settings(save_dir=tmp_path)
    rc = cli.main(
        [
            "mcp",
            "serve",
            "--data-dir",
            str(tmp_path),
            "--allow-live-fetch",
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
        ],
        settings=cfg,
    )
    assert rc == 0
    spy.assert_called_once_with(
        tmp_path,
        allow_live_fetch=True,
        transport="streamable-http",
        host="0.0.0.0",
        port=9000,
    )
