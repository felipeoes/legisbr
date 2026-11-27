"""Content detection, cleaning, and conversion with explicit dependencies."""

from __future__ import annotations

import asyncio
import io
import re
import string
import zipfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from html_to_markdown import ConversionOptions, PreprocessingOptions
from html_to_markdown import convert as html_to_md

from legisbr.core.http import HttpClient, Response
from legisbr.core.images import ImageCache, _detect_image_mime, inline_images_in_html
from legisbr.core.pdf import PdfExecutor, pdf_to_markdown

if TYPE_CHECKING:
    from legisbr.core.ocr import OCR

__all__ = [
    "bytes_to_markdown",
    "calc_pages",
    "clean_markdown",
    "clean_md_tag",
    "clean_norm_soup",
    "detect_extension",
    "html_to_markdown",
    "infer_type_by_prefix",
    "infer_type_from_title",
    "is_pdf",
    "normalize_type",
    "pdf_to_markdown",
    "remove_summary_element",
    "remove_summary_from_markdown",
    "sniff_extension",
    "strip_html_chrome",
    "valid_markdown",
    "wrap_html",
]


def clean_md_tag(md_content: str) -> str:
    if md_content.startswith("```markdown"):
        md_content = md_content.removeprefix("```markdown").strip()
    elif md_content.startswith("```md"):
        md_content = md_content.removeprefix("```md").strip()
    if md_content.endswith("```"):
        md_content = md_content.removesuffix("```").strip()
    return md_content


_SERVER_ERROR_PATTERNS: list[str] = [
    "the requested url was not found on this server",
    "was not found on this server",
    "file or directory not found",
    "failed to open stream",
    "http request failed",
    "service unavailable",
    "doesn't work properly without javascript enabled",
    "object reference not set to an instance of an object",
]
_DISCLAIMER_RE = re.compile(
    "(Est[ea]\\s+(texto|conte[uú]do)|Ess[ea]\\s+texto)\\s+n[aã]o\\s+substitui",
    re.IGNORECASE,
)
_CLEAN_NORM_EMPTY_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "div", "span", "b"]


def is_pdf(body: bytes, content_type: str = "") -> bool:
    """Check if content is a PDF based on content type or magic bytes."""
    return "pdf" in content_type.lower() or body[:4] == b"%PDF"


def _is_image_bytes(raw: bytes) -> bool:
    """Return True if *raw* starts with a strict raster image magic sequence."""
    mime = _detect_image_mime(raw)
    return mime is not None and mime != "image/svg+xml"


def detect_extension(content_type: str, filename: str | None = None) -> str:
    """Determine file extension from content type or filename."""
    if filename:
        ext = Path(filename).suffix
        # Store rejects odd suffixes (".pdf?x=1", "."), so only trust plain ones.
        if re.fullmatch("\\.[A-Za-z0-9]{1,8}", ext):
            return ext.lower()
    ct = (content_type or "").lower()
    if "pdf" in ct:
        return ".pdf"
    if "html" in ct:
        return ".html"
    if "xml" in ct:
        return ".xml"
    if "msword" in ct or "officedocument.wordprocessing" in ct:
        return ".docx"
    if "rtf" in ct:
        return ".rtf"
    if "plain" in ct:
        return ".txt"
    return ".bin"


def sniff_extension(content: bytes) -> str | None:
    """Extension from the bytes themselves; servers often mislabel files."""
    if content.startswith(b"%PDF"):
        return ".pdf"
    if content.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                return ".docx" if "word/document.xml" in archive.namelist() else None
        except zipfile.BadZipFile:
            return None
    if content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):  # OLE2: Word 97 .doc
        return ".doc"
    if content[:1024].lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return ".html"
    return None


def wrap_html(content: str) -> str:
    """Wrap HTML fragment in ``<html><body>`` tags for conversion."""
    return f"<html><body>{content}</body></html>"


