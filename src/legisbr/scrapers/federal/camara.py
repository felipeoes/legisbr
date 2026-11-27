from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import date, timedelta
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from legisbr.core.convert import (
    bytes_to_markdown,
    calc_pages,
    clean_norm_soup,
    infer_type_from_title,
    remove_summary_element,
    remove_summary_from_markdown,
    strip_html_chrome,
)
from legisbr.core.document import DocRef, Document, clean_title
from legisbr.core.query import (
    Query,
    SearchCapabilities,
    UnsupportedQueryError,
    match_type_entry,
    matches_query,
    normalize_search_text,
)
from legisbr.core.scraper import DEFAULT_VALID_SITUATION, BaseScraper

logger = logging.getLogger(__name__)

TYPES = {
    "Alvará": "Álvará",
    "Ato": "Ato",
    "Ato Conjunto": "Ato+Conjunto",
    "Ato da Mesa": "Ato+da+Mesa",
    "Ato da Presidência Sem Número": "Ato+da+Presid%C3%AAncia+Sem+N%C3%BAmero",
    "Ato Declaratório do Presidente da Mesa": "Ato+Declaratório+do+Presidente+da+Mesa",
    "Ato do Presidente da Mesa": "Ato+do+Presidente+da+Mesa",
    "Ato do Presidente Sem Número": "Ato+do+Presidente+Sem+N%C3%BAmero",
    "Ato Sem Número": "Ato+Sem+N%C3%BAmero",
    "Carta Régia": "Carta%20Régia",
    "Carta Imperial": "Carta+Imperial",
    "Constituição": "Constitui%C3%A7%C3%A3o",
    "Decisão da Mesa Sem Número": "Decis%C3%A3o+da+Mesa+Sem+N%C3%BAmero",
    "Decisão": "Decisão",
    "Decreto": "Decreto",
    "Decreto Legislativo": "Decreto+Legislativo",
    "Decreto Sem Número": "Decreto+Sem+N%C3%BAmero",
    "Decreto-Lei": "Decreto-Lei",
    "Emenda Constitucional": "Emenda+Constitucional",
    "Instrução": "Instrução",
    "Lei Complementar": "Lei+Complementar",
    "Lei Ordinária": "Lei+Ordin%C3%A1ria",
    "Manifesto": "Manifesto",
    "Mensagem": "Mensagem",
    "Pacto": "Pacto",
    "Proclamação": "Proclamação",
    "Protocolo": "Protocolo",
    "Medida Provisória": "Medida+Provis%C3%B3ria",
    "Ordem de Serviço": "Ordem+de+Serviço",
    "Portaria": "Portaria",
    "Regulamento": "Regulamento",
    "Resolução": "Resolu%C3%A7%C3%A3o",
    "Resolução da Assembleia Nacional Constituinte": "Resolu%C3%A7%C3%A3o+da+Assembl%C3%A9ia+Nacional+Constituinte",
    "Resolução da Câmara dos Deputados": "Resolu%C3%A7%C3%A3o+da+C%C3%A2mara+dos+Deputados",
    "Resolução da Mesa": "Resolução+da+Mesa",
    "Resolução do Congresso Nacional": "Resolu%C3%A7%C3%A3o+do+Congresso+Nacional",
    "Resolução do Senado Federal": "Resolu%C3%A7%C3%A3o+do+Senado+Federal",
}
_FEDERAL_TYPE_SLUG_MAP = {
    "alvara": "Alvará",
    "ata_sn": "Ata",
    "ato": "Ato",
    "ato_sn": "Ato Sem Número",
    "atocom": "Ato Complementar",
    "atocon": "Ato Conjunto",
    "atocon_sn": "Ato Conjunto",
    "atocsr": "Ato do Comando Supremo da Revolução",
    "atodec": "Ato Declaratório",
    "atodec_sn": "Ato Declaratório",
    "atodecpm": "Ato Declaratório do Presidente da Mesa",
    "atoins": "Ato Institucional",
    "atomes": "Ato da Mesa",
    "atomes_sn": "Ato da Mesa",
    "atonor": "Ato Normativo",
    "atopre_sn": "Ato da Presidência Sem Número",
    "atoprt": "Ato do Presidente",
    "atoprt_sn": "Ato do Presidente Sem Número",
    "atoprtm": "Ato do Presidente da Mesa",
    "atoprtm_sn": "Ato do Presidente da Mesa",
    "atopvp": "Ato do Primeiro-Vice-Presidente",
    "carimp": "Carta Imperial",
    "carlei": "Carta de Lei",
    "carlei_sn": "Carta de Lei",
    "carpat_sn": "Carta Patente",
    "carreg_sn": "Carta Régia",
    "carta_sn": "Carta",
    "circul": "Circular",
    "comuni_sn": "Comunicado",
    "conadc": "Ato das Disposições Constitucionais Transitórias",
    "consti": "Constituição",
    "decisa": "Decisão",
    "decisa_sn": "Decisão",
    "declcn": "Decreto Legislativo",
    "decleg": "Decreto Legislativo",
    "declei": "Decreto-Lei",
    "decmin": "Decreto do Conselho de Ministros",
    "decpre_sn": "Decisão do Presidente",
    "decres": "Decreto Reservado",
    "decret": "Decreto",
    "decret_sn": "Decreto Sem Número",
    "dmsn": "Decisão da Mesa Sem Número",
    "dpsn": "Decisão da Presidência",
    "emecon": "Emenda Constitucional",
    "emecon_sn": "Emenda Constitucional",
    "emecrv": "Emenda Constitucional de Revisão",
    "instno": "Instrução Normativa",
    "instru": "Instrução",
    "instse": "Instrução de Serviço",
    "lei": "Lei Ordinária",
    "lei_sn": "Lei Ordinária",
    "leicom": "Lei Complementar",
    "leicon": "Lei Constitucional",
    "leidel": "Lei Delegada",
    "leimp": "Lei Ordinária",
    "manife_sn": "Manifesto",
    "medpro": "Medida Provisória",
    "mensag": "Mensagem",
    "mensag_sn": "Mensagem",
    "ordsec": "Ordem de Serviço Conjunta",
    "ordser": "Ordem de Serviço",
    "ordser-sn": "Ordem de Serviço",
    "pacto_sn": "Pacto",
    "pactorep": "Pacto Republicano",
    "portar": "Portaria",
    "portar_con": "Portaria Conjunta",
    "portar_sn": "Portaria",
    "procla_sn": "Proclamação",
    "protoc_sn": "Protocolo",
    "regim_sn": "Regimento",
    "regula": "Regulamento",
    "regula_sn": "Regulamento",
    "resaco": "Resolução da Assembleia Nacional Constituinte",
    "rescad": "Resolução da Câmara dos Deputados",
    "rescon": "Resolução do Congresso Nacional",
    "resmes": "Resolução da Mesa",
    "resmes_sn": "Resolução da Mesa",
    "resolu": "Resolução",
    "resolu_sn": "Resolução",
    "ressen": "Resolução do Senado Federal",
}
_FEDERAL_TITLE_PREFIX_ALIASES = {
    "Ato Declaratório do Presidente da Mesa": "Ato Declaratório do Presidente da Mesa",
    "Ato da Presidência": "Ato da Presidência Sem Número",
    "Ato do Presidente da Mesa": "Ato do Presidente da Mesa",
    "Ato do Presidente": "Ato do Presidente",
    "Ato da Mesa": "Ato da Mesa",
    "Ato Conjunto": "Ato Conjunto",
    "Ato Complementar": "Ato Complementar",
    "Ato Institucional": "Ato Institucional",
    "Ato Normativo": "Ato Normativo",
    "Ato": "Ato",
    "Carta de Lei": "Carta de Lei",
    "Carta Régia": "Carta Régia",
    "Carta Imperial": "Carta Imperial",
    "Carta Patente": "Carta Patente",
    "Carta": "Carta",
    "Comunicado": "Comunicado",
    "Circular": "Circular",
    "Constituição. ADCT": "Ato das Disposições Constitucionais Transitórias",
    "Constituição": "Constituição",
    "Decisão da Mesa": "Decisão da Mesa Sem Número",
    "Decisão da Presidência": "Decisão da Presidência",
    "Decisão do Presidente": "Decisão do Presidente",
    "Decisão": "Decisão",
    "Decreto do Conselho de Ministros": "Decreto do Conselho de Ministros",
    "Decreto Legislativo": "Decreto Legislativo",
    "Decreto-Lei": "Decreto-Lei",
    "Decreto Reservado": "Decreto Reservado",
    "Decreto": "Decreto",
    "Emenda Constitucional de Revisão": "Emenda Constitucional de Revisão",
    "Emenda Constitucional": "Emenda Constitucional",
    "Instrução Normativa": "Instrução Normativa",
    "Instrução de Serviço": "Instrução de Serviço",
    "Instrução": "Instrução",
    "Lei Complementar": "Lei Complementar",
    "Lei Constitucional": "Lei Constitucional",
    "Lei Delegada": "Lei Delegada",
    "Lei": "Lei Ordinária",
    "Manifesto": "Manifesto",
    "Mensagem": "Mensagem",
    "Medida Provisória": "Medida Provisória",
    "Ordem de Serviço Conjunta": "Ordem de Serviço Conjunta",
    "Ordem de Serviço": "Ordem de Serviço",
    "Pacto Republicano": "Pacto Republicano",
    "Pacto": "Pacto",
    "Portaria Conjunta": "Portaria Conjunta",
    "Portaria": "Portaria",
    "Proclamação": "Proclamação",
    "Protocolo": "Protocolo",
    "Regimento": "Regimento",
    "Regulamento": "Regulamento",
    "Resolução da Assembleia Nacional Constituinte": "Resolução da Assembleia Nacional Constituinte",
    "Resolução da Câmara dos Deputados": "Resolução da Câmara dos Deputados",
    "Resolução da Mesa": "Resolução da Mesa",
    "Resolução do Congresso Nacional": "Resolução do Congresso Nacional",
    "Resolução do Senado Federal": "Resolução do Senado Federal",
    "Resolução": "Resolução",
}
_FEDERAL_TYPE_PREFIX_RE = re.compile(
    "(.+?)(?:\\s+(?:n[º°o]|n\\.|número)\\s*[\\dA-Za-z]|\\s+\\d|\\s+de\\s)",
    re.IGNORECASE,
)
_OPERATIVE_CLAUSE_START_RE = re.compile(
    r"^(?:art\.?\s*\d|artigo\b|§|par[aá]grafo\b|o\s+presidente\b|a\s+presidenta\b|o\s+congresso\b|fa[çc]o\s+saber\b|decreta\s*:)",
    re.IGNORECASE,
)
EXPORT_MAX_DOCS = 300


