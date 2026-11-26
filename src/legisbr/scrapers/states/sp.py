from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

from legisbr.core.convert import calc_pages
from legisbr.core.document import DocRef, Document, clean_title
from legisbr.core.query import (
    Query,
    SearchCapabilities,
    UnsupportedQueryError,
    match_type_entry,
    matches_query,
)
from legisbr.core.scraper import DEFAULT_VALID_SITUATION, BaseScraper

API_URL = "https://baleg-api-prd.al.sp.gov.br"
SITE_URL = "https://www.al.sp.gov.br"
PAGE_SIZE = 500
_HOME_LINKS = {"http://www.al.sp.gov.br", "https://www.al.sp.gov.br"}

# Ids from the API's /situacoesNormas endpoint.
VALID_SITUATIONS = {"Sem revogação expressa": 1}
INVALID_SITUATIONS = {
    "Declarada inconstitucional": 2,
    "Eficácia suspensa": 3,
    "Eficácia exaurida": 4,
    "Revogada": 5,
    "Anulada": 6,
}
SITUATIONS = {**VALID_SITUATIONS, **INVALID_SITUATIONS}
# idTipoNorma values used by the site's search form; the API returns the name in "tipo".
TYPES = {
    "Constituição Estadual": 59,
    "Emenda Constitucional": 55,
    "Decreto": 3,
    "Decreto-Lei": 25,
    "Decreto-Lei Complementar": 1,
    "Decreto Legislativo": 28,
    "Lei": 9,
    "Lei Complementar": 2,
    "Resolução": 14,
    "Resolução da Alesp": 19,
    "Ato da Mesa": 21,
    "Ato do Presidente": 22,
    "Decisão da Mesa": 12,
    "Decisão do Presidente": 23,
}