_C1_RE = re.compile("[\x80-\x9f]")
_LINE_THROUGH_RE = re.compile(
    r"text-decoration(?:-[a-z]+)?\s*:\s*[^;]*\bline-through\b", re.IGNORECASE
)
_LINE_THROUGH_DECL_RE = re.compile(
    r"text-decoration(?:-[a-z]+)?\s*:\s*[^;]*\bline-through\b[^;]*;?", re.IGNORECASE
)
_STRIKETHROUGH_HINT_RE = re.compile(r"<(?:s|del|strike)\b|line-through", re.IGNORECASE)
_STRIKE_TAGS = frozenset({"s", "del", "strike"})
_BLOCK_STRIKE_TAGS = frozenset(
    {
        "p",
        "div",
        "li",
        "td",
        "th",
        "dt",
        "dd",
        "caption",
        "blockquote",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)
_TABLE_LIST_CONTAINERS = frozenset(
    {"table", "thead", "tbody", "tfoot", "tr", "ul", "ol", "dl"}
)
_STRUCTURAL_STRIKE_TAGS = (
    _BLOCK_STRIKE_TAGS
    | _TABLE_LIST_CONTAINERS
    | frozenset(
        {
            "section",
            "article",
            "main",
            "header",
            "footer",
            "aside",
            "nav",
            "form",
            "figure",
            "figcaption",
        }
    )
)
_ORDINAL_ONLY_RE = re.compile(r"^[ºª°]+$")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,;:])")
_OPERATIVE_CLAUSE_START_RE = re.compile(
    r"^(?:art(?:igo|\.)?\s*\d|§|par[aá]grafo\b|[ivxlcdm]+\s*[-–—])",
    re.IGNORECASE,
)
_PRE_BLOCK_RE = re.compile(r"(<pre\b[^>]*>)(.*?)(</pre>)", re.IGNORECASE | re.DOTALL)
_PRE_NEWLINE_SENTINEL = "\ue000LEGISBR_PRE_NL\ue000"


def _from_cp1252(match: re.Match[str]) -> str:
    return bytes([ord(match.group())]).decode("cp1252", errors="ignore")


def clean_markdown(text: str, replace: list[tuple[str, str]] | None = None) -> str:
    """Clean markdown text by removing links and applying custom replacements."""
    text = clean_md_tag(text)
    # Sources that decode cp1252 as Latin-1 leave C1 controls for dashes and quotes.
    text = _C1_RE.sub(_from_cp1252, text)
    text = text.replace("\u00a0", " ").replace("\u00ad", "")
    text = re.sub("(?<!!)\\[([^\\]]*)\\]\\([^)]*\\)", "\\1", text)
    # Collapse adjacent strikethrough boundaries (e.g. ``<span><s>a</s></span><s>b</s>``
    # or stripped ``[~~a~~](url)~~b~~``) so CommonMark ``~~~~`` fences never appear.
    text = re.sub(r"(?<!~)~~~~(?!~)", "", text)
    if replace:
        for find, replacement in replace:
            text = re.sub(find, replacement, text)
    return text.strip()


def strip_html_chrome(
    soup: BeautifulSoup | Tag, extra_selectors: list[dict] | None = None
) -> BeautifulSoup | Tag:
    """Remove standard chrome tags (script, style, nav, header, footer, aside) from soup.

    Optionally remove elements matching *extra_selectors* (each a dict of
    ``find_all`` kwargs, e.g. ``{"class_": "rodapeTexto"}``).
    """
    for el in soup.find_all(["script", "style", "nav", "header", "footer", "aside"]):
        el.decompose()
    if extra_selectors:
        for selector in extra_selectors:
            for el in soup.find_all(True, **selector):
                el.decompose()
    return soup


def calc_pages(total: int, per_page: int) -> int:
    """Number of pages needed for *total* items at *per_page* each."""
    if total <= 0 or per_page <= 0:
        return 0
    return (total + per_page - 1) // per_page


def _remove_unwanted_tags(soup: BeautifulSoup | Tag, tag_names: list[str]) -> None:
    """Decompose all occurrences of the given tag names (img, style, script, etc.)."""
    if tag_names:
        for tag in soup.find_all(tag_names):
            tag.decompose()


def _strip_boilerplate(soup: BeautifulSoup | Tag) -> None:
    """Remove disclaimer paragraphs common in Brazilian legislation pages."""
    for tag in list(soup.find_all(["p", "span", "div"])):
        if tag.decomposed:
            continue
        txt = tag.get_text(strip=True)
        if _DISCLAIMER_RE.search(txt) and len(txt) < 300:
            tag.decompose()


def _clean_links(soup: BeautifulSoup | Tag) -> None:
    """Remove empty anchor tags and unwrap anchors that contain text."""
    for tag in list(soup.find_all("a")):
        if tag.decomposed:
            continue
        if not tag.get_text(strip=True) and (not tag.find("img")):
            tag.decompose()
        else:
            tag.unwrap()


