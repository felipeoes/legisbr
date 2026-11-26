from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from legisbr.core.convert import bytes_to_markdown, calc_pages, valid_markdown
from legisbr.core.document import Document
from legisbr.core.scraper import DEFAULT_VALID_SITUATION, BaseScraper

logger = logging.getLogger(__name__)

TYPES = {"Lei Ordinária": "ordinaria", "Lei Complementar": "complementar"}
VALID_SITUATIONS = ["Não consta"]
INVALID_SITUATIONS = []
SITUATIONS = {s: s for s in VALID_SITUATIONS + INVALID_SITUATIONS}
_RESULT_COUNT_RE = re.compile(r"(\d[\d.]*)\s+resultado", re.IGNORECASE)
_PAGE_INFO_COUNT_RE = re.compile(r"\bde\s+(\d[\d.]*)", re.IGNORECASE)
_PAGE_INFO_TOTAL_PAGES_RE = re.compile(r"p[aá]gina\s+\d+\s+de\s+(\d+)", re.IGNORECASE)
_PAGE_PARAM_RE = re.compile(r"pagPaginaAtual=(\d+)")
_EMPTY_RESULT_RE = re.compile(
    r"nenhum\s+resultado\s+encontrado|\b0\s+resultados?\b", re.IGNORECASE
)