class SaoPauloAlespScraper(BaseScraper):
    """São Paulo legislation from ALESP (Assembleia Legislativa do Estado de São Paulo), https://www.al.sp.gov.br/legislacao

    Year start (earliest on source): 1835

    Example listing request: https://baleg-api-prd.al.sp.gov.br/normas?ano=2012&idsSituacao=1&page=0&size=500&sort=desc
    """

    name = "SAO_PAULO"
    base_url = SITE_URL
    types = TYPES
    situations = SITUATIONS
    capabilities = SearchCapabilities(
        native_year=True,
        native_type=True,
        native_number=True,
        native_situation=True,
    )

    async def list_documents(self, year: int) -> AsyncIterator[dict]:
        # Listing rows lack the situation; filtering by it avoids a detail request per norm.
        for situation, situation_id in SITUATIONS.items():
            try:
                async for ref in self._list_situation(year, situation, situation_id):
                    yield ref
            except Exception as exc:
                await self.error({"year": year, "situation": situation}, exc)

    async def _search(self, year: int, situation_id: int, page: int) -> dict[str, Any]:
        query = urlencode(
            {
                "ano": year,
                "idsSituacao": situation_id,
                "page": page,
                "size": PAGE_SIZE,
                "sort": "desc",
            }
        )
        data = (await self.http.request(f"{API_URL}/normas?{query}")).json()
        if not (
            isinstance(data, dict)
            and isinstance(data.get("content"), list)
            and isinstance(data.get("totalElements"), int)
        ):
            raise ValueError(
                f"Malformed ALESP listing: year {year}, situation {situation_id}, page {page}"
            )
        return data

    @staticmethod
    def _clean_summary(raw: object) -> str:
        text = str(raw or "").strip()
        if not text:
            return ""
        if "<" in text and ">" in text:
            from bs4 import BeautifulSoup

            text = BeautifulSoup(text, "lxml").get_text()
        return " ".join(text.split())

    @staticmethod
    def _row_to_ref(
        row: dict[str, Any], *, year: int | None, situation: str
    ) -> dict[str, Any]:
        resolved_year = year
        if resolved_year is None:
            raw_year = row.get("ano")
            if raw_year is not None:
                resolved_year = int(raw_year)
            else:
                match = re.search(
                    r"\b((?:18|19|20)\d{2})\b", str(row.get("data") or "")
                )
                resolved_year = int(match.group(1)) if match else 0
        return {
            "year": resolved_year,
            "type": row["tipo"],
            "title": row["nomeNorma"],
            "summary": SaoPauloAlespScraper._clean_summary(row.get("dsEmenta")),
            "situation": situation,
            "number": row.get("nuNorma"),
            "date": row.get("data"),
            "publication_date": row.get("dataPublicacao"),
            "publication_url": row.get("urlPublicacaoDO"),
            "document_url": f"{SITE_URL}/norma/{row['idNorma']}",
            # Same preference as the site's result list.
            "text_url": row.get("urlTextoAtualizado")
            or row.get("urlIntegraListaPesquisa"),
        }

    async def _list_situation(
        self, year: int, situation: str, situation_id: int
    ) -> AsyncIterator[dict]:
        first = await self._search(year, situation_id, 0)

        async def fetch_page(page: int) -> list[dict]:
            data = first if page == 0 else await self._search(year, situation_id, page)
            return data["content"]

        pages = calc_pages(first["totalElements"], PAGE_SIZE)
        async for row in self.paginate(fetch_page, pages - 1, start=0):
            yield self._row_to_ref(row, year=year, situation=situation)

    async def search_refs(
        self,
        query: Query,
        *,
        strict_native: bool = False,
        on_error: Callable[[DocRef, Exception], Awaitable[None] | None] | None = None,
    ) -> AsyncIterator[DocRef]:
        """Targeted ALESP REST search without looping every situation."""
        if strict_native and not self.capabilities.supports_query(query):
            raise UnsupportedQueryError(f"{self.name} does not support {query!r}")
        if query.limit == 0:
            return

        base_params: dict[str, Any] = {"sort": "desc"}
        if query.type is not None:
            matched_type = match_type_entry(TYPES, query.type)
            if matched_type is None:
                return
            base_params["idTipoNorma"] = matched_type[1]
        if query.number is not None:
            base_params["numero"] = query.number
        situation_label = DEFAULT_VALID_SITUATION
        if query.situation is not None:
            matched_sit = match_type_entry(SITUATIONS, query.situation)
            if matched_sit is not None:
                situation_label, base_params["idsSituacao"] = matched_sit

        page_size = (
            min(PAGE_SIZE, max(20, query.limit))
            if query.limit is not None and query.text is None
            else PAGE_SIZE
        )
        target_years: list[int | None] = (
            [None]
            if query.years is None
            else ([query.years] if isinstance(query.years, int) else list(query.years))
        )

        yielded = 0
        for year in target_years:
            page = 0
            total_pages = 1
            while page < total_pages:
                params = {**base_params, "page": page, "size": page_size}
                if year is not None:
                    params["ano"] = year
                url = f"{API_URL}/normas?{urlencode(params)}"
                try:
                    data = (await self.http.request(url)).json()
                    if not (
                        isinstance(data, dict)
                        and isinstance(data.get("content"), list)
                        and isinstance(data.get("totalElements"), int)
                    ):
                        raise ValueError(f"Malformed ALESP search response: {url}")
                    total_pages = calc_pages(data["totalElements"], page_size)
                except Exception as exc:
                    await self._report_search_error(
                        {"year": year, "document_url": url}, exc, on_error
                    )
                    break
                for row in data["content"]:
                    ref = self._row_to_ref(
                        row,
                        year=year,
                        situation=row.get("situacao") or situation_label,
                    )
                    ref["title"] = clean_title(ref["title"])
                    if not matches_query(ref, query):
                        continue
                    yield ref
                    yielded += 1
                    if query.limit is not None and yielded >= query.limit:
                        return
                page += 1

    async def fetch(self, ref: dict) -> Document:
        url = ref["text_url"]
        if not url:
            raise ValueError("ALESP publishes no text for this norm")
        if url.lower().endswith(".pdf"):
            markdown, raw, ext = await self.download(url)
        else:
            response = await self.http.request(url)
            # soup(), not text: pages declare their latin-1 charset only in a meta tag.
            soup = response.soup()
            for link in soup.find_all("a"):
                href = link.get("href", "").rstrip("/")
                link_text = link.get_text(strip=True).casefold()
                if href in _HOME_LINKS or link_text in {
                    "ficha informativa",
                    "texto compilado",
                }:
                    link.decompose()
            markdown = await self.markdown(html=str(soup), base_url=url)
            raw, ext = response.content, ".html"
        return Document(**ref, text_markdown=markdown, raw=raw, raw_ext=ext)
