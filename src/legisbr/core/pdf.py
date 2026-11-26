"""Permissive high-performance PDF engine built on ``pdf_oxide`` and ``pypdf``.

Self-contained (stdlib + ``pdf_oxide`` + ``pypdf`` only) so ``spawn`` worker
subprocesses start fast without importing ``aiohttp``, ``bs4``, or ``lxml``,
and ``legisbr.core.convert`` can import from here without circularity.
"""

from __future__ import annotations

import asyncio
import atexit
import io
import json
import logging
import multiprocessing
import os
import re
import string
import time
import weakref
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import pdf_oxide
import pypdf

if TYPE_CHECKING:
    from legisbr.core.ocr import OCR

__all__ = [
    "PdfExecutor",
    "PdfRunner",
    "default_pdf_workers",
    "is_pdf_scanned",
    "pdf_page_count",
    "pdf_to_markdown",
    "readable_text",
    "render_pdf_batch",
    "run_pdf_task",
    "shutdown_pdf_pool",
    "slice_pdf_pages",
]

logger = logging.getLogger(__name__)

PdfExecutor = Literal["auto", "process", "thread"]
_T = TypeVar("_T")

_OCR_METADATA_KEYWORDS: tuple[str, ...] = (
    "tesseract",
    "ocr",
    "abbyy",
    "omnipage",
    "paper capture",
    "scanner",
    "scanned",
    "clearscan",
    "readiris",
    "iris",
)

_SERVER_ERROR_PATTERNS: tuple[str, ...] = (
    "the requested url was not found on this server",
    "was not found on this server",
    "file or directory not found",
    "failed to open stream",
    "http request failed",
    "service unavailable",
    "doesn't work properly without javascript enabled",
    "object reference not set to an instance of an object",
)

_WATERMARK_CHECK_RE = re.compile(r"(?:\n[^\n]{0,2}){15,}")
_MD_BASE64_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(data:image/[^)]+\)")
_C1_RE = re.compile(r"[\x80-\x9f]")
_NON_IMAGE_LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\([^)]*\)")

# Legislative structure restoration regexes (citation-safe):
# 1. Structural headings merged mid-paragraph after punctuation.
_HEADING_BREAK_RE = re.compile(
    r"([.!?;:\)])\s+"
    r"((?:\*\*)?(?:T[ÍI]TULO|CAP[ÍI]TULO|SE[ÇC][ÃA]O|SUBSE[ÇC][ÃA]O|LIVRO)\s+[IVXLCDM\d]+)"
)
# 2. New Art. N / § N / Parágrafo único after sentence-ending punctuation or bold closing (**).
#    Requires uppercase "Art." + preceding terminator so inline citations like
#    "nos termos do art. 5º", "conforme o Art. 10", or "previsto no § 2º" are never split.
_ARTICLE_PARAGRAPH_BREAK_RE = re.compile(
    r"([.!?;:\)]|\*\*)\s+"
    r"((?:\*\*)?(?:Art\.?\s*\d+[º°oª]?(?:-[A-Z])?\.?|§\s*\d+[º°oª]?\.?|Par[aá]grafo\s+[uú]nico\.?)\s+)"
)
# 3. ALL-CAPS heading immediately followed by Art. N (e.g. "DAS DEFINIÇÕES Art. 4º").
_CAPS_HEADING_ART_RE = re.compile(
    r"([A-ZÀ-Þ]{3,})\s+((?:\*\*)?Art\.?\s*\d+[º°oª]?(?:-[A-Z])?\.?\s+)"
)
# 4. Roman numeral incisos ("I -", "II -", ..., "XXXVIII -") after ";", ":", or "; e".
_ROMAN_NUMERAL = (
    r"(?=[IVXLCDM])M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"
)
_ROMAN_INCISO_BREAK_RE = re.compile(rf"([;:]|;\s*e)\s+({_ROMAN_NUMERAL}\s*[-–—]\s+)")

_WORD_SHAPE_RE = re.compile(r"^(?:[a-zà-ÿ]+|[A-ZÀ-Þ]+|[A-ZÀ-Þ][a-zà-ÿ]+)$")
_LOWER_WORD_RE = re.compile(r"[a-zà-ÿ]+")
_COMMON_WORDS = frozenset(
    "de da do das dos que em no na nos nas para com por os as um uma ao aos se ou "
    "lei art estado sua seu pelo pela este esta sobre mais não nao como ser sem entre "
    "dia ano governo poder fica ficam artigo parágrafo inciso".split()
)


def _from_cp1252(match: re.Match[str]) -> str:
    return bytes([ord(match.group())]).decode("cp1252", errors="ignore")


