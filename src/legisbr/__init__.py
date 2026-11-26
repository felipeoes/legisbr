"""Brazilian legislation scraper library."""

from __future__ import annotations

import logging
from importlib import import_module
from typing import TYPE_CHECKING, Any

from legisbr.registry import SourceInfo, sources

__version__ = "0.1.0"
__all__ = [
    "ArchiveIndex",
    "Client",
    "Document",
    "DocumentSink",
    "ErrorRecord",
    "NullSink",
    "OCR",
    "Query",
    "SearchCapabilities",
    "SearchHit",
    "Settings",
    "SourceInfo",
    "Store",
    "UnsupportedQueryError",
    "__version__",
    "scrape",
    "search_refs",
    "sources",
]

logging.getLogger(__name__).addHandler(logging.NullHandler())

if TYPE_CHECKING:
    from legisbr.client import Client, scrape, search_refs
    from legisbr.core.archive import ArchiveIndex
    from legisbr.core.document import Document
    from legisbr.core.ocr import OCR
    from legisbr.core.query import Query, SearchCapabilities, UnsupportedQueryError
    from legisbr.core.sink import DocumentSink, ErrorRecord, NullSink
    from legisbr.core.store import SearchHit, Store
    from legisbr.settings import Settings

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "ArchiveIndex": ("legisbr.core.archive", "ArchiveIndex"),
    "Client": ("legisbr.client", "Client"),
    "Document": ("legisbr.core.document", "Document"),
    "DocumentSink": ("legisbr.core.sink", "DocumentSink"),
    "ErrorRecord": ("legisbr.core.sink", "ErrorRecord"),
    "NullSink": ("legisbr.core.sink", "NullSink"),
    "OCR": ("legisbr.core.ocr", "OCR"),
    "Query": ("legisbr.core.query", "Query"),
    "SearchCapabilities": ("legisbr.core.query", "SearchCapabilities"),
    "SearchHit": ("legisbr.core.store", "SearchHit"),
    "Settings": ("legisbr.settings", "Settings"),
    "Store": ("legisbr.core.store", "Store"),
    "UnsupportedQueryError": ("legisbr.core.query", "UnsupportedQueryError"),
    "scrape": ("legisbr.client", "scrape"),
    "search_refs": ("legisbr.client", "search_refs"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is not None:
        module_name, attr_name = target
        value = getattr(import_module(module_name), attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
