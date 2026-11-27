"""Uniform on-demand search query, typed norm references, and diacritic-insensitive matching."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, overload

if TYPE_CHECKING:
    from legisbr.core.document import Document

_SPACE_RE = re.compile(r"\s+")
_NUMBER_TOKEN_RE = re.compile(r"\d+(?:\.\d+)*")

_KNOWN_REF_FIELDS: frozenset[str] = frozenset(
    {
        "source",
        "title",
        "document_url",
        "url",
        "year",
        "type",
        "situation",
        "summary",
        "number",
        "norm_number",
        "numero",
    }
)


class UnsupportedQueryError(ValueError):
    """Raised when ``strict_native=True`` and a source cannot answer a query natively."""


class NormNotFoundError(LookupError):
    """Raised by ``get()`` and ``SearchResults.one()`` when no norm matches."""


def normalize_year_filter(
    year: int | None, years: int | range | None
) -> int | range | None:
    """Resolve mutually exclusive ``year`` / ``years`` filters."""
    if year is not None and years is not None:
        raise ValueError("Pass either 'year' or 'years', not both")
    if year is not None:
        if isinstance(year, bool) or not isinstance(year, int):
            raise TypeError("year must be an int or None")
        return year
    return years


@dataclass(frozen=True)
class Query:
    """Uniform search filter shared across all legislation sources."""

    years: int | range | None = None
    number: str | int | None = None
    type: str | None = None
    text: str | None = None
    situation: str | None = None
    limit: int | None = 20

    def __post_init__(self) -> None:
        if self.limit is not None and self.limit < 0:
            raise ValueError("limit must be non-negative or None")
        if isinstance(self.number, bool):
            raise TypeError("number must be a str, int, or None")
        if isinstance(self.number, int):
            object.__setattr__(self, "number", str(self.number))
        elif isinstance(self.number, str):
            stripped = self.number.strip()
            object.__setattr__(self, "number", stripped or None)

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


def _primary_title_number(title: str) -> str | None:
    """Return the first normalized numeric token in *title*, if any."""
    match = _NUMBER_TOKEN_RE.search(title)
    if not match:
        return None
    return normalize_number(match.group(0)) or None


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


def _matches_number(ref: Mapping[str, Any], query_number: str | int) -> bool:
    target = normalize_number(query_number)
    if not target:
        return normalize_search_text(str(query_number)) in normalize_search_text(
            str(ref.get("title") or "")
        )
    for field_name in ("number", "norm_number", "numero"):
        val = ref.get(field_name)
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


@dataclass(frozen=True)
class NormRef:
    """Typed legislation reference returned by ``search()`` and ``list_refs()``."""

    source: str
    title: str
    url: str
    year: int | None = None
    type: str = ""
    situation: str = ""
    summary: str = ""
    number: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)
    _raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    _sync_fetch: Callable[[NormRef], Document] | None = field(
        default=None, repr=False, compare=False
    )
    _async_fetch: Callable[[NormRef], Awaitable[Document]] | None = field(
        default=None, repr=False, compare=False
    )

    @classmethod
    def from_raw(
        cls,
        source: str,
        raw: Mapping[str, Any],
        *,
        sync_fetch: Callable[[NormRef], Document] | None = None,
        async_fetch: Callable[[NormRef], Awaitable[Document]] | None = None,
    ) -> NormRef:
        """Build a typed ``NormRef`` from a scraper's raw reference dictionary."""
        raw_copy = dict(raw)
        title = str(raw_copy.get("title") or "").strip()
        url = str(raw_copy.get("document_url") or raw_copy.get("url") or "").strip()

        raw_year = raw_copy.get("year")
        year: int | None = None
        if raw_year is not None and raw_year != "":
            try:
                year = int(raw_year)
            except (TypeError, ValueError):
                year = None

        doc_type = str(raw_copy.get("type") or "")
        situation = str(raw_copy.get("situation") or "")
        summary = str(raw_copy.get("summary") or "")

        number: str | None = None
        for key in ("number", "norm_number", "numero"):
            val = raw_copy.get(key)
            if val is not None and str(val).strip():
                number = str(val).strip()
                break

        extra = {
            key: value
            for key, value in raw_copy.items()
            if key not in _KNOWN_REF_FIELDS and not str(key).startswith("_")
        }

        return cls(
            source=source,
            title=title,
            url=url,
            year=year,
            type=doc_type,
            situation=situation,
            summary=summary,
            number=number,
            extra=extra,
            _raw=raw_copy,
            _sync_fetch=sync_fetch,
            _async_fetch=async_fetch,
        )

    def raw_payload(self) -> dict[str, Any]:
        """Return a fresh mutable raw dict suitable for passing to ``scraper.fetch()``."""
        if self._raw:
            payload = dict(self._raw)
            if "document_url" not in payload and self.url:
                payload["document_url"] = self.url
            return payload
        payload = dict(self.extra)
        payload.update(
            {
                "title": self.title,
                "document_url": self.url,
                "type": self.type,
                "situation": self.situation,
                "summary": self.summary,
            }
        )
        if self.year is not None:
            payload["year"] = self.year
        if self.number is not None:
            payload["number"] = self.number
        return payload

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable metadata dictionary for this norm reference."""
        data: dict[str, Any] = {
            "source": self.source,
            "title": self.title,
            "url": self.url,
            "year": self.year,
            "type": self.type,
            "situation": self.situation,
            "summary": self.summary,
            "number": self.number,
        }
        if self.extra:
            data["extra"] = dict(self.extra)
        return data

    def fetch(self) -> Document:
        """Synchronously fetch and convert this norm into a validated ``Document``."""
        if self._sync_fetch is not None:
            return self._sync_fetch(self)
        from legisbr.client import Client

        with Client() as client:
            return client.fetch(self)

    async def afetch(self) -> Document:
        """Asynchronously fetch and convert this norm into a validated ``Document``."""
        if self._async_fetch is not None:
            return await self._async_fetch(self)
        from legisbr.client import AsyncClient

        async with AsyncClient() as client:
            return await client.fetch(self)


@dataclass(frozen=True)
class SearchResults(Sequence[NormRef]):
    """Typed, indexable search results returned by ``search()``."""

    source: str
    query: Query
    items: tuple[NormRef, ...] = ()
    _sync_fetch_many: (
        Callable[[Sequence[NormRef], int | None], list[Document]] | None
    ) = field(default=None, repr=False, compare=False)
    _async_fetch_many: (
        Callable[[Sequence[NormRef], int | None], Awaitable[list[Document]]] | None
    ) = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple):
            object.__setattr__(self, "items", tuple(self.items))

    @overload
    def __getitem__(self, index: int) -> NormRef: ...

    @overload
    def __getitem__(self, index: slice) -> SearchResults: ...

    def __getitem__(self, index: int | slice) -> NormRef | SearchResults:
        if isinstance(index, slice):
            return SearchResults(
                source=self.source,
                query=self.query,
                items=self.items[index],
                _sync_fetch_many=self._sync_fetch_many,
                _async_fetch_many=self._async_fetch_many,
            )
        return self.items[index]

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterator[NormRef]:
        return iter(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)

    def __repr__(self) -> str:
        return (
            f"SearchResults(source={self.source!r}, count={len(self.items)}, "
            f"query={self.query!r})"
        )

    @property
    def first(self) -> NormRef | None:
        """Return the top match or ``None`` if no results were found."""
        return self.items[0] if self.items else None

    def one(self) -> NormRef:
        """Return the top match or raise ``NormNotFoundError`` when empty."""
        if not self.items:
            raise NormNotFoundError(
                f"No matching norm found for source={self.source!r} with {self.query!r}"
            )
        return self.items[0]

    def fetch(self, *, concurrency: int | None = None) -> list[Document]:
        """Synchronously fetch all matching norms concurrently (preserving order)."""
        if not self.items:
            return []
        if self._sync_fetch_many is not None:
            return self._sync_fetch_many(self.items, concurrency)
        from legisbr.client import Client

        with Client() as client:
            return client.fetch_many(self.items, concurrency=concurrency)

    async def afetch(self, *, concurrency: int | None = None) -> list[Document]:
        """Asynchronously fetch all matching norms concurrently (preserving order)."""
        if not self.items:
            return []
        if self._async_fetch_many is not None:
            return await self._async_fetch_many(self.items, concurrency)
        from legisbr.client import AsyncClient

        async with AsyncClient() as client:
            return await client.fetch_many(self.items, concurrency=concurrency)

    def to_dicts(self) -> list[dict[str, Any]]:
        """Return JSON-serializable metadata dictionaries for all matched norms."""
        return [item.to_dict() for item in self.items]


def rank_best_ref(refs: Sequence[NormRef], query: Query) -> NormRef:
    """Pick the best matching ``NormRef`` for ``get()`` (preferring exact number matches)."""
    if not refs:
        raise NormNotFoundError(f"No matching norm found for {query!r}")
    if query.number is None:
        return refs[0]
    target = normalize_number(query.number)
    if not target:
        return refs[0]

    q_type = normalize_search_text(query.type) if query.type else None

    def _score(item: tuple[int, NormRef]) -> tuple[int, int, int]:
        idx, ref = item
        if ref.number is not None and ref.number.strip():
            head = ref.number.strip().split("/", 1)[0]
            if normalize_number(head) == target:
                num_tier = 0
            elif target in _extract_numbers(ref.number):
                num_tier = 2
            else:
                num_tier = 3
        else:
            primary = _primary_title_number(ref.title)
            if primary == target:
                num_tier = 0
            elif target in _extract_numbers(ref.title):
                num_tier = 2
            else:
                num_tier = 3

        type_penalty = 0
        if q_type is not None:
            type_penalty = 0 if normalize_search_text(ref.type) == q_type else 1

        return (num_tier, type_penalty, idx)

    return min(enumerate(refs), key=_score)[1]