def _unwrap_font_tags(soup: BeautifulSoup | Tag) -> None:
    """Unwrap ``<font>`` tags, keeping their inner content."""
    for tag in list(soup.find_all("font")):
        if not tag.decomposed:
            tag.unwrap()


def _remove_empty_elements(soup: BeautifulSoup | Tag) -> None:
    """Remove empty heading, div, span, and bold tags."""
    for tag in list(soup.find_all(_CLEAN_NORM_EMPTY_TAGS)):
        if tag.decomposed:
            continue
        if not tag.get_text(strip=True) and (not tag.find("img")):
            tag.decompose()


def _strip_line_through_style(tag: Tag) -> None:
    """Remove the ``line-through`` declaration from an element's ``style`` attribute."""
    style = tag.get("style")
    if not isinstance(style, str):
        return
    cleaned = _LINE_THROUGH_DECL_RE.sub("", style).strip()
    if cleaned:
        tag["style"] = cleaned
    else:
        del tag["style"]


def _wrap_children_in_strike(tag: Tag) -> None:
    """Wrap *tag*'s direct contents in an ``<s>`` element, preserving the outer block."""
    strike = Tag(name="s")
    for child in list(tag.contents):
        strike.append(child.extract())
    tag.append(strike)


def _has_visible_text(nodes: list[Any]) -> bool:
    for node in nodes:
        if isinstance(node, Comment):
            continue
        if isinstance(node, NavigableString):
            if str(node).strip():
                return True
        elif isinstance(node, Tag) and node.get_text(strip=True):
            return True
    return False


def _wrap_inline_run_in_strike(nodes: list[Any]) -> None:
    if not nodes or not _has_visible_text(nodes):
        return
    strike = Tag(name="s")
    nodes[0].insert_before(strike)
    for node in nodes:
        strike.append(node.extract())


def _apply_strikethrough(tag: Tag) -> None:
    """Apply ``<s>`` safely without wrapping tables/lists or dropping mixed direct text."""
    _strip_line_through_style(tag)
    if tag.name in _STRIKE_TAGS or tag.find_parent(_STRIKE_TAGS):
        return
    if not tag.get_text(strip=True):
        return

    if tag.name in _TABLE_LIST_CONTAINERS:
        for child in list(tag.children):
            if isinstance(child, Tag):
                _apply_strikethrough(child)
        return

    if tag.name in _STRUCTURAL_STRIKE_TAGS:
        if not tag.find(_STRUCTURAL_STRIKE_TAGS):
            _wrap_children_in_strike(tag)
            return
        inline_run: list[Any] = []
        for child in list(tag.contents):
            is_structural_child = isinstance(child, Tag) and (
                child.name in _STRUCTURAL_STRIKE_TAGS
                or child.find(_STRUCTURAL_STRIKE_TAGS) is not None
            )
            if is_structural_child:
                _wrap_inline_run_in_strike(inline_run)
                inline_run.clear()
                _apply_strikethrough(child)
            else:
                inline_run.append(child)
        _wrap_inline_run_in_strike(inline_run)
        return

    if tag.find(_STRUCTURAL_STRIKE_TAGS):
        inline_run = []
        for child in list(tag.contents):
            is_structural_child = isinstance(child, Tag) and (
                child.name in _STRUCTURAL_STRIKE_TAGS
                or child.find(_STRUCTURAL_STRIKE_TAGS) is not None
            )
            if is_structural_child:
                _wrap_inline_run_in_strike(inline_run)
                inline_run.clear()
                _apply_strikethrough(child)
            else:
                inline_run.append(child)
        _wrap_inline_run_in_strike(inline_run)
        return

    if tag.name in {"span", "font"}:
        tag.name = "s"
    else:
        tag.wrap(Tag(name="s"))