def _clean_markdown(text: str) -> str:
    """Clean markdown fences, cp1252 C1 controls, and non-image links."""
    if text.startswith("```markdown"):
        text = text.removeprefix("```markdown").strip()
    elif text.startswith("```md"):
        text = text.removeprefix("```md").strip()
    if text.endswith("```"):
        text = text.removesuffix("```").strip()
    text = _C1_RE.sub(_from_cp1252, text)
    text = _NON_IMAGE_LINK_RE.sub(r"\1", text)
    return text.strip()


def _valid_markdown(text: str | None, min_length: int = 50) -> tuple[bool, str]:
    if not text:
        return (False, "text_markdown is None or empty")
    stripped = text.strip()
    if not stripped:
        return (False, "text_markdown is empty after strip")
    cleaned = stripped.translate(
        str.maketrans("", "", string.punctuation + string.whitespace)
    )
    if not cleaned:
        return (False, "text_markdown contains only punctuation/whitespace")
    lower = stripped.lower()
    for pattern in _SERVER_ERROR_PATTERNS:
        if pattern in lower:
            return (False, f"text_markdown contains server error: {pattern}")
    if len(stripped) < min_length:
        return (
            False,
            f"text_markdown too short ({len(stripped)} < {min_length} chars)",
        )
    return (True, "")


def readable_text(text: str) -> bool:
    """False for noisy OCR layers: odd-shaped tokens and few Portuguese function words.

    Calibrated on 2012 archives: flags 13 of ~450 PB/PI scans, no clean documents.
    """
    shaped = odd = 0
    for raw in text.split():
        token = raw.strip(".,;:()[]\"'“”‘’-–—ºª°§/")
        if any(c.isalpha() for c in token):
            if _WORD_SHAPE_RE.match(token):
                shaped += 1
            else:
                odd += 1
    words = [w for w in _LOWER_WORD_RE.findall(text.lower()) if len(w) >= 2]
    if shaped + odd < 20 or not words:
        return True
    common = sum(w in _COMMON_WORDS for w in words) / len(words)
    return shaped / (shaped + odd) >= 0.8 or common >= 0.3


def _restore_legal_line_breaks(md: str) -> str:
    """Restore paragraph breaks merged by ``pdf_oxide`` on single-spaced legal PDFs.

    Splits cleanly before ``TÍTULO``/``CAPÍTULO``/``SEÇÃO``, ``Art. N``,
    ``§ N``, ``Parágrafo único``, and Roman numeral incisos (``I -``, ``II -``)
    only when preceded by a structural terminator, avoiding inline citations
    such as ``nos termos do art. 5º`` or ``conforme o Art. 10``.
    """
    if not md:
        return md
    out = _HEADING_BREAK_RE.sub(r"\1\n\n\2", md)
    out = _ARTICLE_PARAGRAPH_BREAK_RE.sub(r"\1\n\n\2", out)
    out = _CAPS_HEADING_ART_RE.sub(r"\1\n\n\2", out)
    out = _ROMAN_INCISO_BREAK_RE.sub(r"\1\n\n\2", out)
    return out


def _open_pdf(data: bytes) -> pdf_oxide.PdfDocument:
    try:
        return pdf_oxide.PdfDocument.from_bytes(data)
    except Exception as exc:
        raise ValueError(f"Could not open PDF: {exc}") from exc


def _page_dimensions(
    doc: pdf_oxide.PdfDocument, page_index: int
) -> tuple[float, float]:
    box = doc.page_crop_box(page_index) or doc.page_media_box(page_index)
    if not box:
        return (0.0, 0.0)
    return (abs(float(box[2]) - float(box[0])), abs(float(box[3]) - float(box[1])))


def _has_ocr_metadata_pypdf(content: bytes) -> bool:
    """Inspect ``/Producer`` and ``/Creator`` in the PDF Info dict via ``pypdf``."""
    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
        meta = reader.metadata
        if not meta:
            return False
        producer = str(meta.get("/Producer") or "").lower()
        creator = str(meta.get("/Creator") or "").lower()
        combined = f"{producer} {creator}"
        return any(kw in combined for kw in _OCR_METADATA_KEYWORDS)
    except Exception:
        return False


def pdf_page_count(data: bytes) -> int:
    """Return the exact number of pages in a PDF, raising ``ValueError`` if invalid."""
    doc = _open_pdf(data)
    return int(doc.page_count)


