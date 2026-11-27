"""Fakes use the normal scraper constructor and buffered HTTP responses."""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from legisbr.core.http import HttpClient, Response
from legisbr.core.store import Store

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def inline_pdf_work(monkeypatch):
    """Unit tests convert PDFs inline so monkeypatches reach the conversion code."""
    from legisbr.core import pdf

    async def inline(
        function,
        *args,
        executor="auto",
        max_workers=None,
        **kwargs,
    ):
        del executor, max_workers
        return function(*args, **kwargs)

    monkeypatch.setattr(pdf, "run_pdf_task", inline)


def make_scraper(cls, **overrides):
    http = HttpClient(rps=1000, max_retries=1)
    http.request = AsyncMock(
        side_effect=AssertionError("Unexpected network request in unit test")
    )
    scraper = cls(store=Store(":memory:"), http=http, years=range(2024, 2025))
    for key, value in overrides.items():
        setattr(scraper, key, value)
    return scraper


def make_mock_response(body=b"", status=200, content_type="text/html"):
    return Response(
        "https://example.org",
        status,
        {"Content-Type": content_type},
        body.encode() if isinstance(body, str) else body,
    )


def make_mock_json_response(payload, status=200):
    return make_mock_response(json.dumps(payload), status, "application/json")


@pytest.fixture
def integration_scraper_factory(tmp_path):
    @asynccontextmanager
    async def factory(cls, year=2024):
        http = HttpClient(rps=1, max_retries=2)
        async with cls(
            store=Store(tmp_path / cls.name / "documents.db"),
            http=http,
            years=range(year, year + 1),
            workers=2,
        ) as scraper:
            yield scraper

    return factory