def _normalize_strikethrough_soup(soup: BeautifulSoup | Tag) -> None:
    """Normalize CSS line-through, nested strikethroughs, and ordinal-only ``<s>`` tags."""
    # 1. Convert CSS text-decoration: line-through elements into <s>.
    for tag in list(soup.find_all(style=_LINE_THROUGH_RE)):
        if tag.decomposed:
            continue
        _apply_strikethrough(tag)

    # 2. Collapse nested <s>/<del>/<strike> tags so revoked norms never emit ``~~~~`` fences.
    for tag in list(soup.find_all(_STRIKE_TAGS)):
        if tag.decomposed:
            continue
        if tag.find_parent(_STRIKE_TAGS):
            tag.unwrap()

    # 3. Unwrap ordinal-only <s>º</s> / <s>ª</s> / <s>°</s> Word/Tika artifacts
    #    and empty/whitespace-only strike tags (e.g. ``<s><span><o:p></o:p></span></s>``).
    for tag in list(soup.find_all(_STRIKE_TAGS)):
        if tag.decomposed:
            continue
        txt = tag.get_text(strip=True)
        if _ORDINAL_ONLY_RE.fullmatch(txt) or (not txt and not tag.find("img")):
            tag.unwrap()

    # 4. Merge adjacent sibling <s>/<del>/<strike> tags so ``<s>a</s><s>b</s>``
    #    never emits CommonMark ``~~~~`` fenced code blocks.
    for tag in list(soup.find_all(_STRIKE_TAGS)):
        if tag.decomposed:
            continue
        while True:
            sib = tag.next_sibling
            if isinstance(sib, Tag) and sib.name in _STRIKE_TAGS:
                for child in list(sib.contents):
                    tag.append(child.extract())
                sib.decompose()
                continue
            break


def _clean_attributes(soup: BeautifulSoup | Tag) -> None:
    """Strip inline ``style`` attributes from all remaining tags."""
    for tag in soup.find_all(style=True):
        del tag["style"]


def clean_norm_soup(
    soup: BeautifulSoup | Tag,
    *,
    remove_disclaimers: bool = True,
    unwrap_links: bool = True,
    remove_images: bool = False,
    remove_empty_tags: bool = True,
    unwrap_fonts: bool = False,
    strip_styles: bool = False,
    remove_style_tags: bool = False,
    remove_script_tags: bool = False,
) -> BeautifulSoup | Tag:
    """Content-level cleaning for norm text HTML.

    Complements ``strip_html_chrome()`` (structural cleanup) with
    content-level artifact removal common to Brazilian legislation pages.
    All options are independently toggleable.
    """
    tags_to_remove: list[str] = []
    if remove_images:
        tags_to_remove.append("img")
    if remove_style_tags:
        tags_to_remove.append("style")
    if remove_script_tags:
        tags_to_remove.append("script")
    _remove_unwanted_tags(soup, tags_to_remove)
    if remove_disclaimers:
        _strip_boilerplate(soup)
    if unwrap_links:
        _clean_links(soup)
    if unwrap_fonts:
        _unwrap_font_tags(soup)
    _normalize_strikethrough_soup(soup)
    if remove_empty_tags:
        _remove_empty_elements(soup)
    if strip_styles:
        _clean_attributes(soup)
    return soup


def infer_type_from_title(title: str, types: dict | list) -> str | None:
    """Return the first type whose name appears at the start of *title*."""
    type_names = list(types.keys()) if isinstance(types, dict) else list(types)
    for name in sorted(type_names, key=len, reverse=True):
        if title.lower().startswith(name.lower()):
            return name
    return None


def valid_markdown(text_markdown: str | None, min_length: int = 50) -> tuple[bool, str]:
    """Validate markdown text using common patterns found across all scrapers."""
    if not text_markdown:
        return (False, "text_markdown is None or empty")
    stripped = text_markdown.strip()
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


async def html_to_markdown(
    html: str,
    base_url: str,
    fetch_image: Callable[[str], Awaitable[bytes | None]],
    *,
    cache: ImageCache | None = None,
) -> str:
    html = await inline_images_in_html(html, base_url, fetch_image, cache=cache)
    if _STRIKETHROUGH_HINT_RE.search(html):
        soup = BeautifulSoup(html, "lxml")
        _normalize_strikethrough_soup(soup)
        html = str(soup)
    has_pre = bool(_PRE_BLOCK_RE.search(html))
    if has_pre:
        html = _PRE_BLOCK_RE.sub(
            lambda m: (
                m.group(1)
                + m.group(2)
                .replace("\r\n", "\n")
                .replace("\r", "\n")
                .replace("\n", _PRE_NEWLINE_SENTINEL)
                + m.group(3)
            ),
            html,
        )
    result = await asyncio.to_thread(
        html_to_md,
        html,
        ConversionOptions(
            heading_style="atx",
            strip_tags=["script", "style", "svg"],
            extract_metadata=False,
            # ASP.NET/JSF/Domino pages wrap the whole law in a <form>.
            preprocessing=PreprocessingOptions(remove_forms=False),
            hidden_content="all",
            strip_newlines=True,
        ),
    )
    content = result.content or ""
    if has_pre:
        content = content.replace(_PRE_NEWLINE_SENTINEL, "\n")
    return clean_markdown(content)