class TocantinsScraper(BaseScraper):
    """Webscraper for Tocantins state legislation website (https://www.al.to.leg.br/)

    Year start (earliest on source): 1989

    Example search request: POST to https://www.al.to.leg.br/legislacaoEstadual
    """

    types = TYPES
    situations = SITUATIONS
    name = "TOCANTINS"
    base_url = "https://www.al.to.leg.br"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.search_url = f"{self.base_url}/legislacaoEstadual"

    def _format_search_url(self, _norm_type_id: str, _year: int, _page: int = 1) -> str:
        """Format url for search request - returns the search URL"""
        return self.search_url

    def _format_search_payload(
        self, norm_type_id: str, year: int, page: int = 1
    ) -> dict:
        """Format payload for search request"""
        return {
            "pagPaginaAtual": str(page),
            "documento.texto": "",
            "documento.numero": "",
            "documento.ano": str(year),
            "documento.dataInicio": "",
            "documento.dataFinal": "",
            "documento.tipo": norm_type_id,
        }

    async def _get_docs_links_page(
        self, norm_type_id: str, year: int, page: int = 1
    ) -> list[dict]:
        """Get document links from a single page."""
        payload = self._format_search_payload(norm_type_id, year, page)
        response = await self.http.request(self.search_url, method="POST", data=payload)
        soup = BeautifulSoup(response.content, "lxml")
        docs = self._extract_docs_from_soup_obj(soup)
        if not docs:
            total_count = self._extract_total_count(soup)
            if total_count and total_count > 0:
                raise ValueError(
                    f"Tocantins listing page {page} reported {total_count} results but parsed 0 items"
                )
            if soup.get_text(strip=True) and not self._is_empty_results(soup):
                raise ValueError(
                    f"Tocantins listing page {page} returned non-empty HTML with 0 parsed items"
                )
        return docs

    def _extract_docs_from_soup(self, html_content: bytes) -> list[dict]:
        """Extract document information from HTML content."""
        return self._extract_docs_from_soup_obj(BeautifulSoup(html_content, "lxml"))

    def _extract_docs_from_soup_obj(self, soup: BeautifulSoup) -> list[dict]:
        """Extract document entries supporting both current and legacy portal layouts."""
        articles = soup.find_all("article", class_="resultado")
        if articles:
            return self._extract_articles(articles)
        return self._extract_legacy_rows(soup)

    def _extract_articles(self, articles: list[Tag]) -> list[dict]:
        """Parse the redesigned `<article class="resultado">` cards."""
        docs: list[dict] = []
        for article in articles:
            title_heading = article.find(
                ["h2", "h3", "h4"], class_=re.compile(r"resultado-titulo")
            ) or article.find(["h2", "h3", "h4"])
            if not isinstance(title_heading, Tag):
                continue
            link_tag = title_heading.find("a", href=True)
            if not isinstance(link_tag, Tag):
                continue
            title = " ".join(link_tag.get_text(" ", strip=True).split())
            title_href = link_tag.get("href", "")
            if not title or not isinstance(title_href, str):
                continue

            pdf_href = ""
            actions = article.find(class_=re.compile(r"resultado-acoes"))
            if isinstance(actions, Tag):
                action_link = actions.find("a", href=True)
                if isinstance(action_link, Tag) and isinstance(
                    action_link.get("href"), str
                ):
                    pdf_href = action_link["href"]
            if not pdf_href:
                pdf_href = title_href
            pdf_link = urljoin(self.base_url, pdf_href) if pdf_href else ""

            date_text = ""
            meta = article.find(class_=re.compile(r"resultado-meta"))
            time_tag = (
                meta.find("time") if isinstance(meta, Tag) else article.find("time")
            )
            if isinstance(time_tag, Tag):
                dt = time_tag.get("datetime")
                date_text = (
                    dt.strip()
                    if isinstance(dt, str) and dt.strip()
                    else time_tag.get_text(" ", strip=True)
                )

            summary = ""
            summary_tag = article.find(class_=re.compile(r"resultado-ementa"))
            if isinstance(summary_tag, Tag):
                summary = " ".join(summary_tag.get_text(" ", strip=True).split())

            docs.append(
                {
                    "title": title,
                    "summary": summary,
                    "date": date_text,
                    "situation": VALID_SITUATIONS[0],
                    "_pdf_link": pdf_link,
                }
            )
        return docs

    def _extract_legacy_rows(self, soup: BeautifulSoup) -> list[dict]:
        """Parse legacy `<div class="row">` -> `<h4><a>` markup."""
        docs: list[dict] = []
        for row in soup.find_all("div", class_="row"):
            try:
                title_link = row.find("h4")
                if not title_link or not isinstance(title_link, Tag):
                    continue
                link_tag = title_link.find("a")
                if not link_tag or not isinstance(link_tag, Tag):
                    continue
                title = link_tag.get_text(strip=True)
                doc_link = link_tag.get("href", "")
                if not isinstance(doc_link, str):
                    continue
                if not doc_link.startswith("http"):
                    doc_link = f"{self.base_url}{doc_link}"
                date_text = ""
                for small in row.find_all("small"):
                    if isinstance(small, Tag):
                        text = small.get_text(strip=True)
                        if "Data:" in text:
                            date_text = text.replace("Data:", "").strip()
                            date_text = date_text.replace("|", "").strip()
                            break
                summary = ""
                em_tag = row.find("em")
                if em_tag and isinstance(em_tag, Tag):
                    strong_tag = em_tag.find("strong")
                    if strong_tag and isinstance(strong_tag, Tag):
                        summary = strong_tag.get_text(strip=True)
                pdf_link = ""
                pdf_link_tag = row.find("a", {"title": "Download"})
                if pdf_link_tag and isinstance(pdf_link_tag, Tag):
                    pdf_href = pdf_link_tag.get("href", "")
                    if isinstance(pdf_href, str):
                        pdf_link = (
                            pdf_href
                            if pdf_href.startswith("http")
                            else f"{self.base_url}{pdf_href}"
                        )
                docs.append(
                    {
                        "title": title,
                        "summary": summary,
                        "date": date_text,
                        "situation": VALID_SITUATIONS[0],
                        "_pdf_link": pdf_link,
                    }
                )
            except Exception as e:
                logger.error(f"Error extracting document from box: {e}")
                continue
        return docs

    @staticmethod
    def _is_empty_results(soup: BeautifulSoup) -> bool:
        """Return True when the page explicitly reports zero search results."""
        if soup.find(class_="busca-vazio") is not None:
            return True
        for el in soup.find_all(["div", "p", "section"]):
            txt = el.get_text(" ", strip=True)
            if len(txt) < 300 and _EMPTY_RESULT_RE.search(txt):
                return True
        return False

    @staticmethod
    def _extract_total_count(soup: BeautifulSoup) -> int | None:
        """Extract the total result count from the search results banner."""
        contagem = soup.find(class_="busca-contagem")
        if isinstance(contagem, Tag):
            match = _RESULT_COUNT_RE.search(contagem.get_text(" ", strip=True))
            if match:
                return int(match.group(1).replace(".", ""))
        pag_info = soup.find(class_="paginacao-info")
        if isinstance(pag_info, Tag):
            match = _PAGE_INFO_COUNT_RE.search(pag_info.get_text(" ", strip=True))
            if match:
                return int(match.group(1).replace(".", ""))
        for div in soup.find_all("div"):
            text = div.get_text(" ", strip=True)
            if "Registros encontrados" not in text:
                continue
            strong_tag = div.find("strong")
            if not strong_tag or not isinstance(strong_tag, Tag):
                continue
            digits = re.sub("\\D", "", strong_tag.get_text(strip=True))
            if digits:
                return int(digits)
        return None

    @staticmethod
    def _has_table_artifacts(text_markdown: str) -> bool:
        """Detect html-to-markdown outputs that are mostly broken pipe tables."""
        if not text_markdown:
            return False
        pipe_count = text_markdown.count("|")
        pipe_lines = sum(
            (1 for line in text_markdown.splitlines() if line.lstrip().startswith("|"))
        )
        return pipe_count >= 80 or pipe_lines >= 8

    @staticmethod
    def _normalize_table_markdown(text_markdown: str) -> str:
        """Collapse broken markdown tables into plain text lines."""
        cleaned_lines = []
        for raw_line in text_markdown.splitlines():
            line = raw_line.strip()
            if not line:
                cleaned_lines.append("")
                continue
            if re.fullmatch("\\|?(?:\\s*:?-{3,}:?\\s*\\|)+\\s*:?-{3,}:?\\s*\\|?", line):
                continue
            if "|" in line:
                cells = [cell.strip() for cell in line.strip("|").split("|")]
                cells = [cell for cell in cells if cell and set(cell) != {"-"}]
                if cells:
                    cleaned_lines.append(" ".join(cells))
                continue
            cleaned_lines.append(raw_line)
        normalized = "\n".join(cleaned_lines)
        normalized = re.sub("\\n{3,}", "\n\n", normalized)
        return normalized.strip()

    def _parse_total_pages(self, soup: BeautifulSoup, first_page_size: int) -> int:
        """Compute total pages from the first listing page, failing visibly on markup breaks."""
        total_count = self._extract_total_count(soup)
        if total_count == 0:
            return 0
        if total_count is not None and total_count > 0:
            if first_page_size == 0:
                raise ValueError(
                    f"Tocantins listing reported {total_count} results but parsed 0 items"
                )
            return max(1, calc_pages(total_count, first_page_size))
        if first_page_size == 0:
            if self._is_empty_results(soup) or not soup.get_text(strip=True):
                return 0
            raise ValueError(
                "Tocantins listing returned non-empty HTML with 0 parsed items and no empty-results marker"
            )
        nav = soup.find("nav", class_="paginacao") or soup.find(
            "nav", {"aria-label": re.compile(r"pagina", re.IGNORECASE)}
        )
        if not nav or not isinstance(nav, Tag):
            return 1
        pag_info = nav.find(class_="paginacao-info")
        if isinstance(pag_info, Tag):
            m = _PAGE_INFO_TOTAL_PAGES_RE.search(pag_info.get_text(" ", strip=True))
            if m:
                return max(1, int(m.group(1)))
        max_page = 1
        for link in nav.find_all("a", class_="page-link"):
            if not isinstance(link, Tag):
                continue
            text = link.get_text(strip=True)
            if text.isdigit():
                max_page = max(max_page, int(text))
            href = link.get("href")
            if isinstance(href, str):
                m = _PAGE_PARAM_RE.search(href)
                if m:
                    max_page = max(max_page, int(m.group(1)))
        return max_page

    async def _get_total_pages(self, norm_type_id: str, year: int) -> int:
        """Get total number of pages for a search."""
        payload = self._format_search_payload(norm_type_id, year, 1)
        response = await self.http.request(self.search_url, method="POST", data=payload)
        soup = BeautifulSoup(response.content, "lxml")
        first_page_docs = self._extract_docs_from_soup_obj(soup)
        return self._parse_total_pages(soup, len(first_page_docs))

    async def fetch(self, doc_info: dict) -> Document | None:
        """Get document data by downloading PDF and converting to markdown"""
        pdf_link = doc_info.get("_pdf_link")
        title = doc_info.get("title", "")
        if not pdf_link:
            raise ValueError("Missing PDF link")
        try:
            pdf_response = await self.http.request(pdf_link)
            pdf_content = pdf_response.content
            text_markdown = await bytes_to_markdown(
                pdf_content,
                "",
                pdf_link,
                self.http,
                self.ocr,
                pdf_executor=self.pdf_executor,
                pdf_workers=self.pdf_workers,
            )
            if text_markdown and self._has_table_artifacts(text_markdown):
                text_markdown = self._normalize_table_markdown(text_markdown)
            valid, reason = valid_markdown(text_markdown)
            if not valid:
                raise ValueError(f"Invalid markdown: {reason}")
            doc_info.update(
                {
                    "text_markdown": text_markdown,
                    "document_url": pdf_link,
                    "raw": pdf_content,
                    "raw_ext": ".pdf",
                }
            )
            return Document(**doc_info)
        except Exception as e:
            logger.error(f"Error processing document {title}: {e}")
            raise ValueError(f"Exception processing document: {e}")

    async def list_documents(self, year: int) -> AsyncIterator[dict]:
        if year == self.years.start:
            url = f"{self.base_url}/arquivos/documento_68367.PDF#dados"
            yield {
                "year": 1989,
                "type": "Constituição Estadual",
                "title": "Constituição Estadual de Tocantins",
                "document_url": url,
                "_pdf_link": url,
                "situation": DEFAULT_VALID_SITUATION,
            }
        async for ref in super().list_documents(year):
            yield ref

    async def list_type(
        self, year: int, norm_type: str, norm_type_id: str
    ) -> AsyncIterator[dict]:
        payload = self._format_search_payload(norm_type_id, year, 1)
        response = await self.http.request(self.search_url, method="POST", data=payload)
        soup = BeautifulSoup(response.content, "lxml")
        first_docs = self._extract_docs_from_soup_obj(soup)
        pages = self._parse_total_pages(soup, len(first_docs))
        if pages <= 0:
            return

        async def fetch_page(page: int) -> list[dict]:
            if page == 1:
                return first_docs
            return await self._get_docs_links_page(norm_type_id, year, page)

        async for ref in self.paginate(fetch_page, pages):
            yield {
                "year": year,
                "type": norm_type,
                "situation": DEFAULT_VALID_SITUATION,
                **ref,
                "document_url": ref["_pdf_link"],
            }