def is_pdf_scanned(
    content: bytes,
    max_pages_to_check: int = 10,
    *,
    _doc: pdf_oxide.PdfDocument | None = None,
    _plain_pages: list[str] | None = None,
    _page_images_cache: dict[int, tuple[float, list[dict[str, Any]]]] | None = None,
) -> tuple[bool, float]:
    """Determine whether a PDF is primarily a scanned/image document.

    Uses ``doc.page_images(i)`` (NOT ``extract_images(i)``, which misses 1-bit
    CCITT strip scans, and NOT ``classify_document()``, which misclassifies
    OCR-layered scans), ``doc.extract_text(i)``, ``classify_page(i)``
    ``invisible_text_ratio``, and ``pypdf`` ``/Producer``/``/Creator`` metadata.

    Returns:
        ``(is_scanned, confidence)`` where *confidence* is in ``[0, 1]``.
    """
    doc = _doc if _doc is not None else _open_pdf(content)
    total_pages = int(doc.page_count)
    if total_pages == 0:
        return (False, 0.0)

    if total_pages <= max_pages_to_check:
        pages_to_check = list(range(total_pages))
    else:
        step = total_pages / max_pages_to_check
        pages_to_check = sorted({int(i * step) for i in range(max_pages_to_check)})

    ocr_meta_checked: bool | None = None
    page_scores: list[float] = []

    for page_num in pages_to_check:
        width, height = _page_dimensions(doc, page_num)
        page_area = width * height
        if page_area <= 0:
            continue

        images = doc.page_images(page_num)
        if _page_images_cache is not None:
            _page_images_cache[page_num] = (page_area, images)

        total_img_area = sum(
            max(0.0, float(img["width"])) * max(0.0, float(img["height"]))
            for img in images
        )
        coverage = min(1.0, total_img_area / page_area)
        text = (
            _plain_pages[page_num]
            if _plain_pages is not None and page_num < len(_plain_pages)
            else doc.extract_text(page_num)
        ).strip()
        text_length = len(text)

        score = 0.5
        is_blank = False
        if coverage > 0.8:
            try:
                cp = json.loads(doc.classify_page(page_num))
                signals = cp.get("signals") or {}
            except Exception:
                signals = {}
            has_invisible_text = float(signals.get("invisible_text_ratio", 0.0)) > 0.0
            has_ocr_metadata = signals.get("producer_prior") == "scanner"
            if not has_ocr_metadata:
                if ocr_meta_checked is None:
                    ocr_meta_checked = _has_ocr_metadata_pypdf(content)
                has_ocr_metadata = ocr_meta_checked

            if has_invisible_text or has_ocr_metadata:
                score = 1.0
            elif text_length < 50:
                score = 1.0
            elif text_length > 200:
                score = 0.9
            elif width > height:
                score = 0.2
            else:
                score = 0.8
        elif coverage > 0.1:
            score = 0.8 if text_length < 50 else 0.1
        elif text_length > 10:
            score = 0.0
        else:
            is_blank = True

        if not is_blank:
            page_scores.append(score)

    if not page_scores:
        return (False, 1.0)
    avg_score = sum(page_scores) / len(page_scores)
    scanned = avg_score > 0.5
    confidence = avg_score if scanned else 1.0 - avg_score
    return (scanned, round(confidence, 4))


def _page_has_qualifying_image(
    doc: pdf_oxide.PdfDocument,
    page_index: int,
    image_size_limit: float,
    cache: dict[int, tuple[float, list[dict[str, Any]]]],
) -> bool:
    """Return True if *page_index* has a non-decorative, non-full-scan image."""
    cached = cache.get(page_index)
    if cached is not None:
        page_area, images = cached
    else:
        width, height = _page_dimensions(doc, page_index)
        page_area = width * height
        images = doc.page_images(page_index) if page_area > 0 else []
    if page_area <= 0 or not images:
        return False
    for img in images:
        ratio = (
            max(0.0, float(img["width"])) * max(0.0, float(img["height"])) / page_area
        )
        if image_size_limit <= ratio < 0.8:
            return True
    return False


def _digital_pdf(
    doc: pdf_oxide.PdfDocument,
    page_count: int,
    image_size_limit: float,
    embed_images: bool,
    page_images_cache: dict[int, tuple[float, list[dict[str, Any]]]],
    *,
    yield_between_pages: bool = False,
) -> str:
    """Convert a digital PDF page-by-page with selective image embedding."""
    pages: list[str] = []
    for i in range(page_count):
        include_imgs = embed_images and _page_has_qualifying_image(
            doc, i, image_size_limit, page_images_cache
        )
        pages.append(
            doc.to_markdown(
                i,
                detect_headings=True,
                include_images=include_imgs,
                embed_images=include_imgs,
            )
        )
        if yield_between_pages:
            time.sleep(0)
    return _restore_legal_line_breaks("\n\n".join(pages))


