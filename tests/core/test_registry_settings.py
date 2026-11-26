"""Unit tests for lazy registry, PEP 562 package exports, Settings, and zero side effects."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import legisbr
from legisbr.core.http import HttpClient
from legisbr.core.scraper import BaseScraper
from legisbr.core.store import Store
from legisbr.registry import SCRAPERS, SourceInfo, get_spec, sources
from legisbr.settings import Settings, export_dotenv


def test_all_30_sources_resolve_cleanly_and_match_classes():
    assert len(SCRAPERS) == 30
    all_infos = sources()
    assert len(all_infos) == 30
    assert all(isinstance(info, SourceInfo) for info in all_infos)
    assert {spec.code for spec in SCRAPERS if spec.insecure_tls} == {"ac", "ba"}

    seen_names: set[str] = set()
    for spec in SCRAPERS:
        # Pairwise disjoint lookup names across all 30 sources
        overlap = seen_names & spec.names
        assert not overlap, f"Duplicate names for {spec.name}: {overlap}"
        seen_names |= spec.names

        for alias in spec.names:
            assert get_spec(alias) is spec

        cls = spec.load_cls()
        assert cls is spec.cls
        assert cls.name == spec.name
        assert cls.__name__ == spec.cls_name


def test_database_path_routes_by_scope(tmp_path: Path):
    camara = get_spec("camara")
    conama = get_spec("conama")
    acre = get_spec("ac")

    base = tmp_path / "base"
    states = tmp_path / "states"
    env = tmp_path / "env"

    assert camara.database_path(base, state_dir=states, specific_dir=env) == (
        base / "LEGISLACAO_FEDERAL" / "documents.db"
    )
    assert conama.database_path(base, state_dir=states, specific_dir=env) == (
        env / "CONAMA" / "documents.db"
    )
    assert acre.database_path(base, state_dir=states, specific_dir=env) == (
        states / "ACRE" / "documents.db"
    )
    assert acre.database_path(base) == base / "ACRE" / "documents.db"


def test_import_legisbr_and_sources_is_lazy_without_heavy_imports():
    code = (
        "import sys, legisbr; "
        "infos = legisbr.sources(); "
        "assert len(infos) == 30; "
        "forbidden = ('aiohttp', 'bs4', 'pydantic', 'pypdfium2', 'pdfplumber', 'PIL', 'pypdf', 'pydantic_ai'); "
        "loaded = [m for m in forbidden if m in sys.modules]; "
        "scrapers = [m for m in sys.modules if m.startswith('legisbr.scrapers')]; "
        "assert not loaded, f'Heavy modules loaded: {loaded}'; "
        "assert not scrapers, f'Scraper modules loaded: {scrapers}'"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=30)


def test_pep562_lazy_exports():
    assert legisbr.Store is Store
    assert legisbr.Settings is Settings
    with pytest.raises(AttributeError):
        getattr(legisbr, "NotARealExport")


async def test_base_scraper_aenter_creates_no_log_dir(tmp_path: Path, monkeypatch):
    missing_log_dir = tmp_path / "should_not_exist"
    monkeypatch.setenv("LOG_DIR", str(missing_log_dir))
    monkeypatch.setenv("ERROR_LOG_DIR", str(missing_log_dir))

    class DummyScraper(BaseScraper):
        name = "DUMMY"
        base_url = "https://example.org"

    scraper = DummyScraper(
        store=Store(":memory:"), http=HttpClient(), years=range(2024, 2025)
    )
    async with scraper:
        pass
    assert not missing_log_dir.exists()


def test_settings_from_env_reads_env_file_without_mutating_os_environ(
    tmp_path: Path, monkeypatch
):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# comment line",
                'LLM_MODEL="openai:gpt-4.1-mini"',
                "LLM_PDF_INPUT=true",
                "LLM_BATCH_SIZE=7",
                "LLM_CONCURRENCY=4",
                "PDF_WORKERS=3",
                f"SAVE_DIR={tmp_path / 'out'}",
                f"LOG_DIR={tmp_path / 'logs'}",
                "PROXY_FILE_PATH=proxies.txt",
                "export LEGISBR_TEST_ONLY_VAR=from_file",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("LEGISBR_TEST_ONLY_VAR", raising=False)
    before = dict(os.environ)

    settings = Settings.from_env(
        env_file=env_file,
        environ={"LLM_BATCH_SIZE": "12"},  # env takes precedence over file
    )

    assert dict(os.environ) == before
    assert "LEGISBR_TEST_ONLY_VAR" not in os.environ
    assert settings.llm_model == "openai:gpt-4.1-mini"
    assert settings.llm_pdf_input is True
    assert settings.llm_batch_size == 12
    assert settings.llm_concurrency == 4
    assert settings.pdf_workers == 3
    assert settings.save_dir == tmp_path / "out"
    assert settings.log_dir == tmp_path / "logs"
    assert settings.build_proxy_config() == {"file_path": "proxies.txt"}


def test_settings_from_env_explicit_save_dir_overrides_dotenv_scoped_dirs(
    tmp_path: Path,
):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                f"SAVE_DIR={tmp_path / 'default'}",
                f"STATE_LEGISLATION_SAVE_DIR={tmp_path / 'states'}",
                f"SPECIFIC_LEGISLATION_SAVE_DIR={tmp_path / 'specific'}",
            ]
        ),
        encoding="utf-8",
    )

    # Without SAVE_DIR in environ, .env scoped dirs still apply.
    from_dotenv = Settings.from_env(env_file=env_file, environ={})
    assert from_dotenv.save_dir == tmp_path / "default"
    assert from_dotenv.state_legislation_save_dir == tmp_path / "states"
    assert from_dotenv.specific_legislation_save_dir == tmp_path / "specific"

    # Explicit SAVE_DIR in environ suppresses .env's STATE_* / SPECIFIC_* overrides.
    override_dir = tmp_path / "override"
    overridden = Settings.from_env(
        env_file=env_file, environ={"SAVE_DIR": str(override_dir)}
    )
    assert overridden.save_dir == override_dir
    assert overridden.state_legislation_save_dir is None
    assert overridden.specific_legislation_save_dir is None

    # Explicit STATE_LEGISLATION_SAVE_DIR in environ alongside SAVE_DIR is still honored.
    explicit_state = tmp_path / "explicit_states"
    both = Settings.from_env(
        env_file=env_file,
        environ={
            "SAVE_DIR": str(override_dir),
            "STATE_LEGISLATION_SAVE_DIR": str(explicit_state),
        },
    )
    assert both.save_dir == override_dir
    assert both.state_legislation_save_dir == explicit_state
    assert both.specific_legislation_save_dir is None


def test_export_dotenv_exports_missing_keys_and_respects_existing_env(
    tmp_path: Path, monkeypatch
):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "LEGISBR_PROVIDER_A=from_dotenv_a",
                "LEGISBR_PROVIDER_B=from_dotenv_b",
                f"SAVE_DIR={tmp_path / 'dotenv_save'}",
                f"STATE_LEGISLATION_SAVE_DIR={tmp_path / 'dotenv_states'}",
                f"SPECIFIC_LEGISLATION_SAVE_DIR={tmp_path / 'dotenv_specific'}",
            ]
        ),
        encoding="utf-8",
    )

    # Track keys with monkeypatch so teardown cleans up everything added.
    for key in (
        "LEGISBR_PROVIDER_A",
        "LEGISBR_PROVIDER_B",
        "SAVE_DIR",
        "STATE_LEGISLATION_SAVE_DIR",
        "SPECIFIC_LEGISLATION_SAVE_DIR",
    ):
        monkeypatch.setenv(key, "")
        os.environ.pop(key, None)

    monkeypatch.setenv("LEGISBR_PROVIDER_B", "from_shell_b")
    monkeypatch.setenv("SAVE_DIR", str(tmp_path / "shell_save"))

    export_dotenv(env_file)

    assert os.environ["LEGISBR_PROVIDER_A"] == "from_dotenv_a"
    assert os.environ["LEGISBR_PROVIDER_B"] == "from_shell_b"
    assert os.environ["SAVE_DIR"] == str(tmp_path / "shell_save")
    # Scoped dirs from .env are not exported when SAVE_DIR was already explicitly set.
    assert "STATE_LEGISLATION_SAVE_DIR" not in os.environ
    assert "SPECIFIC_LEGISLATION_SAVE_DIR" not in os.environ

    # Without SAVE_DIR in os.environ, .env's SAVE_DIR and scoped dirs are all exported.
    os.environ.pop("SAVE_DIR", None)
    export_dotenv(env_file)
    assert os.environ["SAVE_DIR"] == str(tmp_path / "dotenv_save")
    assert os.environ["STATE_LEGISLATION_SAVE_DIR"] == str(tmp_path / "dotenv_states")
    assert os.environ["SPECIFIC_LEGISLATION_SAVE_DIR"] == str(
        tmp_path / "dotenv_specific"
    )