class CamaraDepScraper(BaseScraper):
    "Scraper for the Câmara dos Deputados legislation portal."

    name = "LEGISLACAO_FEDERAL"
    coverage = ""
    ordering = "data%3AASC"
    types = TYPES
    situations = {}
    base_url = "https://www.camara.leg.br/legislacao/"
    capabilities = SearchCapabilities(
        native_year=True,
        native_type=True,
        native_number=True,
        native_text=True,
        native_situation=True,
    )

    def _format_search_url(
        self,
        year: str,
        norm_type_id: str,
        *,
        number: str = "",
        text: str = "",
        situation: str = "",
        data_inicio: str = "",
        data_fim: str = "",
        ordering: str | None = None,
        export: bool = False,
    ) -> str:
        """Format search or export URL for a given year, norm type, and optional filters."""
        params = {
            "abrangencia": self.coverage,
            "geral": quote_plus(text) if text else "",
            "ano": year,
            "situacao": quote_plus(situation) if situation else "",
            "origem": "",
            "numero": quote_plus(number) if number else "",
            "ordenacao": ordering if ordering is not None else self.ordering,
            "tipo": norm_type_id,
        }
        if data_inicio or data_fim:
            params["dataInicio"] = data_inicio
            params["dataFim"] = data_fim
        endpoint = "busca/exportar?" if export else "busca?"
        return (
            self.base_url
            + endpoint
            + "&".join((f"{key}={value}" for key, value in params.items()))
        )

    @staticmethod
    def _extract_type_facets(soup: BeautifulSoup) -> list[tuple[str, int]]:
        """Extract non-zero `(encoded_tipo, count)` facets from `aside.refine-busca`."""
        aside = soup.find("aside", class_="refine-busca")
        if not isinstance(aside, Tag):
            return []
        facets: list[tuple[str, int]] = []
        seen: set[str] = set()
        for link in aside.find_all("a", href=True):
            href = link.get("href")
            if not isinstance(href, str) or "tipo=" not in href:
                continue
            tipos = parse_qs(urlparse(href).query).get("tipo")
            if not tipos or not tipos[0].strip():
                continue
            tipo_val = tipos[0].strip()
            if tipo_val in seen:
                continue
            match = re.search(r"(\d[\d.]*)\s+filtro", link.get_text(" ", strip=True))
            if not match:
                continue
            count = int(match.group(1).replace(".", ""))
            if count <= 0:
                continue
            seen.add(tipo_val)
            facets.append((quote_plus(tipo_val), count))
        return facets

    @staticmethod
    def _extract_total_results(soup: BeautifulSoup) -> int | None:
        total_element = soup.find(
            "div", class_="busca-info__resultado busca-info__resultado--informado"
        )
        if total_element is None:
            return None
        try:
            return int(total_element.get_text(" ", strip=True).split()[-1])
        except (TypeError, ValueError, IndexError):
            return None

    @staticmethod
    def _strip_prefix(value: str, prefix: str) -> str:
        value = value.strip()
        if value.lower().startswith(prefix.lower()):
            return value[len(prefix) :].strip()
        return value

    def _parse_listing_page_documents(self, soup: BeautifulSoup) -> list[dict]:
        """Parse one standard search-results page into document metadata."""
        documents_metadata = []
        documents = soup.find_all("li", class_="busca-resultados__item")
        for document in documents:
            heading = document.find("h3", class_="busca-resultados__cabecalho")
            if not heading:
                continue
            a_tag = heading.find("a", href=True)
            if not a_tag:
                continue
            metadata_url = a_tag["href"]
            title = a_tag.get_text(" ", strip=True)
            summary_tag = document.find(
                "p", class_="busca-resultados__descricao js-fade-read-more"
            )
            summary = ""
            if summary_tag:
                summary = self._strip_prefix(
                    summary_tag.get_text(" ", strip=True), "Ementa:"
                )
            situation_tag = document.find("p", class_="busca-resultados__situacao")
            situation = ""
            if situation_tag:
                situation = self._strip_prefix(
                    situation_tag.get_text(" ", strip=True), "Situação:"
                )
            documents_metadata.append(
                {
                    "title": title,
                    "summary": summary,
                    "metadata_url": metadata_url,
                    "situation": situation,
                }
            )
        return documents_metadata

    def _parse_export_documents(self, soup: BeautifulSoup) -> list[dict]:
        """Parse the export HTML page into document metadata."""
        container = soup.find("div", id="impressaoPDF") or soup
        documents_metadata = []
        for document in container.find_all("li"):
            a_tag = document.find("a", href=True)
            if not a_tag:
                continue
            title = a_tag.get_text(" ", strip=True)
            metadata_url = a_tag["href"]
            summary = ""
            situation = ""
            for paragraph in document.find_all("p"):
                text = paragraph.get_text(" ", strip=True)
                lowered = text.lower()
                if lowered.startswith("ementa:"):
                    summary = self._strip_prefix(text, "Ementa:")
                elif lowered.startswith("situação:") or lowered.startswith("situacao:"):
                    situation = self._strip_prefix(text, "Situação:")
                    if situation == text:
                        situation = self._strip_prefix(text, "Situacao:")
            documents_metadata.append(
                {
                    "title": title,
                    "summary": summary,
                    "metadata_url": metadata_url,
                    "situation": situation,
                }
            )
        return documents_metadata

    async def _get_docs_links(self, url: str) -> list[dict]:
        """Fetch one search-results page and return document metadata.

        Returns a list of dicts:
            {title, summary, metadata_url, situation}

        On failed fetch, logs an error and returns [].
        """
        soup = (await self.http.request(url)).soup()
        if not soup:
            logger.error(f"Could not fetch listing page: {url}")
            raise ValueError("Could not fetch listing page")
        documents_html_links_info = self._parse_listing_page_documents(soup)
        if not documents_html_links_info:
            logger.debug(f"No documents found for url: {url}")
        return documents_html_links_info

    async def _get_document_text_link(self, doc: dict, norm_type: str) -> dict:
        """Resolve the actual text URL from a document's metadata page.

        Priority: "texto - republicação" > "texto - publicação original"
                  > "texto - emenda" > other "texto - *" > bare "texto"
        Within each bucket, HTML links are preferred over PDFs.
        """
        title = doc.get("title", "")
        summary = doc.get("summary", "")
        situation = doc.get("situation", "") or "Não Informado"
        metadata_url = doc.get("metadata_url", "")
        soup = (await self.http.request(metadata_url)).soup()
        not_found = any(
            (
                heading.get_text(" ", strip=True) == "Not Found"
                for heading in soup.find_all("h1")
            )
        )
        if not_found:
            logger.warning(f"Document not found: {title}")
            raise ValueError("Document text not found (404)")
        sessao_divs = soup.find_all("div", class_="sessao")
        if not sessao_divs:
            logger.error(f"Could not find text link for document: {title}")
            raise ValueError("Could not find text link div.sessao in page")
        original_links = []
        repub_links = []
        emenda_links = []
        dash_links = []
        bare_links = []
        for sessao_div in sessao_divs:
            for link in sessao_div.find_all("a", href=True):
                link_text = link.get_text(" ", strip=True).lower()
                if "texto - publicação original" in link_text:
                    original_links.append(link)
                elif "texto - republicação" in link_text:
                    repub_links.append(link)
                elif "texto - emenda" in link_text:
                    emenda_links.append(link)
                elif "texto -" in link_text:
                    dash_links.append(link)
                elif "texto" in link_text:
                    bare_links.append(link)

        def _prefer_html(links: list) -> str | None:
            html = [
                lnk["href"] for lnk in links if lnk["href"].lower().endswith(".html")
            ]
            fallback = [lnk["href"] for lnk in links]
            candidates = html or fallback
            return candidates[-1] if candidates else None

        chosen = None
        if repub_links:
            chosen = _prefer_html(repub_links)
        elif original_links:
            chosen = _prefer_html(original_links)
        elif emenda_links:
            chosen = _prefer_html(emenda_links)
        elif dash_links:
            chosen = _prefer_html(dash_links)
        elif bare_links:
            chosen = _prefer_html(bare_links)
        if chosen is None:
            logger.error(f"Could not find text link for document: {title}")
            raise ValueError("No text link found in page anchors")
        text_url = urljoin(metadata_url, chosen)
        return {
            "title": title,
            "_norm_type": norm_type,
            "summary": summary,
            "situation": situation,
            "metadata_url": metadata_url,
            "document_url": text_url,
        }

    async def fetch(self, doc: dict) -> Document | None:
        year = doc["year"]
        norm_type = doc["type"]
        doc = {**doc, **await self._get_document_text_link(doc, norm_type)}
        title = doc.get("title", "")
        summary = doc.get("summary", "")
        metadata_url = doc.get("metadata_url", "")
        document_text_link = doc.get("document_url", "")
        situation = doc.get("situation", "") or "Não Informado"
        doc_type = norm_type or self._resolve_norm_type(
            title, metadata_url=metadata_url, document_url=document_text_link
        )
        try:
            resp_or_failed = await self.http.request(document_text_link)
            html_bytes = resp_or_failed.content
            if html_bytes.startswith(b"%PDF"):
                text_markdown = await bytes_to_markdown(
                    html_bytes,
                    resp_or_failed.content_type or "application/pdf",
                    document_text_link,
                    self.http,
                    self.ocr,
                    pdf_executor=self.pdf_executor,
                    pdf_workers=self.pdf_workers,
                )
                return Document(
                    year=year,
                    title=title,
                    type=doc_type,
                    summary=summary,
                    situation=situation,
                    metadata_url=metadata_url,
                    text_markdown=text_markdown.strip(),
                    document_url=document_text_link,
                    raw=html_bytes,
                    raw_ext=".pdf",
                )
            html_text = resp_or_failed.text
            soup = BeautifulSoup(html_text, "lxml")
            texto_div = soup.find("div", class_="texto")
            used_texto_div = isinstance(texto_div, Tag) and bool(
                texto_div.get_text(strip=True)
            )
            content_div = (
                texto_div
                if used_texto_div
                else soup.find("div", id="content")
                or soup.find("div", class_="textoNorma")
                or soup
            )
            strip_html_chrome(
                content_div,
                extra_selectors=[
                    {"class_": "vejaTambem"},
                    {"class_": "documentFirstHeading"},
                    {"class_": "rodapeTexto"},
                    {"class_": "publicacoesTI"},
                ],
            )
            # Legacy Câmara HTML indents quoted amended articles inside <dir> tags,
            # which html-to-markdown strips inline without block breaks; promote to <div>.
            for dir_tag in content_div.find_all("dir"):
                dir_tag.name = "div"
            clean_norm_soup(content_div)
            for h1 in content_div.find_all("h1"):
                h1.decompose()
            ementa_nodes = content_div.find_all("p", class_="ementa")
            html_with_ementa = str(content_div)
            for ementa in ementa_nodes:
                # lxml auto-closes invalid `<p class="ementa"><p>...</p></p>` into an
                # empty `<p class="ementa">` followed by a sibling `<p>` holding the ementa.
                if not ementa.get_text(strip=True):
                    nxt = ementa.find_next_sibling()
                    if isinstance(nxt, Tag) and nxt.name == "p":
                        nxt_text = " ".join(nxt.get_text(" ", strip=True).split())
                        is_safe_ementa = (
                            len(nxt_text) < 600
                            and "art." not in nxt_text.casefold()
                            and not _OPERATIVE_CLAUSE_START_RE.match(nxt_text)
                        )
                        if is_safe_ementa and (
                            not summary
                            or " ".join(summary.split()).casefold()
                            in nxt_text.casefold()
                        ):
                            nxt.decompose()
                ementa.decompose()
            if summary and not used_texto_div:
                remove_summary_element(content_div, summary)
            html_string = str(content_div)
            text_markdown = await self.markdown(
                html=html_string, base_url=document_text_link
            )
            if not text_markdown or not text_markdown.strip():
                if ementa_nodes:
                    fallback_markdown = await self.markdown(
                        html=html_with_ementa, base_url=document_text_link
                    )
                    if fallback_markdown and fallback_markdown.strip():
                        text_markdown = fallback_markdown
                if not text_markdown or not text_markdown.strip():
                    logger.warning(f"Document text is empty after conversion: {title}")
                    raise ValueError("Document text is empty after conversion")
            if summary:
                text_markdown = remove_summary_from_markdown(text_markdown, summary)
            return Document(
                year=year,
                title=title,
                type=doc_type,
                summary=summary,
                situation=situation,
                metadata_url=metadata_url,
                text_markdown=text_markdown.strip(),
                document_url=document_text_link,
                raw=html_bytes,
                raw_ext=".html",
            )
        except Exception as e:
            logger.error(f"Error converting document to markdown: {title} - {e}")
            raise ValueError(str(e)) from e

    def _infer_norm_type(self, title: str) -> str:
        """Infer norm type from metadata/document URL slug or title prefix."""
        normalized_title = " ".join(title.split())
        for prefix, canonical in sorted(
            _FEDERAL_TITLE_PREFIX_ALIASES.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            if normalized_title.casefold().startswith(prefix.casefold()):
                return canonical
        inferred = infer_type_from_title(normalized_title, TYPES)
        if inferred:
            return inferred
        prefix_match = _FEDERAL_TYPE_PREFIX_RE.match(normalized_title)
        if prefix_match:
            return prefix_match.group(1).strip()
        return "Desconhecido"

    @staticmethod
    def _extract_type_slug(*urls: str) -> str:
        """Extract the federal norm-type slug from a metadata/document URL."""
        for url in urls:
            if not url:
                continue
            parts = [part for part in urlparse(url).path.split("/") if part]
            if len(parts) >= 3 and parts[0] == "legin":
                return parts[2]
        return ""

    def _resolve_norm_type(
        self, title: str, *, metadata_url: str = "", document_url: str = ""
    ) -> str:
        """Resolve the most reliable norm type from URL slug first, title second."""
        slug = self._extract_type_slug(metadata_url, document_url)
        if slug:
            mapped = _FEDERAL_TYPE_SLUG_MAP.get(slug)
            if mapped:
                return mapped
        return self._infer_norm_type(title)

    async def _paginate_url(
        self, url: str, total: int, *, first_soup: BeautifulSoup | None = None
    ) -> list[dict]:
        """Collect all pages of a search URL via bounded pagination."""

        async def fetch_page(page: int) -> list[dict]:
            if page == 1 and first_soup is not None:
                return self._parse_listing_page_documents(first_soup)
            return await self._get_docs_links(f"{url}&pagina={page}")

        return [ref async for ref in self.paginate(fetch_page, calc_pages(total, 20))]

    async def _collect_slice(
        self,
        year: int,
        norm_type_id: str,
        *,
        expected_total: int,
        data_inicio: str = "",
        data_fim: str = "",
        first_soup: BeautifulSoup | None = None,
    ) -> list[dict]:
        """Deterministically collect all docs for a `(year, norm_type, date_range)` slice.

        Câmara's paginated `/busca` lacks a deterministic secondary tiebreaker across
        same-date acts. Slices of `<= EXPORT_MAX_DOCS` (300) are fetched atomically in
        a single `/busca/exportar` call; larger slices are recursively bisected by date
        range (`dataInicio`/`dataFim`) until every bucket fits inside the export cap.
        """
        if expected_total <= 0:
            return []
        if expected_total <= 20 and first_soup is not None:
            items = self._parse_listing_page_documents(first_soup)
            if len(items) >= expected_total:
                return items
        if expected_total <= EXPORT_MAX_DOCS:
            export_url = self._format_search_url(
                str(year),
                norm_type_id,
                data_inicio=data_inicio,
                data_fim=data_fim,
                export=True,
            )
            docs = self._parse_export_documents(
                (await self.http.request(export_url)).soup()
            )
            if len(docs) >= expected_total:
                return docs
            # Fallback to pagination if export was incomplete (or mocked without export).
            search_url = self._format_search_url(
                str(year),
                norm_type_id,
                data_inicio=data_inicio,
                data_fim=data_fim,
            )
            extra = await self._paginate_url(
                search_url, expected_total, first_soup=first_soup
            )
            seen: dict[str, dict] = {d["metadata_url"]: d for d in docs}
            for item in extra:
                seen.setdefault(item["metadata_url"], item)
            return list(seen.values())
        return await self._collect_by_date_range(
            year, norm_type_id, date(year, 1, 1), date(year, 12, 31)
        )

    async def _collect_by_date_range(
        self, year: int, norm_type_id: str, start_d: date, end_d: date
    ) -> list[dict]:
        """Bisect `[start_d, end_d]` until each sub-range has `<= EXPORT_MAX_DOCS` docs."""
        d1 = start_d.strftime("%d/%m/%Y")
        d2 = end_d.strftime("%d/%m/%Y")
        search_url = self._format_search_url(
            str(year), norm_type_id, data_inicio=d1, data_fim=d2
        )
        sub_soup = (await self.http.request(search_url)).soup()
        sub_total = self._extract_total_results(sub_soup)
        if not sub_total:
            return []
        if sub_total <= EXPORT_MAX_DOCS:
            return await self._collect_slice(
                year,
                norm_type_id,
                expected_total=sub_total,
                data_inicio=d1,
                data_fim=d2,
                first_soup=sub_soup,
            )
        if start_d >= end_d:
            # Single-day overflow (> 300 acts on one day): merge multiple orderings.
            merged: dict[str, dict] = {}
            for order in ("relevancia%3ADESC", "data%3AASC"):
                u = self._format_search_url(
                    str(year),
                    norm_type_id,
                    data_inicio=d1,
                    data_fim=d2,
                    ordering=order,
                )
                for item in await self._paginate_url(u, sub_total):
                    merged.setdefault(item["metadata_url"], item)
                if len(merged) >= sub_total:
                    break
            return list(merged.values())
        mid = start_d + timedelta(days=(end_d - start_d).days // 2)
        left, right = await asyncio.gather(
            self._collect_by_date_range(year, norm_type_id, start_d, mid),
            self._collect_by_date_range(
                year, norm_type_id, mid + timedelta(days=1), end_d
            ),
        )
        return [*left, *right]

    async def list_documents(self, year: int) -> AsyncIterator[dict]:
        url = self._format_search_url(str(year), "")
        soup = (await self.http.request(url)).soup()
        total = self._extract_total_results(soup)
        if total is None:
            raise ValueError("Could not parse federal legislation result count")
        if total == 0:
            return

        if total <= EXPORT_MAX_DOCS:
            raw_docs = await self._collect_slice(
                year, "", expected_total=total, first_soup=soup
            )
        else:
            facets = self._extract_type_facets(soup)
            if facets:
                sem = asyncio.Semaphore(max(1, min(self.workers, 8)))

                async def _fetch_facet(tipo_encoded: str, count: int) -> list[dict]:
                    async with sem:
                        return await self._collect_slice(
                            year, tipo_encoded, expected_total=count
                        )

                results = await asyncio.gather(
                    *(_fetch_facet(tipo_id, cnt) for tipo_id, cnt in facets),
                    return_exceptions=True,
                )
                for res in results:
                    if isinstance(res, BaseException) and not isinstance(
                        res, Exception
                    ):
                        raise res
                raw_docs = []
                for (tipo_id, _cnt), res in zip(facets, results, strict=True):
                    if isinstance(res, Exception):
                        await self.error(
                            {"year": year, "title": f"listing tipo={tipo_id}"}, res
                        )
                        continue
                    raw_docs.extend(res)
            else:
                raw_docs = await self._paginate_url(url, total, first_soup=soup)

        unique: dict[str, dict] = {}
        for ref in raw_docs:
            unique.setdefault(ref["metadata_url"], ref)

        if len(unique) < total:
            exc = ValueError(
                f"Federal legislation listing for {year} returned "
                f"{len(unique)}/{total} unique documents"
            )
            await self.error(
                {
                    "year": year,
                    "title": f"listing completeness ({len(unique)}/{total})",
                },
                exc,
            )

        for ref in unique.values():
            norm_type = self._resolve_norm_type(
                ref["title"], metadata_url=ref["metadata_url"]
            )
            yield {
                **ref,
                "year": year,
                "type": norm_type,
                "document_url": ref["metadata_url"],
                "situation": ref.get("situation") or DEFAULT_VALID_SITUATION,
            }

    @staticmethod
    def _extract_year_from_ref(ref: dict) -> int:
        for text in (ref.get("title", ""), ref.get("metadata_url", "")):
            matches = re.findall(r"\b((?:18|19|20)\d{2})\b", str(text))
            if matches:
                return int(matches[-1])
        return 0

    async def search_refs(
        self,
        query: Query,
        *,
        strict_native: bool = False,
        on_error: Callable[[DocRef, Exception], Awaitable[None] | None] | None = None,
    ) -> AsyncIterator[DocRef]:
        if strict_native and not self.capabilities.supports_query(query):
            raise UnsupportedQueryError(f"{self.name} does not support {query!r}")
        if query.limit == 0:
            return

        # Câmara's `geral=` parameter searches full norm bodies and subject indexes,
        # not just title/summary; trust native text matching instead of dropping body hits.
        effective_query = replace(query, text=None)
        norm_type_id = ""
        if query.type is not None:
            matched = match_type_entry(TYPES, query.type)
            if matched is None and normalize_search_text(query.type) == "lei":
                matched = ("Lei Ordinária", TYPES["Lei Ordinária"])
            if matched is not None:
                norm_type_id = matched[1]
                effective_query = replace(effective_query, type=matched[0])
            else:
                norm_type_id = quote_plus(query.type)

        target_years: list[int | None] = (
            [None]
            if query.years is None
            else ([query.years] if isinstance(query.years, int) else list(query.years))
        )

        yielded = 0
        for year in target_years:
            base_search_url = self._format_search_url(
                "" if year is None else str(year),
                norm_type_id,
                number=query.number or "",
                text=query.text or "",
                situation=query.situation or "",
            )
            try:
                first_soup = (await self.http.request(base_search_url)).soup()
                total = self._extract_total_results(first_soup)
                first_items = self._parse_listing_page_documents(first_soup)
                total_pages = (
                    calc_pages(total, 20)
                    if total is not None
                    else (1 if first_items else 0)
                )
            except Exception as exc:
                await self._report_search_error(
                    {"year": year, "document_url": base_search_url}, exc, on_error
                )
                continue

            for page in range(1, total_pages + 1):
                if page == 1:
                    items = first_items
                else:
                    page_url = f"{base_search_url}&pagina={page}"
                    try:
                        items = await self._get_docs_links(page_url)
                    except Exception as exc:
                        await self._report_search_error(
                            {"year": year, "document_url": page_url}, exc, on_error
                        )
                        continue
                for raw in items:
                    norm_type = self._resolve_norm_type(
                        raw["title"], metadata_url=raw["metadata_url"]
                    )
                    ref: DocRef = {
                        **raw,
                        "title": clean_title(raw["title"]),
                        "year": year
                        if year is not None
                        else self._extract_year_from_ref(raw),
                        "type": norm_type,
                        "document_url": raw["metadata_url"],
                        "situation": raw.get("situation") or DEFAULT_VALID_SITUATION,
                    }
                    if not matches_query(ref, effective_query):
                        continue
                    yield ref
                    yielded += 1
                    if query.limit is not None and yielded >= query.limit:
                        return
