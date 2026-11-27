"""Pluggable document sinks and typed error records for zero-disk pipelines."""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, Self, runtime_checkable

if TYPE_CHECKING:
    from legisbr.core.document import Document

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ErrorRecord:
    """Structured error captured while listing, fetching, or persisting documents."""

    message: str
    run_id: int | None = None
    year: int | None = None
    type: str = ""
    situation: str = ""
    title: str = ""
    url: str = ""


@runtime_checkable
class DocumentSink(Protocol):
    """Persistence contract shared by SQLite Store, NullSink, and custom sinks."""

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *exc: Any) -> None: ...

    async def start_run(self, scraper: str) -> int: ...

    async def finish_run(
        self,
        run_id: int,
        docs: int,
        errors: int,
        usage: dict[str, Any],
        stats: dict[str, Any] | None = None,
    ) -> None: ...

    def iter_keys(self) -> AsyncIterator[tuple[str, str]]: ...

    async def save(
        self,
        doc: Document,
        *,
        ref_url: str | None = None,
        ref_title: str | None = None,
    ) -> None: ...

    async def record_error(self, error: ErrorRecord) -> None: ...


class NullSink:
    """Stateless zero-disk sink for streaming pipelines and serverless runtimes."""

    def __init__(
        self,
        *,
        on_error: Callable[[ErrorRecord], Awaitable[None] | None] | None = None,
    ) -> None:
        self.on_error = on_error

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def start_run(self, scraper: str) -> int:
        return 0

    async def finish_run(
        self,
        run_id: int,
        docs: int,
        errors: int,
        usage: dict[str, Any],
        stats: dict[str, Any] | None = None,
    ) -> None:
        return None

    async def iter_keys(self) -> AsyncIterator[tuple[str, str]]:
        if False:
            yield ("", "")

    async def save(
        self,
        doc: Document,
        *,
        ref_url: str | None = None,
        ref_title: str | None = None,
    ) -> None:
        return None

    async def record_error(self, error: ErrorRecord) -> None:
        if self.on_error is not None:
            result = self.on_error(error)
            if inspect.isawaitable(result):
                await result
            return
        label = error.title or error.type or "listing"
        if error.url:
            logger.error("%s (%s): %s", label, error.url, error.message)
        else:
            logger.error("%s: %s", label, error.message)
