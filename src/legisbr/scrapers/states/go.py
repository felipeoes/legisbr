from __future__ import annotations
import logging
import re
from collections.abc import AsyncIterator
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup, Tag

from legisbr.core.convert import (
    calc_pages,
    clean_norm_soup,
    remove_summary_from_markdown,
    valid_markdown,
    wrap_html,
)
from legisbr.core.document import Document
from legisbr.core.scraper import DEFAULT_VALID_SITUATION, BaseScraper

logger = logging.getLogger(__name__)

TYPES = {
    "Constituição Estadual": {"id": 12, "url_suffix": "constituicao-estadual"},
    "Emenda Constitucional": {"id": 13, "url_suffix": "emenda-constitucional"},
    "Lei Complementar": {"id": 1, "url_suffix": "lei-complementar"},
    "Lei Ordinária": {"id": 2, "url_suffix": "lei"},
    "Lei Delegada": {"id": 4, "url_suffix": "lei-delegada"},
    "Decreto Lei": {"id": 8, "url_suffix": "decreto-lei"},
    "Decreto Numerado": {"id": 3, "url_suffix": "decreto"},
    "Decreto Orçamentário": {"id": 5, "url_suffix": "decreto-orcamentario"},
    "Portaria Orçamentária": {"id": 6, "url_suffix": "portaria-orcamentaria"},
    "Resolução": {"id": 7, "url_suffix": "resolucao"},
}
SITUATIONS = {}
_SITE_URL = "https://legisla.casacivil.go.gov.br"
_DETAIL_API_URL = f"{_SITE_URL}/api/v2/pesquisa/legislacoes"
_SEARCH_PATH = "/api/v2/pesquisa/legislacoes"
_REDIRECT_MARKER = "Clique no link abaixo para acessar a:"
_PDF_STUB_MIN_BODY_LEN = 150
_TYPE_ID_TO_SUFFIX = {v["id"]: v["url_suffix"] for v in TYPES.values()}
_SPECIAL_RE = re.compile("[^a-zA-ZÀ-ÿ0-9\\s]")
_SPACE_RE = re.compile("\\s+")
_C1_RE = re.compile(r"[\x80-\x9f]")
_EPIGRAFE_RE = re.compile(
    r"\b(?:LEI|DECRETO|EMENDA|PORTARIA|RESOLU[ÇC][ÃA]O|CONSTITUI[ÇC][ÃA]O)\b",
    re.IGNORECASE,
)
_HEADER_CHROME_RE = re.compile(
    r"GOVERNO\s+DO\s+ESTADO\s+DE\s+GOI[AÁ]S"
    r"|Secretaria\s+de\s+Estado\s+da\s+Casa\s+Civil"
    r"|Gabinete\s+Civil\s+da\s+Governadoria"
    r"|Superintend[eê]ncia\s+de\s+Legisla[çc][aã]o\.?",
    re.IGNORECASE,
)
_WINZIP_TEXT_RE = re.compile(r"^\s*download\s+dos?\s+anexos?\s*$", re.IGNORECASE)


def _decode_cp1252_c1(text: str) -> str:
    """Decode raw CP1252 C1 control bytes (`0x80`–`0x9F`) left by Latin-1 decoding."""
    return _C1_RE.sub(
        lambda m: bytes([ord(m.group())]).decode("cp1252", errors="ignore"),
        text,
    )


