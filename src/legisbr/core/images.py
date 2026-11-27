"""Async utility for inlining ``<img>`` sources as base64 data URIs."""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import TypeAlias
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

_IMG_TAG_RE = re.compile(r"<img\b", re.IGNORECASE)


class _SpacerType:
    __slots__ = ()


_SPACER = _SpacerType()
CacheEntry: TypeAlias = tuple[str, str] | _SpacerType


class _InflightFetch:
    __slots__ = ("task", "waiters")

    def __init__(self, task: asyncio.Task[CacheEntry | None]) -> None:
        self.task = task
        self.waiters = 0


class ImageCache:
    """Single-flight, memory-bounded LRU cache for inlined base64 images."""

    def __init__(
        self,
        *,
        max_entries: int = 256,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if max_entries < 1 or max_bytes < 1:
            raise ValueError("max_entries and max_bytes must be positive")
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._entries: OrderedDict[str, tuple[CacheEntry, int]] = OrderedDict()
        self._bytes = 0
        self._inflight: dict[str, _InflightFetch] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, url: object) -> bool:
        return url in self._entries

    @property
    def current_bytes(self) -> int:
        return self._bytes

    def clear(self) -> None:
        self._entries.clear()
        self._bytes = 0

    def _put(self, url: str, entry: CacheEntry) -> None:
        size = 0 if isinstance(entry, _SpacerType) else len(entry[1])
        if size > self.max_bytes // 8:
            return
        if url in self._entries:
            _, old_size = self._entries.pop(url)
            self._bytes -= old_size
        self._entries[url] = (entry, size)
        self._bytes += size
        while len(self._entries) > self.max_entries or self._bytes > self.max_bytes:
            _, (_, evicted_size) = self._entries.popitem(last=False)
            self._bytes -= evicted_size

    async def get_or_fetch(
        self,
        url: str,
        fetch_fn: Callable[[str], Awaitable[bytes | None]],
    ) -> CacheEntry | None:
        """Return cached ``(content_type, b64)`` or ``_SPACER``, deduplicating in-flight fetches."""
        cached = self._entries.get(url)
        if cached is not None:
            self._entries.move_to_end(url)
            return cached[0]

        inflight = self._inflight.get(url)
        if inflight is None:

            async def _load() -> CacheEntry | None:
                try:
                    entry = await _fetch_and_encode(url, fetch_fn)
                    if entry is not None:
                        self._put(url, entry)
                    return entry
                finally:
                    if self._inflight.get(url) is inflight:
                        self._inflight.pop(url, None)

            inflight = _InflightFetch(asyncio.create_task(_load()))
            self._inflight[url] = inflight

        inflight.waiters += 1
        try:
            return await asyncio.shield(inflight.task)
        except asyncio.CancelledError:
            inflight.waiters -= 1
            if inflight.waiters == 0 and not inflight.task.done():
                if self._inflight.get(url) is inflight:
                    self._inflight.pop(url, None)
                inflight.task.cancel()
                try:
                    await inflight.task
                except asyncio.CancelledError:
                    pass
            raise
        except BaseException:
            inflight.waiters -= 1
            raise
        else:
            inflight.waiters -= 1


async def _fetch_and_encode(
    full_url: str,
    fetch_fn: Callable[[str], Awaitable[bytes | None]],
) -> CacheEntry | None:
    try:
        data = await fetch_fn(full_url)
    except Exception as exc:
        logger.debug(f"Image fetch failed for '{full_url[:80]}': {exc}")
        return None
    if not data:
        return None
    content_type = _detect_image_mime(data)
    if content_type is None:
        logger.debug(
            f"Skipping non-image response for '{full_url[:80]}' ({len(data)} bytes)"
        )
        return None
    if len(data) < 100:  # 1x1 spacer GIFs/PNGs carry no content
        return _SPACER
    b64 = base64.b64encode(data).decode("ascii")
    return (content_type, b64)