def _pdf_text_layer(
    data: bytes,
    image_size_limit: float = 0.1,
    check_readable: bool = True,
    embed_images: bool = True,
    *,
    yield_between_pages: bool = False,
) -> str | None:
    """Extract best embedded text via single-open ``pdf_oxide``; ``None`` means OCR.

    Safe to execute in a ``spawn`` worker subprocess (no logging side effects).
    """
    doc = _open_pdf(data)
    page_count = int(doc.page_count)
    if page_count == 0:
        return None

    plain_pages: list[str] = []
    for i in range(page_count):
        plain_pages.append(doc.extract_text(i))
        if yield_between_pages:
            time.sleep(0)
    plain = "\n\n".join(plain_pages)
    minimum = max(50, page_count * 100)

    def long_enough(text: str) -> bool:
        check = _WATERMARK_CHECK_RE.sub("", _MD_BASE64_IMAGE_RE.sub("", text)).strip()
        return _valid_markdown(check, min_length=minimum)[0]

    # Judge the plain layer: calibrated on plain text without markdown table pipes.
    # Without OCR configured, a noisy layer still beats dropping the document.
    if check_readable and not readable_text(plain):
        return None

    page_images_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}
    scanned, confidence = is_pdf_scanned(
        data,
        _doc=doc,
        _plain_pages=plain_pages,
        _page_images_cache=page_images_cache,
    )
    if not scanned and confidence >= 0.7:
        try:
            markdown = _digital_pdf(
                doc,
                page_count,
                image_size_limit,
                embed_images,
                page_images_cache,
                yield_between_pages=yield_between_pages,
            )
        except Exception:
            markdown = ""
        if long_enough(markdown):
            return _clean_markdown(markdown)

    return _clean_markdown(plain) if long_enough(plain) else None


def slice_pdf_pages(pdf_bytes: bytes, pages: list[int]) -> bytes:
    """Extract 1-based *pages* from *pdf_bytes* using ``pypdf``.

    Preserves ``df.py`` semantics:
    - 1-based page numbers, preserving requested order (including duplicates).
    - Out-of-range page numbers are silently ignored.
    - Returns original *pdf_bytes* unchanged when *pages* is empty or no page survives.
    - Avoids ``pdf_oxide.extract_pages_to_bytes()``'s up-to-16.7x object-bloat defect.
    """
    if not pages:
        return pdf_bytes
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:
        raise ValueError(f"Could not open PDF: {exc}") from exc

    total = len(reader.pages)
    valid_indices = [p - 1 for p in pages if 1 <= p <= total]
    if not valid_indices:
        return pdf_bytes

    writer = pypdf.PdfWriter()
    for idx in valid_indices:
        page = writer.add_page(reader.pages[idx])
        try:
            page.compress_content_streams()
        except Exception:
            pass
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def render_pdf_batch(
    data: bytes, start: int, end: int, *, dpi: int = 150
) -> list[bytes]:
    """Render 0-based page slice ``[start, end)`` to PNG bytes at explicit *dpi*."""
    doc = _open_pdf(data)
    total = int(doc.page_count)
    lo = max(0, start)
    hi = min(end, total)
    return [doc.render_page(i, dpi=dpi, format="png") for i in range(lo, hi)]


def default_pdf_workers() -> int:
    """Resolve worker count for PDF extraction at call time.

    Defaults to ``min(2, cpu_count)`` for library safety; set ``PDF_WORKERS``
    (e.g. up to ``min(8, cpu_count)``) for bulk CLI throughput, or ``0`` for
    thread-only execution.
    """
    raw = os.environ.get("PDF_WORKERS")
    if raw is not None and raw.strip():
        return max(0, int(raw))
    return min(2, os.cpu_count() or 1)