class LegislaGoias(BaseScraper):
    """Webscraper for Goias state legislation website (https://legisla.casacivil.go.gov.br)

    Year start (earliest on source): 1887

    Example search request: https://legisla.casacivil.go.gov.br/api/v2/pesquisa/legislacoes?ano=1887&ordenarPor=data&page=1&qtd_por_pagina=100
    """

    name = "GOIAS"
    types = TYPES
    situations = SITUATIONS
    base_url = f"{_SITE_URL}{_SEARCH_PATH}"

    @staticmethod
    def _resolve_norm_type(doc: dict, doc_detail: dict) -> str:
        """Resolve the most reliable norm type from detail or search payloads."""
        detail_type = (
            (doc_detail.get("tipo_legislacao") or {}).get("nome") or ""
        ).strip()
        if detail_type:
            return detail_type
        search_type = ((doc.get("tipo_legislacao") or {}).get("nome") or "").strip()
        if search_type:
            return search_type
        return ""

    def _build_search_url(
        self, year: int, page: int = 1, norm_type_id: int | None = None
    ) -> str:
        """Build search URL from arguments (no shared state mutation)."""
        params = {
            "ano": year,
            "ordenarPor": "data",
            "qtd_por_pagina": 100,
            "page": page,
        }
        if norm_type_id is not None:
            params["tipo_legislacao"] = norm_type_id
        return f"{self.base_url}?{urlencode(params)}"

    def _build_document_url(
        self, doc_id: int | str, tipo_id: int | None, numero: str
    ) -> str:
        """Build the canonical public document URL used for resume keys."""
        norm_url_suffix = _TYPE_ID_TO_SUFFIX.get(tipo_id, "")
        if norm_url_suffix == "constituicao-estadual":
            return f"{_SITE_URL}/pesquisa_legislacao/{doc_id}/{norm_url_suffix}"
        return f"{_SITE_URL}/pesquisa_legislacao/{doc_id}/{norm_url_suffix}-{numero}"

    def _clean_markdown(self, text_markdown: str) -> str:
        """Clean markdown text."""
        return text_markdown.replace("javascript:print()", "").strip()

    def _normalize_for_compare(self, text: str) -> str:
        """Normalize text for body-vs-summary comparison."""
        normalized = _SPECIAL_RE.sub("", text.lower())
        return _SPACE_RE.sub("", normalized).strip()

    def _ensure_html_document(self, html_content: str) -> str:
        """Return a standalone HTML document string."""
        html_string = html_content.strip()
        if re.search("<(?:!doctype|html|body)\\b", html_string, re.IGNORECASE):
            return html_string
        return wrap_html(html_string)

    def _extract_pdf_link(self, soup: BeautifulSoup | Tag) -> str:
        """Extract a downloadable PDF link from the HTML, if present."""
        pdf_link = ""
        for a_tag in soup.find_all("a", href=True):
            img = a_tag.find("img", src=re.compile("/assets/ver_lei\\.jpg$"))
            if img:
                pdf_link = urljoin(_SITE_URL, a_tag["href"])
                a_tag.decompose()
                break
        if pdf_link:
            return pdf_link
        baixar_div = soup.find("div", class_="botao-baixar")
        if baixar_div:
            a_tag = baixar_div.find("a", href=True)
            if a_tag:
                pdf_link = urljoin(_SITE_URL, a_tag["href"])
            baixar_div.decompose()
        return pdf_link

    @staticmethod
    def _direct_rows(table: Tag) -> list[Tag]:
        """Return direct `<tr>` children of *table* (looking through direct `<tbody>`)."""
        rows: list[Tag] = []
        for child in table.children:
            if not isinstance(child, Tag):
                continue
            if child.name == "tr":
                rows.append(child)
            elif child.name in ("tbody", "thead", "tfoot"):
                rows.extend(
                    tr
                    for tr in child.children
                    if isinstance(tr, Tag) and tr.name == "tr"
                )
        return rows

    @classmethod
    def _is_layout_table(cls, table: Tag) -> bool:
        """Identify Goiás's 1-row outer layout wrapper tables without touching annex tables."""
        rows = cls._direct_rows(table)
        if len(rows) != 1:
            return False
        cells = [
            c for c in rows[0].children if isinstance(c, Tag) and c.name in ("td", "th")
        ]
        if not cells:
            return True
        if len(cells) == 1:
            return True
        if len(cells) == 2:
            empties = [
                not cell.get_text(strip=True) and not cell.find("img") for cell in cells
            ]
            return any(empties)
        return False

    @classmethod
    def _unwrap_layout_tables(cls, root: Tag) -> None:
        """Unwrap Goiás's 1-row layout wrapper tables while preserving real data tables."""
        for table in list(root.find_all("table")):
            if table.decomposed or not cls._is_layout_table(table):
                continue
            rows = cls._direct_rows(table)
            for tr in rows:
                for cell in list(tr.children):
                    if not isinstance(cell, Tag) or cell.name not in ("td", "th"):
                        continue
                    if not cell.get_text(strip=True) and not cell.find("img"):
                        cell.decompose()
                    else:
                        cell.name = "div"
                tr.unwrap()
            for child in list(table.children):
                if isinstance(child, Tag) and child.name in ("tbody", "thead", "tfoot"):
                    child.unwrap()
            table.unwrap()

    @classmethod
    def _strip_header_table(cls, root: Tag) -> None:
        """Remove Goiás's letterhead table while preserving any norm epígrafe inside it."""
        header_table = root.find("table")
        if not isinstance(header_table, Tag):
            return
        header_text = " ".join(header_table.get_text(" ", strip=True).split())
        if "GOVERNO DO ESTADO DE GOIÁS" not in header_text.upper():
            return
        if len(header_text) > 400:
            return
        if not _EPIGRAFE_RE.search(header_text):
            header_table.decompose()
            return
        # Preserve the norm epígrafe (e.g. Lei 17.928/2012) while removing letterhead chrome.
        for img in list(header_table.find_all("img")):
            img.decompose()
        for el in list(header_table.find_all(["strong", "b", "font", "span", "p"])):
            if el.decomposed:
                continue
            txt = " ".join(el.get_text(" ", strip=True).split())
            if txt and not _EPIGRAFE_RE.search(txt) and _HEADER_CHROME_RE.search(txt):
                el.decompose()
        for s in list(header_table.find_all(string=_HEADER_CHROME_RE)):
            cleaned = _HEADER_CHROME_RE.sub("", str(s)).strip()
            if cleaned:
                s.replace_with(cleaned)
            else:
                s.extract()

    @staticmethod
    def _strip_chrome_images_and_winzip(root: Tag) -> None:
        """Remove decorative Goiás crest icons and WinZip annex download chrome."""
        for img in list(root.find_all("img")):
            if img.decomposed:
                continue
            src = (img.get("src") or "").lower()
            if "winzip.jpg" in src or "brasao_goias" in src:
                parent = img.parent
                img.decompose()
                if (
                    isinstance(parent, Tag)
                    and parent.name == "a"
                    and not parent.get_text(strip=True)
                    and not parent.find("img")
                ):
                    parent.decompose()
        for el in list(root.find_all(["a", "p", "div", "span", "strong", "b", "font"])):
            if el.decomposed:
                continue
            txt = " ".join(el.get_text(" ", strip=True).split())
            if _WINZIP_TEXT_RE.match(txt) and not el.find("img"):
                el.decompose()

    def _prepare_inline_html(self, html_content: str) -> tuple[str, str, bool]:
        """Clean inline HTML and return ``(clean_html, pdf_link, redirect)``."""
        raw_html = self._ensure_html_document(html_content)
        soup = BeautifulSoup(raw_html, "lxml")
        if _REDIRECT_MARKER.lower() in soup.get_text(" ", strip=True).lower():
            return ("", "", True)
        pdf_link = self._extract_pdf_link(soup)
        # Some API contents prepend an empty HTML shell; keep the body with the text.
        root = max(
            soup.find_all("body"),
            key=lambda body: len(body.get_text(strip=True)),
            default=soup,
        )
        for tag_name in ["meta", "link", "title", "input", "label"]:
            for tag in root.find_all(tag_name):
                tag.decompose()
        for tag in root.find_all(id=re.compile("ficha-tecnica", re.IGNORECASE)):
            tag.decompose()
        for tag in root.find_all(class_=re.compile("ficha-tecnica", re.IGNORECASE)):
            tag.decompose()
        self._strip_header_table(root)
        self._strip_chrome_images_and_winzip(root)
        self._unwrap_layout_tables(root)
        clean_norm_soup(
            root,
            unwrap_links=True,
            remove_disclaimers=True,
            remove_images=False,
            remove_empty_tags=True,
            unwrap_fonts=True,
            strip_styles=True,
            remove_style_tags=True,
            remove_script_tags=True,
        )
        return (self._ensure_html_document(str(root).strip()), pdf_link, False)

    def _strip_summary_from_markdown(self, md: str, summary: str) -> str:
        """Remove a standalone ementa paragraph even when soft hyphens/punctuation spacing differ."""
        if not summary:
            return md
        stripped = remove_summary_from_markdown(md, summary)
        if stripped != md:
            return stripped
        norm_summary = self._normalize_for_compare(summary.replace("\xad", ""))
        if not norm_summary:
            return md
        paragraphs = md.split("\n\n")
        filtered = [
            p
            for p in paragraphs
            if self._normalize_for_compare(re.sub(r"[*_`]+", "", p).replace("\xad", ""))
            != norm_summary
        ]
        if len(filtered) != len(paragraphs):
            return "\n\n".join(filtered)
        return md

    def _should_use_pdf_fallback(
        self, text_markdown: str, summary: str, *, has_pdf_link: bool
    ) -> bool:
        """Return True when inline HTML is likely only a summary/download stub."""
        if not has_pdf_link:
            return False
        body_text = self._normalize_for_compare(text_markdown)
        summary_text = self._normalize_for_compare(summary)
        if summary_text:
            body_text = body_text.replace(summary_text, "").strip()
        return len(body_text) < _PDF_STUB_MIN_BODY_LEN

    def _build_doc_info(
        self, doc: dict, doc_detail: dict, *, norm_type: str, situation: str
    ) -> dict:
        """Build the normalized document metadata dict."""
        title_type = norm_type or (doc_detail.get("tipo_legislacao") or {}).get(
            "nome", "Norma"
        )
        return {
            "id": doc_detail["id"],
            "norm_number": doc_detail.get("numero", ""),
            "year": doc_detail.get("ano", doc.get("ano", "")),
            "type": norm_type,
            "situation": situation,
            "date": doc_detail.get("data_legislacao", ""),
            "title": f"{title_type} {doc_detail.get('numero', '')} de {doc_detail.get('ano', doc.get('ano', ''))}".strip(),
            "summary": _decode_cp1252_c1((doc_detail.get("ementa") or "").strip()),
        }

    async def _process_pdf_link(
        self, link: str, doc_id: str | int, doc_info: dict
    ) -> Document | None:
        """Download a PDF fallback, convert it to markdown, and populate doc_info."""
        pdf_link = urljoin(_SITE_URL, link).strip()
        if not pdf_link:
            logger.error(f"Missing PDF link for doc ID: {doc_id}")
            return None
        original_document_url = doc_info.get("document_url", "")
        text_markdown, raw, content_ext = await self.download(pdf_link)
        text_markdown = self._clean_markdown(text_markdown)
        result = Document(
            **{
                **doc_info,
                "document_url": pdf_link,
                "text_markdown": text_markdown,
                "raw": raw,
                "raw_ext": content_ext or ".pdf",
            }
        )
        summary = doc_info.get("summary", "")
        if summary and result.text_markdown:
            result.text_markdown = remove_summary_from_markdown(
                result.text_markdown, summary
            )
        if original_document_url:
            result.document_url = original_document_url
            result.pdf_link = pdf_link
        else:
            result.document_url = pdf_link
        return result

    async def fetch(self, doc: dict) -> Document | None:
        """Get document info from the detail API and choose HTML or PDF content."""
        doc_id = doc["id"]
        numero = doc.get("numero", "")
        tipo_legislacao = doc.get("tipo_legislacao", {})
        tipo_id = tipo_legislacao.get("id")
        html_link = self._build_document_url(doc_id, tipo_id, numero)
        api_url = f"{_DETAIL_API_URL}/{doc_id}"
        response = await self.http.request(api_url)
        doc_detail = response.json()
        situation = (doc_detail.get("estado_legislacao") or {}).get("nome", "") or (
            doc.get("estado_legislacao") or {}
        ).get("nome", "")
        norm_type = self._resolve_norm_type(doc, doc_detail)
        doc_info = self._build_doc_info(
            doc, doc_detail, norm_type=norm_type, situation=situation
        )
        doc_info["document_url"] = html_link
        pdf_link = ""
        html_reason = ""
        if doc_detail.get("conteudo"):
            clean_html, pdf_link, has_redirect = self._prepare_inline_html(
                doc_detail["conteudo"]
            )
            if has_redirect:
                raise ValueError(
                    "Document redirects via 'Clique no link abaixo' (content not inline)"
                )
            text_markdown = self._clean_markdown(
                await self.markdown(html=clean_html, base_url=doc_info["document_url"])
            )
            html_valid, html_reason = valid_markdown(text_markdown)
            if html_valid and (
                not self._should_use_pdf_fallback(
                    text_markdown, doc_info["summary"], has_pdf_link=bool(pdf_link)
                )
            ):
                doc_info["text_markdown"] = self._strip_summary_from_markdown(
                    text_markdown, doc_info["summary"]
                )
                doc_info["raw"] = self._ensure_html_document(
                    doc_detail["conteudo"]
                ).encode("utf-8")
                doc_info["raw_ext"] = ".html"
        if not doc_info.get("text_markdown"):
            if pdf_link:
                return await self._process_pdf_link(pdf_link, doc_id, doc_info)
            raise ValueError(
                html_reason or "No usable inline HTML content or PDF fallback"
            )
        error_msg = "doesn't work properly without JavaScript enabled"
        if error_msg.lower() in doc_info["text_markdown"].lower():
            logger.warning(f"Invalid doc ID: {doc_id}. Year: {doc_detail['ano']}")
            raise ValueError("Document contains JavaScript error message")
        return Document(**doc_info)

    async def _fetch_search_page(self, year: int, page: int) -> list[dict]:
        """Fetch a specific page from the search API."""
        url = self._build_search_url(year, page=page)
        response = await self.http.request(url)
        data = response.json()
        return data.get("resultados", [])

    async def list_documents(self, year: int) -> AsyncIterator[dict]:
        data = (await self.http.request(self._build_search_url(year, page=1))).json()

        async def fetch_page(page: int) -> list[dict]:
            return (
                data.get("resultados", [])
                if page == 1
                else await self._fetch_search_page(year, page)
            )

        async for ref in self.paginate(
            fetch_page, calc_pages(data["total_resultados"], 100)
        ):
            norm_type = ref.get("tipo_legislacao", {})
            yield {
                **ref,
                "year": year,
                "type": norm_type.get("nome", ""),
                "title": f"{norm_type.get('nome', '')} {ref.get('numero', '')} de {ref.get('ano', year)}",
                "document_url": self._build_document_url(
                    ref["id"], norm_type.get("id"), ref.get("numero", "")
                ),
                "situation": DEFAULT_VALID_SITUATION,
            }
