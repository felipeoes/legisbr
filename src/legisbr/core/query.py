"""Uniform on-demand search query, scraper capabilities, and diacritic-insensitive matching."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_SPACE_RE = re.compile(r"\s+")
_NUMBER_TOKEN_RE = re.compile(r"\d+(?:\.\d+)*")


class UnsupportedQueryError(ValueError):
    """Raised when ``strict_native=True`` and a source cannot answer a query natively."""


@dataclass(frozen=True)
class Query:
    """Uniform search filter shared across all legislation sources."""

    years: int | range | None = None
    number: str | None = None
    type: str | None = None
    text: str | None = None
    situation: str | None = None
    limit: int | None = 20

    def __post_init__(self) -> None:
        if self.limit is not None and self.limit < 0:
            raise ValueError("limit must be non-negative or None")

    def year_range(self, default: range) -> range:
        """Normalize ``years`` (int, range, or None) into a ``range``."""
        if self.years is None:
            return default
        if isinstance(self.years, int):
            return range(self.years, self.years + 1)
        return self.years


@dataclass(frozen=True)
class SearchCapabilities:
    """Declares which filters a scraper pushes down to its upstream portal."""

    native_year: bool = True
    native_type: bool = False
    native_number: bool = False
    native_text: bool = False
    native_situation: bool = False
    fast_catalog: bool = False
    direct_id: bool = False
    heavy_setup: bool = False

    def supports_query(self, query: Query) -> bool:
        """Return True if every active filter in *query* is supported natively or via a fast catalog."""
        if self.heavy_setup:
            return False
        if self.fast_catalog:
            return True
        if query.years is not None and not self.native_year:
            return False
        if query.type is not None and not self.native_type:
            return False
        if query.number is not None and not self.native_number:
            return False
        if query.text is not None and not self.native_text:
            return False
        if query.situation is not None and not self.native_situation:
            return False
        return True


def normalize_search_text(text: str) -> str:
    """Lowercase, strip diacritics, and collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _SPACE_RE.sub(" ", stripped).strip().casefold()


def normalize_number(value: str | int) -> str:
    """Normalize a norm number by stripping non-digits and leading zeros."""
    digits = re.sub(r"\D+", "", str(value))
    return digits.lstrip("0") or ("0" if digits else "")


def _extract_numbers(text: str) -> set[str]:
    """Extract normalized numeric tokens from text (handling thousand separators like 14.133)."""
    return {
        norm
        for token in _NUMBER_TOKEN_RE.findall(text)
        if (norm := normalize_number(token))
    }


def match_type_entry(
    types: Mapping[str, Any] | Sequence[str], query_type: str
) -> tuple[str, Any] | None:
    """Match *query_type* case/diacritic-insensitively against a scraper's ``types``."""
    target = normalize_search_text(query_type)
    if not target:
        return None
    if isinstance(types, Mapping):
        for name, value in types.items():
            if normalize_search_text(name) == target:
                return (name, value)
        return None
    for name in types:
        if normalize_search_text(name) == target:
            return (name, name)
    return None


def _matches_number(ref: Mapping[str, Any], query_number: str) -> bool:
    target = normalize_number(query_number)
    if not target:
        return normalize_search_text(query_number) in normalize_search_text(
            str(ref.get("title") or "")
        )
    for field in ("number", "norm_number", "numero"):
        val = ref.get(field)
        if val is not None and str(val).strip():
            raw = str(val).strip()
            head = raw.split("/", 1)[0]
            if normalize_number(head) == target or target in _extract_numbers(raw):
                return True
            return False
    return target in _extract_numbers(str(ref.get("title") or ""))


def matches_query(ref: Mapping[str, Any], query: Query) -> bool:
    """Return True if *ref* satisfies all active filters in *query*."""
    if query.years is not None:
        raw_year = ref.get("year")
        if raw_year is None:
            return False
        try:
            year = int(raw_year)
        except (TypeError, ValueError):
            return False
        if year not in query.year_range(range(0)):
            return False

    if query.type is not None:
        q_type = normalize_search_text(query.type)
        r_type = normalize_search_text(str(ref.get("type") or ""))
        if q_type != r_type:
            return False

    if query.situation is not None:
        q_sit = normalize_search_text(query.situation)
        r_sit = normalize_search_text(str(ref.get("situation") or ""))
        if q_sit not in r_sit:
            return False

    if query.number is not None and not _matches_number(ref, query.number):
        return False

    if query.text is not None:
        q_text = normalize_search_text(query.text)
        haystack = normalize_search_text(
            f"{ref.get('title') or ''} {ref.get('summary') or ''}"
        )
        if q_text and not all(token in haystack for token in q_text.split()):
            return False

    return True
