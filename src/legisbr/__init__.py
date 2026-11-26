"""Brazilian legislation scraper library."""

from __future__ import annotations

import logging
from importlib import import_module
from typing import TYPE_CHECKING, Any

from legisbr.registry import SourceInfo, sources

__version__ = "0.1.0"
__all__ = [
    "Document",
    "OCR",
    "Settings",
    "SourceInfo",
    "Store",
    "__version__",
    "sources",
]

logging.getLogger(__name__).addHandler(logging.NullHandler())

if TYPE_CHECKING:
    from legisbr.core.document import Document
    from legisbr.core.ocr import OCR
    from legisbr.core.store import Store
    from legisbr.settings import Settings

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "Document": ("legisbr.core.document", "Document"),
    "OCR": ("legisbr.core.ocr", "OCR"),
    "Settings": ("legisbr.settings", "Settings"),
    "Store": ("legisbr.core.store", "Store"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is not None:
        module_name, attr_name = target
        value = getattr(import_module(module_name), attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