async def bytes_to_markdown(
    data: bytes,
    content_type: str,
    url: str,
    http: HttpClient,
    ocr: OCR | None = None,
    *,
    pdf_executor: PdfExecutor = "auto",
    pdf_workers: int | None = None,
) -> str:
    if is_pdf(data, content_type):
        if not data.startswith(b"%PDF"):
            raise ValueError(f"Expected PDF content from {url}")
        return await pdf_to_markdown(
            data, ocr, executor=pdf_executor, max_workers=pdf_workers
        )
    if _is_image_bytes(data):
        if ocr is None:
            raise ValueError("Image needs OCR but no OCR model is configured")
        return clean_markdown(await ocr.images([data]))

    async def fetch_image(image_url: str) -> bytes:
        return (await http.request(image_url)).content

    image_cache = getattr(http, "image_cache", None)
    sniffed = sniff_extension(data)
    if sniffed == ".docx":
        import mammoth

        result = await asyncio.to_thread(mammoth.convert_to_html, io.BytesIO(data))
        return await html_to_markdown(result.value, url, fetch_image, cache=image_cache)
    if sniffed == ".doc":
        # OLE2 binary Word 97 (.doc) has no pure-Python converter; fail explicitly.
        raise ValueError(f"Legacy Word/Office (OLE2) file is not supported: {url}")
    response = Response(url, 200, {"Content-Type": content_type}, data)
    if sniffed is None and response.content_type == "text/plain":
        text = response.text.replace("\r\n", "\n").replace("\r", "\n")
        return clean_markdown(text)
    return await html_to_markdown(response.text, url, fetch_image, cache=image_cache)


def normalize_type(
    raw_type: str,
    known_types: dict | list,
    aliases: dict | None = None,
    fallback: str = "",
) -> str:
    cleaned = re.sub("\\s+", " ", str(raw_type or "")).strip()
    if not cleaned:
        return fallback
    lower = cleaned.casefold()
    if aliases and lower in aliases:
        return aliases[lower]
    return next(
        (key for key in known_types if lower == key.casefold()), fallback or cleaned
    )


def remove_summary_from_markdown(md: str, summary: str) -> str:
    """Remove the summary paragraph from text_markdown to avoid duplication.

    The Bahia document body (``field--name-body``) includes the ementa/summary
    as its second paragraph. Since the summary is already stored separately, it
    is stripped here to avoid repeating it verbatim inside ``text_markdown``.
    """
    if not summary:
        return md
    summary_norm = re.sub("\\s+", " ", summary).strip()
    paragraphs = md.split("\n\n")
    filtered = [
        p
        for p in paragraphs
        if re.sub("\\s+", " ", re.sub("[*_`]+", "", p)).strip() != summary_norm
    ]
    if len(filtered) == len(paragraphs):
        return md
    return "\n\n".join(filtered)


def remove_summary_element(container: BeautifulSoup | Tag, summary: str) -> None:
    """Remove the smallest dedicated summary container without deleting operative clauses."""
    normalized = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", " ".join(summary.split())).casefold()
    if not normalized:
        return
    candidates = []
    for tag in container.find_all(["p", "td", "div", "span", "font", "table"]):
        text = _SPACE_BEFORE_PUNCT_RE.sub(
            r"\1", " ".join(tag.get_text(" ", strip=True).split())
        ).casefold()
        if normalized not in text:
            continue
        if len(text) > len(normalized) * 1.25 or len(text) >= 600:
            continue
        if _OPERATIVE_CLAUSE_START_RE.match(text):
            continue
        if "art." in text and "art." not in normalized:
            continue
        candidates.append((len(text), tag))
    if candidates:
        min(candidates, key=lambda item: item[0])[1].decompose()


def infer_type_by_prefix(title: str, types: dict | list) -> str:
    """Match the longest type prefix; mappings supply canonical labels."""
    normalized = " ".join(title.split()).casefold()
    for name in sorted(types, key=len, reverse=True):
        if normalized.startswith(name.casefold()):
            value = types[name] if isinstance(types, dict) else name
            return value if isinstance(value, str) else name
    return ""