class PdfRunner:
    """Bounded async executor for CPU/GIL-bound ``pdf_oxide`` work.

    - ``"auto"`` (default): uses a bounded ``spawn`` ``ProcessPoolExecutor``
      so ``pdf_oxide``'s first-page GIL-holding lazy init never stalls the
      event loop (<10 ms max tick gap), transparently falling back to
      ``"thread"`` mode if pool creation fails with ``PermissionError`` or
      ``OSError`` (e.g. ``/dev/shm``-less AWS Lambda or restricted containers).
    - ``"process"``: requires a ``spawn`` ``ProcessPoolExecutor`` (no fallback).
    - ``"thread"``: runs inside ``asyncio.to_thread`` serialized by a per-loop
      ``asyncio.Semaphore(1)`` (never spawns child processes).
    """

    def __init__(
        self,
        executor: PdfExecutor = "auto",
        max_workers: int | None = None,
    ) -> None:
        if executor not in ("auto", "process", "thread"):
            raise ValueError(f"Unsupported pdf_executor: {executor!r}")
        self.executor_mode: PdfExecutor = executor
        self.max_workers = max_workers
        self._pool: ProcessPoolExecutor | None = None
        self._process_unavailable = False
        self._thread_sems: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, asyncio.Semaphore
        ] = weakref.WeakKeyDictionary()

    def _get_thread_sem(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        sem = self._thread_sems.get(loop)
        if sem is None:
            sem = asyncio.Semaphore(1)
            self._thread_sems[loop] = sem
        return sem

    def _effective_workers(self) -> int:
        if self.max_workers is not None:
            return max(0, self.max_workers)
        return default_pdf_workers()

    def _ensure_pool(self, workers: int) -> ProcessPoolExecutor:
        if self._pool is None:
            mp_ctx = multiprocessing.get_context("spawn")
            self._pool = ProcessPoolExecutor(max_workers=workers, mp_context=mp_ctx)
        return self._pool

    async def run(self, function: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        workers = self._effective_workers()
        use_thread = (
            self.executor_mode == "thread"
            or workers <= 0
            or (self.executor_mode == "auto" and self._process_unavailable)
        )
        if use_thread:
            async with self._get_thread_sem():
                return await asyncio.to_thread(function, *args, **kwargs)

        try:
            pool = self._ensure_pool(workers)
        except (PermissionError, OSError) as exc:
            if self.executor_mode != "auto":
                raise
            self._process_unavailable = True
            logger.warning(
                "PDF ProcessPoolExecutor unavailable (%s); falling back to thread mode",
                exc,
            )
            async with self._get_thread_sem():
                return await asyncio.to_thread(function, *args, **kwargs)

        loop = asyncio.get_running_loop()
        try:
            if kwargs:
                return await loop.run_in_executor(
                    pool, _apply_kwargs, function, args, kwargs
                )
            return await loop.run_in_executor(pool, function, *args)
        except BrokenProcessPool:
            if self._pool is pool:
                self._pool = None
                pool.shutdown(wait=False, cancel_futures=True)
            raise

    def shutdown(self, *, wait: bool = False) -> None:
        pool = self._pool
        self._pool = None
        if pool is not None:
            pool.shutdown(wait=wait, cancel_futures=not wait)


def _apply_kwargs(
    function: Callable[..., _T], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> _T:
    return function(*args, **kwargs)


_runners: dict[tuple[PdfExecutor, int | None], PdfRunner] = {}


def _get_runner(executor: PdfExecutor, max_workers: int | None) -> PdfRunner:
    key = (executor, max_workers)
    runner = _runners.get(key)
    if runner is None:
        runner = PdfRunner(executor=executor, max_workers=max_workers)
        _runners[key] = runner
    return runner


def shutdown_pdf_pool(*, wait: bool = False) -> None:
    """Shut down all cached module-level PDF worker pools cleanly."""
    runners = list(_runners.values())
    _runners.clear()
    for runner in runners:
        runner.shutdown(wait=wait)


atexit.register(shutdown_pdf_pool)


async def run_pdf_task(
    function: Callable[..., _T],
    *args: Any,
    executor: PdfExecutor = "auto",
    max_workers: int | None = None,
    **kwargs: Any,
) -> _T:
    """Execute a CPU-bound PDF callable using the configured bounded runner."""
    runner = _get_runner(executor, max_workers)
    return await runner.run(function, *args, **kwargs)


async def pdf_to_markdown(
    data: bytes,
    ocr: OCR | None = None,
    *,
    image_size_limit: float = 0.1,
    embed_images: bool = True,
    executor: PdfExecutor = "auto",
    max_workers: int | None = None,
) -> str:
    """Convert PDF *data* to cleaned Markdown, falling back to *ocr* when scanned."""
    runner = _get_runner(executor, max_workers)
    yield_pages = executor == "thread" or (
        executor == "auto"
        and (runner._process_unavailable or runner._effective_workers() <= 0)
    )
    text = await run_pdf_task(
        _pdf_text_layer,
        data,
        image_size_limit,
        ocr is not None,
        embed_images,
        executor=executor,
        max_workers=max_workers,
        yield_between_pages=yield_pages,
    )
    if text is not None:
        return text
    if ocr is not None:
        return _clean_markdown(await ocr.pdf(data))
    raise ValueError("PDF needs OCR but no OCR model is configured")
