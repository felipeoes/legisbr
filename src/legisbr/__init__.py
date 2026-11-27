"""Brazilian legislation scraper library."""

from __future__ import annotations

import logging
from importlib import import_module
from typing import TYPE_CHECKING, Any

from legisbr.registry import Source, SourceInfo, sources

__version__ = "0.1.0"
__all__ = [
    "ArchiveIndex",
    "AsyncArchiveIndex",
    "AsyncClient",
    "Client",
    "Document",
    "DocumentSink",
    "DocumentStream",
    "ErrorRecord",
    "NormNotFoundError",
    "NormRef",
    "NullSink",
    "OCR",
    "Query",
    "RunResult",
    "SearchCapabilities",
    "SearchHit",
    "SearchResults",
    "Settings",
    "Source",
    "SourceInfo",
    "Store",
    "UnsupportedQueryError",
    "__version__",
    "export",
    "get",
    "query",
    "scrape",
    "search",
    "sources",
]

logging.getLogger(__name__).addHandler(logging.NullHandler())

if TYPE_CHECKING:
    from legisbr._sync import DocumentStream
    from legisbr.client import AsyncClient, Client, get, scrape, search
    from legisbr.core.archive import ArchiveIndex, AsyncArchiveIndex, query
    from legisbr.core.document import Document
    from legisbr.core.ocr import OCR
    from legisbr.core.query import (
        NormNotFoundError,
        NormRef,
        Query,
        SearchCapabilities,
        SearchResults,
        UnsupportedQueryError,
    )
    from legisbr.core.scraper import RunResult
    from legisbr.core.sink import DocumentSink, ErrorRecord, NullSink
    from legisbr.core.store import SearchHit, Store
    from legisbr.core.store import export_archive as export
    from legisbr.settings import Settings

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "ArchiveIndex": ("legisbr.core.archive", "ArchiveIndex"),
    "AsyncArchiveIndex": ("legisbr.core.archive", "AsyncArchiveIndex"),
    "AsyncClient": ("legisbr.client", "AsyncClient"),
    "Client": ("legisbr.client", "Client"),
    "Document": ("legisbr.core.document", "Document"),
    "DocumentSink": ("legisbr.core.sink", "DocumentSink"),
    "DocumentStream": ("legisbr._sync", "DocumentStream"),
    "ErrorRecord": ("legisbr.core.sink", "ErrorRecord"),
    "NormNotFoundError": ("legisbr.core.query", "NormNotFoundError"),
    "NormRef": ("legisbr.core.query", "NormRef"),
    "NullSink": ("legisbr.core.sink", "NullSink"),
    "OCR": ("legisbr.core.ocr", "OCR"),
    "Query": ("legisbr.core.query", "Query"),
    "RunResult": ("legisbr.core.scraper", "RunResult"),
    "SearchCapabilities": ("legisbr.core.query", "SearchCapabilities"),
    "SearchHit": ("legisbr.core.store", "SearchHit"),
    "SearchResults": ("legisbr.core.query", "SearchResults"),
    "Settings": ("legisbr.settings", "Settings"),
    "Store": ("legisbr.core.store", "Store"),
    "UnsupportedQueryError": ("legisbr.core.query", "UnsupportedQueryError"),
    "export": ("legisbr.core.store", "export_archive"),
    "get": ("legisbr.client", "get"),
    "query": ("legisbr.core.archive", "query"),
    "scrape": ("legisbr.client", "scrape"),
    "search": ("legisbr.client", "search"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is not None:
        module_name, attr_name = target
        value = getattr(import_module(module_name), attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
