"""Santa Catarina state legislation scraper.

Scrapes the ALESC legislation system at ``leis.alesc.sc.gov.br``.
The site is a NextJS app with server-rendered HTML.

Search: GET ``/legislativo?ano={year}&page={page}`` (legislative acts)
        GET ``/executivo?ano={year}&page={page}`` (executive decrees)
Document: GET ``/ato-normativo/{path}/{id}`` returns full HTML content.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from legisbr.core.convert import strip_html_chrome
from legisbr.core.document import Document
from legisbr.core.scraper import (
    DEFAULT_INVALID_SITUATION,
    DEFAULT_VALID_SITUATION,
    BaseScraper,
)

_RE_TYPE_FROM_TITLE = re.compile(
    "^(ATO DA MESA|LEI COMPLEMENTAR|LEI|DECRETO LEGISLATIVO|DECRETO-LEI|DECRETO|EMENDA CONSTITUCIONAL|RESOLUÇÃO|PORTARIA|INSTRUÇÃO NORMATIVA)",
    re.IGNORECASE,
)
_LOWERCASE_TYPE_WORDS = frozenset({"da", "de", "do", "das", "dos", "e"})
_RE_HEADER_REVOKED = re.compile(r"\brevogad[ao]\s+pel[ao]\b", re.IGNORECASE)
_RE_BODY_START = re.compile(r"^(?:art\.?\s*1|artigo\s+1)\b", re.IGNORECASE)
# Executive titles are "<CODE>-<number> <date>" (INT-000001); names confirmed from the
# pages' TipoNorma meta ("Instrução Normativa - INT"). Never store the code as the type.
_RE_CODE_TITLE = re.compile("^([A-Z]{2,4})-\\d+")
_EXEC_CODES = {
    "DEC": "Decreto",
    "DEL": "Decreto-Lei",
    "INT": "Instrução Normativa",
    "MPV": "Medida Provisória",
    "ONI": "Orientação Normativa",
    "PRT": "Portaria",
    "PRV": "Provimento",
    "RES": "Resolução",
}
TYPES = {"Legislativo": "legislativo", "Executivo": "executivo"}
SITUATIONS: list[str] = []


class SantaCatarinaScraper(BaseScraper):
    """Scraper for Santa Catarina legislation (leis.alesc.sc.gov.br).

    Year start (earliest on source): 1946
    """

    types = TYPES
    situations = SITUATIONS
    name = "SANTA_CATARINA"
    base_url = "https://leis.alesc.sc.gov.br"

    def _format_search_url(self, norm_type_id: str, year: int) -> str:
        return f"{self.base_url}/{norm_type_id}?ano={year}"

    @staticmethod
    def _title_case_type(raw: str) -> str:
        words = raw.split()
        return " ".join(
            word.lower()
            if idx > 0 and word.lower() in _LOWERCASE_TYPE_WORDS
            else word.title()
            for idx, word in enumerate(words)
        )

    @classmethod
    def _infer_type(cls, title: str) -> str:
        if code := _RE_CODE_TITLE.match(title):
            if code[1] not in _EXEC_CODES:
                raise ValueError(f"Unknown SC act code {code[1]!r} in {title!r}")
            return _EXEC_CODES[code[1]]
        m = _RE_TYPE_FROM_TITLE.match(title)
        if m:
            return cls._title_case_type(m.group(1))
        prefix_match = re.match(
            "(.+?)(?:\\s+(?:n[º°o]|n\\.|número)\\s*\\d|\\s+\\d)", title, re.IGNORECASE
        )
        raw_type = prefix_match.group(1).strip() if prefix_match else title
        return cls._title_case_type(raw_type) if raw_type.isupper() else raw_type

    @staticmethod
    def _find_meta_content(soup: BeautifulSoup, name: str) -> str:
        target = name.casefold()
        for meta in soup.find_all("meta"):
            meta_name = str(meta.get("name") or "").strip().casefold()
            if meta_name == target:
                return " ".join(str(meta.get("content") or "").split())
        return ""

    @classmethod
    def _extract_summary(cls, soup: BeautifulSoup) -> str:
        meta_ementa = cls._find_meta_content(soup, "Ementa")
        if meta_ementa:
            return meta_ementa
        ementa_p = soup.find("p", class_=re.compile(r"\bementa\b", re.IGNORECASE))
        if ementa_p:
            return " ".join(ementa_p.get_text(" ", strip=True).split())
        return ""

    @classmethod
    def _extract_situation(cls, soup: BeautifulSoup) -> str:
        vigente = cls._find_meta_content(soup, "Vigente").casefold()
        if vigente == "sim":
            return "Vigente"
        if vigente in {"nao", "não"}:
            return "Não vigente"
        for el in soup.find_all(["p", "div", "span", "td"], limit=40):
            if el.find(["p", "div"]):
                continue
            text = " ".join(el.get_text(" ", strip=True).split())
            if not text:
                continue
            if _RE_BODY_START.match(text):
                break
            if len(text) < 250 and _RE_HEADER_REVOKED.search(text):
                if not re.search(r"\bfica(?:m)?\s+revogad", text, re.IGNORECASE):
                    return DEFAULT_INVALID_SITUATION
        return "Não consta"

    async def fetch(self, doc_info: dict) -> Document:
        """Fetch the server-rendered document HTML and convert to markdown."""
        doc_info = dict(doc_info)
        if not doc_info.get("constitution"):
            doc_info["type"] = self._infer_type(" ".join(doc_info["title"].split()))
        document_url = doc_info["document_url"]
        response = await self.http.request(document_url)
        soup = response.soup()
        summary = self._extract_summary(soup)
        if summary and not doc_info.get("summary"):
            doc_info["summary"] = summary
        doc_info["situation"] = self._extract_situation(soup)
        strip_html_chrome(soup)
        return await self.fetch_html(
            doc_info, str(soup), document_url, response.content, ".html"
        )

    async def list_documents(self, year: int | None) -> AsyncIterator[dict]:
        # The constitution is only linked from every page's footer; list it once.
        if year == self.years.start:
            yield {
                "year": 1989,
                "type": "Constituição Estadual",
                "title": "Constituição do Estado de Santa Catarina",
                "document_url": f"{self.base_url}/ato-normativo/legislativo/52785",
                "situation": DEFAULT_VALID_SITUATION,
                "constitution": True,
            }
        async for ref in super().list_documents(year):
            yield ref

    async def list_type(
        self, year: int, norm_type: str, path: str
    ) -> AsyncIterator[dict]:
        seen = set()
        page = 1
        while True:
            soup = (
                await self.http.request(
                    f"{self.base_url}/{path}?ano={year}&page={page}"
                )
            ).soup()
            if "Resultados da Pesquisa" not in soup.get_text():
                raise ValueError(f"Malformed SC listing page {page}")
            links = soup.select('.card-title a[href*="/ato-normativo/"]')
            new_links = [link for link in links if link.get("href") not in seen]
            if not new_links:
                break
            for link in new_links:
                seen.add(link["href"])
                title = link.get_text(strip=True)
                if title:
                    yield {
                        "year": year,
                        "type": norm_type,
                        "title": title,
                        "document_url": urljoin(self.base_url, link["href"]),
                        "situation": DEFAULT_VALID_SITUATION,
                    }
            page += 1
