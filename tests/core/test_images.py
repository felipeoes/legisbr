"""Unit tests for HTML image fast-path and single-flight bounded ImageCache."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from legisbr.core import images
from legisbr.core.images import ImageCache, inline_images_in_html

_PNG_HEADER = b"\x89PNG\r\n\x1a\n"
_IMG_BYTES = _PNG_HEADER + b"A" * 200
_SPACER_BYTES = b"GIF89a" + b"\x00" * 20


async def test_no_img_tag_fast_path_skips_beautifulsoup_and_fetch(monkeypatch):
    def fail_bs(*args, **kwargs):
        raise AssertionError("BeautifulSoup should not be called without <img>")

    monkeypatch.setattr(images, "BeautifulSoup", fail_bs)
    fetch = AsyncMock(side_effect=AssertionError("fetch should not be called"))
    html = "<html><body><p>Art. 1º Sem imagens.</p></body></html>"
    assert await inline_images_in_html(html, "https://example.org", fetch) == html
    fetch.assert_not_awaited()


async def test_single_flight_deduplicates_concurrent_fetches():
    cache = ImageCache()
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def fetch(url: str) -> bytes:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return _IMG_BYTES

    html = '<p><img src="/brasao.png"></p>'
    tasks = [
        asyncio.create_task(
            inline_images_in_html(html, "https://example.org", fetch, cache=cache)
        )
        for _ in range(8)
    ]
    await started.wait()
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks)

    assert calls == 1
    assert len(cache) == 1
    assert all("data:image/png;base64," in r for r in results)


async def test_spacer_image_is_cached_and_decomposed():
    cache = ImageCache()
    fetch = AsyncMock(return_value=_SPACER_BYTES)
    html = '<p><img src="/spacer.gif">Texto</p>'

    first = await inline_images_in_html(html, "https://example.org", fetch, cache=cache)
    second = await inline_images_in_html(
        html, "https://example.org", fetch, cache=cache
    )

    assert fetch.await_count == 1
    assert "https://example.org/spacer.gif" in cache
    assert cache.current_bytes == 0
    assert "<img" not in first and "<img" not in second


async def test_failed_and_empty_fetches_are_not_cached():
    cache = ImageCache()
    fetch = AsyncMock(side_effect=[RuntimeError("boom"), None, _IMG_BYTES])
    html = '<p><img src="/flaky.png"></p>'

    r1 = await inline_images_in_html(html, "https://example.org", fetch, cache=cache)
    assert "data:image/" not in r1 and len(cache) == 0

    r2 = await inline_images_in_html(html, "https://example.org", fetch, cache=cache)
    assert "data:image/" not in r2 and len(cache) == 0

    r3 = await inline_images_in_html(html, "https://example.org", fetch, cache=cache)
    assert "data:image/png;base64," in r3 and len(cache) == 1
    assert fetch.await_count == 3


async def test_lru_eviction_by_max_entries():
    cache = ImageCache(max_entries=2, max_bytes=1024 * 1024)
    fetch = AsyncMock(return_value=_IMG_BYTES)

    await inline_images_in_html(
        '<img src="/a.png">', "https://x.org", fetch, cache=cache
    )
    await inline_images_in_html(
        '<img src="/b.png">', "https://x.org", fetch, cache=cache
    )
    # Touch a.png so b.png becomes least-recently-used.
    await inline_images_in_html(
        '<img src="/a.png">', "https://x.org", fetch, cache=cache
    )
    await inline_images_in_html(
        '<img src="/c.png">', "https://x.org", fetch, cache=cache
    )

    assert fetch.await_count == 3
    assert "https://x.org/a.png" in cache
    assert "https://x.org/c.png" in cache
    assert "https://x.org/b.png" not in cache


async def test_lru_eviction_by_max_bytes_and_skips_oversized_entries():
    # max_bytes=1600 -> single-entry cap is 200 b64 bytes; max_entries=50 so only max_bytes triggers eviction.
    cache = ImageCache(max_entries=50, max_bytes=1600)
    small = _PNG_HEADER + b"X" * 112  # 120 raw bytes -> 160 b64 bytes (<= 200)
    oversized = _PNG_HEADER + b"Y" * 200  # 208 raw bytes -> 280 b64 bytes (> 200)

    fetch_small = AsyncMock(return_value=small)
    for i in range(12):
        await inline_images_in_html(
            f'<img src="/img{i}.png">', "https://x.org", fetch_small, cache=cache
        )
    assert cache.current_bytes == 1600
    assert len(cache) == 10
    assert "https://x.org/img0.png" not in cache
    assert "https://x.org/img1.png" not in cache
    assert "https://x.org/img11.png" in cache

    # Oversized entry (> max_bytes // 8) is still returned/inlined, but NOT cached.
    fetch_big = AsyncMock(return_value=oversized)
    rendered = await inline_images_in_html(
        '<img src="/huge.png">', "https://x.org", fetch_big, cache=cache
    )
    assert "data:image/png;base64," in rendered
    assert "https://x.org/huge.png" not in cache


def test_image_cache_validates_positive_limits():
    with pytest.raises(ValueError):
        ImageCache(max_entries=0)
    with pytest.raises(ValueError):
        ImageCache(max_bytes=0)


@pytest.mark.parametrize(
    "payload",
    [
        b"<!DOCTYPE html><html><body>404 Not Found</body></html>" + b"x" * 200,
        b"  \n<html><head><title>Login</title></head></html>" + b"x" * 200,
        b'{"error": "not found"}' + b" " * 200,
        b"BM25 plain text starting with BM" + b"x" * 200,
        b"RIFF\x00\x00\x00\x00WAVEfmt " + b"x" * 200,
        b'<?xml version="1.0"?><html><body><svg></svg></body></html>' + b" " * 200,
    ],
    ids=[
        "doctype-html",
        "html-tag",
        "json-error",
        "bm-plain-text",
        "riff-wav-not-webp",
        "xhtml-with-svg",
    ],
)
async def test_non_image_payload_is_never_encoded_as_data_uri(payload):
    cache = ImageCache()
    fetch = AsyncMock(return_value=payload)
    html = '<p><img src="/fake.gif">Art. 1º Texto.</p>'

    result = await inline_images_in_html(
        html, "https://example.org", fetch, cache=cache
    )

    assert "data:image/" not in result
    assert len(cache) == 0


@pytest.mark.parametrize(
    ("payload", "expected_mime"),
    [
        (_PNG_HEADER + b"A" * 200, "image/png"),
        (b"\xff\xd8\xff\xe0" + b"J" * 200, "image/jpeg"),
        (b"GIF89a" + b"G" * 200, "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBP" + b"W" * 200, "image/webp"),
        (
            b"BM\x36\x01\x00\x00\x00\x00\x00\x00\x36\x00\x00\x00" + b"B" * 200,
            "image/bmp",
        ),
        (b"II*\x00" + b"T" * 200, "image/tiff"),
        (b"MM\x00*" + b"T" * 200, "image/tiff"),
        (
            b'<svg xmlns="http://www.w3.org/2000/svg">' + b" " * 150 + b"</svg>",
            "image/svg+xml",
        ),
    ],
)
async def test_magic_bytes_override_misleading_url_extension(payload, expected_mime):
    fetch = AsyncMock(return_value=payload)
    html = '<p><img src="/asset.gif"></p>'

    result = await inline_images_in_html(html, "https://example.org", fetch)

    assert f"data:{expected_mime};base64," in result


async def test_relative_src_starting_with_http_resolves_via_urljoin():
    fetch = AsyncMock(return_value=_IMG_BYTES)
    html = '<p><img src="http_logo.png"></p>'

    result = await inline_images_in_html(
        html, "https://example.org/docs/lei.html", fetch
    )

    fetch.assert_awaited_once_with("https://example.org/docs/http_logo.png")
    assert "data:image/png;base64," in result


async def test_inflight_cancels_only_when_last_waiter_cancels():
    cache = ImageCache()
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = False

    async def slow_fetch(url: str) -> bytes:
        nonlocal cancelled
        started.set()
        try:
            await release.wait()
            return _IMG_BYTES
        except asyncio.CancelledError:
            cancelled = True
            raise

    # Two waiters: cancelling one keeps the fetch alive for the other.
    t1 = asyncio.create_task(cache.get_or_fetch("https://x.org/a.png", slow_fetch))
    t2 = asyncio.create_task(cache.get_or_fetch("https://x.org/a.png", slow_fetch))
    await started.wait()
    await asyncio.sleep(0)

    t1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t1
    assert cancelled is False
    release.set()
    assert await t2 is not None
    assert "https://x.org/a.png" in cache

    # Single waiter cancelled -> underlying fetch is cancelled and evicted from _inflight.
    started.clear()
    release.clear()
    cancelled = False
    solo = asyncio.create_task(cache.get_or_fetch("https://x.org/b.png", slow_fetch))
    await started.wait()
    await asyncio.sleep(0)
    solo.cancel()
    with pytest.raises(asyncio.CancelledError):
        await solo
    assert cancelled is True
    assert "https://x.org/b.png" not in cache._inflight