async def inline_images_in_html(
    html: str,
    base_url: str,
    fetch_fn: Callable[[str], Awaitable[bytes | None]],
    *,
    max_concurrent: int = 10,
    cache: ImageCache | None = None,
) -> str:
    """Replace every ``<img src>`` in *html* with an inline base64 data URI.

    Parameters
    ----------
    html:
        Raw HTML string.
    base_url:
        Used to resolve relative ``src`` attributes.
    fetch_fn:
        ``async (url) -> bytes | None`` — fetches image bytes.
        Return ``None`` to skip the image gracefully.
    max_concurrent:
        Maximum number of concurrent image fetches.
    cache:
        Optional single-flight bounded ``ImageCache`` shared across documents.

    Returns
    -------
    str
        HTML with images inlined. Failed fetches are left unchanged.
    """
    if not _IMG_TAG_RE.search(html):
        return html
    soup = BeautifulSoup(html, "lxml")
    imgs = soup.find_all("img")
    if not imgs:
        return html
    sem = asyncio.Semaphore(max_concurrent)

    async def _bounded_fetch(full_url: str) -> bytes | None:
        async with sem:
            return await fetch_fn(full_url)

    async def _fetch_one(img_tag: Tag) -> None:
        src = _get_image_source(img_tag)
        if not src:
            return
        full_url = urljoin(base_url, src)
        if cache is not None:
            entry = await cache.get_or_fetch(full_url, _bounded_fetch)
        else:
            entry = await _fetch_and_encode(full_url, _bounded_fetch)
        if entry is None:
            return
        if isinstance(entry, _SpacerType):
            img_tag.decompose()
            return
        content_type, b64 = entry
        img_tag["src"] = f"data:{content_type};base64,{b64}"
        for attr in ("data-src", "srcset", "data-srcset"):
            img_tag.attrs.pop(attr, None)

    await asyncio.gather(*(_fetch_one(img) for img in imgs))
    return str(soup)


def _get_image_source(img_tag: Tag) -> str | None:
    """Return the best fetchable image source from an ``<img>`` or its picture."""
    direct_source = _first_fetchable_attr(img_tag, ("src", "data-src"))
    if direct_source:
        return direct_source
    srcset_source = _first_srcset_candidate(
        img_tag.get("srcset") or img_tag.get("data-srcset")
    )
    if srcset_source:
        return srcset_source
    parent = img_tag.find_parent("picture")
    if isinstance(parent, Tag) and parent.name == "picture":
        for source_tag in parent.find_all("source", recursive=False):
            direct_source = _first_fetchable_attr(source_tag, ("src", "data-src"))
            if direct_source:
                return direct_source
            srcset_source = _first_srcset_candidate(
                source_tag.get("srcset") or source_tag.get("data-srcset")
            )
            if srcset_source:
                return srcset_source
    return None


def _first_fetchable_attr(tag: Tag, attrs: tuple[str, ...]) -> str | None:
    for attr in attrs:
        value = tag.get(attr)
        if isinstance(value, list):
            value = " ".join(value)
        if not isinstance(value, str):
            continue
        value = value.strip()
        if value and _is_fetchable_source(value):
            return value
    return None


def _first_srcset_candidate(srcset: str | None) -> str | None:
    if not isinstance(srcset, str):
        return None
    for candidate in srcset.split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        source = candidate.split()[0].strip()
        if source and _is_fetchable_source(source):
            return source
    return None


def _is_fetchable_source(source: str) -> bool:
    lower_source = source.lower()
    return not lower_source.startswith(
        ("data:", "cid:", "javascript:", "about:", "file:")
    )


def _detect_image_mime(data: bytes) -> str | None:
    """Detect image MIME type strictly from magic bytes, or ``None`` if not an image."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 14 and data[:2] == b"BM" and data[6:10] == b"\x00\x00\x00\x00":
        return "image/bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    head = data[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if b"<html" not in head and b"<!doctype html" not in head:
        if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
            return "image/svg+xml"
    return None


def _guess_mime(url: str, data: bytes) -> str:
    """Best-effort MIME type from magic bytes first, then URL extension."""
    magic_mime = _detect_image_mime(data)
    if magic_mime:
        return magic_mime
    mime, _ = mimetypes.guess_type(url.split("?")[0])
    if mime:
        return mime
    return "image/png"
