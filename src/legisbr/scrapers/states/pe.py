from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from legisbr.core.convert import (
    _DISCLAIMER_RE,
    _STRIKETHROUGH_HINT_RE,
    clean_norm_soup,
    html_to_markdown,
    valid_markdown,
    wrap_html,
)
from legisbr.core.document import Document
from legisbr.core.scraper import (
    DEFAULT_INVALID_SITUATION,
    DEFAULT_VALID_SITUATION,
    BaseScraper,
)

logger = logging.getLogger(__name__)

TYPES = {
    "Ato Administrativo Normativo": 0,
    "Ato Administrativo Parlamentar": 1,
    "Constituição Estadual": 2,
    "Decreto do Executivo": 3,
    "Decreto Legislativo": 4,
    "Decreto-Lei": 5,
    "Emenda Constitucional": 6,
    "Lei Complementar": 7,
    "Lei Delegada": 8,
    "Lei Ordinária": 9,
    "Lei Provincial": 10,
    "Portaria Administrativa da Alepe": 11,
    "Resolução Conjunta": 12,
    "Resolução da Alepe": 13,
    "Resolução do Poder Judiciário": 14,
}
SITUATIONS: dict[str, str] = {}
_SEARCH_PATH = "/Paginas/pesquisaAvancada.aspx"
_TEXT_VERSION_PRIORITY = ("TEXTOATUALIZADO", "TEXTOORIGINAL", "TEXTOANOTADO", "")
_WORD_SECTION_CLASS_RE = re.compile(r"^WordSection\d*$")
_THREAD_OFFLOAD_BYTES = 65_536
_GIANT_HTML_BYTES = 150_000
_ASPNET_HIDDEN_INPUT_RE = re.compile(
    rb'<input\b[^>]*\bname\s*=\s*["\']__(?:VIEWSTATE|EVENTVALIDATION|VIEWSTATEGENERATOR)["\'][^>]*>',
    re.IGNORECASE,
)
_NON_CONTENT_BLOCK_RE = re.compile(
    rb"<(style|script|xml)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_HTML_COMMENT_RE = re.compile(rb"<!--.*?-->", re.DOTALL)
_DIV_TEXTO_RE = re.compile(
    rb'<div\b[^>]*\bid\s*=\s*(?:"divTexto"|\'divTexto\'|divTexto(?=[\s>]))[^>]*>',
    re.IGNORECASE,
)
_DIV_REVOGADA_RE = re.compile(
    rb'<div\b[^>]*\bid\s*=\s*(?:"divRevogada"|\'divRevogada\'|divRevogada(?=[\s>]))',
    re.IGNORECASE,
)
_EMBEDDED_HTML_END_RE = re.compile(rb"</html\s*>", re.IGNORECASE)
_EMBEDDED_BODY_RE = re.compile(
    rb"<body\b[^>]*>(.*)</body\s*>", re.IGNORECASE | re.DOTALL
)
_LINE_THROUGH_BYTES_RE = re.compile(rb"line-through", re.IGNORECASE)
_INLINE_STYLE_ATTR_RE = re.compile(
    rb"""\s+style\s*=\s*(?:'[^']*'|\"[^\"]*\")""", re.IGNORECASE
)
_BARE_SPAN_LEAF_RE = re.compile(rb"<span\s*>([^<]*)</span>", re.IGNORECASE)
_TD_SINGLE_P_RE = re.compile(
    rb"(<td\b[^>]*>)\s*<p(?:\s+(?:class|align|lang)=[^\s>]+)*\s*>([^<]*)</p>\s*(</td>)",
    re.IGNORECASE,
)
_TD_PLAIN_OPEN_RE = re.compile(
    rb"""<td(?:\s+(?:width|height|valign|align|nowrap|bgcolor)(?:=(?:"[^"]*"|'[^']*'|[^\s>]+))?)*\s*>""",
    re.IGNORECASE,
)
_TD_PRESENTATIONAL_ATTR_RE = re.compile(
    rb"""\s+(?:width|height|valign|align|nowrap|bgcolor)(?:=(?:"[^"]*"|'[^']*'|[^\s>]+))?""",
    re.IGNORECASE,
)
_TD_OPEN_RE = re.compile(rb"<td\b[^>]*>", re.IGNORECASE)
_WORD_SECTION_BYTES_RE = re.compile(rb"WordSection\d*")
_TYPE_ALIASES = {
    "ato administrativo normativo": "Ato Administrativo Normativo",
    "ato administrativo parlamentar": "Ato Administrativo Parlamentar",
    "constituição estadual": "Constituição Estadual",
    "constituição do estado": "Constituição Estadual",
    "decreto do executivo": "Decreto do Executivo",
    "decreto legislativo": "Decreto Legislativo",
    "decreto-lei": "Decreto-Lei",
    "emenda constitucional": "Emenda Constitucional",
    "lei complementar": "Lei Complementar",
    "lei delegada": "Lei Delegada",
    "lei ordinária": "Lei Ordinária",
    "lei provincial": "Lei Provincial",
    "portaria administrativa da alepe": "Portaria Administrativa da Alepe",
    "resolução conjunta": "Resolução Conjunta",
    "resolução da alepe": "Resolução da Alepe",
    "resolução do poder judiciário": "Resolução do Poder Judiciário",
}


@dataclass(slots=True)
class PagerSlot:
    slot_id: str
    page_number: int
    is_active: bool = False


class PernambucoAlepeScraper(BaseScraper):
    """Scraper for Assembleia Legislativa de Pernambuco (ALEPE).

    Year start (earliest on source): 1835

    The search form is an ASP.NET postback page. The fastest reliable flow is to
    search by year only, then infer the norm type from the result row title.
    ALEPE also exposes multiple text versions per document; this scraper prefers
    ``TEXTOATUALIZADO`` and falls back to ``TEXTOORIGINAL`` when needed.
    """

    types = TYPES
    situations = SITUATIONS
    name = "PERNAMBUCO"
    base_url = "https://legis.alepe.pe.gov.br"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.search_url = urljoin(self.base_url, _SEARCH_PATH)
        self.params = {
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": "",
            "__VIEWSTATEGENERATOR": "",
            "__EVENTVALIDATION": "",
            "ctl00$hfUrl": self.search_url,
            "ctl00$tbxLogin": "",
            "ctl00$tbxSenha": "",
            "ctl00$conteudo$tbxNumero": "",
            "ctl00$conteudo$tbxAno": "",
            "ctl00$conteudo$tbxTextoPesquisa": "",
            "ctl00$conteudo$tbxTextoPesquisaNeg": "",
            "ctl00$conteudo$cbxlPesquisa$cbxlPesquisa_0": "CONTTXTORIGINAL",
            "ctl00$conteudo$cbxlPesquisa$cbxlPesquisa_1": "EMENTA",
            "ctl00$conteudo$cbxlPesquisa$cbxlPesquisa_2": "APELIDO",
            "ctl00$conteudo$cbxlPesquisa$cbxlPesquisa_3": "NOME",
            "ctl00$conteudo$tbxThesaurus": "",
            "ctl00$conteudo$rbOpThesaurus": "Todos",
            "ctl00$conteudo$tbxAssuntoGeral": "",
            "ctl00$conteudo$rbOpAssuntoGeral": "Todos",
            "ctl00$conteudo$tbxDataInicialNorma": "",
            "ctl00$conteudo$tbxDataFinalNorma": "",
            "ctl00$conteudo$ddlPublicacao": "",
            "ctl00$conteudo$tbxDataInicialPublicacao": "",
            "ctl00$conteudo$tbxDataFinalPublicacao": "",
            "ctl00$conteudo$tbxIniciativa": "",
            "ctl00$conteudo$tbxNumeroProjeto": "",
            "ctl00$conteudo$tbxAnoProjeto": "",
            "ctl00$conteudo$btnPesquisar": "Pesquisar",
            "ctl00$tbxNomeErro": "",
            "ctl00$tbxEmailErro": "",
            "ctl00$tbxMensagemErro": "",
            "ctl00$tbxLoginMob": "",
            "ctl00$tbxSenhaMob": "",
        }
        self._known_types = tuple(sorted(TYPES.keys(), key=len, reverse=True))

    @staticmethod
    def _attr_str(tag: Tag, attr_name: str) -> str:
        value = tag.get(attr_name, "")
        return value if isinstance(value, str) else ""

    def _get_form_state(self, soup: BeautifulSoup) -> dict[str, str]:
        """Extract the ASP.NET form state from a page."""
        state: dict[str, str] = {}
        for field_name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
            input_tag = soup.find("input", {"name": field_name})
            if isinstance(input_tag, Tag):
                state[field_name] = self._attr_str(input_tag, "value")
        return state

    def _build_search_payload(
        self, year: int, form_state: dict[str, str]
    ) -> dict[str, str]:
        payload = self.params.copy()
        payload.update(form_state)
        payload["ctl00$conteudo$tbxAno"] = str(year)
        return payload

    def _build_postback_payload(
        self, base_payload: dict[str, str], source_soup: BeautifulSoup, control_id: str
    ) -> dict[str, str]:
        payload = base_payload.copy()
        payload.update(self._get_form_state(source_soup))
        payload["__EVENTTARGET"] = (
            control_id
            if control_id.startswith("ctl00$")
            else f"ctl00$conteudo${control_id}"
        )
        payload["__EVENTARGUMENT"] = ""
        payload["__LASTFOCUS"] = ""
        payload["ctl00$conteudo$hfPage"] = "0"
        payload.pop("ctl00$conteudo$btnPesquisar", None)
        return payload

    async def _post_results_page(self, payload: dict[str, str]) -> BeautifulSoup | None:
        response = await self.http.request(
            self.search_url, method="POST", data=payload, timeout=60
        )
        return response.soup()

    @staticmethod
    def _extract_text_version(url: str) -> str:
        upper_url = url.upper()
        for version in ("TEXTOATUALIZADO", "TEXTOORIGINAL", "TEXTOANOTADO"):
            if f"TIPO={version}" in upper_url:
                return version
        return ""

    def _get_candidate_document_urls(self, row: Tag) -> list[str]:
        urls_by_version: dict[str, str] = {}
        title_link = row.select_one("span.nome-norma a[href]")
        if isinstance(title_link, Tag):
            href = self._attr_str(title_link, "href")
            if href:
                urls_by_version[""] = urljoin(self.base_url, href)
        for link in row.select("td.textos a[href], td#textos a[href]"):
            if not isinstance(link, Tag):
                continue
            href = self._attr_str(link, "href")
            if not href:
                continue
            abs_url = urljoin(self.base_url, href)
            version = self._extract_text_version(abs_url)
            urls_by_version[version] = abs_url
        ordered_urls: list[str] = []
        for version in _TEXT_VERSION_PRIORITY:
            candidate_url = urls_by_version.get(version)
            if candidate_url and candidate_url not in ordered_urls:
                ordered_urls.append(candidate_url)
        return ordered_urls

    def _extract_norm_type(self, title: str) -> str:
        normalized_title = " ".join(title.split())
        lowered = normalized_title.casefold()
        for alias, canonical in sorted(
            _TYPE_ALIASES.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if lowered.startswith(alias):
                return canonical
        for norm_type in self._known_types:
            if normalized_title.startswith(norm_type):
                return norm_type
        match = re.match("(.+?)\\s+n[°ºo]", normalized_title, re.IGNORECASE)
        if match:
            fallback = " ".join(match.group(1).split())
            logger.warning(f"PERNAMBUCO | Unknown norm type fallback: {fallback}")
            return fallback
        match = re.match("(.+?)\\s+de\\s+\\d{4}$", normalized_title, re.IGNORECASE)
        if match:
            fallback = " ".join(match.group(1).split())
            logger.warning(f"PERNAMBUCO | Unknown year-title fallback: {fallback}")
            return fallback
        logger.warning(f"PERNAMBUCO | Could not infer norm type from title: {title}")
        return normalized_title

    def _extract_documents(self, soup: BeautifulSoup, year: int) -> list[dict]:
        """Extract document metadata from a result page."""
        div_resultado = soup.find("div", id="divResultado")
        if not isinstance(div_resultado, Tag):
            return []
        tbody = div_resultado.find("tbody")
        if not isinstance(tbody, Tag):
            return []
        documents: list[dict] = []
        for row in tbody.find_all("tr"):
            if not isinstance(row, Tag):
                continue
            title_link = row.select_one("span.nome-norma a[href]")
            if not isinstance(title_link, Tag):
                continue
            title = title_link.get_text(" ", strip=True)
            if not title:
                continue
            candidate_urls = self._get_candidate_document_urls(row)
            if not candidate_urls:
                continue
            summary_div = row.select_one("td.ementa-norma .fLeft")
            additional_data_link = row.select_one(
                "td.ementa-norma a[href*='dadosReferenciais.aspx']"
            )
            publication_span = row.select_one("span.publicacao")
            documents.append(
                {
                    "year": year,
                    "type": self._extract_norm_type(title),
                    "title": title,
                    "summary": summary_div.get_text(" ", strip=True)
                    if isinstance(summary_div, Tag)
                    else "",
                    "publication": publication_span.get_text(" ", strip=True)
                    if isinstance(publication_span, Tag)
                    else "",
                    "document_url": candidate_urls[0],
                    "additional_data_url": urljoin(
                        self.base_url, self._attr_str(additional_data_link, "href")
                    )
                    if isinstance(additional_data_link, Tag)
                    else "",
                    "_candidate_document_urls": candidate_urls,
                }
            )
        return documents

    @staticmethod
    def _parse_pager_slots(soup: BeautifulSoup) -> list[PagerSlot]:
        slots: list[PagerSlot] = []
        for tag in soup.select("a[id^='lbtn'], span[id^='lbtn']"):
            if not isinstance(tag, Tag):
                continue
            slot_id = PernambucoAlepeScraper._attr_str(tag, "id")
            label = tag.get_text(" ", strip=True)
            if not slot_id or not label.isdigit():
                continue
            slots.append(
                PagerSlot(
                    slot_id=slot_id,
                    page_number=int(label),
                    is_active="active" in (tag.get("class") or []),
                )
            )
        return slots

    @staticmethod
    def _extract_reference_value(soup: BeautifulSoup, label: str) -> str:
        for header in soup.find_all("th"):
            if not isinstance(header, Tag):
                continue
            if header.get_text(" ", strip=True) != label:
                continue
            value_cell = header.find_next_sibling("td")
            if isinstance(value_cell, Tag):
                return value_cell.get_text(" ", strip=True)
        return ""

    @staticmethod
    def _infer_situation(
        text_soup: BeautifulSoup | None, reference_soup: BeautifulSoup | None
    ) -> str:
        for soup in (text_soup, reference_soup):
            if isinstance(soup, BeautifulSoup) and soup.find("div", id="divRevogada"):
                return DEFAULT_INVALID_SITUATION
        return DEFAULT_VALID_SITUATION

    def _get_additional_data(
        self,
        reference_soup: BeautifulSoup | None,
        *,
        text_soup: BeautifulSoup | None = None,
    ) -> dict[str, str]:
        data = {
            "situation": self._infer_situation(text_soup, reference_soup),
            "date": "",
            "initiative": "",
            "publication": "",
            "subject": "",
            "updates": "",
            "indexation": "",
        }
        if not isinstance(reference_soup, BeautifulSoup):
            return data
        data.update(
            {
                "date": self._extract_reference_value(reference_soup, "Data"),
                "initiative": self._extract_reference_value(
                    reference_soup, "Iniciativa"
                ),
                "publication": self._extract_reference_value(
                    reference_soup, "Publicação"
                ),
                "subject": self._extract_reference_value(
                    reference_soup, "Assunto Geral"
                ),
                "updates": self._extract_reference_value(
                    reference_soup, "Atualizações"
                ),
                "indexation": self._extract_reference_value(
                    reference_soup, "Indexação"
                ),
            }
        )
        summary = self._extract_reference_value(reference_soup, "Ementa")
        if summary:
            data["summary"] = summary
        return data

    @staticmethod
    def _extract_content_container(soup: BeautifulSoup) -> Tag | None:
        """Extract all WordSection divs (preferring ``#divTexto``) to avoid annex truncation."""
        div_texto = soup.find("div", id="divTexto")
        search_root: BeautifulSoup | Tag = (
            div_texto if isinstance(div_texto, Tag) else soup
        )
        sections = [
            tag
            for tag in search_root.find_all("div", class_=_WORD_SECTION_CLASS_RE)
            if isinstance(tag, Tag)
        ]
        if len(sections) == 1:
            return sections[0]
        if len(sections) > 1:
            wrapper = BeautifulSoup("<div></div>", "lxml").div
            assert wrapper is not None
            for section in sections:
                wrapper.append(section.extract())
            return wrapper
        if isinstance(div_texto, Tag):
            for tag in div_texto.find_all(["style", "title", "meta"]):
                tag.decompose()
            return div_texto
        return None

    @staticmethod
    def _strip_presentational_td_attrs(match: re.Match[bytes]) -> bytes:
        tag = match.group(0)
        if b"line-through" in tag.lower():
            return tag
        return _TD_PRESENTATIONAL_ATTR_RE.sub(b"", tag)

    @classmethod
    def _preclean_document_bytes(cls, raw: bytes) -> tuple[bytes, bool]:
        """Strip ASP.NET ViewState, MSO comments, style/script blocks, and inline styles before DOM parsing."""
        has_div_revogada = False
        if b"divRevogada" in raw:
            # Verify outside comments/scripts without expanding giant __VIEWSTATE.
            check_region = _ASPNET_HIDDEN_INPUT_RE.sub(b"", raw)
            check_region = _NON_CONTENT_BLOCK_RE.sub(b"", check_region)
            check_region = _HTML_COMMENT_RE.sub(b"", check_region)
            has_div_revogada = bool(_DIV_REVOGADA_RE.search(check_region))
            del check_region

        # Fast C substring lookup to skip giant __VIEWSTATE before divTexto (57%+ of PE pages).
        div_start = -1
        for marker in (b'id="divTexto"', b"id='divTexto'"):
            pos = raw.find(marker)
            if pos != -1:
                open_pos = raw.rfind(b"<div", max(0, pos - 256), pos)
                if open_pos != -1:
                    div_start = open_pos
                    break
        if div_start == -1:
            div_match = _DIV_TEXTO_RE.search(raw)
            if div_match is not None:
                div_start = div_match.start()

        if div_start != -1:
            chunk = raw[div_start:]
            end_match = _EMBEDDED_HTML_END_RE.search(chunk)
            if end_match is not None:
                chunk = chunk[: end_match.end()]
        else:
            chunk = _ASPNET_HIDDEN_INPUT_RE.sub(b"", raw)

        chunk = _NON_CONTENT_BLOCK_RE.sub(b"", chunk)
        chunk = _HTML_COMMENT_RE.sub(b"", chunk)
        if not _LINE_THROUGH_BYTES_RE.search(chunk):
            chunk = _INLINE_STYLE_ATTR_RE.sub(b"", chunk)
        else:
            chunk = _INLINE_STYLE_ATTR_RE.sub(
                lambda m: (
                    m.group(0) if _LINE_THROUGH_BYTES_RE.search(m.group(0)) else b""
                ),
                chunk,
            )
        return (chunk, has_div_revogada)

    @classmethod
    def _extract_giant_word_html(
        cls, chunk: bytes, charset: str
    ) -> tuple[str | None, bool]:
        """Low-memory extraction path for multi-megabyte Word table HTML exports."""
        chunk = _BARE_SPAN_LEAF_RE.sub(rb"\1", chunk)
        chunk = _TD_SINGLE_P_RE.sub(rb"\1\2\3", chunk)
        chunk = _TD_PLAIN_OPEN_RE.sub(b"<td>", chunk)
        chunk = _TD_OPEN_RE.sub(cls._strip_presentational_td_attrs, chunk)

        has_word_section = bool(_WORD_SECTION_BYTES_RE.search(chunk))
        has_disclaimer_hint = b"substitui" in chunk.lower()
        has_linked_img = b"<img" in chunk.lower() and b"<a" in chunk.lower()
        body_match = _EMBEDDED_BODY_RE.search(chunk)
        if (
            body_match is not None
            and has_word_section
            and not has_disclaimer_hint
            and not has_linked_img
        ):
            body_html = body_match.group(1).decode(charset, errors="replace")
            full_html = wrap_html(body_html)
            return (full_html, not _STRIKETHROUGH_HINT_RE.search(full_html))

        import lxml.html

        doc = lxml.html.fromstring(chunk.decode(charset, errors="replace"))
        del chunk
        div_texto_list = doc.xpath(".//div[@id='divTexto']")
        root = (
            div_texto_list[0]
            if div_texto_list
            else (doc if doc.tag == "div" and doc.get("id") == "divTexto" else doc)
        )
        sections = [
            el
            for el in root.xpath(".//div[@class]")
            if _WORD_SECTION_CLASS_RE.match((el.get("class") or "").strip())
        ]
        if sections:
            targets = sections
        elif div_texto_list or (doc.tag == "div" and doc.get("id") == "divTexto"):
            for unwanted in root.xpath(".//style | .//title | .//meta | .//script"):
                parent = unwanted.getparent()
                if parent is not None:
                    parent.remove(unwanted)
            targets = [root]
        else:
            return (None, False)

        for target in targets:
            if has_disclaimer_hint:
                candidates: list[Any] = []
                seen_ids: set[int] = set()
                for el in target.iter():
                    hit_self = bool(el.text and "substitui" in el.text.lower())
                    hit_tail = bool(el.tail and "substitui" in el.tail.lower())
                    if not hit_self and not hit_tail:
                        continue
                    start = el if hit_self else el.getparent()
                    ancestors: list[Any] = []
                    cur = start
                    while cur is not None and cur is not target:
                        if cur.tag in ("p", "span", "div"):
                            ancestors.append(cur)
                        cur = cur.getparent()
                    for anc in reversed(ancestors):
                        key = id(anc)
                        if key not in seen_ids:
                            seen_ids.add(key)
                            candidates.append(anc)
                for el in candidates:
                    parent = el.getparent()
                    if parent is None:
                        continue
                    parts: list[str] = []
                    total_len = 0
                    too_long = False
                    for piece in el.itertext():
                        stripped = piece.strip()
                        if not stripped:
                            continue
                        total_len += len(stripped)
                        if total_len >= 300:
                            too_long = True
                            break
                        parts.append(stripped)
                    if not too_long and _DISCLAIMER_RE.search("".join(parts)):
                        parent.remove(el)
            for anchor in list(target.xpath(".//a")):
                parent = anchor.getparent()
                if parent is None:
                    continue
                if not "".join(anchor.itertext()).strip() and not anchor.xpath(
                    ".//img"
                ):
                    parent.remove(anchor)
                else:
                    anchor.drop_tag()

        serialized = "".join(
            lxml.html.tostring(target, encoding="unicode") for target in targets
        )
        full_html = wrap_html(serialized)
        return (full_html, not _STRIKETHROUGH_HINT_RE.search(full_html))

    @classmethod
    def _prepare_document_html(
        cls, raw: bytes, charset: str
    ) -> tuple[str | None, bool, bool]:
        """Extract and clean PE document HTML, avoiding giant BS4 trees on massive Word exports."""
        chunk, has_div_revogada = cls._preclean_document_bytes(raw)
        if len(chunk) >= _GIANT_HTML_BYTES:
            full_html, strike_normalized = cls._extract_giant_word_html(chunk, charset)
            return (full_html, has_div_revogada, strike_normalized)

        soup = BeautifulSoup(chunk, "lxml", from_encoding=charset)
        del chunk
        if not has_div_revogada and soup.find("div", id="divRevogada") is not None:
            has_div_revogada = True
        content_div = cls._extract_content_container(soup)
        if not isinstance(content_div, Tag):
            return (None, has_div_revogada, False)
        clean_norm_soup(
            content_div,
            remove_disclaimers=True,
            unwrap_links=True,
            remove_empty_tags=True,
            strip_styles=True,
            remove_style_tags=True,
            remove_script_tags=True,
        )
        full_html = wrap_html(str(content_div))
        return (full_html, has_div_revogada, True)

    async def fetch(self, doc_info: dict) -> Document | None:
        year = doc_info["year"]
        norm_type = doc_info.get("type", "")
        candidate_urls = list(
            dict.fromkeys(
                doc_info.get("_candidate_document_urls")
                or [doc_info.get("document_url", "")]
            )
        )
        candidate_urls = [url for url in candidate_urls if url]
        additional_data_url = doc_info.get("additional_data_url", "")
        selected_url = ""
        selected_revogada = False
        raw = b""
        text_markdown = ""
        last_failure_reason = "Failed to retrieve document page"

        async def _fetch_image(url: str) -> bytes:
            return (await self.http.request(url)).content

        for candidate_url in candidate_urls:
            fetch_result = await self.http.request(
                candidate_url, timeout=60, ok_statuses=(404,)
            )
            if fetch_result.status == 404:
                last_failure_reason = f"No document at {candidate_url}"
                continue
            charset = fetch_result.charset
            if len(fetch_result.content) >= _THREAD_OFFLOAD_BYTES:
                (
                    full_html,
                    has_div_revogada,
                    strike_normalized,
                ) = await asyncio.to_thread(
                    self._prepare_document_html, fetch_result.content, charset
                )
            else:
                full_html, has_div_revogada, strike_normalized = (
                    self._prepare_document_html(fetch_result.content, charset)
                )
            if full_html is None:
                last_failure_reason = (
                    "Could not find WordSection or divTexto in document page"
                )
                continue
            candidate_markdown = await html_to_markdown(
                full_html,
                self.base_url,
                _fetch_image,
                cache=self.image_cache,
                strikethrough_normalized=strike_normalized,
            )
            valid, reason = valid_markdown(candidate_markdown)
            if not valid:
                last_failure_reason = f"Invalid markdown: {reason}"
                continue
            selected_url = candidate_url
            selected_revogada = has_div_revogada
            raw = fetch_result.content
            text_markdown = candidate_markdown
            break
        if not selected_url:
            raise ValueError(last_failure_reason)
        reference_page: BeautifulSoup | None = None
        if additional_data_url:
            reference_page = await self._fetch_reference_page(additional_data_url)
        result = {
            **doc_info,
            "year": year,
            "type": norm_type,
            "document_url": selected_url,
            "text_version": self._extract_text_version(selected_url) or "DEFAULT",
            "text_markdown": text_markdown,
            "raw": raw,
            "raw_ext": ".html",
        }
        result.pop("_candidate_document_urls", None)
        additional_data = self._get_additional_data(reference_page)
        if selected_revogada:
            additional_data["situation"] = DEFAULT_INVALID_SITUATION
        for key, value in additional_data.items():
            if value or key == "situation":
                result[key] = value
        if not result.get("situation"):
            result["situation"] = DEFAULT_VALID_SITUATION
        return Document(**result)

    async def list_documents(self, year: int) -> AsyncIterator[dict]:
        soup = (await self.http.request(self.search_url)).soup()
        state = self._get_form_state(soup)
        if not state:
            raise ValueError("Missing Pernambuco search form state")
        payload = self._build_search_payload(year, state)
        soup = await self._post_results_page(payload)
        seen = set()
        while True:
            slots = self._parse_pager_slots(soup)
            current = next((slot.page_number for slot in slots if slot.is_active), 1)
            if current not in seen:
                for ref in self._extract_documents(soup, year):
                    yield {"situation": DEFAULT_VALID_SITUATION, **ref}
                seen.add(current)
            next_slot = next(
                (
                    slot
                    for slot in slots
                    if slot.page_number > current and slot.page_number not in seen
                ),
                None,
            )
            if next_slot:
                target = next_slot.slot_id
            elif soup.find("a", id="lbtnProx") is not None:
                target = "lbtnProx"
            else:
                break
            next_soup = await self._post_results_page(
                self._build_postback_payload(payload, soup, target)
            )
            if next_soup is None:
                raise ValueError("Pernambuco pagination returned no page")
            next_slots = self._parse_pager_slots(next_soup)
            if next_slots == slots:
                raise ValueError("Pernambuco pagination did not advance")
            soup = next_soup

    async def _fetch_reference_page(self, url: str) -> BeautifulSoup:
        return (await self.http.request(url, timeout=60)).soup()
