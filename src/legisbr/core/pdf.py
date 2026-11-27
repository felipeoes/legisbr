"""Permissive high-performance PDF engine built on ``pypdfium2``, ``pypdf``, and ``pillow``.

Self-contained (stdlib + ``pypdfium2`` at module import; ``pypdf`` and ``PIL``
are imported lazily on demand) so ``spawn`` worker subprocesses start fast
without importing ``aiohttp``, ``bs4``, or ``lxml``. Ruled tables
are extracted natively from PDFium C++ ``FPDF_PAGEOBJ_PATH`` geometry and
``FPDFText`` em-boxes.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import bisect
import ctypes
import hashlib
import io
import itertools
import logging
import math
import multiprocessing
import os
import re
import string
import time
import weakref
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from operator import itemgetter
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

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
_MULTI_BLANK_RE = re.compile(r"\n{3,}")

# ---------------------------------------------------------------------------
# Legislative structure restoration regexes (citation-safe, ALL PDF routes)
# ---------------------------------------------------------------------------
_ROMAN_NUMERAL = (
    r"(?=[IVXLCDM])M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"
)

# 1. Structural headings merged mid-paragraph after punctuation / emphasis / quote.
_HEADING_BREAK_RE = re.compile(
    r"([.!?;:\)\"”]|(?:\*{1,3}))\s+"
    r"((?:\*{1,3})?(?:[\"“])?(?:T[ÍI]TULO|CAP[ÍI]TULO|SE[ÇC][ÃA]O|SUBSE[ÇC][ÃA]O|LIVRO)\s+[IVXLCDM\d]+)"
)

# 2. New Art. N / Artigo N / Art. 1.º / § N / Parágrafo único after sentence terminator,
#    bold/italic close (** or *), or closing quote. Supports optional leading bold/italic,
#    opening quotes ("Art. / “Art. / “§), and bold closing right after number (**Art. 104.**).
#    Requires uppercase "Art."/"Artigo" + preceding terminator so inline citations like
#    "nos termos do art. 5º", "conforme o Art. 10", or "observado o § 2º" are never split.
_ART_LABEL = (
    r"(?:Artigo|Art\.?)\s*\d+(?:\.\d+)*\.?[º°oª]?(?:-[A-Z])?\.?"
    r"|§\s*\d+\.?[º°oª]?(?:-[A-Z])?\.?"
    r"|Par[aá]grafo\s+[uú]nico\.?"
)
_ARTICLE_PARAGRAPH_BREAK_RE = re.compile(
    rf"([.!?;:\)\"”]|(?:\*{{1,3}}))\s+"
    rf"((?:\*{{1,3}})?(?:[\"“])?(?:{_ART_LABEL})(?:\*{{1,3}})?(?:\s+|(?=[A-ZÀ-Þ]))"
    rf")"
)

# 3. ALL-CAPS heading immediately followed by Art. N / Artigo N (e.g. "DAS DEFINIÇÕES Art. 4º").
_CAPS_HEADING_ART_RE = re.compile(
    r"([A-ZÀ-Þ]{3,})\s+((?:\*{1,3})?(?:[\"“])?(?:Artigo|Art\.?)\s*\d+\.?[º°oª]?(?:-[A-Z])?\.?(?:\*{1,3})?\s+)"
)

# 4. Roman numeral incisos ("I -", "II -", "**II** -", "**III -**") after ";", ":", "; e", or italic "*".
_ROMAN_INCISO_BREAK_RE = re.compile(
    rf"([;:]|;\s*e|\.\*)\s+((?:\*{{1,3}})?{_ROMAN_NUMERAL}(?:\*{{1,3}})?\s*[-–—](?:\*{{1,3}})?\s+)"
)

# Line-start structural pattern for paragraph reconstruction (keeps legal clauses on own paragraphs).
_LEGAL_LINE_START_RE = re.compile(
    rf"^(?:\*{{1,3}})?(?:[\"“])?(?:"
    rf"(?:Artigo|Art\.?)\s*\d+"
    rf"|§\s*\d+"
    rf"|Par[aá]grafo\s+[uú]nico\b"
    rf"|(?:T[ÍI]TULO|CAP[ÍI]TULO|SE[ÇC][ÃA]O|SUBSE[ÇC][ÃA]O|LIVRO|ANEXO|TABELA|QUADRO)\b"
    rf"|{_ROMAN_NUMERAL}\s*[-–—]\s+"
    rf"|[a-z]\)\s+"
    rf"|\d+\.\d*\s+"
    rf"|[•●▪\-–—]\s+"
    rf")"
)

# ---------------------------------------------------------------------------
# Calibrated readability metrics (T0 + large-doc-garble; zero `or common >= 0.3`)
# ---------------------------------------------------------------------------
_PURE_WORD = r"(?:[a-zà-ÿ]+|[A-ZÀ-Þ]+|[A-ZÀ-Þ][a-zà-ÿ]+)"
_LEGAL_TOKEN_RE = re.compile(
    rf"^(?:"
    rf"{_PURE_WORD}(?:[-/]{_PURE_WORD})*"
    rf"|R\$"
    rf"|\d+(?:[.,]\d+)*(?:[º°oª²³]|m²?|km|ha|cc|ml|mg|kg|MVA|kV|MW|GWh|ppm|%)?"
    rf"|[A-ZÀ-Þ]{{1,5}}[-/]\d+(?:[.,]\d+)*[A-Z]?"
    rf"|\d+[-/][A-ZÀ-Þ]{{1,3}}"
    rf"|[A-ZÀ-Þ]{{1,3}}\d{{1,3}}[A-ZÀ-Þ]?"
    rf"|[ENW]=\d+(?:[.,]\d+)*m?"
    rf"|\d+[º°'\"][NSEWO]"
    rf"|m[²³]"
    rf"|n[º°oª]"
    rf")$"
)
# Exclude \x02 (PDFium's internal soft-hyphen marker) and normal whitespace \t\n\r.
_CTRL_CHAR_RE = re.compile(r"[\x00\x01\x03-\x08\x0b\x0c\x0e-\x1f\ufffd]")
_OCR_NOISE_SYM_RE = re.compile(r"[\^~|`£±¥€™©®¤¦¬¶µ¼½¾¿¡]")
_MIXED_CASE_WORD_RE = re.compile(
    r"^[a-zà-ÿ]*[a-zà-ÿ][A-ZÀ-Þ][a-zà-ÿA-ZÀ-Þ]*$"
    r"|^[A-ZÀ-Þ]{2,}[a-zà-ÿ]+[A-ZÀ-Þ][a-zà-ÿA-ZÀ-Þ]*$"
)
# Exclude single lowercase accented vowel inside ALL-CAPS OCR (e.g. ESPíRITO, PARAíBA).
_ALLCAPS_ACCENT_QUIRK_RE = re.compile(r"^[A-ZÀ-Þ]{2,}[áéíóúâêôãõàç][A-ZÀ-Þ]{1,}$")
_DIGIT_IN_WORD_RE = re.compile(r"[a-zà-ÿ]{2,}\d[a-zà-ÿ]{2,}")
_PUNCT_ODD_CHARS = frozenset("!@#$%^&*+={}[]<>~`|\\")


def _from_cp1252(match: re.Match[str]) -> str:
    return bytes([ord(match.group())]).decode("cp1252", errors="ignore")


def _clean_markdown(text: str) -> str:
    """Clean markdown fences, cp1252 C1 controls, non-image links, and excess blank lines."""
    if text.startswith("```markdown"):
        text = text.removeprefix("```markdown").strip()
    elif text.startswith("```md"):
        text = text.removeprefix("```md").strip()
    if text.endswith("```"):
        text = text.removesuffix("```").strip()
    text = _C1_RE.sub(_from_cp1252, text)
    text = _NON_IMAGE_LINK_RE.sub(r"\1", text)
    text = _MULTI_BLANK_RE.sub("\n\n", text)
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


def _text_quality_stats(text: str) -> dict[str, float]:
    n_ch = len(text)
    if n_ch == 0:
        return {
            "tokens": 0,
            "odd": 0,
            "shaped": 1.0,
            "ctrl_count": 0,
            "ctrl_ratio": 0.0,
            "noise_count": 0,
            "noise_ratio": 0.0,
            "mix_count": 0,
            "mix_ratio": 0.0,
            "dig_w_count": 0,
            "dig_w_ratio": 0.0,
            "punct_odd_count": 0,
            "punct_odd_ratio": 0.0,
        }
    ctrl_count = len(_CTRL_CHAR_RE.findall(text))
    noise_count = len(_OCR_NOISE_SYM_RE.findall(text))
    alpha_count = sum(1 for c in text if c.isalpha())
    punct_odd_count = sum(1 for c in text if c in _PUNCT_ODD_CHARS)

    shaped = odd = mix = dig_w = 0
    for raw in text.split():
        tok = raw.strip(".,;:()[]\"'“”‘’-–—ºª°§/*")
        if not tok or not any(c.isalpha() for c in tok):
            continue
        if "://" in tok or "www." in tok or "\\" in tok:
            shaped += 1
            continue
        if _LEGAL_TOKEN_RE.match(tok) or _ALLCAPS_ACCENT_QUIRK_RE.match(tok):
            shaped += 1
        else:
            odd += 1
        if len(tok) >= 4 and "-" not in tok and "/" not in tok and "'" not in tok:
            if (
                _MIXED_CASE_WORD_RE.match(tok)
                and not tok.startswith(("Mc", "Mac"))
                and not _ALLCAPS_ACCENT_QUIRK_RE.match(tok)
            ):
                mix += 1
        if _DIGIT_IN_WORD_RE.search(tok):
            dig_w += 1

    total_tok = shaped + odd
    return {
        "tokens": total_tok,
        "odd": odd,
        "shaped": shaped / total_tok if total_tok else 1.0,
        "ctrl_count": ctrl_count,
        "ctrl_ratio": ctrl_count / n_ch,
        "noise_count": noise_count,
        "noise_ratio": noise_count / n_ch,
        "mix_count": mix,
        "mix_ratio": mix / max(1, total_tok),
        "dig_w_count": dig_w,
        "dig_w_ratio": dig_w / max(1, total_tok),
        "punct_odd_count": punct_odd_count,
        "punct_odd_ratio": punct_odd_count / max(1, alpha_count + punct_odd_count),
    }


def _page_has_cmap_gibberish(text: str) -> bool:
    """True when a single page has severe C0 control-char / missing-ToUnicode corruption."""
    stripped = text.strip()
    if len(stripped) < 60:
        return False
    ctrl_count = len(_CTRL_CHAR_RE.findall(text))
    if ctrl_count >= 25:
        if (ctrl_count / len(text)) > 0.025:
            return True
        # Missing ToUnicode maps (e.g. RN id=56 pp.20-31) emit C0 controls + pure punctuation.
        alpha_count = sum(1 for c in text if c.isalpha())
        punct_odd = sum(1 for c in text if c in _PUNCT_ODD_CHARS)
        if punct_odd / max(1, alpha_count + punct_odd) > 0.20:
            return True
    return False


def readable_text(text: str, *, strict_ocr_layer: bool = False) -> bool:
    """Return False for garbled OCR layers or broken-ToUnicode/Caesar-shift text.

    Calibrated with the ``T0 + large-doc-garble`` policy across 6,614 real PDFs:
    - Removes the permissive ``or common >= 0.3`` bypass.
    - Keeps digital corpora (AL/MA/PA/RS/CONAMA), ``NAME_LIST`` decrees, Latin
      botanical lists, and clean ABBYY FineReader PDFs at 0% false positives.
    - Keeps ES/SE 1-page stamped-header scans cost-effective while catching severe
      garbled scans (PB 277/58, RR 9, DF 2253) and broken CMaps (DF 361/1851/1885, RN 56).
    """
    m = _text_quality_stats(text)
    if m["ctrl_count"] >= 25 and m["ctrl_ratio"] > 0.025:
        return False
    if m["tokens"] < 20:
        return True

    # Core T0 thresholds (applies to all PDFs)
    if m["shaped"] < 0.80 and m["odd"] >= 10:
        return False
    if m["noise_ratio"] > 0.008 and m["noise_count"] >= 8:
        return False
    if m["mix_ratio"] > 0.040 and m["mix_count"] >= 6:
        return False
    if m["dig_w_ratio"] > 0.025 and m["dig_w_count"] >= 4:
        return False

    # Severe multi-page / heavy-garble scan catch (only on confirmed scanned PDFs)
    if strict_ocr_layer:
        # Large multi-page garbled OCR (e.g. 57p Readiris DF id=2253) without hitting
        # short 1-page ES/SE stamped Atos (~80-200 tokens) or clean ABBYY (shaped ~1.00).
        if m["tokens"] >= 500 and (
            m["shaped"] < 0.91 or m["mix_ratio"] > 0.018 or m["noise_ratio"] > 0.0023
        ):
            return False
        # Severe mixed-case or punctuation-heavy garble on shorter scans without hitting
        # clean stamped headers (keeps ES at ~191/2,224 instead of 441).
        if m["mix_ratio"] > 0.030 and m["mix_count"] >= 5:
            return False
        if (
            m["punct_odd_ratio"] > 0.012
            and m["punct_odd_count"] >= 16
            and m["shaped"] < 0.92
        ):
            return False
    return True


def _restore_legal_line_breaks(md: str) -> str:
    """Restore paragraph breaks on single-spaced legal PDFs (digital & plain fallback).

    Handles ``TÍTULO``/``CAPÍTULO``/``SEÇÃO``, ``Art. N``, ``Artigo N``, ``Art. 1.º``,
    bold ``**Art. 104.**``, italic ``*`` terminators, quoted ``"Art.``/``“§``,
    ``Parágrafo único``, and plain/bold Roman incisos without breaking inline citations.
    """
    if not md:
        return md
    out = _HEADING_BREAK_RE.sub(r"\1\n\n\2", md)
    out = _ARTICLE_PARAGRAPH_BREAK_RE.sub(r"\1\n\n\2", out)
    out = _CAPS_HEADING_ART_RE.sub(r"\1\n\n\2", out)
    out = _ROMAN_INCISO_BREAK_RE.sub(r"\1\n\n\2", out)
    return out


# ---------------------------------------------------------------------------
# PDFium helpers: open, metadata, page analysis, line extraction, tables, images
# ---------------------------------------------------------------------------
def _open_pdf(data: bytes) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(data)
    except Exception as exc:
        raise ValueError(f"Could not open PDF: {exc}") from exc


def _is_valid_zero_page_pdf(data: bytes) -> bool:
    """Return True if *data* is a valid 0-page PDF (which PDFium refuses to open)."""
    try:
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(data))
        return len(reader.pages) == 0
    except Exception:
        return False


def _has_ocr_metadata_doc(doc: pdfium.PdfDocument) -> bool:
    """Inspect ``/Producer`` and ``/Creator`` natively via ``pypdfium2``."""
    try:
        producer = (doc.get_metadata_value("Producer") or "").lower()
        creator = (doc.get_metadata_value("Creator") or "").lower()
        combined = f"{producer} {creator}"
        return any(kw in combined for kw in _OCR_METADATA_KEYWORDS)
    except Exception:
        return False


def pdf_page_count(data: bytes) -> int:
    """Return the exact number of pages in a PDF, raising ``ValueError`` if invalid."""
    try:
        doc = _open_pdf(data)
    except ValueError:
        if _is_valid_zero_page_pdf(data):
            return 0
        raise
    try:
        return len(doc)
    finally:
        doc.close()


@dataclass
class _ImageInfo:
    page_index: int
    x0: float
    top: float
    x1: float
    bottom: float
    ratio: float
    nbbox: tuple[float, float, float, float]
    bpp: int
    sha256: bytes
    vis_text_overlaps: int
    total_vis_text: int
    raw_len: int
    is_jpeg: bool
    obj_index: int


@dataclass
class _PageAnalysis:
    page_index: int
    width: float
    height: float
    text_raw: str
    text_len: int
    img_coverage: float
    bpp1_unocrd_coverage: float
    invis_text_ratio: float
    has_ruled_table_candidate: bool
    images: list[_ImageInfo]


def _normalize_pdfium_soft_hyphens(text: str) -> str:
    """Clean PDFium soft-hyphen marker (U+0002 / STX)."""
    if "\x02" not in text:
        return text
    text = re.sub(r"\x02\r?\n", "", text)
    return text.replace("\x02", "-")


def _analyze_page(
    page: pdfium.PdfPage,
    page_index: int,
    *,
    inspect_objects: bool = True,
    collect_image_hashes: bool = True,
    image_size_limit: float = 0.1,
) -> _PageAnalysis:
    """Single-pass PDFium page inspection: text, image coverage, 1-bit masks, invisible text, rules."""
    pw, ph = page.get_size()
    page_area = pw * ph if pw > 0 and ph > 0 else 0.0

    tp = page.get_textpage()
    try:
        raw_text = tp.get_text_bounded()
    finally:
        tp.close()
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    norm_text = _normalize_pdfium_soft_hyphens(raw_text)
    text_len = len(norm_text.strip())

    pa = _PageAnalysis(
        page_index=page_index,
        width=pw,
        height=ph,
        text_raw=norm_text,
        text_len=text_len,
        img_coverage=0.0,
        bpp1_unocrd_coverage=0.0,
        invis_text_ratio=0.0,
        has_ruled_table_candidate=False,
        images=[],
    )
    if not inspect_objects or page_area <= 0:
        return pa
    return _populate_page_objects(
        page,
        pa,
        collect_image_hashes=collect_image_hashes,
        image_size_limit=image_size_limit,
    )


def _populate_page_objects(
    page: pdfium.PdfPage,
    pa: _PageAnalysis,
    *,
    collect_image_hashes: bool = True,
    image_size_limit: float = 0.1,
) -> _PageAnalysis:
    """Inspect PDFium page objects (images, invisible text, ruled paths) on an analyzed page."""
    pw, ph = pa.width, pa.height
    page_area = pw * ph if pw > 0 and ph > 0 else 0.0
    if page_area <= 0:
        return pa

    text_centers: list[tuple[float, float]] = []
    vis_text_centers: list[tuple[float, float]] = []
    raw_imgs: list[
        tuple[float, float, float, float, float, int, bytes, int, bool, int]
    ] = []
    txt_objs = 0
    invis_objs = 0
    h_buckets: set[int] = set()
    v_buckets: set[int] = set()
    rects = 0
    has_rules = False
    img_idx = 0

    obj_text = pdfium_c.FPDF_PAGEOBJ_TEXT
    obj_image = pdfium_c.FPDF_PAGEOBJ_IMAGE
    obj_path = pdfium_c.FPDF_PAGEOBJ_PATH
    obj_form = pdfium_c.FPDF_PAGEOBJ_FORM
    invis_mode = pdfium_c.FPDF_TEXTRENDERMODE_INVISIBLE

    page_count = pdfium_c.FPDFPage_CountObjects
    page_get = pdfium_c.FPDFPage_GetObject
    form_count = pdfium_c.FPDFFormObj_CountObjects
    form_get = pdfium_c.FPDFFormObj_GetObject
    get_type = pdfium_c.FPDFPageObj_GetType
    get_bounds = pdfium_c.FPDFPageObj_GetBounds
    get_render_mode = pdfium_c.FPDFTextObj_GetTextRenderMode
    get_img_meta = pdfium_c.FPDFImageObj_GetImageMetadata
    count_segs = pdfium_c.FPDFPath_CountSegments

    c_l = ctypes.c_float()
    c_b = ctypes.c_float()
    c_r = ctypes.c_float()
    c_t = ctypes.c_float()
    meta = pdfium_c.FPDF_IMAGEOBJ_METADATA()
    page_raw = page.raw

    def _walk(container: Any, is_form: bool, depth: int) -> None:
        nonlocal txt_objs, invis_objs, rects, has_rules, img_idx
        n_objs = form_count(container) if is_form else page_count(container)
        if n_objs <= 0:
            return
        for i in range(n_objs):
            raw_obj = form_get(container, i) if is_form else page_get(container, i)
            if not raw_obj:
                continue
            ot = get_type(raw_obj)
            if ot == obj_text:
                txt_objs += 1
                if get_bounds(raw_obj, c_l, c_b, c_r, c_t):
                    cx = (c_l.value + c_r.value) * 0.5
                    cy = (c_b.value + c_t.value) * 0.5
                    text_centers.append((cx, cy))
                    if get_render_mode(raw_obj) == invis_mode:
                        invis_objs += 1
                    else:
                        vis_text_centers.append((cx, cy))
            elif ot == obj_image:
                cur_idx = img_idx
                img_idx += 1
                if get_bounds(raw_obj, c_l, c_b, c_r, c_t):
                    left, b, r, t = c_l.value, c_b.value, c_r.value, c_t.value
                    w, h = max(0.0, r - left), max(0.0, t - b)
                    ratio = (w * h) / page_area
                    bpp = (
                        int(meta.bits_per_pixel)
                        if get_img_meta(raw_obj, page_raw, meta)
                        else -1
                    )
                    sha = b""
                    raw_len = 0
                    is_jpeg = False
                    if collect_image_hashes and image_size_limit <= ratio < 0.80:
                        try:
                            img_helper = pdfium.PdfObject(
                                raw_obj, page=page, pdf=page.pdf, level=depth
                            )
                            raw_data = bytes(img_helper.get_data(decode_simple=False))
                            raw_len = len(raw_data)
                            sha = hashlib.sha256(raw_data).digest()
                            filts = img_helper.get_filters()
                            is_jpeg = filts == ["DCTDecode"] and raw_data.startswith(
                                b"\xff\xd8"
                            )
                        except Exception:
                            pass
                    raw_imgs.append(
                        (left, b, r, t, ratio, bpp, sha, raw_len, is_jpeg, cur_idx)
                    )
            elif ot == obj_path:
                if has_rules:
                    continue
                if get_bounds(raw_obj, c_l, c_b, c_r, c_t):
                    left, b, r, t = c_l.value, c_b.value, c_r.value, c_t.value
                    w, h = abs(r - left), abs(t - b)
                    # Symmetric >= 10pt thresholds handle both portrait and 90-deg rotated pages.
                    if w >= 10.0 and h <= 4.0:
                        h_buckets.add(round(((b + t) * 0.5) / 3.0))
                        if len(h_buckets) >= 3 and len(v_buckets) >= 3:
                            has_rules = True
                    elif h >= 10.0 and w <= 4.0:
                        v_buckets.add(round(((left + r) * 0.5) / 3.0))
                        if len(h_buckets) >= 3 and len(v_buckets) >= 3:
                            has_rules = True
                    elif w >= 10.0 and h >= 10.0:
                        n_segs = count_segs(raw_obj)
                        if n_segs >= 4:
                            rects += max(1, n_segs // 5)
                            if rects >= 4:
                                has_rules = True
            elif ot == obj_form and depth < 3:
                _walk(raw_obj, True, depth + 1)

    try:
        _walk(page_raw, False, 0)
    except Exception:
        pass

    images: list[_ImageInfo] = []
    total_cov = 0.0
    bpp1_unocrd_cov = 0.0
    total_vis = len(vis_text_centers)

    for left, b, r, t, ratio, bpp, sha, raw_len, is_jpeg, cur_idx in raw_imgs:
        total_cov += ratio
        inside_any = 0
        inside_vis = 0
        if ratio >= 0.05:
            inside_any = sum(
                1 for cx, cy in text_centers if left <= cx <= r and b <= cy <= t
            )
            inside_vis = sum(
                1 for cx, cy in vis_text_centers if left <= cx <= r and b <= cy <= t
            )
        if bpp == 1 and ratio >= 0.05 and inside_any < 5:
            bpp1_unocrd_cov += ratio
        top = ph - t
        bottom = ph - b
        nbbox = (
            round(left / pw, 2) if pw else 0.0,
            round(top / ph, 2) if ph else 0.0,
            round(r / pw, 2) if pw else 0.0,
            round(bottom / ph, 2) if ph else 0.0,
        )
        images.append(
            _ImageInfo(
                page_index=pa.page_index,
                x0=left,
                top=top,
                x1=r,
                bottom=bottom,
                ratio=ratio,
                nbbox=nbbox,
                bpp=bpp,
                sha256=sha,
                vis_text_overlaps=inside_vis,
                total_vis_text=total_vis,
                raw_len=raw_len,
                is_jpeg=is_jpeg,
                obj_index=cur_idx,
            )
        )

    invis_ratio = invis_objs / txt_objs if txt_objs > 0 else 0.0

    pa.img_coverage = min(1.0, total_cov)
    pa.bpp1_unocrd_coverage = min(1.0, bpp1_unocrd_cov)
    pa.invis_text_ratio = invis_ratio
    pa.has_ruled_table_candidate = has_rules
    pa.images = images
    return pa


def _score_page_scan(
    pa: _PageAnalysis,
    has_ocr_meta_fn: Callable[[], bool],
) -> tuple[float, bool]:
    """Return ``(scan_score, is_blank)`` for a single analyzed page."""
    if pa.width * pa.height <= 0:
        return (0.0, True)

    cov = pa.img_coverage
    text_len = pa.text_len

    # 1. Un-OCR'd 1-bit CCITT scanned text columns (e.g. Piauí Diário Oficial).
    if pa.bpp1_unocrd_coverage >= 0.25:
        return (1.0, False)

    # 2. Full-page image scan (coverage > 0.80).
    if cov > 0.8:
        has_invis = pa.invis_text_ratio > 0.0
        has_ocr_meta = has_ocr_meta_fn()
        if has_invis or has_ocr_meta:
            return (1.0, False)
        if text_len < 50:
            return (1.0, False)
        if text_len > 200:
            return (0.9, False)
        if pa.width > pa.height:
            return (0.2, False)
        return (0.8, False)

    # 3. Medium/large image with tiny text (partial scan / image-body norm like DF 2395).
    if cov >= 0.30 and text_len < 200:
        return (0.9, False)
    if cov >= 0.20 and text_len < 180:
        return (0.8, False)
    if cov > 0.1:
        return (0.8 if text_len < 10 else 0.1, False)
    if text_len > 10:
        return (0.0, False)
    return (0.0, True)


def is_pdf_scanned(
    content: bytes,
    max_pages_to_check: int = 10,
    *,
    _analyses: dict[int, _PageAnalysis] | None = None,
    _doc: pdfium.PdfDocument | None = None,
) -> tuple[bool, float]:
    """Determine whether a PDF is primarily a scanned/image document.

    Detects:
    - Full-page scans (with or without invisible OCR text layers)
    - 1-bit CCITTFaxDecode un-OCR'd scan columns (e.g. Piauí Diário Oficial)
    - Partial-coverage scans with tiny native text (e.g. header-only + scanned body)

    Returns:
        ``(is_scanned, confidence)`` where *confidence* is in ``[0, 1]``.
    """
    owns_doc = _doc is None
    if owns_doc:
        try:
            doc = _open_pdf(content)
        except ValueError:
            if _is_valid_zero_page_pdf(content):
                return (False, 0.0)
            raise
    else:
        doc = _doc
    assert doc is not None
    try:
        total_pages = len(doc)
        if total_pages == 0:
            return (False, 0.0)

        if total_pages <= max_pages_to_check:
            pages_to_check = list(range(total_pages))
        else:
            step = total_pages / max_pages_to_check
            pages_to_check = sorted({int(i * step) for i in range(max_pages_to_check)})

        ocr_meta_checked: bool | None = None

        def get_ocr_meta() -> bool:
            nonlocal ocr_meta_checked
            if ocr_meta_checked is None:
                ocr_meta_checked = _has_ocr_metadata_doc(doc)
            return ocr_meta_checked

        page_scores: list[float] = []
        for page_num in pages_to_check:
            if _analyses is not None and page_num in _analyses:
                pa = _analyses[page_num]
            else:
                page = doc[page_num]
                try:
                    pa = _analyze_page(page, page_num, collect_image_hashes=False)
                finally:
                    page.close()
                if _analyses is not None:
                    _analyses[page_num] = pa
            score, is_blank = _score_page_scan(pa, get_ocr_meta)
            if not is_blank:
                page_scores.append(score)

        if not page_scores:
            return (False, 1.0)
        avg_score = sum(page_scores) / len(page_scores)
        scanned = avg_score > 0.5
        confidence = avg_score if scanned else 1.0 - avg_score
        return (scanned, round(confidence, 4))
    finally:
        if owns_doc:
            doc.close()


# ---------------------------------------------------------------------------
# Visual line extraction (single-column safe + multi-column DOU aware)
# ---------------------------------------------------------------------------
@dataclass
class _LineBox:
    x0: float
    top: float
    x1: float
    bottom: float
    text: str


_VISUAL_LINE_RE = re.compile(r"[^\r\n]+")


def _make_user_to_visual(
    mb: tuple[float, float, float, float], rot: int
) -> Callable[[float, float], tuple[float, float]]:
    """Map unrotated PDF user-space ``(ux, uy)`` to top-down visual ``(vx, vy)``."""
    mx0, my0, mx1, my1 = mb
    mw = mx1 - mx0
    mh = my1 - my0
    r = rot % 360
    if r == 0:
        return lambda ux, uy: (ux - mx0, mh - (uy - my0))
    if r == 90:
        return lambda ux, uy: (uy - my0, ux - mx0)
    if r == 180:
        return lambda ux, uy: (mw - (ux - mx0), uy - my0)
    return lambda ux, uy: (mh - (uy - my0), mw - (ux - mx0))


def _extract_visual_lines(page: pdfium.PdfPage) -> list[_LineBox]:
    """Extract lines with top-down bounding boxes via PDFium ``FPDFText``."""
    pw, ph = page.get_size()
    tp = page.get_textpage()
    try:
        n_chars = tp.count_chars()
        if n_chars <= 0:
            return []
        full = tp.get_text_range(0, n_chars)
        if len(full) != n_chars:
            plain = _normalize_pdfium_soft_hyphens(
                tp.get_text_bounded().replace("\r\n", "\n").replace("\r", "\n")
            )
            lines = [ln.strip() for ln in plain.splitlines() if ln.strip()]
            step = ph / max(1, len(lines))
            return [
                _LineBox(0.0, i * step, pw, (i + 1) * step, ln)
                for i, ln in enumerate(lines)
            ]

        mb = page.get_mediabox()
        mx0, my0, _, my1 = mb
        mh = my1 - my0
        rot = page.get_rotation() % 360
        simple_transform = rot == 0 and mx0 == 0.0 and my0 == 0.0
        to_vis = None if simple_transform else _make_user_to_visual(mb, rot)

        raw_lines: list[_LineBox] = []
        c_left = ctypes.c_double()
        c_right = ctypes.c_double()
        c_bottom = ctypes.c_double()
        c_top = ctypes.c_double()
        get_box = pdfium_c.FPDFText_GetCharBox
        tp_raw = tp.raw

        for match in _VISUAL_LINE_RE.finditer(full):
            seg = match.group()
            txt = seg.strip()
            if not txt:
                continue
            s = match.start() + (len(seg) - len(seg.lstrip()))
            e = s + len(txt) - 1
            probes = (s, (s + e) // 2, e) if e - s >= 2 else (s, e)
            vx0 = pw
            vx1 = 0.0
            vtop = ph
            vbot = 0.0
            got_any = False
            for idx in probes:
                ch = full[idx]
                if ch.isspace() or ch == "\x02":
                    continue
                if get_box(tp_raw, idx, c_left, c_right, c_bottom, c_top):
                    lx, rx = c_left.value, c_right.value
                    by, ty = c_bottom.value, c_top.value
                    if rx >= lx and ty >= by:
                        if simple_transform:
                            px0, px1 = lx, rx
                            py0, py1 = mh - ty, mh - by
                        else:
                            assert to_vis is not None
                            px0, py0 = to_vis(lx, ty)
                            px1, py1 = to_vis(rx, by)
                            if px0 > px1:
                                px0, px1 = px1, px0
                            if py0 > py1:
                                py0, py1 = py1, py0
                        if px0 < vx0:
                            vx0 = px0
                        if px1 > vx1:
                            vx1 = px1
                        if py0 < vtop:
                            vtop = py0
                        if py1 > vbot:
                            vbot = py1
                        got_any = True
            if got_any:
                raw_lines.append(
                    _LineBox(
                        x0=vx0,
                        top=vtop,
                        x1=vx1,
                        bottom=vbot,
                        text=txt,
                    )
                )
        return raw_lines
    finally:
        tp.close()


def _detect_vertical_columns(
    lines: list[_LineBox], pw: float, ph: float
) -> list[float]:
    """Detect genuine multi-column gutters (e.g., 2–3 column Diário Oficial).

    Never triggers on single-column legislation with right-aligned ementas or
    indented paragraphs: requires >= 8 left lines and >= 8 right lines whose
    Y-spans overlap by >= 35% of page height with <= 2 crossing lines.
    """
    if len(lines) < 16 or pw <= 0 or ph <= 0:
        return []
    body = [
        ln for ln in lines if (ln.x1 - ln.x0) >= 25.0 and (ln.x1 - ln.x0) < pw * 0.60
    ]
    if len(body) < 16:
        return []

    step = pw / 40.0
    best_cuts: list[tuple[float, float]] = []
    for k in range(9, 32):
        x = k * step
        left = [ln for ln in body if ln.x1 <= x + 2.0]
        right = [ln for ln in body if ln.x0 >= x - 2.0]
        if len(left) < 8 or len(right) < 8:
            continue
        ly0, ly1 = min(ln.top for ln in left), max(ln.bottom for ln in left)
        ry0, ry1 = min(ln.top for ln in right), max(ln.bottom for ln in right)
        ov0, ov1 = max(ly0, ry0), min(ly1, ry1)
        overlap = (ov1 - ov0) / ph
        if overlap < 0.35:
            continue
        crossing = sum(
            1
            for ln in lines
            if ln.top >= ov0 - 4.0
            and ln.bottom <= ov1 + 4.0
            and ln.x0 < x - 4.0
            and ln.x1 > x + 4.0
        )
        if crossing <= 2:
            best_cuts.append((x, overlap))

    if not best_cuts:
        return []

    clusters: list[list[float]] = []
    for x, _ in best_cuts:
        if not clusters or x - clusters[-1][-1] > step * 1.5:
            clusters.append([x])
        else:
            clusters[-1].append(x)
    return [sum(cl) / len(cl) for cl in clusters][:3]


def _merge_same_baseline(lines: list[_LineBox]) -> list[_LineBox]:
    """Sort top-to-bottom and merge fragments sitting on the same visual baseline."""
    if not lines:
        return []
    ordered = sorted(lines, key=lambda ln: (ln.top, ln.x0))
    merged: list[_LineBox] = [ordered[0]]
    for ln in ordered[1:]:
        prev = merged[-1]
        prev_h = max(6.0, prev.bottom - prev.top)
        ln_h = max(6.0, ln.bottom - ln.top)
        tol = min(prev_h, ln_h) * 0.45
        prev_mid = (prev.top + prev.bottom) * 0.5
        ln_mid = (ln.top + ln.bottom) * 0.5
        if abs(ln_mid - prev_mid) <= tol:
            if ln.x0 < prev.x0:
                left, right = ln, prev
            else:
                left, right = prev, ln
            gap = right.x0 - left.x1
            sep = (
                ""
                if (gap <= 1.0 or right.text.startswith((",", ".", ";", ":", ")", "]")))
                else " "
            )
            merged[-1] = _LineBox(
                x0=min(prev.x0, ln.x0),
                top=min(prev.top, ln.top),
                x1=max(prev.x1, ln.x1),
                bottom=max(prev.bottom, ln.bottom),
                text=f"{left.text}{sep}{right.text}",
            )
        else:
            merged.append(ln)
    return merged


def _sort_and_merge_lines(
    lines: list[_LineBox], pw: float, ph: float
) -> list[_LineBox]:
    """Order lines in visual reading order (single-column top-down, or true multi-column)."""
    if not lines:
        return []

    cuts = _detect_vertical_columns(lines, pw, ph)
    if cuts:
        top_band = ph * 0.12
        bot_band = ph * 0.88
        headers: list[_LineBox] = []
        footers: list[_LineBox] = []
        cols: list[list[_LineBox]] = [[] for _ in range(len(cuts) + 1)]
        for ln in lines:
            crosses_any = any(ln.x0 < c - 4.0 and ln.x1 > c + 4.0 for c in cuts)
            if crosses_any and ln.top <= top_band:
                headers.append(ln)
            elif crosses_any and ln.bottom >= bot_band:
                footers.append(ln)
            else:
                cx = (ln.x0 + ln.x1) * 0.5
                col_idx = 0
                for c in cuts:
                    if cx >= c:
                        col_idx += 1
                cols[col_idx].append(ln)
        ordered: list[_LineBox] = []
        ordered.extend(_merge_same_baseline(headers))
        for col in cols:
            ordered.extend(_merge_same_baseline(col))
        ordered.extend(_merge_same_baseline(footers))
        return ordered

    return _merge_same_baseline(lines)


def _join_lines_to_paragraphs(lines: list[_LineBox]) -> str:
    """Rejoin soft-wrapped prose lines while preserving legal structure and tabular rows."""
    if not lines:
        return ""
    heights = sorted(max(6.0, ln.bottom - ln.top) for ln in lines)
    med_h = heights[len(heights) // 2]
    widths = sorted(ln.x1 - ln.x0 for ln in lines)
    p85_w = widths[int(len(widths) * 0.85)] if widths else 400.0

    paragraphs: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            joined = buf[0]
            for nxt in buf[1:]:
                if joined.endswith("\x02"):
                    joined = joined[:-1] + nxt
                else:
                    joined = f"{joined} {nxt}"
            joined = joined.replace("\x02", "-")
            joined = re.sub(r" {2,}", " ", joined).strip()
            if joined:
                paragraphs.append(joined)
            buf.clear()

    prev: _LineBox | None = None
    for ln in lines:
        txt = ln.text.strip()
        if not txt:
            continue
        if prev is None:
            buf.append(txt)
            prev = ln
            continue

        v_gap = ln.top - prev.bottom
        prev_w = prev.x1 - prev.x0
        prev_txt = prev.text.strip()
        starts_legal = bool(_LEGAL_LINE_START_RE.match(txt))
        prev_ends_sentence = prev_txt.endswith((".", ":", ";", "!", "?"))
        prev_short = prev_w < p85_w * 0.72
        big_gap = v_gap > med_h * 0.85
        prev_ends_money = bool(re.search(r"\d,\d{2}$", prev_txt))
        is_all_caps = txt.isupper() and len(txt) >= 4
        prev_all_caps = prev_txt.isupper() and len(prev_txt) >= 4
        indent_jump = (ln.x0 - prev.x0) > 15.0 and prev_ends_sentence

        if (
            big_gap
            or starts_legal
            or indent_jump
            or prev_ends_money
            or (prev_ends_sentence and prev_short)
            or (is_all_caps != prev_all_caps and (prev_short or len(txt) < 70))
        ):
            flush()
        buf.append(txt)
        prev = ln

    flush()
    return "\n\n".join(paragraphs)


# ---------------------------------------------------------------------------
# Ruled-table extraction (PDFium-native C++ path geometry + FPDFText em-boxes)
# ---------------------------------------------------------------------------
_SEG_LINETO = pdfium_c.FPDF_SEGMENT_LINETO
_SEG_BEZIERTO = pdfium_c.FPDF_SEGMENT_BEZIERTO
_SEG_MOVETO = pdfium_c.FPDF_SEGMENT_MOVETO
_OBJ_PATH = pdfium_c.FPDF_PAGEOBJ_PATH
_OBJ_FORM = pdfium_c.FPDF_PAGEOBJ_FORM
_ID_MATRIX: tuple[float, float, float, float, float, float] = (
    1.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
)


@dataclass
class _TableBlock:
    x0: float
    top: float
    x1: float
    bottom: float
    markdown: str


def _format_gfm_table(rows: list[list[str | None]]) -> str | None:
    """Format a validated ruled-table cell matrix into clean GFM Markdown."""
    if not rows or len(rows) < 2:
        return None
    ncols = max(len(r) for r in rows)
    if ncols < 2:
        return None

    clean_rows: list[list[str]] = []
    non_empty_cells = 0
    for r in rows:
        cells = []
        for c in r:
            s = " ".join((c or "").replace("\r", " ").replace("\n", " ").split())
            s = s.replace("|", "\\|")
            if s:
                non_empty_cells += 1
            cells.append(s)
        while len(cells) < ncols:
            cells.append("")
        if any(cells):
            clean_rows.append(cells)

    if len(clean_rows) < 2 or non_empty_cells < 3:
        return None

    keep_cols = [c for c in range(ncols) if any(r[c] for r in clean_rows)]
    if len(keep_cols) < 2:
        return None
    clean_rows = [[r[c] for c in keep_cols] for r in clean_rows]

    joined_all = " ".join(cell for r in clean_rows for cell in r).upper()
    if len(clean_rows) <= 2 and "FIM DO DOCUMENTO" in joined_all:
        return None

    header = clean_rows[0]
    sep = ["---"] * len(header)
    md_lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(sep) + " |",
    ]
    for r in clean_rows[1:]:
        md_lines.append("| " + " | ".join(r) + " |")
    return "\n".join(md_lines)


def _mul_matrix(
    m1: tuple[float, float, float, float, float, float],
    m2: tuple[float, float, float, float, float, float],
) -> tuple[float, float, float, float, float, float]:
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def _extract_raw_edges_pdfium(
    page: pdfium.PdfPage,
    *,
    min_len_prefilter: float = 1.0,
    axis_eps: float = 0.05,
    max_depth: int = 8,
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    """Extract horizontal & vertical vector edges in visual top-down coordinates via PDFium C++."""
    mb = page.get_mediabox()
    rot = page.get_rotation() % 360
    mx0, my0, mx1, my1 = mb
    mw = mx1 - mx0
    mh = my1 - my0
    to_vis = _make_user_to_visual(mb, rot)

    h_edges: list[tuple[float, float, float]] = []
    v_edges: list[tuple[float, float, float]] = []

    c_fill = ctypes.c_int()
    c_stroke = ctypes.c_int()
    c_mat = pdfium_c.FS_MATRIX()
    c_px = ctypes.c_float()
    c_py = ctypes.c_float()
    c_l = ctypes.c_float()
    c_b = ctypes.c_float()
    c_r = ctypes.c_float()
    c_t = ctypes.c_float()

    get_type = pdfium_c.FPDFPageObj_GetType
    get_bounds = pdfium_c.FPDFPageObj_GetBounds
    get_mat = pdfium_c.FPDFPageObj_GetMatrix
    get_draw_mode = pdfium_c.FPDFPath_GetDrawMode
    count_segs = pdfium_c.FPDFPath_CountSegments
    get_seg = pdfium_c.FPDFPath_GetPathSegment
    seg_type = pdfium_c.FPDFPathSegment_GetType
    seg_pt = pdfium_c.FPDFPathSegment_GetPoint
    seg_close = pdfium_c.FPDFPathSegment_GetClose
    form_count = pdfium_c.FPDFFormObj_CountObjects
    form_get = pdfium_c.FPDFFormObj_GetObject
    page_count = pdfium_c.FPDFPage_CountObjects
    page_get = pdfium_c.FPDFPage_GetObject

    def emit_seg(x0: float, y0: float, x1: float, y1: float) -> None:
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        if dy <= axis_eps and dx >= min_len_prefilter:
            y = (y0 + y1) * 0.5
            h_edges.append((y, x0, x1) if x0 <= x1 else (y, x1, x0))
        elif dx <= axis_eps and dy >= min_len_prefilter:
            x = (x0 + x1) * 0.5
            v_edges.append((x, y0, y1) if y0 <= y1 else (x, y1, y0))

    def emit_rect_user(lx: float, by: float, rx: float, ty: float) -> None:
        if rot == 0:
            vx0, vx1 = lx - mx0, rx - mx0
            vy0, vy1 = mh - (ty - my0), mh - (by - my0)
        elif rot == 90:
            vx0, vx1 = by - my0, ty - my0
            vy0, vy1 = lx - mx0, rx - mx0
        elif rot == 180:
            vx0, vx1 = mw - (rx - mx0), mw - (lx - mx0)
            vy0, vy1 = by - my0, ty - my0
        else:
            vx0, vx1 = mh - (ty - my0), mh - (by - my0)
            vy0, vy1 = mw - (rx - mx0), mw - (lx - mx0)
        if vx0 > vx1:
            vx0, vx1 = vx1, vx0
        if vy0 > vy1:
            vy0, vy1 = vy1, vy0
        if (vx1 - vx0) >= min_len_prefilter:
            h_edges.append((vy0, vx0, vx1))
            h_edges.append((vy1, vx0, vx1))
        if (vy1 - vy0) >= min_len_prefilter:
            v_edges.append((vx0, vy0, vy1))
            v_edges.append((vx1, vy0, vy1))

    def walk_container(
        container: Any,
        is_form: bool,
        parent_mat: tuple[float, float, float, float, float, float],
        depth: int,
    ) -> None:
        n_objs = form_count(container) if is_form else page_count(container)
        if n_objs <= 0:
            return
        for i in range(n_objs):
            raw_obj = form_get(container, i) if is_form else page_get(container, i)
            if not raw_obj:
                continue
            ot = get_type(raw_obj)
            if ot == _OBJ_FORM:
                if depth + 1 < max_depth:
                    if get_mat(raw_obj, c_mat):
                        fm = (c_mat.a, c_mat.b, c_mat.c, c_mat.d, c_mat.e, c_mat.f)
                        next_mat = _mul_matrix(parent_mat, fm)
                    else:
                        next_mat = parent_mat
                    walk_container(raw_obj, True, next_mat, depth + 1)
                continue
            if ot != _OBJ_PATH:
                continue

            # Fast bounds rejection + filled thin-bar fast path (Word/LibreOffice rules)
            if not is_form and get_bounds(raw_obj, c_l, c_b, c_r, c_t):
                bl, bb, br, bt = c_l.value, c_b.value, c_r.value, c_t.value
                bw = br - bl
                bh = bt - bb
                if bw < min_len_prefilter and bh < min_len_prefilter:
                    continue
                if not get_draw_mode(raw_obj, c_fill, c_stroke):
                    continue
                if c_fill.value == 0 and c_stroke.value == 0:
                    continue
                ns = count_segs(raw_obj)
                if ns < 2:
                    continue
                if (
                    c_stroke.value == 0
                    and ns == 5
                    and (bw < min_len_prefilter or bh < min_len_prefilter)
                ):
                    if rot == 0:
                        vx0, vx1 = bl - mx0, br - mx0
                        vy0, vy1 = mh - (bt - my0), mh - (bb - my0)
                        if bw >= min_len_prefilter:
                            h_edges.append((vy0, vx0, vx1))
                            h_edges.append((vy1, vx0, vx1))
                        if bh >= min_len_prefilter:
                            v_edges.append((vx0, vy0, vy1))
                            v_edges.append((vx1, vy0, vy1))
                    else:
                        emit_rect_user(bl, bb, br, bt)
                    continue
            else:
                if not get_draw_mode(raw_obj, c_fill, c_stroke):
                    continue
                if c_fill.value == 0 and c_stroke.value == 0:
                    continue
                ns = count_segs(raw_obj)
                if ns < 2:
                    continue

            if get_mat(raw_obj, c_mat):
                pm = (c_mat.a, c_mat.b, c_mat.c, c_mat.d, c_mat.e, c_mat.f)
                a, b, c, d, e, f = (
                    pm if parent_mat == _ID_MATRIX else _mul_matrix(parent_mat, pm)
                )
            else:
                a, b, c, d, e, f = parent_mat

            is_id = (
                a == 1.0
                and b == 0.0
                and c == 0.0
                and d == 1.0
                and e == 0.0
                and f == 0.0
            )

            start_x = start_y = cur_x = cur_y = 0.0
            has_cur = False
            bezier_rem = 0

            for si in range(ns):
                seg = get_seg(raw_obj, si)
                if not seg:
                    continue
                st = seg_type(seg)
                if not seg_pt(seg, c_px, c_py):
                    continue
                px, py = c_px.value, c_py.value
                if is_id:
                    vx, vy = to_vis(px, py)
                else:
                    vx, vy = to_vis(a * px + c * py + e, b * px + d * py + f)

                if st == _SEG_MOVETO:
                    start_x = cur_x = vx
                    start_y = cur_y = vy
                    has_cur = True
                    bezier_rem = 0
                elif st == _SEG_LINETO:
                    if has_cur:
                        emit_seg(cur_x, cur_y, vx, vy)
                    cur_x, cur_y = vx, vy
                    has_cur = True
                    bezier_rem = 0
                    if seg_close(seg):
                        if abs(cur_x - start_x) > 1e-4 or abs(cur_y - start_y) > 1e-4:
                            emit_seg(cur_x, cur_y, start_x, start_y)
                        cur_x, cur_y = start_x, start_y
                elif st == _SEG_BEZIERTO:
                    bezier_rem = (bezier_rem + 1) % 3
                    if bezier_rem == 0:
                        cur_x, cur_y = vx, vy
                        has_cur = True
                        if seg_close(seg):
                            if (
                                abs(cur_x - start_x) > 1e-4
                                or abs(cur_y - start_y) > 1e-4
                            ):
                                emit_seg(cur_x, cur_y, start_x, start_y)
                            cur_x, cur_y = start_x, start_y

    walk_container(page.raw, False, _ID_MATRIX, 0)
    return h_edges, v_edges


def _snap_and_merge_1d(
    edges: list[tuple[float, float, float]],
    snap_tol: float = 3.0,
    join_tol: float = 3.0,
    min_length: float = 3.0,
) -> list[tuple[float, float, float]]:
    """Snap collinear edges within *snap_tol*, merge gaps <= *join_tol*, and filter length."""
    if not edges:
        return []
    edges_sorted = sorted(edges, key=itemgetter(0))
    merged: list[tuple[float, float, float]] = []

    cluster: list[tuple[float, float, float]] = [edges_sorted[0]]
    last_pos = edges_sorted[0][0]

    def flush_cluster(cl: list[tuple[float, float, float]]) -> None:
        avg_pos = sum(e[0] for e in cl) / len(cl)
        cl_by_start = sorted(cl, key=itemgetter(1))
        s0, s1 = cl_by_start[0][1], cl_by_start[0][2]
        for _, lo, hi in cl_by_start[1:]:
            if lo <= s1 + join_tol:
                if hi > s1:
                    s1 = hi
            else:
                if s1 - s0 >= min_length:
                    merged.append((avg_pos, s0, s1))
                s0, s1 = lo, hi
        if s1 - s0 >= min_length:
            merged.append((avg_pos, s0, s1))

    for e in edges_sorted[1:]:
        if e[0] <= last_pos + snap_tol:
            cluster.append(e)
        else:
            flush_cluster(cluster)
            cluster = [e]
        last_pos = e[0]
    flush_cluster(cluster)
    return merged


def _find_table_cell_groups(
    h_edges: list[tuple[float, float, float]],
    v_edges: list[tuple[float, float, float]],
    *,
    snap_tol: float = 3.0,
    join_tol: float = 3.0,
    min_length: float = 3.0,
    inter_tol: float = 3.0,
) -> list[list[tuple[float, float, float, float]]]:
    """Find connected ruled-table cell groups ``[(x0, top, x1, bottom), ...]``."""
    hs = _snap_and_merge_1d(h_edges, snap_tol, join_tol, min_length)
    vs = _snap_and_merge_1d(v_edges, snap_tol, join_tol, min_length)
    if len(hs) < 2 or len(vs) < 2:
        return []

    hs.sort(key=itemgetter(0, 1))
    vs.sort(key=itemgetter(0, 1))
    h_ys = [e[0] for e in hs]

    inter_v: dict[tuple[float, float], set[int]] = {}
    inter_h: dict[tuple[float, float], set[int]] = {}
    pts_by_x: dict[float, list[float]] = defaultdict(list)
    pts_by_y: dict[float, list[float]] = defaultdict(list)

    for vi, (vx, vtop, vbot) in enumerate(vs):
        lo = bisect.bisect_left(h_ys, vtop - inter_tol)
        hi = bisect.bisect_right(h_ys, vbot + inter_tol)
        for hi_idx in range(lo, hi):
            hy, hx0, hx1 = hs[hi_idx]
            if (hx0 - inter_tol) <= vx <= (hx1 + inter_tol):
                pt = (vx, hy)
                vset = inter_v.get(pt)
                if vset is None:
                    inter_v[pt] = {vi}
                    inter_h[pt] = {hi_idx}
                    pts_by_x[vx].append(hy)
                    pts_by_y[hy].append(vx)
                else:
                    vset.add(vi)
                    inter_h[pt].add(hi_idx)

    if len(inter_v) < 4:
        return []

    for ys in pts_by_x.values():
        ys.sort()
    for xs in pts_by_y.values():
        xs.sort()

    cells: list[tuple[float, float, float, float]] = []
    for vx in sorted(pts_by_x):
        ys = pts_by_x[vx]
        ny = len(ys)
        for yi in range(ny - 1):
            hy = ys[yi]
            pt = (vx, hy)
            pt_vs = inter_v[pt]
            pt_hs = inter_h[pt]

            xs_row = pts_by_y[hy]
            xi_start = bisect.bisect_right(xs_row, vx)
            if xi_start >= len(xs_row):
                continue

            right_xs = [
                rx
                for rx in xs_row[xi_start:]
                if not pt_hs.isdisjoint(inter_h[(rx, hy)])
            ]
            if not right_xs:
                continue

            found = False
            for yj in range(yi + 1, ny):
                by = ys[yj]
                bpt = (vx, by)
                if pt_vs.isdisjoint(inter_v[bpt]):
                    continue
                bpt_hs = inter_h[bpt]
                for rx in right_xs:
                    br = (rx, by)
                    br_vs = inter_v.get(br)
                    if br_vs is None:
                        continue
                    if not br_vs.isdisjoint(inter_v[(rx, hy)]) and not inter_h[
                        br
                    ].isdisjoint(bpt_hs):
                        cells.append((vx, hy, rx, by))
                        found = True
                        break
                if found:
                    break

    if len(cells) < 2:
        return []

    # Group contiguous cells sharing corners via union-find.
    parent = list(range(len(cells)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    corner_owner: dict[tuple[float, float], int] = {}
    for ci, (x0, top, x1, bot) in enumerate(cells):
        for corner in ((x0, top), (x0, bot), (x1, top), (x1, bot)):
            prev = corner_owner.get(corner)
            if prev is None:
                corner_owner[corner] = ci
            else:
                ra, rb = find(ci), find(prev)
                if ra != rb:
                    parent[ra] = rb

    groups: dict[int, list[tuple[float, float, float, float]]] = defaultdict(list)
    for ci, cell in enumerate(cells):
        groups[find(ci)].append(cell)

    tables = [g for g in groups.values() if len(g) > 1]
    tables.sort(key=lambda g: min((c[1], c[0]) for c in g))
    return tables


def _extract_page_chars_visual(
    page: pdfium.PdfPage,
    tables_bboxes: list[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float, float, float, str]]:
    """Extract PDFium characters inside table bboxes as ``(cx, cy, x0, top, x1, bottom, ch)``."""
    tp = page.get_textpage()
    try:
        n_chars = tp.count_chars()
        if n_chars <= 0:
            return []
        mb = page.get_mediabox()
        mx0, my0, _, my1 = mb
        mh = my1 - my0
        rot = page.get_rotation() % 360
        to_vis = _make_user_to_visual(mb, rot)

        ux0 = min(b[0] for b in tables_bboxes) - 2.0
        utop = min(b[1] for b in tables_bboxes) - 2.0
        ux1 = max(b[2] for b in tables_bboxes) + 2.0
        ubot = max(b[3] for b in tables_bboxes) + 2.0
        ux0_m, utop_m, ux1_m, ubot_m = ux0 - 20.0, utop - 20.0, ux1 + 20.0, ubot + 20.0

        rect = pdfium_c.FS_RECTF()
        c_mat = pdfium_c.FS_MATRIX()
        get_loose = pdfium_c.FPDFText_GetLooseCharBox
        get_uni = pdfium_c.FPDFText_GetUnicode
        is_gen = pdfium_c.FPDFText_IsGenerated
        get_fsize = pdfium_c.FPDFText_GetFontSize
        get_tmat = pdfium_c.FPDFText_GetMatrix
        tp_raw = tp.raw

        chars: list[tuple[float, float, float, float, float, float, str]] = []
        for idx in range(n_chars):
            cp = get_uni(tp_raw, idx)
            if cp == 2:
                # PDFium soft-hyphen marker at line end -> explicit hyphen
                ch = "-"
            elif cp == 32:
                # Keep explicit PDF space glyphs; skip synthetic spaces whose bboxes can drift.
                if is_gen(tp_raw, idx):
                    continue
                ch = " "
            elif cp < 32:
                continue
            else:
                ch = chr(cp)
            if not get_loose(tp_raw, idx, rect):
                continue
            lx, rx = rect.left, rect.right
            by, ty = rect.bottom, rect.top
            if rx < lx or ty < by:
                continue
            if rot == 0:
                cx_approx = (lx + rx) * 0.5 - mx0
                cy_approx = mh - ((by + ty) * 0.5 - my0)
                if not (ux0_m <= cx_approx <= ux1_m and utop_m <= cy_approx <= ubot_m):
                    continue
            # Normalize em-box height to exact scaled font size along the glyph ascent
            # axis (c, d) from FPDFText_GetMatrix so all glyphs on a baseline share cy.
            fs = get_fsize(tp_raw, idx)
            if fs > 0.0 and get_tmat(tp_raw, idx, c_mat):
                mc, md = c_mat.c, c_mat.d
                sfs = fs * math.hypot(mc, md)
                if sfs > 0.0:
                    if md > abs(mc):
                        ty = by + sfs
                    elif md < -abs(mc):
                        by = ty - sfs
                    elif mc < -abs(md):
                        lx = rx - sfs
                    elif mc > abs(md):
                        rx = lx + sfs
            if rot == 0:
                vx0, vx1 = lx - mx0, rx - mx0
                vy0, vy1 = mh - (ty - my0), mh - (by - my0)
            else:
                vx0, vy0 = to_vis(lx, ty)
                vx1, vy1 = to_vis(rx, by)
                if vx0 > vx1:
                    vx0, vx1 = vx1, vx0
                if vy0 > vy1:
                    vy0, vy1 = vy1, vy0
            cx = (vx0 + vx1) * 0.5
            cy = (vy0 + vy1) * 0.5
            if ux0 <= cx <= ux1 and utop <= cy <= ubot:
                chars.append((cx, cy, vx0, vy0, vx1, vy1, ch))
        return chars
    finally:
        tp.close()


def _chars_to_cell_text(
    cell_chars: list[tuple[float, float, float, float, float, float, str]],
    *,
    x_tol: float = 3.0,
    y_tol: float = 3.0,
) -> str:
    """Reconstruct visual reading-order text for one table cell from em-box chars."""
    if not cell_chars:
        return ""
    by_top = sorted(cell_chars, key=itemgetter(3))
    lines: list[list[tuple[float, float, float, float, float, float, str]]] = [
        [by_top[0]]
    ]
    last_top = by_top[0][3]
    for item in by_top[1:]:
        if item[3] <= last_top + y_tol:
            lines[-1].append(item)
        else:
            lines.append([item])
        last_top = item[3]

    out_lines: list[str] = []
    for ln in lines:
        ln.sort(key=itemgetter(2))
        line_h = max((c[5] - c[3]) for c in ln)
        gap_thresh = min(x_tol, max(1.2, line_h * 0.20))
        parts: list[str] = []
        prev_x1: float | None = None
        for _, _, x0, _, x1, _, ch in ln:
            if prev_x1 is not None and (x0 - prev_x1) > gap_thresh:
                if parts and parts[-1] != " ":
                    parts.append(" ")
            parts.append(ch)
            if prev_x1 is None or x1 > prev_x1:
                prev_x1 = x1
        txt = "".join(parts).strip()
        if txt:
            out_lines.append(txt)
    return " ".join(out_lines)


def _extract_tables_pdfium_page(page: pdfium.PdfPage) -> list[_TableBlock]:
    """Extract ruled GFM tables from one PDFium page using native C++ path & text APIs."""
    h_edges, v_edges = _extract_raw_edges_pdfium(page)
    if len(h_edges) < 2 or len(v_edges) < 2:
        return []
    table_groups = _find_table_cell_groups(h_edges, v_edges)
    if not table_groups:
        return []

    table_bboxes = [
        (
            min(c[0] for c in g),
            min(c[1] for c in g),
            max(c[2] for c in g),
            max(c[3] for c in g),
        )
        for g in table_groups
    ]
    page_chars = _extract_page_chars_visual(page, table_bboxes)

    blocks: list[_TableBlock] = []
    for g, tbbox in zip(table_groups, table_bboxes):
        tx0, ttop, tx1, tbot = tbbox
        t_chars = sorted(
            (ch for ch in page_chars if tx0 <= ch[0] < tx1 and ttop <= ch[1] < tbot),
            key=itemgetter(1),
        )
        t_cys = [ch[1] for ch in t_chars]
        xs = sorted({c[0] for c in g})
        sorted_cells = sorted(g, key=itemgetter(1, 0))
        rows_out: list[list[str | None]] = []
        for _, row_iter in itertools.groupby(sorted_cells, key=itemgetter(1)):
            row_cells = list(row_iter)
            r_top = min(c[1] for c in row_cells)
            r_bot = max(c[3] for c in row_cells)
            r_x0 = min(c[0] for c in row_cells)
            r_x1 = max(c[2] for c in row_cells)
            lo_y = bisect.bisect_left(t_cys, r_top)
            hi_y = bisect.bisect_left(t_cys, r_bot)
            row_chars = [ch for ch in t_chars[lo_y:hi_y] if r_x0 <= ch[0] < r_x1]
            # Deduplicate characters if nested inset boxes overlap horizontally on the same row.
            has_row_overlap = any(
                row_cells[k][0] < row_cells[k - 1][2] - 1e-6
                for k in range(1, len(row_cells))
            )
            if has_row_overlap and row_chars:
                ordered_idx = sorted(
                    range(len(row_cells)),
                    key=lambda k: (
                        (row_cells[k][2] - row_cells[k][0])
                        * (row_cells[k][3] - row_cells[k][1])
                    ),
                )
                buckets: list[
                    list[tuple[float, float, float, float, float, float, str]]
                ] = [[] for _ in row_cells]
                for ch in row_chars:
                    cx, cy = ch[0], ch[1]
                    for k in ordered_idx:
                        cx0, ctop, cx1, cbot = row_cells[k]
                        if cx0 <= cx < cx1 and ctop <= cy < cbot:
                            buckets[k].append(ch)
                            break
                xdict_text = {
                    row_cells[k][0]: _chars_to_cell_text(buckets[k])
                    for k in range(len(row_cells))
                }
                row_vals: list[str | None] = [xdict_text.get(x) for x in xs]
            else:
                row_chars_x = sorted(row_chars, key=itemgetter(0))
                r_cxs = [ch[0] for ch in row_chars_x]
                xdict = {c[0]: c for c in row_cells}
                row_vals = []
                for x in xs:
                    cell = xdict.get(x)
                    if cell is None:
                        row_vals.append(None)
                    else:
                        cx0, ctop, cx1, cbot = cell
                        lo_x = bisect.bisect_left(r_cxs, cx0)
                        hi_x = bisect.bisect_left(r_cxs, cx1)
                        c_chars = [
                            ch for ch in row_chars_x[lo_x:hi_x] if ctop <= ch[1] < cbot
                        ]
                        row_vals.append(_chars_to_cell_text(c_chars))
            rows_out.append(row_vals)
        md = _format_gfm_table(rows_out)
        if md:
            blocks.append(_TableBlock(tx0, ttop, tx1, tbot, md))
    return blocks


def _extract_tables_for_pages(
    data: bytes,
    page_indices_0based: list[int],
    *,
    _doc: pdfium.PdfDocument | None = None,
) -> dict[int, list[_TableBlock]]:
    """Extract ruled GFM tables natively via PDFium on candidate pages."""
    if not page_indices_0based:
        return {}
    owns_doc = _doc is None
    doc = _open_pdf(data) if owns_doc else _doc
    assert doc is not None
    result: dict[int, list[_TableBlock]] = {}
    try:
        n_pages = len(doc)
        for idx0 in page_indices_0based:
            if not (0 <= idx0 < n_pages):
                continue
            page = doc[idx0]
            try:
                blocks = _extract_tables_pdfium_page(page)
                if blocks:
                    result[idx0] = blocks
            finally:
                page.close()
    except Exception:
        logger.debug(
            "PDFium table extraction failed; falling back to text only",
            exc_info=True,
        )
    finally:
        if owns_doc:
            doc.close()
    return result


# ---------------------------------------------------------------------------
# Smart embedded image filtering & in-place base64 encoding
# ---------------------------------------------------------------------------
def _select_substantive_images(
    analyses: list[_PageAnalysis],
    image_size_limit: float,
) -> dict[int, set[int]]:
    """Return ``{page_index: {obj_index, ...}}`` for non-watermark substantive images."""
    n_pages = len(analyses)
    sha_pages: dict[bytes, set[int]] = {}
    bbox_pages: dict[tuple[float, float, float, float], set[int]] = {}

    for pa in analyses:
        for im in pa.images:
            if im.sha256:
                sha_pages.setdefault(im.sha256, set()).add(pa.page_index)
            bbox_pages.setdefault(im.nbbox, set()).add(pa.page_index)

    keep: dict[int, set[int]] = {}
    for pa in analyses:
        for im in pa.images:
            if not (image_size_limit <= im.ratio < 0.80):
                continue
            if im.raw_len and im.raw_len < 100:
                continue
            if n_pages >= 2:
                if im.sha256 and len(sha_pages.get(im.sha256, ())) >= 2:
                    continue
                nbbox_freq = len(bbox_pages.get(im.nbbox, ()))
                if nbbox_freq >= 2 and (
                    im.vis_text_overlaps >= 5 or nbbox_freq >= max(3, n_pages // 2)
                ):
                    continue
            if n_pages == 1 and im.total_vis_text > 0:
                cx = (im.nbbox[0] + im.nbbox[2]) * 0.5
                cy = (im.nbbox[1] + im.nbbox[3]) * 0.5
                if (
                    0.15 <= cx <= 0.85
                    and 0.15 <= cy <= 0.85
                    and im.vis_text_overlaps >= 15
                    and (im.vis_text_overlaps / im.total_vis_text) >= 0.15
                ):
                    continue
            keep.setdefault(pa.page_index, set()).add(im.obj_index)
    return keep


def _encode_page_images(
    page: pdfium.PdfPage,
    pa: _PageAnalysis,
    selected_obj_indices: set[int],
) -> list[tuple[float, str]]:
    """Extract and base64-encode selected substantive images with their top Y-coord."""
    if not selected_obj_indices:
        return []
    info_by_idx = {im.obj_index: im for im in pa.images}
    encoded: list[tuple[float, str]] = []
    cur_idx = 0
    for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=4):
        idx = cur_idx
        cur_idx += 1
        if idx not in selected_obj_indices:
            continue
        im = info_by_idx.get(idx)
        if im is None:
            continue
        try:
            if im.is_jpeg:
                raw_bytes = bytes(obj.get_data(decode_simple=False))
                if len(raw_bytes) < 100:
                    continue
                b64 = base64.b64encode(raw_bytes).decode("ascii")
                encoded.append((im.top, f"![image](data:image/jpeg;base64,{b64})"))
            else:
                bmp = obj.get_bitmap()
                pil_img = None
                try:
                    pil_img = bmp.to_pil()
                    buf = io.BytesIO()
                    pil_img.save(buf, format="PNG", optimize=True)
                    png_bytes = buf.getvalue()
                finally:
                    if pil_img is not None:
                        pil_img.close()
                    bmp.warn_on_close = False
                    bmp.close()
                if len(png_bytes) < 100:
                    continue
                b64 = base64.b64encode(png_bytes).decode("ascii")
                encoded.append((im.top, f"![image](data:image/png;base64,{b64})"))
        except Exception:
            logger.debug(
                "Skipping non-decodable PDF image on page %d",
                pa.page_index,
                exc_info=True,
            )
    return encoded


# ---------------------------------------------------------------------------
# Page assembly (Y-ordered lines + in-place tables + in-place images)
# ---------------------------------------------------------------------------
def _assemble_page_markdown(
    page: pdfium.PdfPage,
    pa: _PageAnalysis,
    tables: list[_TableBlock],
    images_md: list[tuple[float, str]],
) -> str:
    """Assemble clean reading-order Markdown for one page with in-place tables and images."""
    raw_lines = _extract_visual_lines(page)
    lines = _sort_and_merge_lines(raw_lines, pa.width, pa.height)

    if tables:
        filtered: list[_LineBox] = []
        for ln in lines:
            cx = (ln.x0 + ln.x1) * 0.5
            cy = (ln.top + ln.bottom) * 0.5
            in_table = any(
                (t.x0 - 2.0) <= cx <= (t.x1 + 2.0)
                and (t.top - 2.0) <= cy <= (t.bottom + 2.0)
                for t in tables
            )
            if not in_table:
                filtered.append(ln)
        lines = filtered

    if not tables and not images_md:
        return _join_lines_to_paragraphs(lines)

    anchors: list[tuple[float, str]] = []
    for t in tables:
        anchors.append((t.top, t.markdown))
    for y_top, img_md in images_md:
        anchors.append((y_top, img_md))
    anchors.sort(key=lambda item: item[0])

    chunks: list[str] = []
    line_idx = 0
    n_lines = len(lines)
    for anchor_y, block_md in anchors:
        seg: list[_LineBox] = []
        while line_idx < n_lines and lines[line_idx].top < anchor_y:
            seg.append(lines[line_idx])
            line_idx += 1
        if seg:
            txt = _join_lines_to_paragraphs(seg)
            if txt:
                chunks.append(txt)
        chunks.append(block_md)
    if line_idx < n_lines:
        txt = _join_lines_to_paragraphs(lines[line_idx:])
        if txt:
            chunks.append(txt)
    return "\n\n".join(chunks)


def _digital_pdf(
    doc: pdfium.PdfDocument,
    analyses: list[_PageAnalysis],
    tables_by_page: dict[int, list[_TableBlock]],
    selected_imgs: dict[int, set[int]],
    bad_set: set[int],
    *,
    yield_between_pages: bool = False,
) -> list[str]:
    """Convert non-bad PDF pages to reading-order Markdown with in-place tables and images."""
    page_mds: list[str] = []
    for pa in analyses:
        if pa.page_index in bad_set:
            page_mds.append("")
            continue
        page = doc[pa.page_index]
        try:
            imgs_md = (
                _encode_page_images(page, pa, selected_imgs[pa.page_index])
                if pa.page_index in selected_imgs
                else []
            )
            pg_md = _assemble_page_markdown(
                page,
                pa,
                tables_by_page.get(pa.page_index, []),
                imgs_md,
            )
        finally:
            page.close()
        page_mds.append(_restore_legal_line_breaks(pg_md))
        if yield_between_pages:
            time.sleep(0)
    return page_mds


# ---------------------------------------------------------------------------
# Main extraction pipeline (_extract_pdf_core & selective per-page OCR support)
# ---------------------------------------------------------------------------
@dataclass
class _ExtractionResult:
    """Internal result distinguishing full text, full-doc OCR, or partial per-page OCR."""

    markdown: str | None
    page_markdowns: list[str] | None = None
    ocr_pages_0based: list[int] | None = None


def _extract_pdf_core(
    data: bytes,
    image_size_limit: float = 0.1,
    check_readable: bool = True,
    embed_images: bool = True,
    *,
    yield_between_pages: bool = False,
) -> _ExtractionResult:
    """Single-open PDFium extraction (text, native ruled tables, images, selective OCR).

    Two-stage design for maximum speed and flat RSS:
      1. Fast text pass on all pages + object inspection ONLY on <=10 sampled
         scan-detection pages and low-text (<50 char) pages.
      2. For scanned PDFs with readable embedded text (e.g. 250p ABBYY), skips
         expensive object/image/table walking completely (~10-14x faster).
      3. For digital PDFs, walks remaining pages for ruled-table gating and
         substantive image hashing (only for ``image_size_limit <= ratio < 0.80``).
      4. Supports selective per-page OCR when a multi-page PDF has localized
         missing-ToUnicode or image-only pages (e.g. RN id=56 pp.20-31, RR id=1/19).
    """
    try:
        doc = _open_pdf(data)
    except ValueError:
        if _is_valid_zero_page_pdf(data):
            return _ExtractionResult(markdown=None)
        raise
    try:
        page_count = len(doc)
        if page_count == 0:
            return _ExtractionResult(markdown=None)

        if page_count <= 10:
            sample_set = set(range(page_count))
        else:
            step = page_count / 10.0
            sample_set = {int(i * step) for i in range(10)}

        # Stage 1: fast text extraction on ALL pages; object walk ONLY on sample_set or low-text pages.
        analyses: list[_PageAnalysis] = []
        inspected_objects: set[int] = set()
        for i in range(page_count):
            page = doc[i]
            try:
                do_obj = i in sample_set
                pa = _analyze_page(
                    page,
                    i,
                    inspect_objects=do_obj,
                    collect_image_hashes=embed_images and do_obj,
                    image_size_limit=image_size_limit,
                )
                if do_obj:
                    inspected_objects.add(i)
                elif pa.text_len < 50:
                    _populate_page_objects(
                        page,
                        pa,
                        collect_image_hashes=embed_images,
                        image_size_limit=image_size_limit,
                    )
                    inspected_objects.add(i)
                analyses.append(pa)
            finally:
                page.close()
            if yield_between_pages:
                time.sleep(0)

        analyses_map = {pa.page_index: pa for pa in analyses}
        scanned, _confidence = is_pdf_scanned(data, _analyses=analyses_map, _doc=doc)

        plain_full = "\n\n".join(pa.text_raw for pa in analyses)
        minimum = max(50, page_count * 100)

        def long_enough(text: str) -> bool:
            check = _WATERMARK_CHECK_RE.sub(
                "", _MD_BASE64_IMAGE_RE.sub("", text)
            ).strip()
            return _valid_markdown(check, min_length=minimum)[0]

        # Early exit for unreadable or full-scan PDFs BEFORE digital table/image work.
        if check_readable:
            doc_readable = readable_text(plain_full, strict_ocr_layer=scanned)
            if not doc_readable:
                return _ExtractionResult(markdown=None)

        # Stage 2:
        # - For digital PDFs, inspect remaining pages for ruled lines & substantive image hashes.
        # - For scanned PDFs with check_readable=True, also inspect non-sampled pages
        #   (without hashing images) when any sampled page has un-OCR'd 1-bit coverage or
        #   modest text (< 400 chars), or the page itself has < 400 chars, so >10p scanned
        #   PDFs do not skip non-sampled un-OCR'd 1-bit CCITT pages.
        inspect_all_scanned = (
            scanned
            and check_readable
            and any(
                pa.bpp1_unocrd_coverage > 0.0 or pa.text_len < 400
                for pa in analyses
                if pa.page_index in sample_set
            )
        )
        if not scanned or (scanned and check_readable):
            hash_images = embed_images and not scanned
            for pa in analyses:
                if pa.page_index in inspected_objects:
                    continue
                if scanned and not inspect_all_scanned and pa.text_len >= 400:
                    continue
                page = doc[pa.page_index]
                try:
                    _populate_page_objects(
                        page,
                        pa,
                        collect_image_hashes=hash_images,
                        image_size_limit=image_size_limit,
                    )
                finally:
                    page.close()
                inspected_objects.add(pa.page_index)
                if yield_between_pages:
                    time.sleep(0)

        # Per-page coverage & CMap-gibberish audit across ALL pages.
        bad_pages: list[int] = []
        for pa in analyses:
            if pa.bpp1_unocrd_coverage >= 0.25:
                bad_pages.append(pa.page_index)
            elif _page_has_cmap_gibberish(pa.text_raw):
                bad_pages.append(pa.page_index)
            elif pa.text_len < 50 and pa.img_coverage >= 0.80:
                # Ignore graphic front/back covers on large (>= 10p) digital PDFs (e.g. TO Const).
                is_book_cover = (
                    not scanned
                    and page_count >= 10
                    and pa.page_index in (0, page_count - 1)
                )
                if not is_book_cover:
                    bad_pages.append(pa.page_index)
            elif page_count == 1 and pa.img_coverage >= 0.20 and pa.text_len < 180:
                # Single-page header-only + image-body norm (e.g. DF id=2395).
                bad_pages.append(pa.page_index)

        if check_readable:
            # If ALL pages are bad (e.g. pure scan or PI 20 CCITT docs), send full PDF to OCR.
            if bad_pages and len(bad_pages) == page_count:
                return _ExtractionResult(markdown=None)
            # If whole doc is scanned with too little text and no readable partial pages, full OCR.
            if scanned and not bad_pages and not long_enough(plain_full):
                return _ExtractionResult(markdown=None)

        bad_set = set(bad_pages) if check_readable else set()
        table_pages = (
            [
                pa.page_index
                for pa in analyses
                if pa.has_ruled_table_candidate and pa.page_index not in bad_set
            ]
            if not scanned
            else []
        )
        tables_by_page = (
            _extract_tables_for_pages(data, table_pages, _doc=doc)
            if table_pages
            else {}
        )

        selected_imgs = (
            _select_substantive_images(analyses, image_size_limit)
            if (embed_images and not scanned)
            else {}
        )

        try:
            raw_page_mds = _digital_pdf(
                doc,
                analyses,
                tables_by_page,
                selected_imgs,
                bad_set,
                yield_between_pages=yield_between_pages,
            )
            page_mds = (
                [raw_page_mds] if isinstance(raw_page_mds, str) else list(raw_page_mds)
            )
        except Exception:
            page_mds = []

        # Localized bad pages (e.g. RN id=56 pp.20-31, RR id=1/19 pp.2-3): OCR ONLY those pages!
        if check_readable and bad_pages:
            # Ensure we have a slot for every page before returning partial OCR instructions.
            if len(page_mds) == page_count:
                return _ExtractionResult(
                    markdown=None,
                    page_markdowns=page_mds,
                    ocr_pages_0based=bad_pages,
                )
            return _ExtractionResult(markdown=None)

        combined = _clean_markdown("\n\n".join(p for p in page_mds if p))
        if long_enough(combined):
            return _ExtractionResult(markdown=combined)

        # Fallback: cleaned plain text with legal line breaks restored.
        fallback = _clean_markdown(_restore_legal_line_breaks(plain_full))
        return _ExtractionResult(markdown=fallback if long_enough(fallback) else None)
    finally:
        doc.close()


def slice_pdf_pages(pdf_bytes: bytes, pages: list[int]) -> bytes:
    """Extract 1-based *pages* from *pdf_bytes* using ``pypdf``.

    Preserves ``df.py`` semantics:
    - 1-based page numbers, preserving requested order (including duplicates).
    - Out-of-range page numbers are silently ignored.
    - Returns original *pdf_bytes* unchanged when *pages* is empty or no page survives.
    """
    if not pages:
        return pdf_bytes
    import pypdf

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
    try:
        doc = _open_pdf(data)
    except ValueError:
        if _is_valid_zero_page_pdf(data):
            return []
        raise
    try:
        total = len(doc)
        lo = max(0, start)
        hi = min(end, total)
        scale = dpi / 72.0
        pngs: list[bytes] = []
        for i in range(lo, hi):
            page = doc[i]
            try:
                bitmap = page.render(scale=scale)
                pil_image = None
                try:
                    pil_image = bitmap.to_pil()
                    buf = io.BytesIO()
                    pil_image.save(buf, format="PNG", compress_level=1)
                    pngs.append(buf.getvalue())
                finally:
                    if pil_image is not None:
                        pil_image.close()
                    bitmap.warn_on_close = False
                    bitmap.close()
            finally:
                page.close()
        return pngs
    finally:
        doc.close()


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
    """Bounded async executor for CPU/GIL-bound PDF extraction work.

    - ``"auto"`` (default): uses a bounded ``spawn`` ``ProcessPoolExecutor``
      so PDF extraction never stalls the event loop (<10 ms max tick gap),
      transparently falling back to ``"thread"`` mode if pool creation fails
      with ``PermissionError`` or ``OSError`` (e.g. ``/dev/shm``-less AWS Lambda
      or restricted containers).
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
        self._pool_started = False
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
            self._pool_started = False
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
                result = await loop.run_in_executor(
                    pool, _apply_kwargs, function, args, kwargs
                )
            else:
                result = await loop.run_in_executor(pool, function, *args)
            self._pool_started = True
            return result
        except (PermissionError, OSError) as exc:
            if self.executor_mode != "auto" or self._pool_started:
                raise
            if self._pool is pool:
                self._pool = None
                self._pool_started = False
            pool.shutdown(wait=False, cancel_futures=True)
            self._process_unavailable = True
            logger.warning(
                "PDF ProcessPoolExecutor unavailable (%s); falling back to thread mode",
                exc,
            )
            async with self._get_thread_sem():
                return await asyncio.to_thread(function, *args, **kwargs)
        except BrokenProcessPool:
            if self._pool is pool:
                self._pool = None
                self._pool_started = False
                pool.shutdown(wait=False, cancel_futures=True)
            raise

    def shutdown(self, *, wait: bool = False) -> None:
        pool = self._pool
        self._pool = None
        self._pool_started = False
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
    """Convert PDF *data* to cleaned Markdown, falling back to *ocr* when scanned/unreadable.

    For partially-OCR'd or partially broken-CMap PDFs (e.g. ``RN id=56`` pp. 20–31,
    ``RR id=1``/``19`` pp. 2–3), slices and OCRs *only* the affected pages and splices
    them back into their exact document order.
    """
    runner = _get_runner(executor, max_workers)
    yield_pages = executor == "thread" or (
        executor == "auto"
        and (runner._process_unavailable or runner._effective_workers() <= 0)
    )
    res = await run_pdf_task(
        _extract_pdf_core,
        data,
        image_size_limit,
        ocr is not None,
        embed_images,
        executor=executor,
        max_workers=max_workers,
        yield_between_pages=yield_pages,
    )
    if res.markdown is not None:
        return res.markdown
    if ocr is None:
        raise ValueError("PDF needs OCR but no OCR model is configured")

    # Selective per-page OCR for localized unreadable / image-only pages.
    if res.page_markdowns is not None and res.ocr_pages_0based:
        pages_md = list(res.page_markdowns)
        runs: list[list[int]] = []
        for idx in sorted(res.ocr_pages_0based):
            if not runs or idx != runs[-1][-1] + 1:
                runs.append([idx])
            else:
                runs[-1].append(idx)
        for run in runs:
            sliced = await run_pdf_task(
                slice_pdf_pages,
                data,
                [p + 1 for p in run],
                executor=executor,
                max_workers=max_workers,
            )
            ocr_md = _clean_markdown(await ocr.pdf(sliced))
            pages_md[run[0]] = ocr_md
            for extra_p in run[1:]:
                pages_md[extra_p] = ""
        return _clean_markdown("\n\n".join(p for p in pages_md if p))

    return _clean_markdown(await ocr.pdf(data))
