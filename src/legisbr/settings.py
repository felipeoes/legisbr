"""Runtime settings loaded explicitly from environment variables or a .env file."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


_SAVE_DIR_OVERRIDE_KEYS = frozenset(
    {"STATE_LEGISLATION_SAVE_DIR", "SPECIFIC_LEGISLATION_SAVE_DIR"}
)


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines from *path* without mutating ``os.environ``."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {'"', "'"}:
            val = val[1:-1]
        if key:
            values[key] = val
    return values


def export_dotenv(env_file: Path) -> None:
    """Export entries from *env_file* into ``os.environ`` via ``setdefault``.

    Existing environment variables always take precedence. If ``SAVE_DIR`` is
    already explicitly set in ``os.environ``, ``STATE_LEGISLATION_SAVE_DIR`` and
    ``SPECIFIC_LEGISLATION_SAVE_DIR`` from *env_file* are skipped so explicit
    ``SAVE_DIR`` overrides stay consistent across child processes too.

    Intended strictly for CLI entry points; library code should use
    :meth:`Settings.from_env`, which never mutates ``os.environ``.
    """
    has_explicit_save_dir = bool(os.environ.get("SAVE_DIR"))
    for key, value in _parse_env_file(env_file).items():
        if has_explicit_save_dir and key in _SAVE_DIR_OVERRIDE_KEYS:
            continue
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    """Explicit runtime configuration for CLI and library entry points."""

    llm_model: str = ""
    llm_pdf_input: bool = False
    llm_batch_size: int = 5
    llm_concurrency: int = 8
    pdf_workers: int = 0
    save_dir: Path = Path("outputs/legislation")
    state_legislation_save_dir: Path | None = None
    specific_legislation_save_dir: Path | None = None
    log_dir: Path | None = None
    proxy_file_path: str | None = None
    proxy_endpoint: str | None = None

    @classmethod
    def from_env(
        cls,
        *,
        env_file: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Settings:
        """Load settings from *environ* (default ``os.environ``) and optional *env_file*.

        Environment variables take precedence over *env_file*. If ``SAVE_DIR`` is
        explicitly set in *environ*, scoped save-dir defaults from *env_file*
        (``STATE_LEGISLATION_SAVE_DIR`` / ``SPECIFIC_LEGISLATION_SAVE_DIR``) are
        ignored unless also set in *environ*. Never mutates ``os.environ``.
        """
        env_map = os.environ if environ is None else environ
        file_map = _parse_env_file(env_file) if env_file is not None else {}
        merged: dict[str, str] = {**file_map, **env_map}

        raw_pdf_workers = merged.get("PDF_WORKERS")
        pdf_workers = (
            int(raw_pdf_workers) if raw_pdf_workers else min(8, os.cpu_count() or 1)
        )
        scoped_source = env_map if env_map.get("SAVE_DIR") else merged
        raw_state = scoped_source.get("STATE_LEGISLATION_SAVE_DIR")
        raw_specific = scoped_source.get("SPECIFIC_LEGISLATION_SAVE_DIR")
        raw_log_dir = merged.get("LOG_DIR") or merged.get("ERROR_LOG_DIR")
        return cls(
            llm_model=merged.get("LLM_MODEL", ""),
            llm_pdf_input=merged.get("LLM_PDF_INPUT", "false").lower()
            in {"1", "true", "yes"},
            llm_batch_size=int(merged.get("LLM_BATCH_SIZE", "5")),
            llm_concurrency=int(merged.get("LLM_CONCURRENCY", "8")),
            pdf_workers=pdf_workers,
            save_dir=Path(merged.get("SAVE_DIR") or "outputs/legislation"),
            state_legislation_save_dir=Path(raw_state) if raw_state else None,
            specific_legislation_save_dir=Path(raw_specific) if raw_specific else None,
            log_dir=Path(raw_log_dir) if raw_log_dir else None,
            proxy_file_path=merged.get("PROXY_FILE_PATH") or None,
            proxy_endpoint=merged.get("PROXY_ENDPOINT") or None,
        )

    def build_proxy_config(self) -> dict[str, str] | None:
        """Build proxy config dict from settings, or return None."""
        if not (self.proxy_file_path or self.proxy_endpoint):
            return None
        config: dict[str, str] = {}
        if self.proxy_file_path:
            config["file_path"] = self.proxy_file_path
        if self.proxy_endpoint:
            config["endpoint"] = self.proxy_endpoint
        return config
